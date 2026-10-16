"""Orchestrate a benchmark or validation run: bring up regtest, drive each pool, report.

Every subcommand follows the same arc - `session()` brings the regtest bitcoind up (and down), then
each pool goes through `PoolUnderTest` (build/run/ready/teardown) and is driven by a probe while its
CPU/RSS are sampled. The functions here own the *what to measure and how to report*; the lifecycle
mechanics live in regtest.Backend and adapters.PoolUnderTest.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import os
import pathlib
import re
import shutil
import tempfile
from collections.abc import Iterator
from collections.abc import Sequence
from typing import Any

import msgspec
import yaml

from openbench import adapters
from openbench import config
from openbench import context
from openbench import docker
from openbench import regtest as regtest_module
from openbench import report
from openbench import results

LOG = logging.getLogger(__name__)

_REGTEST_COMPOSE = "regtest/docker-compose.yml"
_SCRATCH_DIR = ".openbench"
_PRIME_BLOCKS = 20
_PROBE_OVERHEAD_SECONDS = 180
_ESTABLISHED_RE = re.compile(r"ESTABLISHED \d+/\d+")
_RESULT_LINE_RE = re.compile(r"^RESULT .*", re.MULTILINE)
_WIDE_PORT_RANGE = ["--sysctl", "net.ipv4.ip_local_port_range=1024 65535"]
_POOL_ERRORS = (adapters.AdapterError, docker.DockerError, KeyError, IndexError, ValueError)
_SUITE_SWEEP_CONNS = [1, 16, 64, 128]
_SUITE_CONNSCALE_CONNS = [1000, 32000]
_SUITE_CONN_CAP = 32000
_SUITE_LATENCY_ROUNDS = 20
_SUITE_MAX_WORKERS = 16
_VARDIFF_AUDIT_WORKERS = 4
_VARDIFF_AUDIT_DURATION = 90.0
_RATE_DRAIN_SECONDS = 5.0  # how long the rate probe waits for the last acks after sending stops
_RATE_MAX_SEND_LAG_MS = 1.0  # send-lag p99 above this means the client couldn't keep schedule
_RATE_NOTE_CHARS = 160


@dataclasses.dataclass(frozen=True)
class BenchKnobs:
    """The load-generator knobs shared by `bench` and each point of a `sweep`."""

    connections: int = 50
    pipeline: int = 16
    workers: int = 1
    warmup: float = 3.0
    duration: float = 20.0


def _write_pinning_override(path: pathlib.Path, pinning: config.Pinning) -> None:
    """Compose override pinning the regtest node to its cores. The pool/load/miner containers run
    via `docker run --cpuset-cpus`, not compose, so they're pinned in adapters.PoolUnderTest."""
    services: dict[str, dict[str, str]] = {}
    if pinning.bitcoind_cpus:
        services["bitcoind"] = {"cpuset": pinning.bitcoind_cpus}
    path.write_text(yaml.safe_dump({"services": services}), encoding="utf-8")


@contextlib.contextmanager
def session(
    registry: config.Registry, *, mine_maturity: bool = False, keep: bool = False
) -> Iterator[context.RunContext]:
    """Bring the regtest bitcoind up for the duration of a run, then tear it down."""
    scratch_root = registry.root / _SCRATCH_DIR
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch = pathlib.Path(tempfile.mkdtemp(prefix="run-", dir=scratch_root))
    files = [registry.root / _REGTEST_COMPOSE]
    if registry.pinning.enabled:
        override = scratch / "regtest-pinning.yml"
        _write_pinning_override(override, registry.pinning)
        files.append(override)
    backend = regtest_module.Backend(registry.regtest, files, registry.regtest.compose_project)
    run = context.RunContext(registry=registry, backend=backend, scratch=scratch)
    try:
        backend.up()
        docker.ensure_probe_image(registry.root / "openbench" / "probes")
        if mine_maturity:
            backend.mine_to_maturity()
        else:
            backend.generate(_PRIME_BLOCKS, registry.regtest.address)
        yield run
    finally:
        if not keep:
            docker.remove(adapters.POOL_CONTAINER, adapters.LB_CONTAINER)
            backend.down()
            shutil.rmtree(scratch, ignore_errors=True)


def _stratum_args(pool: adapters.PoolUnderTest, address: str) -> list[str]:
    return ["--host", pool.stratum_host, "--port", str(pool.stratum_port), "--address", address]


def _run_bench_probe(
    pool: adapters.PoolUnderTest, address: str, knobs: BenchKnobs
) -> dict[str, Any]:
    """Drive the synthetic Stratum load generator while sampling CPU/RSS; return the result."""
    args = _stratum_args(pool, address) + [
        "--connections",
        str(knobs.connections),
        "--pipeline",
        str(knobs.pipeline),
        "--workers",
        str(knobs.workers),
        "--warmup",
        str(knobs.warmup),
        "--duration",
        str(knobs.duration),
    ]
    timeout = knobs.warmup + knobs.duration + _PROBE_OVERHEAD_SECONDS
    with pool.sampler() as sampler:
        output = pool.run_probe("stratum_bench.py", args, timeout=timeout)
    lines = output.strip().splitlines()
    if not lines:
        raise adapters.AdapterError("load generator produced no output")
    try:
        result: dict[str, Any] = msgspec.json.decode(lines[-1])
    except msgspec.DecodeError as exc:
        raise adapters.AdapterError(
            f"load generator output was not JSON: {lines[-1][:200]!r}"
        ) from exc
    if "validated_per_sec" not in result or "latency_ms" not in result:
        raise adapters.AdapterError(f"load generator result missing expected keys: {result}")
    result["cpu_pct"], result["rss_mib"] = report.parse_docker_stats(sampler.lines)
    return result


def _bench_pool(
    pool: adapters.PoolUnderTest, address: str, knobs: BenchKnobs, repeat: int
) -> dict[str, Any]:
    """Run the load window `repeat` times against the (warm) pool and return the BEST run.

    Repeating back-to-back windows against one warm pool, then taking the highest-throughput run,
    discards windows that were dragged down by transient noise (a scheduler blip, a slow
    connection-setup wave) - the best run is the pool's peak under this load. All reported metrics
    (latency, cpu, rss) come from that single best run, so they stay internally consistent.
    """
    runs = [_run_bench_probe(pool, address, knobs) for _ in range(repeat)]
    per_run_validated = [run["validated_per_sec"] for run in runs]
    best = max(runs, key=lambda run: run["validated_per_sec"])
    if repeat > 1:
        spread = ", ".join(f"{value:.0f}" for value in per_run_validated)
        LOG.info("  %d runs val/s: [%s] best %.0f", repeat, spread, best["validated_per_sec"])
    return {
        **best,
        "runs": repeat,
        "validated_per_sec_per_run": [round(value, 1) for value in per_run_validated],
    }


def _emit(headers: Sequence[str], rows: Sequence[Sequence[object]], csv_path: str | None) -> None:
    print(report.render_table(headers, rows))
    if csv_path:
        report.write_csv(csv_path, headers, rows)
        LOG.info("wrote %s", csv_path)


def _pool_specs(registry: config.Registry, names: Sequence[str]) -> list[config.PoolSpec]:
    return [registry.pool(name) for name in names]


def _default_label(registry: config.Registry) -> str:
    """A short run label for the report - the pool's core count, which is what runs vary by."""
    pinning = registry.pinning
    if not pinning.enabled:
        return "unpinned"
    return f"{config.cpuset_count(pinning.pool_cpus)}-core"


def _persist(
    out: str | None,
    label: str | None,
    kind: str,
    registry: config.Registry,
    profile_name: str,
    knobs: dict[str, object] | None,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    records: Sequence[dict[str, Any]],
) -> None:
    if not out:
        return
    payload = results.make_payload(
        kind,
        label=label or _default_label(registry),
        profile=profile_name,
        pinning=dataclasses.asdict(registry.pinning),
        knobs=knobs,
        columns=headers,
        rows=rows,
        records=records,
    )
    LOG.info("wrote %s", results.write_run(out, payload))


def bench(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    knobs: BenchKnobs,
    *,
    repeat: int = 1,
    csv_path: str | None = None,
    json_path: str | None = None,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """Share-validation throughput + submit->ack latency + CPU/RSS, one row per pool.

    With `repeat` > 1 each pool's row is its best (highest-throughput) of that many windows.
    """
    profile = registry.profile(profile_name)
    headers = ["pool", "val/s", "p50ms", "p95ms", "p99ms", "cpu%", "rssMiB"]
    rows: list[list[object]] = []
    records: list[dict[str, Any]] = []
    failed = False
    with session(registry, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            LOG.info("benchmarking %s (%d run%s)", spec.name, repeat, "" if repeat == 1 else "s")
            try:
                with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                    result = _bench_pool(pool, run.address, knobs, repeat)
                latency = result["latency_ms"]
                rows.append(
                    [
                        spec.name,
                        f"{result['validated_per_sec']:.0f}",
                        f"{latency['p50']:.3f}",
                        f"{latency['p95']:.3f}",
                        f"{latency['p99']:.3f}",
                        f"{result['cpu_pct']:.0f}",
                        f"{result['rss_mib']:.0f}",
                    ]
                )
                records.append({"pool": spec.name, "profile": profile_name, **result})
            except _POOL_ERRORS as exc:
                LOG.error("pool %s failed: %s", spec.name, exc)
                rows.append([spec.name, "FAILED", "-", "-", "-", "-", "-"])
                failed = True
    _emit(headers, rows, csv_path)
    if json_path:
        formatted = msgspec.json.format(msgspec.json.encode(records), indent=2)
        pathlib.Path(json_path).write_bytes(formatted)
        LOG.info("wrote %s", json_path)
    _persist(
        out,
        label,
        "bench",
        registry,
        profile_name,
        dataclasses.asdict(knobs),
        headers,
        rows,
        records,
    )
    return 1 if failed else 0


def sweep(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    connections: Sequence[int],
    *,
    pipeline: int = 16,
    workers: int = 4,
    warmup: float = 2.0,
    duration: float = 8.0,
    csv_path: str | None = None,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """Sweep throughput over connection counts: a 1-conn latency point, then each count."""
    profile = registry.profile(profile_name)
    headers = [
        "pool",
        "connections",
        "workers",
        "pipeline",
        "val/s",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "cpu_pct",
        "rss_mib",
    ]
    rows: list[list[object]] = []
    records: list[dict[str, Any]] = []
    failed = False
    with session(registry, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            try:
                with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                    points = [BenchKnobs(1, 1, 1, warmup, duration)]
                    points += [
                        BenchKnobs(count, pipeline, workers, warmup, duration)
                        for count in connections
                    ]
                    for knobs in points:
                        LOG.info("sweep %s: connections=%d", spec.name, knobs.connections)
                        result = _run_bench_probe(pool, run.address, knobs)
                        latency = result["latency_ms"]
                        rows.append(
                            [
                                spec.name,
                                knobs.connections,
                                knobs.workers,
                                knobs.pipeline,
                                f"{result['validated_per_sec']:.0f}",
                                f"{latency['p50']:.3f}",
                                f"{latency['p95']:.3f}",
                                f"{latency['p99']:.3f}",
                                f"{result['cpu_pct']:.0f}",
                                f"{result['rss_mib']:.0f}",
                            ]
                        )
                        records.append(
                            {
                                "pool": spec.name,
                                "connections": knobs.connections,
                                "workers": knobs.workers,
                                "pipeline": knobs.pipeline,
                                **result,
                            }
                        )
            except _POOL_ERRORS as exc:
                LOG.error("pool %s failed: %s", spec.name, exc)
                rows.append([spec.name, "FAILED", "-", "-", "-", "-", "-", "-", "-", "-"])
                failed = True
    _emit(headers, rows, csv_path)
    _persist(out, label, "sweep", registry, profile_name, None, headers, rows, records)
    return 1 if failed else 0


@dataclasses.dataclass(frozen=True)
class RateKnobs:
    """Knobs for the open-loop latency test (probes/rate_latency.py), shared by every rate."""

    connections: int = 16
    workers: int = 0  # sender processes per step; 0 = one per ~2k requests/s
    warmup: float = 2.0
    duration: float = 10.0  # minimum measured seconds per rate...
    min_samples: int = 60  # ...stretched so even a slow rate gets this many requests
    repeat: int = 3


def rate_step_seconds(rate: float, knobs: RateKnobs) -> float:
    """Measured seconds at one rate: at least `duration`, and long enough for `min_samples`."""
    return max(knobs.duration, knobs.min_samples / rate)


def _run_rate_probe(
    pool: adapters.PoolUnderTest, address: str, rate: float, knobs: RateKnobs
) -> dict[str, Any]:
    """One open-loop step at `rate` requests/s while sampling the pool's CPU/RSS."""
    seconds = rate_step_seconds(rate, knobs)
    args = _stratum_args(pool, address) + [
        "--rate",
        str(rate),
        "--connections",
        str(knobs.connections),
        "--workers",
        str(knobs.workers),
        "--warmup",
        str(knobs.warmup),
        "--duration",
        str(seconds),
        "--drain",
        str(_RATE_DRAIN_SECONDS),
    ]
    timeout = knobs.warmup + seconds + _RATE_DRAIN_SECONDS + _PROBE_OVERHEAD_SECONDS
    with pool.sampler() as sampler:
        output = pool.run_probe("rate_latency.py", args, timeout=timeout)
    lines = output.strip().splitlines()
    if not lines:
        raise adapters.AdapterError("rate probe produced no output")
    try:
        result: dict[str, Any] = msgspec.json.decode(lines[-1])
    except msgspec.DecodeError as exc:
        raise adapters.AdapterError(f"rate probe output was not JSON: {lines[-1][:200]!r}") from exc
    if result.get("errors"):
        raise adapters.AdapterError(f"rate probe at {rate:g} req/s: {'; '.join(result['errors'])}")
    if "latency_ms" not in result or "send_lag_ms" not in result:
        raise adapters.AdapterError(f"rate probe result missing expected keys: {result}")
    result["cpu_pct"], result["rss_mib"] = report.parse_docker_stats(sampler.lines)
    return result


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def rate_row(pool_name: str, rate: float, runs: Sequence[dict[str, Any]]) -> list[object]:
    """One table row for a rate: each statistic is the median over the repeats, next to the p99
    range across them (how repeatable the step was) and a note when the run can't be trusted."""

    def latency(key: str) -> str:
        return f"{_median([run['latency_ms'][key] for run in runs]):.3f}"

    p99s = [run["latency_ms"]["p99"] for run in runs]
    lag_p99 = max(run["send_lag_ms"]["p99"] for run in runs)
    lost = sum(int(run["lost"]) for run in runs)
    notes = []
    if lag_p99 > _RATE_MAX_SEND_LAG_MS:
        notes.append("client behind schedule")
    if lost:
        notes.append("acks lost")
    return [
        pool_name,
        f"{rate:g}",
        f"{_median([run['achieved_rate'] for run in runs]):.0f}",
        sum(int(run["acked"]) for run in runs),
        latency("p50"),
        latency("p90"),
        latency("p99"),
        latency("p999"),
        latency("max"),
        f"{min(p99s):.3f}-{max(p99s):.3f}",
        lost,
        f"{lag_p99:.3f}",
        f"{_median([run['cpu_pct'] for run in runs]):.0f}",
        "; ".join(notes) or "ok",
    ]


def rate(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    rates: Sequence[float],
    knobs: RateKnobs,
    *,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """Open-loop latency: submit->ack latency at each fixed request rate, repeated per rate.

    Requests go out on a fixed schedule whatever the replies, and latency is timed from each
    request's scheduled send, so a slow pool can't hide its delay by slowing the sender down.
    """
    profile = registry.profile(profile_name)
    headers = [
        "pool",
        "rate/s",
        "sent/s",
        "n",
        "p50_ms",
        "p90_ms",
        "p99_ms",
        "p99.9_ms",
        "max_ms",
        "p99_range_ms",
        "lost",
        "lag_p99_ms",
        "cpu_pct",
        "note",
    ]
    rows: list[list[object]] = []
    records: list[dict[str, Any]] = []
    failed = False
    step_total = sum(knobs.warmup + rate_step_seconds(r, knobs) for r in rates) * knobs.repeat
    LOG.info(
        "rate: %s req/s, ~%.0fs of load per pool", " ".join(f"{r:g}" for r in rates), step_total
    )
    with session(registry, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            try:
                with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                    for value in rates:
                        runs = []
                        try:
                            for attempt in range(knobs.repeat):
                                LOG.info(
                                    "rate %s: %g req/s (run %d/%d)",
                                    spec.name,
                                    value,
                                    attempt + 1,
                                    knobs.repeat,
                                )
                                runs.append(_run_rate_probe(pool, run.address, value, knobs))
                        except (
                            _POOL_ERRORS
                        ) as exc:  # keep the other rates; show why this one failed
                            LOG.error("pool %s at %g req/s failed: %s", spec.name, value, exc)
                            note = (
                                f"FAILED: {str(exc)[:_RATE_NOTE_CHARS]}"  # full text: records/log
                            )
                            rows.append(
                                [spec.name, f"{value:g}", *["-"] * (len(headers) - 3), note]
                            )
                            records.append({"pool": spec.name, "rate": value, "error": str(exc)})
                            failed = True
                            continue
                        rows.append(rate_row(spec.name, value, runs))
                        records.append({"pool": spec.name, "rate": value, "runs": runs})
            except _POOL_ERRORS as exc:
                LOG.error("pool %s failed: %s", spec.name, exc)
                rows.append([spec.name, "FAILED", *["-"] * (len(headers) - 2)])
                failed = True
    _emit(headers, rows, None)
    _persist(
        out,
        label,
        "rate",
        registry,
        profile_name,
        dataclasses.asdict(knobs),
        headers,
        rows,
        records,
    )
    return 1 if failed else 0


def connscale(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    connections: Sequence[int],
    *,
    hold: float = 14.0,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """Per-connection memory: open N idle Stratum connections and read the pool's peak RSS."""
    profile = registry.profile(profile_name)
    headers = ["pool", "conns", "established", "peak_MiB"]
    rows: list[list[object]] = []
    with session(registry, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            for count in connections:
                LOG.info("connscale %s: %d connections", spec.name, count)
                try:
                    with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                        args = _stratum_args(pool, run.address) + [
                            "--connections",
                            str(count),
                            "--hold",
                            str(hold),
                        ]
                        with pool.sampler() as sampler:
                            output = pool.run_probe(
                                "connscale.py",
                                args,
                                timeout=hold + max(_PROBE_OVERHEAD_SECONDS, count / 50),
                                extra_run_args=_WIDE_PORT_RANGE,
                            )
                        match = _ESTABLISHED_RE.search(output)
                        established = match.group(0).removeprefix("ESTABLISHED ") if match else "?"
                        _, peak_rss = report.parse_docker_stats(sampler.lines)
                        rows.append([spec.name, count, established, f"{peak_rss:.0f}"])
                except _POOL_ERRORS as exc:
                    LOG.error("pool %s failed: %s", spec.name, exc)
                    rows.append([spec.name, count, "FAILED", "-"])
    _emit(headers, rows, None)
    _persist(out, label, "connscale", registry, profile_name, None, headers, rows, [])
    return 0


def conn_limit(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    *,
    cap: int = 5000,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """Open connections one at a time until the pool stops accepting; report the ceiling."""
    profile = registry.profile(profile_name)
    headers = ["pool", "result"]
    rows: list[list[object]] = []
    with session(registry, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            LOG.info("conn-limit %s (cap %d)", spec.name, cap)
            try:
                with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                    args = [pool.stratum_host, str(pool.stratum_port), run.address, str(cap)]
                    timeout = max(600.0, cap / 50 + 300)
                    output = pool.run_probe(
                        "conn_probe.py", args, timeout=timeout, extra_run_args=_WIDE_PORT_RANGE
                    )
                    match = _RESULT_LINE_RE.search(output)
                    rows.append([spec.name, match.group(0) if match else output.strip()[-200:]])
            except _POOL_ERRORS as exc:
                LOG.error("pool %s failed: %s", spec.name, exc)
                rows.append([spec.name, "FAILED"])
    _emit(headers, rows, None)
    _persist(out, label, "conn-limit", registry, profile_name, None, headers, rows, [])
    return 0


def latency(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    *,
    rounds: int = 20,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """New-block -> miner-gets-new-work latency, timed from a connected Stratum client's view."""
    profile = registry.profile(profile_name)
    headers = ["pool", "new block -> new work"]
    rows: list[list[object]] = []
    with session(registry, mine_maturity=True, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            LOG.info("latency %s (%d rounds)", spec.name, rounds)
            try:
                with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                    args = _stratum_args(pool, run.address) + [
                        "--rpc",
                        run.backend.rpc_endpoint_in_network(),
                        "--rpc-user",
                        run.backend.rpc_user,
                        "--rpc-pass",
                        run.backend.rpc_pass,
                        "--rounds",
                        str(rounds),
                    ]
                    output = pool.run_probe(
                        "zmqlatency.py", args, timeout=rounds * 15 + _PROBE_OVERHEAD_SECONDS
                    )
                    lines = output.strip().splitlines()
                    rows.append([spec.name, lines[-1] if lines else "no output"])
            except _POOL_ERRORS as exc:
                LOG.error("pool %s failed: %s", spec.name, exc)
                rows.append([spec.name, "FAILED"])
    _emit(headers, rows, None)
    _persist(out, label, "latency", registry, profile_name, None, headers, rows, [])
    return 0


def _run_work_audit(
    pool: adapters.PoolUnderTest,
    address: str,
    *,
    connections: int,
    duration: float,
    workers: int = 1,
) -> tuple[int, str]:
    """Drive the test miner with --audit-work across N connections; return (exit_code, output).

    The miner fingerprints the (extranonce1, work) each connection is handed and exits 3 if the pool
    re-issues identical work, hands two connections the same search space, or (since pools run
    vardiff-on) re-issues already-searched work across a difficulty change. It also prints an
    `AUDIT {json}` summary line.
    """
    args = [
        "--url",
        f"{pool.stratum_host}:{pool.stratum_port}",
        "--user",
        address,
        "--connections",
        str(connections),
        "--workers",
        str(workers),
        "--audit-work",
        "--duration",
        str(duration),
    ]
    return pool.run_miner(args, timeout=duration + _PROBE_OVERHEAD_SECONDS)


def _parse_audit_line(output: str) -> dict[str, Any]:
    """Pull the miner's `AUDIT {json}` summary line out of its combined output (empty if absent)."""
    for line in reversed(output.splitlines()):
        if line.startswith("AUDIT "):
            try:
                parsed: dict[str, Any] = msgspec.json.decode(line[len("AUDIT ") :])
            except msgspec.DecodeError:
                return {}
            return parsed
    return {}


def _as_int(stats: dict[str, Any], key: str) -> int:
    """Read an integer stat from a parsed AUDIT summary, tolerating a missing or odd value."""
    try:
        return int(stats.get(key, 0))
    except TypeError, ValueError:
        return 0


def _block_outcome(blocks_submitted: int, height_delta: int, payout_ok: bool) -> str:
    """Whether the pool turned a block-valid share into an actual, correctly-paid on-chain block."""
    if height_delta > 0:
        return "yes" if payout_ok else "wrong-payout"  # block landed but paid the wrong address
    if blocks_submitted > 0:
        return "no-relay"  # the miner found a block but the chain never advanced -- pool bug
    return "none"  # the miner never found a block-valid share (start difficulty too high?)


def _validate_verdict(audit_code: int, retargets: int, block: str) -> str:
    """Combine the checks into one verdict. A real block defect -- found but never relayed, or paid
    to the wrong address -- FAILs. But block production is best-effort: the CPU test miner can't
    always reach a pool's difficulty to mine one, so not mining a block ("none") is skipped, not
    held against the pool. Vardiff never firing is still INCONCLUSIVE (the run didn't test it)."""
    if audit_code != 0 or block in ("no-relay", "wrong-payout"):
        return "FAIL"
    if retargets == 0:
        return "INCONCLUSIVE"
    return "PASS"


def validate(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    *,
    connections: int = 4,
    duration: float = _VARDIFF_AUDIT_DURATION,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """Validation suite. One run per pool measures three things at once, with pools in their
    vardiff-on production config:

    - work distribution: no duplicate / overlapping work across connections;
    - vardiff: difficulty actually retargets, and no work is re-issued across a change;
    - block production (best-effort): if the test miner can mine one, the pool must turn it into a
      real block bitcoind accepts whose coinbase pays the configured address.

    A real defect -- duplicate work, a block the pool never relayed, or one that paid the wrong
    address -- is FAIL. Vardiff never firing is INCONCLUSIVE (the run didn't exercise it). Not
    mining a block (the miner can't always reach a pool's difficulty) is skipped, not a failure.
    Exit 0 iff every pool passed.
    """
    profile = registry.profile(profile_name)
    headers = ["pool", "audit", "retargets", "block", "dup-work", "vardiff-dups"]
    rows: list[list[object]] = []
    records: list[dict[str, Any]] = []
    overall_ok = True
    with session(registry, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            print(f"== validating {spec.name} ==")
            try:
                with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                    height_before = run.backend.block_count()
                    code, output = _run_work_audit(
                        pool,
                        run.address,
                        connections=connections,
                        duration=duration,
                        workers=_VARDIFF_AUDIT_WORKERS,
                    )
                    mined = run.backend.block_count() - height_before
                    payout_ok, tag_ok = True, True
                    for height in range(height_before + 1, height_before + mined + 1):
                        paid, tagged = run.backend.coinbase_pays(
                            height, run.address, profile.coinbase_tag
                        )
                        payout_ok = payout_ok and paid
                        tag_ok = tag_ok and tagged
                        if not tagged:
                            LOG.warning(
                                "pool %s: block at height %d lacks the coinbase tag %r",
                                spec.name,
                                height,
                                profile.coinbase_tag,
                            )
                    stats = _parse_audit_line(output)
                    retargets = _as_int(stats, "retargets")
                    dups = _as_int(stats, "duplicates")
                    vdups = _as_int(stats, "vardiff_duplicates")
                    block = _block_outcome(_as_int(stats, "blocks_submitted"), mined, payout_ok)
                    verdict = _validate_verdict(code, retargets, block)
                    print(
                        f"  audit: {verdict} ({retargets} retargets, block {block}, "
                        f"{vdups} vardiff dups)\n"
                    )
                    rows.append([spec.name, verdict, retargets, block, dups, vdups])
                    records.append(
                        {
                            "pool": spec.name,
                            "verdict": verdict,
                            "block": block,
                            "mined": mined,
                            "coinbase_tag_ok": tag_ok if mined > 0 else None,
                            **stats,
                        }
                    )
                    overall_ok = overall_ok and verdict == "PASS"
            except _POOL_ERRORS as exc:
                LOG.error("pool %s failed: %s", spec.name, exc)
                print(f"  ERROR: {exc}\n")
                rows.append([spec.name, "ERROR", 0, "none", 0, 0])
                overall_ok = False
    print("ALL POOLS PASSED" if overall_ok else "VALIDATION INCOMPLETE/FAILED")
    knobs: dict[str, object] = {
        "connections": connections,
        "workers": _VARDIFF_AUDIT_WORKERS,
        "duration": duration,
    }
    _persist(out, label, "validate", registry, profile_name, knobs, headers, rows, records)
    return 0 if overall_ok else 1


def test(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    knobs: BenchKnobs,
    *,
    audit_connections: int = 4,
    audit_duration: float = 20.0,
    out: str | None = None,
    label: str | None = None,
    keep: bool = False,
) -> int:
    """Build + exercise each pool end to end: the work audit (correctness) and the throughput bench
    (performance), in a single regtest session. Exit 0 iff every pool passes its audit."""
    profile = registry.profile(profile_name)
    headers = ["pool", "work-audit", "val/s", "p50ms", "cpu%", "rssMiB"]
    rows: list[list[object]] = []
    records: list[dict[str, Any]] = []
    overall_ok = True
    with session(registry, keep=keep) as run:
        for spec in _pool_specs(registry, pool_names):
            LOG.info("testing %s", spec.name)
            try:
                with adapters.PoolUnderTest(run, spec, profile, keep=keep) as pool:
                    code, _ = _run_work_audit(
                        pool, run.address, connections=audit_connections, duration=audit_duration
                    )
                    result = _run_bench_probe(pool, run.address, knobs)
                audit = "PASS" if code == 0 else "FAIL"
                overall_ok = overall_ok and code == 0
                rows.append(
                    [
                        spec.name,
                        audit,
                        f"{result['validated_per_sec']:.0f}",
                        f"{result['latency_ms']['p50']:.3f}",
                        f"{result['cpu_pct']:.0f}",
                        f"{result['rss_mib']:.0f}",
                    ]
                )
                records.append({"pool": spec.name, "work_audit": audit, **result})
            except _POOL_ERRORS as exc:
                LOG.error("pool %s failed: %s", spec.name, exc)
                rows.append([spec.name, "ERROR", "-", "-", "-", "-"])
                overall_ok = False
    _emit(headers, rows, None)
    _persist(
        out,
        label,
        "test",
        registry,
        profile_name,
        dataclasses.asdict(knobs),
        headers,
        rows,
        records,
    )
    return 0 if overall_ok else 1


def _format_cpuset(cores: Sequence[int]) -> str:
    """Render sorted core indices as a docker cpuset, collapsing runs ([5,6,7] -> "5-7")."""
    parts = []
    start = prev = cores[0]
    for core in cores[1:]:
        if core == prev + 1:
            prev = core
        else:
            parts.append(f"{start}-{prev}" if start != prev else str(start))
            start = prev = core
    parts.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(parts)


def _registry_for_cores(
    registry: config.Registry, pool_cpus: str, host_cores: int
) -> config.Registry:
    """A copy of registry pinned with the pool on `pool_cpus` and the load generator on the cores
    left over after bitcoind, the pool and (with --via-lb) the load balancer: none share a core."""
    reserved = config.cpuset_members(registry.pinning.bitcoind_cpus)
    if registry.via_lb:
        reserved |= config.cpuset_members(registry.pinning.lb_cpus)
    free = sorted(set(range(host_cores)) - reserved - config.cpuset_members(pool_cpus))
    if not free:
        raise config.ConfigError(
            f"no cores left for the load generator with pool_cpus={pool_cpus!r} on a "
            f"{host_cores}-core host (pick a smaller --cores)"
        )
    pinning = dataclasses.replace(
        registry.pinning, enabled=True, pool_cpus=pool_cpus, bench_cpus=_format_cpuset(free)
    )
    if registry.via_lb:
        config.check_lb_pinning(pinning)
    return dataclasses.replace(registry, pinning=pinning)


def _suite_bench_knobs(knobs: BenchKnobs, bench_cpus: str) -> BenchKnobs:
    """Default the load generator to one worker per spare core (capped) so it can saturate a fast
    pool instead of being the bottleneck; an explicit `--workers` (> 0) is left untouched."""
    if knobs.workers > 0:
        return knobs
    workers = min(config.cpuset_count(bench_cpus), _SUITE_MAX_WORKERS)
    return dataclasses.replace(knobs, workers=max(1, workers))


def suite(
    registry: config.Registry,
    pool_names: Sequence[str],
    profile_name: str,
    knobs: BenchKnobs,
    *,
    cores: Sequence[str],
    repeat: int = 3,
    out: str | None = None,
) -> int:
    """Run every measurement (each in its own regtest session), all persisted to `out`.

    bench runs at each cpuset in `cores` (labeled by core count, best-of-`repeat` to damp noise);
    sweep -> connscale -> conn-limit -> latency -> validate run once at the largest config. The load
    generator gets the cores left over after bitcoind + the pool, and (unless --workers is given)
    one worker per spare core so it never bottlenecks the pool. Non-zero if any failed.
    """
    host_cores = os.cpu_count() or 1
    overall = 0
    for pool_cpus in cores:
        core_label = f"{config.cpuset_count(pool_cpus)}-core"
        core_registry = _registry_for_cores(registry, pool_cpus, host_cores)
        run_knobs = _suite_bench_knobs(knobs, core_registry.pinning.bench_cpus)
        LOG.info("=== suite: bench (%s, %d workers) ===", core_label, run_knobs.workers)
        overall |= bench(
            core_registry,
            pool_names,
            profile_name,
            run_knobs,
            repeat=repeat,
            out=out,
            label=core_label,
        )

    largest = max(cores, key=config.cpuset_count)
    rest_label = f"{config.cpuset_count(largest)}-core"
    rest = _registry_for_cores(registry, largest, host_cores)
    LOG.info("=== suite: sweep ===")
    overall |= sweep(rest, pool_names, profile_name, _SUITE_SWEEP_CONNS, out=out, label=rest_label)
    LOG.info("=== suite: connscale ===")
    overall |= connscale(
        rest, pool_names, profile_name, _SUITE_CONNSCALE_CONNS, out=out, label=rest_label
    )
    LOG.info("=== suite: conn-limit ===")
    overall |= conn_limit(
        rest, pool_names, profile_name, cap=_SUITE_CONN_CAP, out=out, label=rest_label
    )
    LOG.info("=== suite: latency ===")
    overall |= latency(
        rest, pool_names, profile_name, rounds=_SUITE_LATENCY_ROUNDS, out=out, label=rest_label
    )
    LOG.info("=== suite: validate ===")
    overall |= validate(rest, pool_names, profile_name, out=out, label=rest_label)
    return overall
