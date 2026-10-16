"""Render a pool's native config from a logical profile, and drive its container lifecycle.

The two pure functions here - `build_substitutions` and `render_config` - are what makes a pool
*pluggable*: a single logical profile (difficulty, coinbase tag) and the shared regtest backend are
projected into whatever native config syntax the pool wants (TOML / JSON / YAML / `.env`, validated
by syntax). `PoolUnderTest` wraps the Docker side: obtain the image, mount the rendered config, run
it on the regtest network pinned to its cores, wait for readiness, and tear it down.
"""

from __future__ import annotations

import logging
import pathlib
import re
import subprocess
import tomllib
from typing import TYPE_CHECKING

import msgspec
import yaml

from openbench import config
from openbench import docker

if TYPE_CHECKING:
    from openbench.context import RunContext

LOG = logging.getLogger(__name__)

POOL_CONTAINER = "openbench-pool"
LB_CONTAINER = "openbench-lb"
_LOCALHOST = "127.0.0.1"
_READY_LOG_TAIL = 20
_NOFILE_ARGS = ["--ulimit", "nofile=65535"]
# A proxy holds two descriptors per connection, and opens one upstream connection per client from
# a single IP to one pool address, so it needs the same wide ephemeral port range as the probes
# (the default 32768-60999 caps it at ~28k connections).
_LB_RUN_ARGS = [
    "--ulimit",
    "nofile=262144",
    "--sysctl",
    "net.ipv4.ip_local_port_range=1024 65535",
]
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class AdapterError(RuntimeError):
    """A pool adapter could not be prepared, started, or rendered into a valid native config."""


def format_difficulty(value: float) -> str:
    """Render difficulty without a spurious decimal: 1000000.0 -> "1000000", 0.001 -> "0.001"."""
    return str(int(value)) if value == int(value) else repr(value)


def build_substitutions(
    spec: config.PoolSpec,
    profile: config.Profile,
    regtest: config.Regtest,
    pinning: config.Pinning,
) -> dict[str, str]:
    """The `${VAR}` -> value mapping for a pool's config template and readiness target."""
    pool_cores = str(config.cpuset_count(pinning.pool_cpus)) if pinning.enabled else "max"
    lb_cores = (
        str(config.cpuset_count(pinning.lb_cpus)) if pinning.enabled and pinning.lb_cpus else "0"
    )
    return {
        "RPC_HOST": regtest.bitcoind_container,
        "RPC_PORT": str(regtest.rpc_port),
        "RPC_USER": regtest.rpc_user,
        "RPC_PASS": regtest.rpc_pass,
        "DIFFICULTY": format_difficulty(profile.difficulty),
        "MIN_DIFFICULTY": format_difficulty(profile.min_difficulty),
        "MAX_DIFFICULTY": format_difficulty(profile.max_difficulty),
        "TAG": profile.coinbase_tag,
        "ADDRESS": regtest.address,
        "API_PORT": str(spec.api_port) if spec.api_port is not None else "",
        "POOL_HOST": POOL_CONTAINER,
        "POOL_CORES": pool_cores,
        "BACKEND_HOST": POOL_CONTAINER,
        "BACKEND_PORT": str(spec.stratum_port),
        "LB_CORES": lb_cores,
    }


def build_lb_substitutions(
    lb: config.PoolSpec,
    pool: config.PoolSpec,
    profile: config.Profile,
    regtest: config.Regtest,
    pinning: config.Pinning,
) -> dict[str, str]:
    """The mapping for the load balancer's template and readiness target: POOL_HOST/API_PORT are
    its own, BACKEND_HOST/BACKEND_PORT the Stratum endpoint of the pool behind it."""
    mapping = build_substitutions(lb, profile, regtest, pinning)
    mapping["POOL_HOST"] = LB_CONTAINER
    mapping["BACKEND_PORT"] = str(pool.stratum_port)
    return mapping


def substitute(text: str, mapping: dict[str, str]) -> str:
    """Replace every `${IDENT}` placeholder, hard-failing on an unknown (mistyped) variable name."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in mapping:
            allowed = ", ".join(sorted(config.TEMPLATE_VARIABLES))
            raise AdapterError(
                f"template references unknown variable ${{{name}}}; allowed: {allowed}"
            )
        return mapping[name]

    return _PLACEHOLDER_RE.sub(replace, text)


def _validate_rendered(text: str, fmt: str) -> None:
    """Validate the rendered config, so a broken render fails here rather than as a pool crash."""
    if fmt == "toml":
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise AdapterError(f"rendered config is not valid TOML: {exc}") from exc
    elif fmt == "json":
        try:
            msgspec.json.decode(text)
        except msgspec.DecodeError as exc:
            raise AdapterError(f"rendered config is not valid JSON: {exc}") from exc
    elif fmt == "yaml":
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise AdapterError(f"rendered config is not valid YAML: {exc}") from exc
    elif fmt == "env":
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" not in stripped:
                raise AdapterError(f"rendered .env line {number} is not KEY=VALUE: {line!r}")


def render_config(template_text: str, fmt: str, mapping: dict[str, str]) -> str:
    """Substitute `${VAR}` placeholders into a config template and validate the result."""
    rendered = substitute(template_text, mapping)
    _validate_rendered(rendered, fmt)
    return rendered


def _git_clone(repo: str, dest: pathlib.Path, ref: str | None, pool_name: str) -> None:
    """Shallow-clone repo into dest (a branch/tag via ref), surfacing git's error on failure."""
    args = ["git", "clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += [repo, str(dest)]
    LOG.info("cloning %s -> %s", repo, dest)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise AdapterError(f"git is not installed - needed to auto-clone {pool_name!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(f"git clone for pool {pool_name!r} timed out") from exc
    if result.returncode != 0:
        raise AdapterError(
            f"git clone of {repo} for pool {pool_name!r} failed "
            f"(private repo needs credentials on this host?):\n{result.stderr.strip()}"
        )


class PoolUnderTest:
    """A started, ready pool on the regtest network, as a context manager.

    Enter: obtain the image (pull or build), render + mount the config, run it on the regtest net
    pinned to the configured pool cores with its Stratum/API ports published to localhost, and block
    until ready. With `registry.via_lb`, then start the registry's load balancer in front of it on
    its own cores; the Stratum endpoint the probes use becomes the load balancer's, while CPU/RSS
    sampling stays on the pool. Exit (and any failure mid-enter): remove the containers.
    """

    def __init__(
        self,
        run: RunContext,
        spec: config.PoolSpec,
        profile: config.Profile,
        *,
        keep: bool = False,
    ) -> None:
        self._run = run
        self._spec = spec
        self._profile = profile
        self._backend = run.backend
        self._pinning = run.registry.pinning
        self._keep = keep
        self._mapping = build_substitutions(spec, profile, run.registry.regtest, self._pinning)
        self._image = ""
        self._lb = run.registry.loadbalancer if run.registry.via_lb else None
        self._lb_mapping = (
            build_lb_substitutions(self._lb, spec, profile, run.registry.regtest, self._pinning)
            if self._lb is not None
            else {}
        )

    @property
    def stratum_host(self) -> str:
        return LB_CONTAINER if self._lb is not None else POOL_CONTAINER

    @property
    def stratum_port(self) -> int:
        return self._lb.stratum_port if self._lb is not None else self._spec.stratum_port

    @property
    def address(self) -> str:
        return self._backend.address

    def sampler(self) -> docker.StatsSampler:
        return docker.StatsSampler(POOL_CONTAINER)

    def run_probe(
        self,
        script: str,
        args: list[str],
        *,
        timeout: float | None = None,
        extra_run_args: list[str] | None = None,
    ) -> str:
        """Run one of openbench/probes/*.py in a throwaway container on the regtest network."""
        return docker.run_oneshot(
            docker.PROBE_IMAGE,
            ["python", f"/probes/{script}", *args],
            network=self._backend.network,
            cpuset=self._bench_cpus(),
            volumes=[self._probes_mount()],
            extra_args=_NOFILE_ARGS + (extra_run_args or []),
            timeout=timeout,
        )

    def run_miner(self, args: list[str], *, timeout: float | None = None) -> tuple[int, str]:
        """Run the Stratum test miner (a validation tool) in a container; return (code, output)."""
        miner_mount = docker.mount(self._run.registry.root / "miner", "/miner")
        return docker.run_oneshot_status(
            docker.PROBE_IMAGE,
            ["python", "/miner/openbench_miner.py", *args],
            network=self._backend.network,
            cpuset=self._bench_cpus(),
            volumes=[miner_mount],
            extra_args=_NOFILE_ARGS,
            timeout=timeout,
        )

    def __enter__(self) -> PoolUnderTest:
        try:
            docker.remove(POOL_CONTAINER, LB_CONTAINER)
            self._image = self._ensure_image(self._spec)
            volumes = self._render_config_volume(self._spec, self._mapping)
            self._start(volumes)
            self._await_ready(self._spec, POOL_CONTAINER, self._mapping)
            if self._lb is not None:
                self._start_lb(self._lb)
            return self
        except BaseException:
            docker.remove(POOL_CONTAINER, LB_CONTAINER)
            raise

    def __exit__(self, *exc: object) -> None:
        if not self._keep:
            docker.remove(POOL_CONTAINER, LB_CONTAINER)

    def _bench_cpus(self) -> str | None:
        return self._pinning.bench_cpus if self._pinning.enabled else None

    def _probes_mount(self) -> str:
        return docker.mount(self._run.registry.root / "openbench" / "probes", "/probes")

    def _resolve(self, path: str) -> pathlib.Path:
        candidate = pathlib.Path(path)
        return candidate if candidate.is_absolute() else (self._run.registry.root / candidate)

    def _ensure_image(self, spec: config.PoolSpec) -> str:
        if spec.source == "image":
            assert spec.image is not None
            if not docker.image_exists(spec.image):
                docker.pull(spec.image)
            return spec.image
        assert spec.build_context is not None
        context = self._resolve(spec.build_context)
        if not context.is_dir() and spec.repo:
            self._clone_source(spec)
        if not context.is_dir():
            hint = (
                "set its `repo:` in pools.yml to auto-clone, or"
                if not spec.repo
                else "the auto-clone did not produce it; check the `repo`/`repo_dir` paths, or"
            )
            raise AdapterError(
                f"build_context for pool {spec.name!r} not found: {context} - {hint} "
                f"clone the pool's repo there manually (see the comment in pools.yml)"
            )
        dockerfile = None
        if spec.dockerfile:
            in_context = context / spec.dockerfile
            dockerfile = str(in_context if in_context.is_file() else self._resolve(spec.dockerfile))
        tag = f"openbench-pool-{spec.name}:bench"
        docker.build(tag, context, dockerfile)
        return tag

    def _clone_source(self, spec: config.PoolSpec) -> None:
        """Clone the spec's `repo` so a missing build context appears. When the context is a subdir
        of the repo, `repo_dir` says where the repo root goes (default: the build context)."""
        assert spec.repo is not None and spec.build_context is not None
        dest = self._resolve(spec.repo_dir or spec.build_context)
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git_clone(spec.repo, dest, spec.repo_ref, spec.name)

    def _render_config_volume(self, spec: config.PoolSpec, mapping: dict[str, str]) -> list[str]:
        cfg = spec.config
        if cfg.format == "none" or not cfg.template:
            return []
        if not cfg.mount:
            raise AdapterError(f"pool {spec.name!r}: config has a template but no 'mount' path")
        template_path = self._resolve(cfg.template)
        if not template_path.is_file():
            raise AdapterError(f"config template not found: {template_path}")
        rendered = render_config(template_path.read_text(encoding="utf-8"), cfg.format, mapping)
        rendered_path = self._run.scratch / f"{spec.name}-{pathlib.Path(cfg.mount).name}"
        rendered_path.write_text(rendered, encoding="utf-8")
        LOG.info("rendered %s config -> %s", spec.name, cfg.mount)
        return [docker.mount(rendered_path, cfg.mount, read_only=True)]

    def _start(self, volumes: list[str]) -> None:
        spec = self._spec
        ports = [f"{_LOCALHOST}::{spec.stratum_port}"]
        if spec.api_port is not None:
            ports.append(f"{_LOCALHOST}::{spec.api_port}")
        env = {key: substitute(value, self._mapping) for key, value in spec.env.items()}
        cpuset = self._pinning.pool_cpus if self._pinning.enabled else None
        LOG.info("starting pool %s (%s) pinned to cpus %s", spec.name, self._image, cpuset or "all")
        docker.run_detached(
            POOL_CONTAINER,
            self._image,
            network=self._backend.network,
            cpuset=cpuset,
            env=env,
            ports=ports,
            volumes=volumes,
            extra_args=_NOFILE_ARGS,
        )
        mapping = docker.published_ports(POOL_CONTAINER)
        if mapping:
            LOG.info("pool %s host ports (random):\n%s", spec.name, mapping)

    def _start_lb(self, lb: config.PoolSpec) -> None:
        """Start the load balancer in front of the (ready) pool, pinned to its own cores."""
        image = self._ensure_image(lb)
        volumes = self._render_config_volume(lb, self._lb_mapping)
        ports = [f"{_LOCALHOST}::{lb.stratum_port}"]
        if lb.api_port is not None:
            ports.append(f"{_LOCALHOST}::{lb.api_port}")
        env = {key: substitute(value, self._lb_mapping) for key, value in lb.env.items()}
        cpuset = self._pinning.lb_cpus if self._pinning.enabled else None
        LOG.info(
            "starting load balancer %s (%s) pinned to cpus %s in front of %s",
            lb.name,
            image,
            cpuset or "all",
            self._spec.name,
        )
        docker.run_detached(
            LB_CONTAINER,
            image,
            network=self._backend.network,
            cpuset=cpuset,
            env=env,
            ports=ports,
            volumes=volumes,
            extra_args=_LB_RUN_ARGS,
        )
        self._await_ready(lb, LB_CONTAINER, self._lb_mapping)

    def _await_ready(self, spec: config.PoolSpec, container: str, mapping: dict[str, str]) -> None:
        readiness = spec.readiness
        if readiness.kind == "log":
            assert readiness.pattern is not None
            ready = docker.wait_log(
                container, readiness.pattern, timeout_seconds=readiness.timeout_seconds
            )
        else:
            ready = self._await_ready_in_network(spec, container, mapping)
        if not ready:
            tail = "\n".join(docker.logs(container).splitlines()[-_READY_LOG_TAIL:])
            raise AdapterError(
                f"{spec.name!r} did not become ready via {readiness.kind}; recent logs:\n{tail}"
            )
        role = "pool" if container == POOL_CONTAINER else "load balancer"
        LOG.info("%s %s is ready", role, spec.name)

    def _await_ready_in_network(
        self, spec: config.PoolSpec, container: str, mapping: dict[str, str]
    ) -> bool:
        """Poll http/tcp readiness from a sibling container (works under docker-out-of-docker)."""
        readiness = spec.readiness
        args = ["--timeout", str(readiness.timeout_seconds)]
        if readiness.kind == "http":
            assert readiness.url is not None
            args += [
                "--http",
                substitute(readiness.url, mapping),
                "--status",
                str(readiness.status),
            ]
            if readiness.body is not None:
                args += ["--body", readiness.body]
        else:
            args += ["--tcp", f"{container}:{spec.stratum_port}"]
        code, _ = docker.run_oneshot_status(
            docker.PROBE_IMAGE,
            ["python", "/probes/wait_ready.py", *args],
            network=self._backend.network,
            cpuset=self._bench_cpus(),
            volumes=[self._probes_mount()],
            timeout=readiness.timeout_seconds + 30,
        )
        return code == 0
