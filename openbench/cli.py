"""Command-line entry point: parse args, load the registry, dispatch to a runner subcommand.

`openbench <command> --pools <names|all> --profile <name>`. Each command brings the regtest backend
up, drives the selected pools, prints a table, and tears it down. Run `openbench list` to see the
configured pools and profiles.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from collections.abc import Sequence

from openbench import __version__
from openbench import config
from openbench import docker
from openbench import regtest as regtest_module
from openbench import report
from openbench import results
from openbench import runner

LOG = logging.getLogger(__name__)

_DEFAULT_REGISTRY = "pools.yml"
_DEFAULT_PROFILE = "validation"


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def _int_list(text: str) -> list[int]:
    return [int(token) for token in text.replace(",", " ").split()]


def _rate_list(text: str) -> list[float]:
    rates = [float(token) for token in text.replace(",", " ").split()]
    if not rates or any(value <= 0 for value in rates):
        raise config.ConfigError(f"--rates needs positive requests/s values, got {text!r}")
    return rates


def _resolve_pools(registry: config.Registry, requested: str) -> list[str]:
    if requested == "all":
        names = registry.enabled_pools()
        if not names:
            raise config.ConfigError("no enabled pools in the registry")
        return names
    names = [name.strip() for name in requested.split(",") if name.strip()]
    if not names:
        raise config.ConfigError("--pools is empty; pass a comma-separated list or 'all'")
    for name in names:
        registry.pool(name)
    return names


def _add_lb_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--via-lb",
        action="store_true",
        help="put the registry's `loadbalancer:` in front of each pool (probes connect to it)",
    )
    parser.add_argument("--lb-cpus", help="cpuset for the load balancer (with --via-lb)")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--pools", default="all", help="comma-separated pool names, or 'all' (default)"
    )
    parser.add_argument(
        "--profile", default=_DEFAULT_PROFILE, help=f"bench profile (default {_DEFAULT_PROFILE})"
    )
    parser.add_argument(
        "--keep", action="store_true", help="leave the pool + regtest running for inspection"
    )
    parser.add_argument("--pool-cpus", help="cpuset for the pool under test")
    parser.add_argument("--bench-cpus", help="cpuset for the load generators / probes / miner")
    parser.add_argument("--bitcoind-cpus", help="cpuset for the regtest node")
    parser.add_argument("--no-pin", action="store_true", help="disable CPU pinning entirely")
    _add_lb_options(parser)
    parser.add_argument(
        "--out",
        default=results.DEFAULT_OUT_DIR,
        help="dir to store run data for the report (default: results; '' to disable)",
    )
    parser.add_argument(
        "--label", help="label for this run in the report (default: the pool core count)"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openbench", description=__doc__.splitlines()[0])
    parser.add_argument("--version", action="version", version=f"openbench {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument(
        "--registry",
        default=_DEFAULT_REGISTRY,
        help=f"path to the registry (default {_DEFAULT_REGISTRY})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="show configured pools and profiles")
    sub.add_parser("up", help="bring up the regtest bitcoind and leave it running")
    sub.add_parser("down", help="tear down the regtest bitcoind")

    bench = sub.add_parser("bench", help="share-validation throughput + latency + CPU/RSS per pool")
    _add_common(bench)
    bench.add_argument("--connections", type=int, default=50)
    bench.add_argument(
        "--pipeline", type=int, default=16, help="max in-flight submits per connection"
    )
    bench.add_argument("--workers", type=int, default=1, help="load-generator processes")
    bench.add_argument("--warmup", type=float, default=3.0)
    bench.add_argument("--duration", type=float, default=20.0)
    bench.add_argument(
        "--repeat", type=int, default=1, help="run each pool's window N times and report the best"
    )
    bench.add_argument("--csv", help="also write the table as CSV to this path")
    bench.add_argument("--json", dest="json_path", help="also write per-pool raw results as JSON")

    sweep = sub.add_parser("sweep", help="throughput swept over connection counts -> CSV")
    _add_common(sweep)
    sweep.add_argument(
        "--conns", default="1 4 16 64", help="connection counts to sweep (default '1 4 16 64')"
    )
    sweep.add_argument("--pipeline", type=int, default=16)
    sweep.add_argument("--workers", type=int, default=4)
    sweep.add_argument("--warmup", type=float, default=2.0)
    sweep.add_argument("--duration", type=float, default=8.0)
    sweep.add_argument("--csv", default="results.csv", help="CSV output path (default results.csv)")

    connscale = sub.add_parser(
        "connscale", help="per-connection memory: peak RSS at N idle connections"
    )
    _add_common(connscale)
    connscale.add_argument(
        "--conns", default="1000 32000", help="connection counts (default '1000 32000')"
    )
    connscale.add_argument(
        "--hold", type=float, default=14.0, help="seconds to hold connections open while sampling"
    )

    conn_limit = sub.add_parser(
        "conn-limit", help="connection ceiling: how many connections a pool holds"
    )
    _add_common(conn_limit)
    conn_limit.add_argument(
        "--cap", type=int, default=32000, help="safety cap on connections to attempt"
    )

    latency = sub.add_parser("latency", help="new-block -> miner-gets-new-work latency")
    _add_common(latency)
    latency.add_argument("--rounds", type=int, default=20)

    rate_cmd = sub.add_parser(
        "rate", help="open-loop submit->ack latency at fixed request rates (repeatable)"
    )
    _add_common(rate_cmd)
    rate_cmd.add_argument(
        "--rates",
        default="1 100 1000 10000",
        help="requests/s to test, in order (default '1 100 1000 10000')",
    )
    rate_cmd.add_argument("--connections", type=int, default=16, help="spread over N connections")
    rate_cmd.add_argument(
        "--workers", type=int, default=0, help="sender processes (0 = one per ~2k requests/s)"
    )
    rate_cmd.add_argument("--warmup", type=float, default=2.0, help="unmeasured seconds per run")
    rate_cmd.add_argument("--duration", type=float, default=10.0, help="min measured s per run")
    rate_cmd.add_argument(
        "--min-samples",
        type=int,
        default=60,
        help="stretch a run so even a slow rate gets this many requests (default 60)",
    )
    rate_cmd.add_argument("--repeat", type=int, default=3, help="runs per rate (default 3)")

    validate = sub.add_parser(
        "validate", help="validation suite (vardiff-on work audit: duplicate / overlapping work)"
    )
    _add_common(validate)
    validate.add_argument("--connections", type=int, default=4)
    validate.add_argument(
        "--duration",
        type=float,
        default=runner._VARDIFF_AUDIT_DURATION,
        help="audit window in seconds; long enough that vardiff retargets at least once",
    )

    test = sub.add_parser(
        "test", help="build + exercise each pool: work audit (validate) + throughput (bench)"
    )
    _add_common(test)
    test.add_argument("--connections", type=int, default=50)
    test.add_argument("--pipeline", type=int, default=16)
    test.add_argument("--workers", type=int, default=1)
    test.add_argument("--warmup", type=float, default=3.0)
    test.add_argument("--duration", type=float, default=20.0)

    suite = sub.add_parser(
        "suite", help="run all measurements; bench at each --cores config (best-of-3) -> results/"
    )
    # `suite` manages pinning itself via --cores (it benches at each cpuset and gives the load
    # generator the leftover cores), so it omits the shared --pool-cpus/--no-pin/--keep flags.
    suite.add_argument(
        "--pools", default="all", help="comma-separated pool names, or 'all' (default)"
    )
    suite.add_argument(
        "--profile", default=_DEFAULT_PROFILE, help=f"bench profile (default {_DEFAULT_PROFILE})"
    )
    suite.add_argument(
        "--out", default=results.DEFAULT_OUT_DIR, help="dir to store run data (default: results)"
    )
    _add_lb_options(suite)
    suite.add_argument(
        "--cores",
        default="1 1-4",
        help="pool cpusets to bench at, space-separated (default '1 1-4'); the load generator "
        "takes whatever cores are left",
    )
    suite.add_argument("--connections", type=int, default=128)
    suite.add_argument("--pipeline", type=int, default=32)
    suite.add_argument(
        "--workers",
        type=int,
        default=0,
        help="load-generator processes (0 = auto: one per spare core, capped)",
    )
    suite.add_argument("--warmup", type=float, default=5.0)
    suite.add_argument("--duration", type=float, default=20.0)
    suite.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="bench: run the window N times, keep the best (default 3)",
    )

    report_cmd = sub.add_parser(
        "report", help="render an interactive HTML report from stored run data (no Docker needed)"
    )
    report_cmd.add_argument(
        "--out",
        default=results.DEFAULT_OUT_DIR,
        help=f"directory with run JSON (default {results.DEFAULT_OUT_DIR})",
    )
    return parser


def _cmd_list(registry: config.Registry) -> int:
    pool_headers = ["pool", "enabled", "source", "stratum", "api", "description"]
    pool_rows = [
        [
            spec.name,
            "yes" if spec.enabled else "no",
            spec.source,
            spec.stratum_port,
            spec.api_port if spec.api_port is not None else "-",
            spec.description,
        ]
        for spec in registry.pools.values()
    ]
    print(report.render_table(pool_headers, pool_rows))
    print()
    profile_headers = ["profile", "difficulty", "coinbase_tag"]
    profile_rows = [
        [profile.name, profile.difficulty, profile.coinbase_tag]
        for profile in registry.profiles.values()
    ]
    print(report.render_table(profile_headers, profile_rows))
    pinning = registry.pinning
    print()
    if pinning.enabled:
        lb = f"  lb={pinning.lb_cpus}" if pinning.lb_cpus else ""
        print(
            f"cpu pinning: bitcoind={pinning.bitcoind_cpus}  pool={pinning.pool_cpus}  "
            f"bench={pinning.bench_cpus}{lb}"
        )
    else:
        print("cpu pinning: disabled")
    if registry.loadbalancer is not None:
        print(f"load balancer (--via-lb): {registry.loadbalancer.description}")
    return 0


def _apply_pinning_overrides(
    registry: config.Registry, args: argparse.Namespace
) -> config.Registry:
    base = registry.pinning
    bitcoind_cpus = getattr(args, "bitcoind_cpus", None)
    pool_cpus = getattr(args, "pool_cpus", None)
    bench_cpus = getattr(args, "bench_cpus", None)
    lb_cpus = getattr(args, "lb_cpus", None)
    no_pin = getattr(args, "no_pin", False)
    explicit = any((bitcoind_cpus, pool_cpus, bench_cpus, lb_cpus))
    pinning = config.Pinning(
        enabled=(base.enabled or explicit) and not no_pin,
        bitcoind_cpus=bitcoind_cpus or base.bitcoind_cpus,
        pool_cpus=pool_cpus or base.pool_cpus,
        bench_cpus=bench_cpus or base.bench_cpus,
        lb_cpus=lb_cpus or base.lb_cpus,
    )
    return registry if pinning == base else dataclasses.replace(registry, pinning=pinning)


def _apply_via_lb(registry: config.Registry, args: argparse.Namespace) -> config.Registry:
    """With --via-lb, route every pool through the registry's load balancer. The suite checks its
    pinning per core config (it reassigns pool/bench cores); other commands check it here."""
    if not getattr(args, "via_lb", False):
        return registry
    if registry.loadbalancer is None:
        raise config.ConfigError("--via-lb needs a `loadbalancer:` section in the registry")
    if args.command != "suite":
        config.check_lb_pinning(registry.pinning)
    return dataclasses.replace(registry, via_lb=True)


def _standalone_backend(registry: config.Registry) -> regtest_module.Backend:
    return regtest_module.Backend(
        registry.regtest,
        [registry.root / "regtest/docker-compose.yml"],
        registry.regtest.compose_project,
    )


def _require_docker() -> None:
    if not docker.available():
        raise SystemExit(
            "docker is not available - install Docker and ensure the daemon is running"
        )


def _cmd_report(out_dir: str) -> int:
    path = results.write_report(out_dir)
    print(f"wrote {path}")
    return 0


def _dispatch(args: argparse.Namespace, registry: config.Registry) -> int:
    if args.command == "list":
        return _cmd_list(registry)

    _require_docker()

    if args.command == "up":
        _standalone_backend(registry).up()
        print("regtest bitcoind is up")
        return 0
    if args.command == "down":
        _standalone_backend(registry).down()
        print("regtest bitcoind is down")
        return 0

    pools = _resolve_pools(registry, args.pools)
    if getattr(args, "keep", False) and len(pools) > 1:
        raise config.ConfigError(
            "--keep requires a single --pool (it leaves one pool running for inspection)"
        )
    if args.command == "bench":
        knobs = runner.BenchKnobs(
            args.connections, args.pipeline, args.workers, args.warmup, args.duration
        )
        return runner.bench(
            registry,
            pools,
            args.profile,
            knobs,
            repeat=args.repeat,
            csv_path=args.csv,
            json_path=args.json_path,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "sweep":
        return runner.sweep(
            registry,
            pools,
            args.profile,
            _int_list(args.conns),
            pipeline=args.pipeline,
            workers=args.workers,
            warmup=args.warmup,
            duration=args.duration,
            csv_path=args.csv,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "connscale":
        return runner.connscale(
            registry,
            pools,
            args.profile,
            _int_list(args.conns),
            hold=args.hold,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "conn-limit":
        return runner.conn_limit(
            registry,
            pools,
            args.profile,
            cap=args.cap,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "latency":
        return runner.latency(
            registry,
            pools,
            args.profile,
            rounds=args.rounds,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "rate":
        rate_knobs = runner.RateKnobs(
            connections=args.connections,
            workers=args.workers,
            warmup=args.warmup,
            duration=args.duration,
            min_samples=args.min_samples,
            repeat=max(1, args.repeat),
        )
        return runner.rate(
            registry,
            pools,
            args.profile,
            _rate_list(args.rates),
            rate_knobs,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "validate":
        return runner.validate(
            registry,
            pools,
            args.profile,
            connections=args.connections,
            duration=args.duration,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "test":
        knobs = runner.BenchKnobs(
            args.connections, args.pipeline, args.workers, args.warmup, args.duration
        )
        return runner.test(
            registry,
            pools,
            args.profile,
            knobs,
            out=args.out or None,
            label=args.label,
            keep=args.keep,
        )
    if args.command == "suite":
        knobs = runner.BenchKnobs(
            args.connections, args.pipeline, args.workers, args.warmup, args.duration
        )
        return runner.suite(
            registry,
            pools,
            args.profile,
            knobs,
            cores=args.cores.split(),
            repeat=args.repeat,
            out=args.out or None,
        )
    raise AssertionError(f"unhandled command {args.command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        if args.command == "report":
            return _cmd_report(args.out)
        registry = _apply_pinning_overrides(config.load_registry(args.registry), args)
        registry = _apply_via_lb(registry, args)
        return _dispatch(args, registry)
    except config.ConfigError as exc:
        LOG.error("config error: %s", exc)
        return 2
    except OSError as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
