"""Load and model `pools.yml`: the pool registry, the regtest backend, and bench profiles.

The registry is the single surface a user edits to add or tune a pool. Each pool entry is an
*adapter*: how to obtain its runtime (a published image, a local build context, or a compose file),
where its Stratum + HTTP ports live inside the container, how to render its native config (TOML,
`.env`, YAML) from a logical bench profile, and how to tell when it is ready to accept load.

This module is pure data + validation - no Docker, no I/O beyond reading the YAML. It hard-fails
with a precise message on a malformed registry rather than letting a typo surface as an obscure
Docker error three steps later.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import yaml

TEMPLATE_VARIABLES = frozenset(
    {
        "RPC_HOST",
        "RPC_PORT",
        "RPC_USER",
        "RPC_PASS",
        "DIFFICULTY",
        "MIN_DIFFICULTY",
        "MAX_DIFFICULTY",
        "TAG",
        "ADDRESS",
        "API_PORT",
        "POOL_HOST",
        "POOL_CORES",
    }
)


def cpuset_members(spec: str) -> set[int]:
    """The CPU indices a docker cpuset string covers ("1-4,8" -> {1, 2, 3, 4, 8})."""
    members: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            members.update(range(int(low), int(high) + 1))
        else:
            members.add(int(part))
    return members


def cpuset_count(spec: str) -> int:
    """Number of CPUs in a docker cpuset string ("1", "1-4", "1,3", "0-2,5")."""
    return len(cpuset_members(spec))


VALID_SOURCES = frozenset({"image", "build"})
VALID_CONFIG_FORMATS = frozenset({"toml", "json", "yaml", "env", "none"})
VALID_READINESS_KINDS = frozenset({"http", "tcp", "log"})


class ConfigError(Exception):
    """A `pools.yml` that is missing, malformed, or internally inconsistent."""


@dataclasses.dataclass(frozen=True)
class Profile:
    """A logical, pool-independent benchmark profile. Adapters map it to native config keys."""

    name: str
    difficulty: float
    coinbase_tag: str
    # The vardiff floor and ceiling: the difficulty range the pool may retarget within. `difficulty`
    # is the (low) start so the test miner can submit shares immediately; vardiff then moves up
    # toward max or down toward min, so both are kept distinct from the start.
    min_difficulty: float = 1.0
    max_difficulty: float = 1_000_000.0


@dataclasses.dataclass(frozen=True)
class Regtest:
    """The shared regtest bitcoind every pool-under-test points its backend at."""

    rpc_user: str
    rpc_pass: str
    rpc_port: int
    bitcoind_container: str
    network: str
    compose_project: str
    address: str


@dataclasses.dataclass(frozen=True)
class Pinning:
    """CPU-core pinning (cpuset) for measurement consistency.

    Each workload is confined to its own disjoint cores so the pool and the load generators never
    fight over a core mid-measurement. The strings are docker cpuset syntax ("0", "1-3", "1,3").
    """

    enabled: bool
    bitcoind_cpus: str
    pool_cpus: str
    bench_cpus: str

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        shared = cpuset_members(self.bitcoind_cpus) & (
            cpuset_members(self.pool_cpus) | cpuset_members(self.bench_cpus)
        )
        if shared:
            cpus = ", ".join(str(cpu) for cpu in sorted(shared))
            raise ConfigError(
                f"bitcoind_cpus {self.bitcoind_cpus!r} overlaps the pool/bench cpus on {cpus}; "
                "the regtest node needs cores of its own"
            )


@dataclasses.dataclass(frozen=True)
class Readiness:
    """How to decide a started pool is ready to accept load."""

    kind: str
    timeout_seconds: int = 60
    url: str | None = None
    status: int = 200
    body: str | None = None
    pattern: str | None = None


@dataclasses.dataclass(frozen=True)
class ConfigSpec:
    """How a pool's native config is rendered from a profile and mounted into its container."""

    format: str
    template: str | None = None
    mount: str | None = None


@dataclasses.dataclass(frozen=True)
class PoolSpec:
    """One pool adapter: everything needed to build/run it and drive load at it."""

    name: str
    description: str
    source: str
    stratum_port: int
    config: ConfigSpec
    readiness: Readiness
    api_port: int | None = None
    enabled: bool = True
    image: str | None = None
    build_context: str | None = None
    dockerfile: str | None = None
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    repo: str | None = None
    repo_dir: str | None = None
    repo_ref: str | None = None


@dataclasses.dataclass(frozen=True)
class Registry:
    """The fully-parsed `pools.yml`, plus the repo root used to resolve relative paths."""

    pools: dict[str, PoolSpec]
    profiles: dict[str, Profile]
    regtest: Regtest
    pinning: Pinning
    root: pathlib.Path

    def pool(self, name: str) -> PoolSpec:
        try:
            return self.pools[name]
        except KeyError:
            known = ", ".join(sorted(self.pools)) or "(none)"
            raise ConfigError(f"unknown pool {name!r}; configured pools: {known}") from None

    def profile(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            known = ", ".join(sorted(self.profiles)) or "(none)"
            raise ConfigError(f"unknown profile {name!r}; configured profiles: {known}") from None

    def enabled_pools(self) -> list[str]:
        return [name for name, spec in self.pools.items() if spec.enabled]


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _as_mapping(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _parse_readiness(raw: dict[str, Any], where: str) -> Readiness:
    kind = _require(raw, "kind", where)
    if kind not in VALID_READINESS_KINDS:
        raise ConfigError(
            f"{where}: readiness kind {kind!r} not in {sorted(VALID_READINESS_KINDS)}"
        )
    if kind == "http" and "url" not in raw:
        raise ConfigError(f"{where}: http readiness requires a 'url'")
    if kind == "log" and "pattern" not in raw:
        raise ConfigError(f"{where}: log readiness requires a 'pattern'")
    return Readiness(
        kind=str(kind),
        timeout_seconds=int(raw.get("timeout_seconds", 60)),
        url=raw.get("url"),
        status=int(raw.get("status", 200)),
        body=raw.get("body"),
        pattern=raw.get("pattern"),
    )


def _parse_config(raw: dict[str, Any], where: str) -> ConfigSpec:
    config_format = _require(raw, "format", where)
    if config_format not in VALID_CONFIG_FORMATS:
        raise ConfigError(
            f"{where}: config format {config_format!r} not in {sorted(VALID_CONFIG_FORMATS)}"
        )
    if config_format != "none" and "template" not in raw:
        raise ConfigError(f"{where}: config format {config_format!r} requires a 'template' path")
    return ConfigSpec(
        format=str(config_format), template=raw.get("template"), mount=raw.get("mount")
    )


def _parse_pool(name: str, raw: dict[str, Any]) -> PoolSpec:
    where = f"pools.{name}"
    source = _require(raw, "source", where)
    if source not in VALID_SOURCES:
        raise ConfigError(f"{where}: source {source!r} not in {sorted(VALID_SOURCES)}")
    if source == "image" and not raw.get("image"):
        raise ConfigError(f"{where}: source 'image' requires an 'image'")
    if source == "build" and not raw.get("build_context"):
        raise ConfigError(f"{where}: source 'build' requires a 'build_context'")

    config = _parse_config(
        _as_mapping(_require(raw, "config", where), f"{where}.config"), f"{where}.config"
    )
    readiness = _parse_readiness(
        _as_mapping(_require(raw, "readiness", where), f"{where}.readiness"), f"{where}.readiness"
    )
    return PoolSpec(
        name=name,
        description=str(raw.get("description", "")),
        source=str(source),
        stratum_port=int(_require(raw, "stratum_port", where)),
        api_port=int(raw["api_port"]) if raw.get("api_port") is not None else None,
        config=config,
        readiness=readiness,
        enabled=bool(raw.get("enabled", True)),
        image=raw.get("image"),
        build_context=raw.get("build_context"),
        dockerfile=raw.get("dockerfile"),
        env={
            str(key): str(value)
            for key, value in _as_mapping(raw.get("env", {}), f"{where}.env").items()
        },
        repo=raw.get("repo"),
        repo_dir=raw.get("repo_dir"),
        repo_ref=raw.get("repo_ref"),
    )


def _parse_pinning(raw: dict[str, Any]) -> Pinning:
    return Pinning(
        enabled=bool(raw.get("enabled", True)),
        bitcoind_cpus=str(raw.get("bitcoind_cpus", "0")),
        pool_cpus=str(raw.get("pool_cpus", "1")),
        bench_cpus=str(raw.get("bench_cpus", "2")),
    )


def _parse_regtest(raw: dict[str, Any]) -> Regtest:
    where = "regtest"
    return Regtest(
        rpc_user=str(raw.get("rpc_user", "openbench")),
        rpc_pass=str(raw.get("rpc_pass", "openbenchpass")),
        rpc_port=int(raw.get("rpc_port", 18443)),
        bitcoind_container=str(raw.get("bitcoind_container", "bitcoind")),
        network=str(raw.get("network", "openbench-regtest_default")),
        compose_project=str(raw.get("compose_project", "openbench-regtest")),
        address=str(_require(raw, "address", where)),
    )


def load_registry(path: str | pathlib.Path) -> Registry:
    """Parse `pools.yml` into a validated Registry, or raise ConfigError with a precise message."""
    path = pathlib.Path(path)
    if not path.is_file():
        raise ConfigError(f"registry file not found: {path}")
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"{path}: top level must be a mapping")

    profiles_raw = _as_mapping(_require(document, "profiles", str(path)), "profiles")
    profiles = {
        name: Profile(
            name=name,
            difficulty=float(
                _require(_as_mapping(body, f"profiles.{name}"), "difficulty", f"profiles.{name}")
            ),
            coinbase_tag=str(body.get("coinbase_tag", "/openbench/")),
            min_difficulty=float(body.get("min_difficulty", 1.0)),
            max_difficulty=float(body.get("max_difficulty", 1_000_000.0)),
        )
        for name, body in profiles_raw.items()
    }
    if not profiles:
        raise ConfigError(f"{path}: at least one profile is required")

    pools_raw = _as_mapping(_require(document, "pools", str(path)), "pools")
    pools = {
        name: _parse_pool(name, _as_mapping(body, f"pools.{name}"))
        for name, body in pools_raw.items()
    }
    if not pools:
        raise ConfigError(f"{path}: at least one pool is required")

    regtest = _parse_regtest(_as_mapping(_require(document, "regtest", str(path)), "regtest"))
    pinning = _parse_pinning(_as_mapping(document.get("pinning", {}), "pinning"))
    return Registry(
        pools=pools,
        profiles=profiles,
        regtest=regtest,
        pinning=pinning,
        root=path.parent.resolve(),
    )
