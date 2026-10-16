# open-pool-benchmark

A pluggable harness to **benchmark and validate Bitcoin solo mining pools** of any implementation
against a private regtest chain. 

## Quick start

Only prerequisite: **Docker** (with the daemon running, and the compose plugin). Run any test
through `docker compose` - the `openbench` service is the driver; it builds/runs everything else via
the Docker socket, so no host Python toolchain is needed:

```sh
docker compose run --rm openbench list
docker compose run --rm openbench suite  --pools pogolo,ckpool
docker compose run --rm openbench report
```

Every run is also saved under `results/` and `openbench report` turns the lot into one interactive
HTML page - see [Output & report](#output--report).

## What it measures

The headline is the **share-validation hot path** - the part that costs CPU under load: parse
`mining.submit` -> rebuild coinbase/merkle/header -> double-SHA256 -> target check. The load generator
([`stratum_bench.py`](openbench/probes/stratum_bench.py)) opens N connections and floods well-formed
shares with a rolling nonce kept *above* the network target, so every share runs the pool's **full**
validation path and is then rejected "above target" - no accepts, no blocks, a stable job: pure
validation throughput, not luck.

| Command | What it reports |
| --- | --- |
| `test` | build + exercise a pool end to end: work audit (pass/fail) **and** throughput, one table |
| `bench` | shares validated/sec, submit->ack latency (p50/p95/p99), pool CPU% + peak RSS |
| `sweep` | the above swept over connection counts -> CSV |
| `connscale` | peak RSS holding N idle connections (per-connection memory cost) |
| `conn-limit` | how many connections a pool will hold before refusing |
| `latency` | new-block -> miner-gets-new-work latency, timed from a client's view |
| `validate` | **validation suite** - the cross-connection work audit (see below) |
| `suite` | run **all** of the above measurements back to back into `results/`, in one command |
| `rate` | **open-loop latency**: submit->ack latency at fixed request rates (default 1, 100, 1k, 10k/s), repeated; not part of `suite` |
| `up` / `down` | manage the shared regtest bitcoind by hand |
| `list` | show configured pools + profiles |

### Reading the numbers

- **val/s** - shares fully validated per second (all rejected above-target; the reject is the same
  hash work as an accept, which is the point).
- **cpu% / rssMiB** - the pool container's mean CPU and peak RSS during the run.
- **Bottleneck check** - if a pool's `cpu%` is well under `100 x cores` while `val/s` plateaus, the
  *load generator* is the limit, not the pool - give it more cores (`--bench-cpus`) and workers.
- **bench vs rate latency** - `bench`/`sweep` are closed loop: every connection keeps its pipeline
  full, so the pool is saturated and its percentiles mostly measure queueing (the average is pinned
  at in-flight / throughput). `rate` is open loop: shares go out evenly spaced at a fixed rate
  whatever the replies, and each is timed from its *scheduled* send, so a slow pool can't hide its
  delay. Each rate runs `--repeat` times; the table shows the median of each percentile, the p99
  range across repeats (how repeatable it was), and flags runs where the client itself fell
  behind schedule (`lag_p99_ms` > 1) or acks never came back.

### Output & report

Every measurement command stores its run as a self-describing JSON file under `results/` (override
with `--out DIR`, disable with `--out ""`), tagged with `--label`. `openbench suite` runs them **all**
in one go, and `openbench report` then renders a single **self-contained, offline** HTML page from
everything in that directory:

```sh
docker compose run --rm openbench suite
docker compose run --rm openbench report

# or run them individually, e.g. the 1-core vs 4-core bench comparison the report charts:
docker compose run --rm openbench bench  --pools all --pool-cpus 1   --bench-cpus 2-15 --label 1-core
docker compose run --rm openbench bench  --pools all --pool-cpus 1-4 --bench-cpus 5-15 --label 4-core
docker compose run --rm openbench sweep  --pools all --conns "1 16 64 128" --label sweep
docker compose run --rm openbench report
```

`results/report.html` embeds the data and draws its own charts with a little vanilla JS (no external
assets, no network) - open it straight from disk. It shows grouped **bar charts** for `bench` (pools
x core-config, switchable between val/s, p50/p95/p99, CPU and RSS, with a clickable legend and hover
read-outs), **line charts** for `sweep` (val/s vs connection count per pool), and **sortable tables**
for every run (click a column header to sort; numeric columns sort by value, click again to reverse).
`report` needs no Docker - it's pure file processing. The `results/` directory is git-ignored.
