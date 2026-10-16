"""Unit tests for the open-loop rate probe's pure scheduling and statistics helpers."""

from __future__ import annotations

import importlib
import pathlib
import sys
import unittest

_PROBES = pathlib.Path(__file__).resolve().parents[2] / "probes"
sys.path.insert(0, str(_PROBES))  # the probes are standalone scripts, imported side by side
rate_latency = importlib.import_module("rate_latency")


class ScheduleTests(unittest.TestCase):
    def test_workers_interleave_into_one_evenly_spaced_stream(self) -> None:
        rate, workers = 1000.0, 4
        slots = sorted(
            rate_latency.slot_time(10.0, rate, workers, worker, k)
            for worker in range(workers)
            for k in range(25)
        )
        gaps = {round(b - a, 9) for a, b in zip(slots, slots[1:], strict=False)}
        self.assertEqual(gaps, {0.001})  # exactly 1/rate apart, no doubling up
        self.assertEqual(slots[0], 10.0)

    def test_auto_workers_scale_with_rate_within_cpus_and_connections(self) -> None:
        self.assertEqual(rate_latency.worker_count(1, cpus=12, connections=16), 1)
        self.assertEqual(rate_latency.worker_count(10000, cpus=12, connections=16), 5)
        self.assertEqual(rate_latency.worker_count(100000, cpus=12, connections=16), 12)
        self.assertEqual(rate_latency.worker_count(100000, cpus=12, connections=3), 3)
        self.assertEqual(rate_latency.worker_count(1, cpus=12, connections=16, requested=4), 4)

    def test_connections_split_round_robin(self) -> None:
        self.assertEqual(rate_latency.split_connections(16, 5), [4, 3, 3, 3, 3])
        self.assertEqual(sum(rate_latency.split_connections(7, 7)), 7)


class StatisticsTests(unittest.TestCase):
    def test_percentile_interpolates(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(rate_latency.percentile(values, 0.5), 2.5)
        self.assertEqual(rate_latency.percentile(values, 1.0), 4.0)
        self.assertEqual(rate_latency.percentile([], 0.5), 0.0)

    def test_summarize_sorts_and_rounds(self) -> None:
        stats = rate_latency.summarize([float(v) for v in range(1000, 0, -1)])
        self.assertAlmostEqual(stats["p50"], 500.5)
        self.assertAlmostEqual(stats["p99"], 990.01)
        self.assertEqual(stats["max"], 1000.0)
        self.assertEqual(stats["mean"], 500.5)
        self.assertEqual(rate_latency.summarize([])["p99"], 0.0)


if __name__ == "__main__":
    unittest.main()
