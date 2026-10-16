"""Persist benchmark runs to an output directory and render an interactive HTML report.

Each measurement run writes one self-describing JSON file under the output dir (default `results/`);
`openbench report` reads them all and renders a single **self-contained** HTML page - the data is
embedded and the charts are drawn by a small vanilla-JS SVG renderer, so the report has no external
assets and works offline straight from disk.
"""

from __future__ import annotations

import datetime
import logging
import os
import pathlib
import re
from collections.abc import Sequence
from typing import Any

import msgspec

LOG = logging.getLogger(__name__)

DEFAULT_OUT_DIR = "results"
_REPORT_NAME = "report.html"
_SLUG_RE = re.compile(r"[^\w.-]+", re.UNICODE)


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text).strip("-") or "run"


def make_payload(
    kind: str,
    *,
    label: str,
    profile: str | None,
    pinning: dict[str, Any] | None,
    knobs: dict[str, Any] | None,
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    records: Sequence[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Assemble one run's self-describing record (metadata + printed table + structured results)."""
    return {
        "kind": kind,
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "label": label,
        "profile": profile,
        "host": {"cpus": os.cpu_count()},
        "pinning": pinning,
        "knobs": knobs,
        "columns": [str(column) for column in columns],
        "rows": [[str(cell) for cell in row] for row in rows],
        "records": list(records or []),
    }


def write_run(out_dir: str | pathlib.Path, payload: dict[str, Any]) -> pathlib.Path:
    """Write one run payload to a uniquely-named JSON file under out_dir; return the path.

    The timestamp in the base name is only second-resolution, so two same-kind/same-label runs in the
    same second would collide; we append `-2`, `-3`, ... to an existing name so a run is never lost.
    """
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = _slug(str(payload.get("timestamp", "")))
    base = f"{_slug(str(payload.get('kind', 'run')))}__{_slug(str(payload.get('label', 'run')))}__{stamp}"
    path = out / f"{base}.json"
    suffix = 2
    while path.exists():
        path = out / f"{base}-{suffix}.json"
        suffix += 1
    path.write_bytes(msgspec.json.format(msgspec.json.encode(payload), indent=2))
    return path


def load_runs(out_dir: str | pathlib.Path) -> list[dict[str, Any]]:
    """Load every run JSON from out_dir, oldest first (by embedded timestamp then filename)."""
    out = pathlib.Path(out_dir)
    runs: list[dict[str, Any]] = []
    for path in sorted(out.glob("*.json")):
        if path.name == "data.json":
            continue
        try:
            runs.append(msgspec.json.decode(path.read_bytes()))
        except (OSError, msgspec.DecodeError) as exc:
            LOG.warning("skipping unreadable run %s: %s", path, exc)
    runs.sort(key=lambda run: (str(run.get("timestamp", "")), str(run.get("label", ""))))
    return runs


def write_report(out_dir: str | pathlib.Path) -> pathlib.Path:
    """Render the HTML report for every run under out_dir; return the report path."""
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runs = load_runs(out)
    html = render_report(runs)
    report_path = out / _REPORT_NAME
    report_path.write_text(html, encoding="utf-8")
    (out / "data.json").write_bytes(msgspec.json.format(msgspec.json.encode(runs), indent=2))
    return report_path


def render_report(runs: list[dict[str, Any]]) -> str:
    """Render the full self-contained HTML report from the loaded run records."""
    generated = datetime.datetime.now().isoformat(timespec="seconds")
    cpus = next(
        (
            count
            for run in runs
            if isinstance(run.get("host"), dict) and (count := run["host"].get("cpus")) is not None
        ),
        None,
    )
    data = msgspec.json.encode({"runs": runs, "generated": generated, "cpus": cpus}).decode()
    data = data.replace("</", "<\\/")
    shell = _HTML_TEMPLATE.replace("/*GENERATED*/", generated).replace(
        "/*CPUS*/", str(cpus if cpus is not None else "?")
    )
    return shell.replace("/*DATA*/", data)


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>open-pool-benchmark report</title>
<style>
  :root { color-scheme: light dark; --fg:#1b1b1f; --muted:#6b7280; --bg:#ffffff; --card:#f6f7f9;
          --line:#d8dbe0; --accent:#06403f; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e7e7ea; --muted:#9aa0ab; --bg:#16171b; --card:#202228; --line:#33363d; --accent:#7fd1cb; } }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         color:var(--fg); background:var(--bg); }
  header { padding:24px 28px 8px; }
  h1 { margin:0 0 4px; font-size:20px; }
  h2 { margin:28px 0 10px; font-size:16px; }
  .sub { color:var(--muted); font-size:13px; }
  main { padding:0 28px 48px; max-width:1100px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin:14px 0; }
  .row { display:flex; flex-wrap:wrap; gap:8px 16px; align-items:center; margin-bottom:10px; }
  label.ctl { color:var(--muted); font-size:13px; }
  select { font:inherit; padding:3px 6px; border-radius:6px; border:1px solid var(--line);
           background:var(--bg); color:var(--fg); }
  table { border-collapse:collapse; width:100%; font-variant-numeric:tabular-nums; font-size:13px; }
  th, td { padding:5px 9px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; }
  thead th { color:var(--muted); font-weight:600; cursor:pointer; user-select:none; }
  thead th:hover { color:var(--fg); }
  thead th .arrow { opacity:.4; font-size:9px; }
  thead th.sorted { color:var(--fg); }
  thead th.sorted .arrow { opacity:1; }
  .legend { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:8px; font-size:12.5px; }
  .legend span { display:inline-flex; align-items:center; gap:5px; cursor:pointer; user-select:none; }
  .legend i { width:11px; height:11px; border-radius:3px; display:inline-block; }
  .legend .off { opacity:.35; text-decoration:line-through; }
  svg { display:block; width:100%; height:auto; overflow:visible; }
  svg text { fill:var(--fg); font:12px system-ui; }
  svg .axis { stroke:var(--line); } svg .grid { stroke:var(--line); opacity:.5; }
  svg .lbl { fill:var(--muted); }
  .tip { position:fixed; pointer-events:none; background:var(--fg); color:var(--bg); padding:4px 8px;
         border-radius:6px; font-size:12px; opacity:0; transition:opacity .08s; white-space:nowrap; z-index:9; }
  .empty { color:var(--muted); padding:8px 0; }
  footer { color:var(--muted); font-size:12px; padding:0 28px 28px; }
</style>
</head>
<body>
<header>
  <h1>open-pool-benchmark report</h1>
  <div class="sub">generated /*GENERATED*/ | host /*CPUS*/ cores</div>
</header>
<main id="main"></main>
<footer>Self-contained - all data embedded below. Re-generate with <code>openbench report</code>.</footer>
<div class="tip" id="tip"></div>
<script type="application/json" id="data">/*DATA*/</script>
<script>
(function () {
  "use strict";
  var DATA = JSON.parse(document.getElementById("data").textContent);
  var runs = DATA.runs || [];
  var main = document.getElementById("main");
  var tooltip = document.getElementById("tip");
  var COLORS = ["#2f6df6","#06b6a4","#f59e0b","#ef4444","#8b5cf6","#10b981","#e879f9","#64748b"];
  var SVG_NS = "http://www.w3.org/2000/svg";
  var SVG_TAGS = ["svg", "g", "rect", "line", "text", "circle", "polyline", "path"];

  function elem(tag, attrs, children) {
    var node = SVG_TAGS.indexOf(tag) >= 0
      ? document.createElementNS(SVG_NS, tag)
      : document.createElement(tag);
    if (attrs) {
      for (var key in attrs) {
        if (key === "text") node.textContent = attrs[key];
        else node.setAttribute(key, attrs[key]);
      }
    }
    (children || []).forEach(function (child) { node.appendChild(child); });
    return node;
  }
  function showTooltip(event, text) {
    tooltip.textContent = text;
    tooltip.style.opacity = 1;
    tooltip.style.left = (event.clientX + 12) + "px";
    tooltip.style.top = (event.clientY + 12) + "px";
  }
  function hideTooltip() { tooltip.style.opacity = 0; }
  function toNumber(value) {
    var parsed = parseFloat(value);
    return isFinite(parsed) ? parsed : null;
  }
  function formatNumber(value) {
    if (value == null) return "-";
    return value >= 1000
      ? Math.round(value).toLocaleString()
      : (Math.round(value * 1000) / 1000).toString();
  }

  // bench: one chart comparing pools across runs, with a metric picker.
  var BENCH_METRICS = [
    { key: "validated_per_sec", name: "shares validated/s" },
    { key: "p50", name: "latency p50 (ms)", isLatency: true },
    { key: "p95", name: "latency p95 (ms)", isLatency: true },
    { key: "p99", name: "latency p99 (ms)", isLatency: true },
    { key: "cpu_pct", name: "pool CPU %" },
    { key: "rss_mib", name: "pool RSS (MiB)" }
  ];
  function metricValue(record, metric) {
    return metric.isLatency ? (record.latency_ms || {})[metric.key] : record[metric.key];
  }

  // groups = pool names (x axis); series = [{label, color, on}]; valueAt(group, series) -> number|null
  function groupedBar(host, groups, series, valueAt, formatValue) {
    var width = 900, height = 320, marginLeft = 56, marginRight = 12, marginTop = 12, marginBottom = 64;
    var innerWidth = width - marginLeft - marginRight, innerHeight = height - marginTop - marginBottom;
    var active = series.filter(function (entry) { return entry.on; });
    var max = 0;
    groups.forEach(function (group) {
      active.forEach(function (entry) {
        var value = valueAt(group, entry);
        if (value != null && value > max) max = value;
      });
    });
    max = max || 1;
    var svg = elem("svg", { viewBox: "0 0 " + width + " " + height, role: "img" });
    for (var tick = 0; tick <= 4; tick++) {
      var gridY = marginTop + innerHeight - (innerHeight * tick / 4);
      svg.appendChild(elem("line", { class: "grid", x1: marginLeft, y1: gridY, x2: width - marginRight, y2: gridY }));
      svg.appendChild(elem("text", { class: "lbl", x: marginLeft - 8, y: gridY + 4, "text-anchor": "end", text: formatNumber(max * tick / 4) }));
    }
    var groupWidth = innerWidth / groups.length;
    var barWidth = Math.min(46, (groupWidth - 10) / Math.max(1, active.length));
    groups.forEach(function (group, groupIndex) {
      var groupX = marginLeft + groupIndex * groupWidth;
      active.forEach(function (entry, seriesIndex) {
        var value = valueAt(group, entry);
        var barX = groupX + (groupWidth - barWidth * active.length) / 2 + seriesIndex * barWidth;
        var barHeight = value == null ? 0 : innerHeight * value / max;
        var rect = elem("rect", { x: barX + 2, y: marginTop + innerHeight - barHeight,
          width: Math.max(0, barWidth - 4), height: barHeight, fill: entry.color, rx: 2 });
        rect.addEventListener("mousemove", function (event) {
          showTooltip(event, group + " - " + entry.label + ": " + formatValue(value));
        });
        rect.addEventListener("mouseleave", hideTooltip);
        svg.appendChild(rect);
      });
      svg.appendChild(elem("text", { class: "lbl", x: groupX + groupWidth / 2, y: height - marginBottom + 18, "text-anchor": "middle", text: group }));
    });
    svg.appendChild(elem("line", { class: "axis", x1: marginLeft, y1: marginTop + innerHeight, x2: width - marginRight, y2: marginTop + innerHeight }));
    host.appendChild(svg);
  }

  function legend(host, series, onToggle) {
    var box = elem("div", { class: "legend" });
    series.forEach(function (entry) {
      var span = elem("span", entry.on ? {} : { class: "off" },
        [elem("i", { style: "background:" + entry.color }), document.createTextNode(entry.label)]);
      span.addEventListener("click", function () {
        entry.on = !entry.on;
        span.className = entry.on ? "" : "off";
        onToggle();
      });
      box.appendChild(span);
    });
    host.appendChild(box);
  }

  // Pull the first signed number out of a cell ("held=33,000" -> 33000, "FAILED" -> null) so a column
  // of numbers sorts numerically even when it carries units or thousands separators.
  function sortKey(text) {
    var match = String(text).replace(/,/g, "").match(/-?\d+(?:\.\d+)?/);
    return match ? parseFloat(match[0]) : null;
  }
  function makeSortable(table) {
    var headerCells = [].slice.call(table.tHead.rows[0].cells);
    var tbody = table.tBodies[0];
    var state = { column: -1, direction: 1 };
    headerCells.forEach(function (headerCell, columnIndex) {
      headerCell.appendChild(elem("span", { class: "arrow", text: "" }));
      headerCell.addEventListener("click", function () {
        var direction = state.column === columnIndex ? -state.direction : 1;
        state = { column: columnIndex, direction: direction };
        var rows = [].slice.call(tbody.rows);
        var cellText = function (row) { return row.cells[columnIndex] ? row.cells[columnIndex].textContent : ""; };
        var numericCount = rows.filter(function (row) { return sortKey(cellText(row)) !== null; }).length;
        var numeric = numericCount >= rows.length - numericCount;  // sort as numbers if at least half are
        rows.sort(function (rowA, rowB) {
          if (numeric) {
            var numA = sortKey(cellText(rowA)), numB = sortKey(cellText(rowB));
            return ((numA === null ? -Infinity : numA) - (numB === null ? -Infinity : numB)) * direction;
          }
          return cellText(rowA).localeCompare(cellText(rowB)) * direction;
        });
        rows.forEach(function (row) { tbody.appendChild(row); });  // re-attach in sorted order
        headerCells.forEach(function (otherCell, otherIndex) {
          otherCell.classList.toggle("sorted", otherIndex === columnIndex);
          otherCell.querySelector(".arrow").textContent =
            otherIndex === columnIndex ? (direction > 0 ? " ^" : " v") : "";
        });
      });
    });
  }

  function table(run) {
    var tableEl = elem("table"), thead = elem("thead"), headerRow = elem("tr");
    (run.columns || []).forEach(function (column) { headerRow.appendChild(elem("th", { text: column })); });
    thead.appendChild(headerRow);
    tableEl.appendChild(thead);
    var tbody = elem("tbody");
    (run.rows || []).forEach(function (row) {
      var rowEl = elem("tr");
      row.forEach(function (cell) { rowEl.appendChild(elem("td", { text: cell })); });
      tbody.appendChild(rowEl);
    });
    tableEl.appendChild(tbody);
    if ((run.rows || []).length) { makeSortable(tableEl); }
    return tableEl;
  }

  function section(title) {
    var card = elem("div", { class: "card" });
    main.appendChild(elem("h2", { text: title }));
    main.appendChild(card);
    return card;
  }

  function benchSection(benchRuns) {
    var host = section("Throughput - bench");
    if (!benchRuns.length) {
      host.appendChild(elem("div", { class: "empty", text: "no bench runs" }));
      return;
    }
    var pools = [];
    benchRuns.forEach(function (run) {
      (run.records || []).forEach(function (record) {
        if (record.pool && pools.indexOf(record.pool) < 0) pools.push(record.pool);
      });
    });
    var series = benchRuns.map(function (run, index) {
      return { label: run.label || run.kind, run: run, color: COLORS[index % COLORS.length], on: true };
    });
    var select = elem("select");
    BENCH_METRICS.forEach(function (metric, index) {
      select.appendChild(elem("option", { value: index, text: metric.name }));
    });
    host.appendChild(elem("div", { class: "row" }, [elem("label", { class: "ctl", text: "metric" }), select]));
    var chart = elem("div");
    host.appendChild(chart);
    function recordFor(run, pool) {
      return (run.records || []).filter(function (record) { return record.pool === pool; })[0];
    }
    function draw() {
      chart.innerHTML = "";
      var metric = BENCH_METRICS[+select.value];
      groupedBar(chart, pools, series, function (pool, entry) {
        var record = recordFor(entry.run, pool);
        return record ? toNumber(metricValue(record, metric)) : null;
      }, function (value) { return formatNumber(value) + (metric.isLatency ? " ms" : ""); });
    }
    legend(host, series, draw);
    select.addEventListener("change", draw);
    draw();
    benchRuns.forEach(function (run) {
      host.appendChild(elem("div", { class: "sub", style: "margin-top:14px", text: run.label + " | " + run.timestamp }));
      host.appendChild(table(run));
    });
  }

  // sweep: val/s vs connection count, one line per pool.
  function sweepSection(sweepRuns) {
    if (!sweepRuns.length) return;
    var host = section("Throughput sweep - sweep");
    sweepRuns.forEach(function (run) {
      var byPool = {};
      (run.records || []).forEach(function (record) {
        var pool = record.pool, connections = toNumber(record.connections), value = toNumber(record.validated_per_sec);
        if (pool == null || connections == null || value == null) return;
        (byPool[pool] = byPool[pool] || []).push([connections, value]);
      });
      var pools = Object.keys(byPool);
      pools.forEach(function (pool) { byPool[pool].sort(function (pointA, pointB) { return pointA[0] - pointB[0]; }); });
      lineChart(host, byPool, pools);
      host.appendChild(elem("div", { class: "sub", style: "margin-top:6px", text: run.label + " | " + run.timestamp }));
      host.appendChild(table(run));
    });
  }
  // rate: open-loop submit->ack latency vs offered request rate (log x), one line per pool; each
  // point is the median of that rate's repeats, like the table.
  var RATE_METRICS = [["p50", "p50"], ["p90", "p90"], ["p99", "p99"], ["p999", "p99.9"], ["max", "max"]];
  function median(values) {
    var sorted = values.slice().sort(function (a, b) { return a - b; }), middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  }
  function rateSection(rateRuns) {
    if (!rateRuns.length) return;
    var host = section("Latency at fixed request rates - rate");
    rateRuns.forEach(function (run) {
      var select = elem("select");
      RATE_METRICS.forEach(function (metric) { select.appendChild(elem("option", { value: metric[0], text: metric[1] })); });
      select.value = "p99";
      host.appendChild(elem("div", { class: "row" }, [elem("label", { class: "ctl", text: "latency" }), select]));
      var box = elem("div");
      host.appendChild(box);
      function draw() {
        box.innerHTML = "";
        var byPool = {}, name = select.options[select.selectedIndex].text;
        (run.records || []).forEach(function (record) {
          var rate = toNumber(record.rate);
          var values = (record.runs || []).map(function (attempt) { return toNumber((attempt.latency_ms || {})[select.value]); })
            .filter(function (value) { return value != null; });
          if (record.pool == null || rate == null || !values.length) return;
          (byPool[record.pool] = byPool[record.pool] || []).push([rate, median(values)]);
        });
        var pools = Object.keys(byPool);
        pools.forEach(function (pool) { byPool[pool].sort(function (pointA, pointB) { return pointA[0] - pointB[0]; }); });
        lineChart(box, byPool, pools, {
          xLabel: "requests/s (log scale) - y: " + name + " ms from scheduled send",
          logX: true,
          xTicks: true,
          tip: function (pool, x, y) { return pool + " @ " + formatNumber(x) + " req/s: " + name + " " + formatNumber(y) + " ms"; }
        });
      }
      select.addEventListener("change", draw);
      draw();
      host.appendChild(elem("div", { class: "sub", style: "margin-top:6px", text: run.label + " | " + run.timestamp }));
      host.appendChild(table(run));
    });
  }

  // opts (all optional): xLabel, logX (log10 x axis), xTicks (label each data x), tip(pool, x, y).
  function lineChart(host, byPool, pools, opts) {
    opts = opts || {};
    var tip = opts.tip || function (pool, x, y) { return pool + " @ " + x + " conns: " + formatNumber(y) + "/s"; };
    var series = pools.map(function (pool, index) {
      return { label: pool, color: COLORS[index % COLORS.length], on: true };
    });
    var box = elem("div");
    host.appendChild(box);
    function draw() {
      box.innerHTML = "";
      var active = series.filter(function (entry) { return entry.on; });
      var width = 900, height = 320, marginLeft = 60, marginRight = 12, marginTop = 12, marginBottom = 40;
      var innerWidth = width - marginLeft - marginRight, innerHeight = height - marginTop - marginBottom;
      var maxX = 1, maxY = 1, minX = Infinity, xs = {};
      active.forEach(function (entry) {
        byPool[entry.label].forEach(function (point) {
          if (point[0] > maxX) maxX = point[0];
          if (point[0] < minX) minX = point[0];
          if (point[1] > maxY) maxY = point[1];
          xs[point[0]] = true;
        });
      });
      if (!(minX < maxX)) minX = maxX / 10;  // a single x (or none): give the log axis a span
      var svg = elem("svg", { viewBox: "0 0 " + width + " " + height, role: "img" });
      for (var tick = 0; tick <= 4; tick++) {
        var gridY = marginTop + innerHeight - innerHeight * tick / 4;
        svg.appendChild(elem("line", { class: "grid", x1: marginLeft, y1: gridY, x2: width - marginRight, y2: gridY }));
        svg.appendChild(elem("text", { class: "lbl", x: marginLeft - 8, y: gridY + 4, "text-anchor": "end", text: formatNumber(maxY * tick / 4) }));
      }
      function scaleX(value) {
        if (opts.logX) return marginLeft + innerWidth * (Math.log10(value) - Math.log10(minX)) / (Math.log10(maxX) - Math.log10(minX));
        return marginLeft + innerWidth * value / maxX;
      }
      function scaleY(value) { return marginTop + innerHeight - innerHeight * value / maxY; }
      if (opts.xTicks) {
        Object.keys(xs).forEach(function (x) {
          svg.appendChild(elem("text", { class: "lbl", x: scaleX(Number(x)), y: marginTop + innerHeight + 14, "text-anchor": "middle", text: formatNumber(Number(x)) }));
        });
      }
      active.forEach(function (entry) {
        var points = byPool[entry.label].map(function (point) { return scaleX(point[0]) + "," + scaleY(point[1]); }).join(" ");
        svg.appendChild(elem("polyline", { points: points, fill: "none", stroke: entry.color, "stroke-width": 2 }));
        byPool[entry.label].forEach(function (point) {
          var dot = elem("circle", { cx: scaleX(point[0]), cy: scaleY(point[1]), r: 3.5, fill: entry.color });
          dot.addEventListener("mousemove", function (event) {
            showTooltip(event, tip(entry.label, point[0], point[1]));
          });
          dot.addEventListener("mouseleave", hideTooltip);
          svg.appendChild(dot);
        });
      });
      svg.appendChild(elem("line", { class: "axis", x1: marginLeft, y1: marginTop + innerHeight, x2: width - marginRight, y2: marginTop + innerHeight }));
      svg.appendChild(elem("text", { class: "lbl", x: marginLeft + innerWidth / 2, y: height - 6, "text-anchor": "middle", text: opts.xLabel || "connections" }));
      box.appendChild(svg);
    }
    draw();
    legend(host, series, draw);
  }

  // Every other run kind is shown as a table only.
  var TITLES = {
    connscale: "Per-connection memory - connscale",
    "conn-limit": "Connection ceiling - conn-limit",
    latency: "New-block -> new-work latency - latency",
    validate: "Validation - validate",
    test: "Combined test - test"
  };

  function runsOfKind(kind) { return runs.filter(function (run) { return run.kind === kind; }); }

  benchSection(runsOfKind("bench"));
  sweepSection(runsOfKind("sweep"));
  rateSection(runsOfKind("rate"));
  ["test", "latency", "connscale", "conn-limit", "validate"].forEach(function (kind) {
    var kindRuns = runsOfKind(kind);
    if (!kindRuns.length) return;
    var host = section(TITLES[kind] || kind);
    kindRuns.forEach(function (run) {
      host.appendChild(elem("div", { class: "sub", style: "margin-top:8px", text: run.label + " | " + run.timestamp }));
      host.appendChild(table(run));
    });
  });
  if (!runs.length) {
    main.appendChild(elem("p", { class: "empty", text: "No runs found. Run a benchmark with --out results, then `openbench report`." }));
  }
})();
</script>
</body>
</html>
"""
