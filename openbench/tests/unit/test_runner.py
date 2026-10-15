"""Unit tests for the runner's pure helpers (the Docker-driven parts are covered by live runs)."""

from __future__ import annotations

import contextlib
import json
import pathlib
import unittest
from unittest import mock

from openbench import config
from openbench import runner
from openbench.tests import base


class _FakePool:
    def __enter__(self) -> object:
        return mock.Mock()

    def __exit__(self, *_args: object) -> bool:
        return False


def _audit_line(**fields: int) -> str:
    return "AUDIT " + json.dumps(fields)


def _registry(pinning: config.Pinning | None = None) -> config.Registry:
    return config.Registry(
        pools={},
        profiles={},
        regtest=base.sample_regtest(),
        pinning=pinning or base.sample_pinning(),
        root=pathlib.Path("."),
    )


def _probe_result(val: float, p50: float, cpu: float, rss: float) -> dict:
    return {
        "validated_per_sec": val,
        "latency_ms": {"p50": p50, "p95": p50 * 2, "p99": p50 * 3, "max": p50 * 4},
        "cpu_pct": cpu,
        "rss_mib": rss,
    }


class BenchBestRunTests(unittest.TestCase):
    def test_best_run_wins_and_metrics_are_from_it(self) -> None:
        runs = [
            _probe_result(100.0, 1.0, 50.0, 10.0),
            _probe_result(200.0, 3.0, 70.0, 20.0),
            _probe_result(180.0, 9.0, 90.0, 99.0),
        ]
        with mock.patch("openbench.runner._run_bench_probe", side_effect=runs):
            result = runner._bench_pool(
                pool=mock.Mock(), address="addr", knobs=runner.BenchKnobs(), repeat=3
            )
        self.assertEqual(result["validated_per_sec"], 200.0)
        self.assertEqual(result["latency_ms"]["p50"], 3.0)
        self.assertEqual(result["latency_ms"]["p95"], 6.0)
        self.assertEqual(result["cpu_pct"], 70.0)
        self.assertEqual(result["rss_mib"], 20.0)
        self.assertEqual(result["runs"], 3)
        self.assertEqual(result["validated_per_sec_per_run"], [100.0, 200.0, 180.0])

    def test_single_run_is_passthrough(self) -> None:
        with mock.patch(
            "openbench.runner._run_bench_probe", side_effect=[_probe_result(123.0, 1.0, 5.0, 6.0)]
        ):
            result = runner._bench_pool(mock.Mock(), "addr", runner.BenchKnobs(), 1)
        self.assertEqual(result["validated_per_sec"], 123.0)
        self.assertEqual(result["runs"], 1)
        self.assertEqual(result["validated_per_sec_per_run"], [123.0])

    def test_calls_probe_once_per_repeat(self) -> None:
        with mock.patch(
            "openbench.runner._run_bench_probe", side_effect=[_probe_result(1.0, 1.0, 1.0, 1.0)] * 3
        ) as probe:
            runner._bench_pool(mock.Mock(), "addr", runner.BenchKnobs(), 3)
        self.assertEqual(probe.call_count, 3)


class CoreAllocationTests(unittest.TestCase):
    def test_format_cpuset_collapses_runs(self) -> None:
        self.assertEqual(runner._format_cpuset([5, 6, 7, 8]), "5-8")
        self.assertEqual(runner._format_cpuset([2, 4, 5, 7]), "2,4-5,7")
        self.assertEqual(runner._format_cpuset([3]), "3")

    def test_load_generator_gets_the_leftover_cores(self) -> None:
        pinned = runner._registry_for_cores(_registry(), "1-4", host_cores=16).pinning
        self.assertEqual(pinned.pool_cpus, "1-4")
        self.assertEqual(pinned.bench_cpus, "5-15")  # everything after bitcoind(0) + pool(1-4)
        self.assertEqual(pinned.bitcoind_cpus, "0")  # unchanged
        self.assertTrue(pinned.enabled)

    def test_no_free_cores_raises(self) -> None:
        with self.assertRaises(config.ConfigError):
            runner._registry_for_cores(_registry(), "1-3", host_cores=4)  # 0 + 1,2,3 = all 4

    def test_pool_on_a_bitcoind_core_raises(self) -> None:
        two_core_node = base.sample_pinning(bitcoind_cpus="0-1", pool_cpus="2", bench_cpus="3")
        with self.assertRaises(config.ConfigError):
            runner._registry_for_cores(_registry(two_core_node), "1-4", host_cores=16)

    def test_auto_workers_is_one_per_spare_core_capped(self) -> None:
        auto = runner.BenchKnobs(workers=0)
        self.assertEqual(runner._suite_bench_knobs(auto, "2-15").workers, 14)  # 14 spare cores
        self.assertEqual(runner._suite_bench_knobs(auto, "2-31").workers, 16)  # capped at the max
        self.assertEqual(runner._suite_bench_knobs(auto, "2").workers, 1)  # single spare core

    def test_explicit_workers_left_alone(self) -> None:
        self.assertEqual(runner._suite_bench_knobs(runner.BenchKnobs(workers=8), "2-15").workers, 8)


class SuiteTests(unittest.TestCase):
    def test_benches_each_core_config_then_rest_once_at_largest(self) -> None:
        calls = []

        def stub(name: str, code: int):
            def run(*_args: object, **kwargs: object) -> int:
                calls.append((name, kwargs.get("label")))
                return code

            return run

        with (
            mock.patch.multiple(
                "openbench.runner",
                bench=stub("bench", 0),
                sweep=stub("sweep", 0),
                connscale=stub("connscale", 0),
                conn_limit=stub("conn-limit", 1),  # one failure -> non-zero overall
                latency=stub("latency", 0),
                validate=stub("validate", 0),
            ),
            mock.patch(
                "openbench.runner._registry_for_cores", side_effect=lambda reg, cpus, host: reg
            ),
        ):
            code = runner.suite(
                mock.Mock(), ["pogolo"], "validation", runner.BenchKnobs(), cores=["1", "1-4"]
            )

        self.assertEqual(
            [name for name, _ in calls],
            ["bench", "bench", "sweep", "connscale", "conn-limit", "latency", "validate"],
        )
        self.assertEqual([lbl for name, lbl in calls if name == "bench"], ["1-core", "4-core"])
        self.assertEqual([lbl for name, lbl in calls if name == "sweep"], ["4-core"])  # the largest
        self.assertEqual(code, 1)


class ParseAuditLineTests(unittest.TestCase):
    def test_extracts_the_audit_json_amid_log_lines(self) -> None:
        output = (
            "12:00:00 INFO    running 2 connection(s)\n"
            'AUDIT {"retargets": 3, "duplicates": 1, "vardiff_duplicates": 1}\n'
            "12:00:30 INFO    stopping\n"
        )
        self.assertEqual(
            runner._parse_audit_line(output),
            {"retargets": 3, "duplicates": 1, "vardiff_duplicates": 1},
        )

    def test_takes_the_last_audit_line(self) -> None:
        output = 'AUDIT {"retargets": 1}\nAUDIT {"retargets": 9}\n'
        self.assertEqual(runner._parse_audit_line(output), {"retargets": 9})

    def test_missing_or_malformed_yields_empty(self) -> None:
        self.assertEqual(runner._parse_audit_line("no audit here\n"), {})
        self.assertEqual(runner._parse_audit_line("AUDIT not-json\n"), {})


class ValidateVerdictTests(unittest.TestCase):
    """validate()'s verdict + row assembly, with the Docker-driven audit and chain stubbed out."""

    def _validate(
        self,
        audit_results: list[tuple[int, str]],
        heights: list[int],
        payout: tuple[bool, bool] = (True, True),
    ) -> tuple[int, list]:
        specs = []
        for index in range(len(audit_results)):
            spec = mock.Mock()
            spec.name = f"pool{index}"
            specs.append(spec)
        results = iter(audit_results)
        block_heights = iter(heights)
        self.coinbase = mock.Mock(return_value=payout)

        def fake_audit(*_args: object, **_kwargs: object) -> tuple[int, str]:
            return next(results)

        @contextlib.contextmanager
        def fake_session(*_args: object, **_kwargs: object):
            run = mock.Mock()
            run.address = "addr"
            run.backend.block_count.side_effect = lambda: next(block_heights)
            run.backend.coinbase_pays = self.coinbase
            yield run

        with (
            mock.patch("openbench.runner.session", fake_session),
            mock.patch("openbench.runner._pool_specs", return_value=specs),
            mock.patch("openbench.adapters.PoolUnderTest", lambda *a, **k: _FakePool()),
            mock.patch("openbench.runner._run_work_audit", side_effect=fake_audit),
            mock.patch("openbench.runner._persist") as persist,
        ):
            code = runner.validate(mock.Mock(), ["pool0"], "validation", out=None)
        rows = persist.call_args.args[7]
        return code, rows

    def test_pass_needs_retarget_clean_work_and_a_mined_block(self) -> None:
        code, rows = self._validate([(0, _audit_line(retargets=3))], heights=[100, 103])
        self.assertEqual(rows[0], ["pool0", "PASS", 3, "yes", 0, 0])
        self.assertEqual(code, 0)

    def test_block_found_but_chain_did_not_advance_fails(self) -> None:
        audit = (0, _audit_line(retargets=3, blocks_submitted=5))
        code, rows = self._validate([audit], heights=[100, 100])  # block submitted, no new block
        self.assertEqual(rows[0], ["pool0", "FAIL", 3, "no-relay", 0, 0])
        self.assertEqual(code, 1)

    def test_no_block_mined_is_skipped_not_failed(self) -> None:
        code, rows = self._validate([(0, _audit_line(retargets=3))], heights=[100, 100])
        self.assertEqual(rows[0][3], "none")  # miner couldn't reach a mineable difficulty
        self.assertEqual(rows[0][1], "PASS")  # block is best-effort; not held against the pool
        self.assertEqual(code, 0)

    def test_block_paid_to_wrong_address_fails(self) -> None:
        audit = (0, _audit_line(retargets=3))
        code, rows = self._validate([audit], heights=[100, 103], payout=(False, True))
        self.assertEqual(rows[0], ["pool0", "FAIL", 3, "wrong-payout", 0, 0])
        self.assertEqual(code, 1)

    def test_zero_retargets_is_inconclusive_not_pass(self) -> None:
        code, rows = self._validate([(0, _audit_line(retargets=0))], heights=[100, 103])
        self.assertEqual(rows[0][1], "INCONCLUSIVE")  # vardiff never fired
        self.assertEqual(code, 1)

    def test_duplicate_work_fails(self) -> None:
        audit = (3, _audit_line(retargets=2, duplicates=1, vardiff_duplicates=1))
        code, rows = self._validate([audit], heights=[100, 103])
        self.assertEqual(rows[0], ["pool0", "FAIL", 2, "yes", 1, 1])
        self.assertEqual(code, 1)

    def test_overall_fails_if_any_pool_is_not_pass(self) -> None:
        audits = [(0, _audit_line(retargets=2)), (0, _audit_line(retargets=0))]
        code, rows = self._validate(audits, heights=[10, 12, 10, 12])
        self.assertEqual([row[1] for row in rows], ["PASS", "INCONCLUSIVE"])
        self.assertEqual(code, 1)

    def test_checks_every_mined_block_not_just_the_tip(self) -> None:
        self._validate([(0, _audit_line(retargets=3))], heights=[100, 103])  # mined 3 blocks
        checked = [call.args[0] for call in self.coinbase.call_args_list]
        self.assertEqual(checked, [101, 102, 103])  # all three, not just the tip

    def test_rpc_value_error_marks_one_pool_error_not_a_crash(self) -> None:
        spec = mock.Mock()
        spec.name = "pool0"

        @contextlib.contextmanager
        def fake_session(*_args: object, **_kwargs: object):
            run = mock.Mock()
            run.address = "addr"
            run.backend.block_count.side_effect = ValueError("garbage from bitcoin-cli")
            yield run

        with (
            mock.patch("openbench.runner.session", fake_session),
            mock.patch("openbench.runner._pool_specs", return_value=[spec]),
            mock.patch("openbench.adapters.PoolUnderTest", lambda *a, **k: _FakePool()),
            mock.patch("openbench.runner._persist") as persist,
        ):
            code = runner.validate(mock.Mock(), ["pool0"], "validation", out=None)
        self.assertEqual(persist.call_args.args[7][0][1], "ERROR")  # not a whole-run crash
        self.assertEqual(code, 1)
