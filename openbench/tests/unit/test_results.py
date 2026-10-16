"""Unit tests for run persistence and HTML report rendering."""

from __future__ import annotations

import pathlib
import unittest

from openbench import cli
from openbench import results
from openbench.tests import base


def _bench_payload(label: str = "1-core") -> dict:
    return results.make_payload(
        "bench",
        label=label,
        profile="validation",
        pinning={"enabled": True, "pool_cpus": "1"},
        knobs={"connections": 128},
        columns=["pool", "val/s"],
        rows=[["pogolo", 26902]],
        records=[
            {
                "pool": "pogolo",
                "validated_per_sec": 26902.0,
                "latency_ms": {"p50": 1.1},
                "cpu_pct": 94,
                "rss_mib": 23,
            }
        ],
    )


class PayloadTests(unittest.TestCase):
    def test_shape_and_stringified_rows(self) -> None:
        payload = _bench_payload()
        self.assertEqual(payload["kind"], "bench")
        self.assertEqual(payload["label"], "1-core")
        self.assertEqual(payload["columns"], ["pool", "val/s"])
        self.assertEqual(payload["rows"], [["pogolo", "26902"]])
        self.assertEqual(payload["records"][0]["pool"], "pogolo")
        self.assertIn("timestamp", payload)
        self.assertIn("cpus", payload["host"])


class PersistTests(base.TempDirTestCase):
    def test_write_and_load_roundtrip(self) -> None:
        out = self.make_tempdir()
        path = results.write_run(out, _bench_payload())
        self.assertTrue(path.is_file())
        loaded = results.load_runs(out)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["kind"], "bench")

    def test_load_skips_data_json_and_unparseable(self) -> None:
        out = self.make_tempdir()
        (out / "data.json").write_text("[]", encoding="utf-8")
        (out / "bad__x__y.json").write_text("not json", encoding="utf-8")
        results.write_run(out, _bench_payload())
        self.assertEqual(len(results.load_runs(out)), 1)

    def test_same_second_collision_does_not_overwrite(self) -> None:
        out = self.make_tempdir()
        payload = _bench_payload()
        payload["timestamp"] = "2026-06-26T12:00:00"
        first = results.write_run(out, payload)
        second = results.write_run(out, dict(payload))
        self.assertNotEqual(first, second)
        self.assertEqual(len(results.load_runs(out)), 2)

    def test_unicode_labels_stay_distinct(self) -> None:
        out = self.make_tempdir()
        for label in ("\u65e5\u672c\u8a9e", "\ud55c\uad6d\uc5b4"):
            payload = _bench_payload(label)
            payload["timestamp"] = "2026-06-26T12:00:00"
            results.write_run(out, payload)
        self.assertEqual(len(list(pathlib.Path(out).glob("*.json"))), 2)


class ReportTests(base.TempDirTestCase):
    def test_render_is_html_with_embedded_data(self) -> None:
        html = results.render_report([_bench_payload()])
        self.assertIn("<!doctype html>", html)
        self.assertIn('type="application/json"', html)
        self.assertIn("pogolo", html)
        self.assertIn("26902", html)

    def test_tables_are_sortable(self) -> None:
        html = results.render_report([_bench_payload()])
        self.assertIn("makeSortable", html)

    def test_rate_runs_get_their_own_chart(self) -> None:
        payload = results.make_payload(
            "rate",
            label="1-core",
            profile="validation",
            pinning=None,
            knobs=None,
            columns=["pool", "rate/s"],
            rows=[["pogolo", "100"]],
            records=[{"pool": "pogolo", "rate": 100.0, "runs": [{"latency_ms": {"p99": 1.5}}]}],
        )
        html = results.render_report([payload])
        self.assertIn('rateSection(runsOfKind("rate"))', html)  # not dropped like unknown kinds
        self.assertIn('"rate":100.0', html)

    def test_write_report_creates_files(self) -> None:
        out = self.make_tempdir()
        results.write_run(out, _bench_payload())
        path = results.write_report(out)
        self.assertEqual(path.name, "report.html")
        self.assertTrue(path.is_file())
        self.assertTrue((out / "data.json").is_file())

    def test_write_report_creates_missing_dir(self) -> None:
        out = self.make_tempdir() / "sub" / "nested"
        path = results.write_report(out)
        self.assertTrue(path.is_file())

    def test_render_report_skips_null_cpu_count(self) -> None:
        runs = [
            results.make_payload(
                "bench",
                label="a",
                profile=None,
                pinning=None,
                knobs=None,
                columns=["pool"],
                rows=[],
                records=[],
            ),
            results.make_payload(
                "bench",
                label="b",
                profile=None,
                pinning=None,
                knobs=None,
                columns=["pool"],
                rows=[],
                records=[],
            ),
        ]
        runs[0]["host"] = {"cpus": None}
        runs[1]["host"] = {"cpus": 8}
        html = results.render_report(runs)
        self.assertIn("host 8 cores", html)
        self.assertNotIn("host ? cores", html)

    def test_script_close_sequence_is_neutralised(self) -> None:
        payload = results.make_payload(
            "bench",
            label="x",
            profile=None,
            pinning=None,
            knobs=None,
            columns=["pool"],
            rows=[["</script>evil"]],
            records=[],
        )
        html = results.render_report([payload])
        self.assertNotIn("</script>evil", html)
        self.assertIn("<\\/script>evil", html)


class ReportCommandTests(base.TempDirTestCase):
    def test_report_command_needs_no_registry(self) -> None:
        out = self.make_tempdir()
        results.write_run(out, _bench_payload())
        code = cli.main(["--registry", "does-not-exist.yml", "report", "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertTrue((pathlib.Path(out) / "report.html").is_file())
