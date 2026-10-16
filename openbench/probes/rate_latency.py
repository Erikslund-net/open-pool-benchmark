#!/usr/bin/env python3
"""Open-loop Stratum v1 latency probe: submit->ack latency at one fixed request rate.

stratum_bench.py is closed loop: it keeps every connection's pipeline full, so the pool is always
saturated and its percentiles mostly measure queueing. This probe instead sends mining.submit
shares on a fixed schedule - evenly spaced at --rate per second, whether or not earlier ones have
been acked - and times every ack from the share's *scheduled* send time. A slow pool therefore
can't slow the sender down and hide its own delay (coordinated omission). If the probe itself
falls behind schedule, that lateness is counted too, and reported separately as send lag, so a
client-limited run is visible as such rather than blamed on the pool.

Shares are built exactly like stratum_bench.py's: structurally valid, unique, hashed to stay above
the network target, so each runs the pool's full validation and is rejected low-difficulty.

The rate is split over --workers processes, each owning a slice of the --connections: worker w
sends slot k at t0 + (k * workers + w) / rate, so together they produce one evenly spaced stream.
Each worker is a single thread running a non-blocking select() loop that spins for the last few
milliseconds (SPIN_SECONDS) before each send, so sends land within microseconds of their slot.

Output: one JSON line on stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import math
import multiprocessing as mp
import os
import selectors
import socket
import struct
import time

from _stratum import DECODER, encode, frame
from stratum_bench import _bits_to_target, _dsha, _prevhash_header_bytes

# Busy-poll this close to a send slot instead of sleeping in select(): waking from a sleep costs up
# to ~1 ms on a VM, which at low rates would show up as send lag (and so as latency).
SPIN_SECONDS = 0.005
PER_WORKER_RATE = 2000.0  # auto --workers: one sender process per this many requests/s
PERCENTILES = {"p50": 0.50, "p90": 0.90, "p99": 0.99, "p999": 0.999}
FIRST_SHARE_ID = 100  # ids below this are the subscribe/authorize handshake


def worker_count(rate: float, cpus: int, connections: int, requested: int = 0) -> int:
    """Sender processes for a rate: --workers if given, else one per PER_WORKER_RATE, never more
    than the cpus or connections available (each worker needs a connection of its own)."""
    wanted = requested if requested > 0 else math.ceil(rate / PER_WORKER_RATE)
    return max(1, min(wanted, cpus, connections))


def split_connections(connections: int, workers: int) -> list[int]:
    """How many connections each worker drives: round-robin, so counts differ by at most one."""
    return [connections // workers + (1 if w < connections % workers else 0) for w in range(workers)]


def slot_time(t0: float, rate: float, workers: int, worker: int, k: int) -> float:
    """Scheduled send time of worker `worker`'s k-th request: the workers interleave into one
    stream spaced exactly 1/rate apart."""
    return t0 + (k * workers + worker) / rate


def percentile(sorted_values: list[float], p: float) -> float:
    """Linear-interpolated percentile of an already sorted list (0.0 when empty)."""
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def summarize(values_ms: list[float]) -> dict[str, float]:
    """p50/p90/p99/p99.9/max/mean of a latency sample, in milliseconds, rounded to 1 us."""
    ordered = sorted(values_ms)
    stats = {name: round(percentile(ordered, p), 3) for name, p in PERCENTILES.items()}
    stats["max"] = round(ordered[-1], 3) if ordered else 0.0
    stats["mean"] = round(sum(ordered) / len(ordered), 3) if ordered else 0.0
    return stats


class _Conn:
    """One Stratum connection: handshake, current job, and next-share framing."""

    def __init__(self, host: str, port: int, address: str) -> None:
        self.address = address
        self.sock = socket.create_connection((host, port), timeout=30)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.rbuf = b""
        self.wbuf = b""
        self.extranonce1 = ""
        self.extranonce2_size = 8
        self.pending: dict[int, tuple[float, float, bool]] = {}  # id -> (scheduled, sent, measured)
        self._mid = None
        self._tail = b""
        self._target = 0
        self._tpl = b""
        self._nonce = 0

    def handshake(self) -> None:
        """Subscribe, then authorize once the subscribe reply is in (blocking), then wait for the
        first job. Not pipelined: ckpool can handle a back-to-back authorize before the subscribe
        and reject it, which is also why real miners wait for the subscribe reply."""
        start = time.monotonic()
        self.sock.sendall(frame({"id": 1, "method": "mining.subscribe", "params": ["rate/1.0"]}))
        authorize_sent = False
        authorized = None
        deadline = start + 30
        seen = []  # what the pool did send, for the error message
        try:
            while (authorized is None or not self._tpl) and time.monotonic() < deadline:
                for message in self._read_lines():  # blocking here: the socket still has its timeout
                    seen.append(message.method or f"reply {message.id}={message.result!r}"[:40])
                    if message.id == 1 and message.result:
                        self.extranonce1 = message.result[1]
                        self.extranonce2_size = int(message.result[2])
                        if not authorize_sent:
                            self.sock.sendall(frame(
                                {"id": 2, "method": "mining.authorize", "params": [self.address, "x"]}))
                            authorize_sent = True
                    elif message.id == 2:
                        authorized = message.result is True
                    elif message.method == "mining.notify":
                        self._prepare(message.params)
        except TimeoutError:
            pass  # reported below as the step that never answered
        if authorized is False:
            raise RuntimeError(f"pool rejected authorize for {self.address!r}; it sent {seen}")
        if not self.extranonce1 or authorized is None or not self._tpl:
            waiting = ("subscribe reply" if not self.extranonce1 else
                       "authorize reply" if authorized is None else "first mining.notify")
            raise RuntimeError(
                f"handshake: no {waiting} after {time.monotonic() - start:.1f}s; it sent {seen}")
        self.sock.setblocking(False)

    def _read_lines(self) -> list:
        try:
            chunk = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return []
        if not chunk:
            raise ConnectionError("pool closed the connection")
        self.rbuf += chunk
        *lines, self.rbuf = self.rbuf.split(b"\n")
        return [DECODER.decode(line) for line in lines if line.strip()]

    def _prepare(self, params: list) -> None:
        job_id, prevhash, coinbase1, coinbase2, branches, version, nbits, ntime = params[:8]
        if not self.extranonce1:
            return
        extranonce2 = "00" * self.extranonce2_size
        root = _dsha(bytes.fromhex(coinbase1 + self.extranonce1 + extranonce2 + coinbase2))
        for branch in branches:
            root = _dsha(root + bytes.fromhex(branch))
        prefix = struct.pack("<I", int(version, 16)) + _prevhash_header_bytes(prevhash) + root
        self._mid = hashlib.sha256(prefix[:64])
        self._tail = prefix[64:] + struct.pack("<I", int(ntime, 16)) + struct.pack("<I", int(nbits, 16))
        self._target = _bits_to_target(nbits)
        self._tpl = (
            b',"method":"mining.submit","params":[' + encode(self.address) + b"," + encode(job_id)
            + b"," + encode(extranonce2) + b"," + encode(ntime) + b',"'
        )

    def share(self, mid: int) -> bytes:
        """The next unique share frame, its nonce chosen so the hash stays above network target."""
        while True:
            nonce = self._nonce
            self._nonce = (self._nonce + 1) & 0xFFFFFFFF
            ctx = self._mid.copy()
            ctx.update(self._tail + struct.pack("<I", nonce))
            if int.from_bytes(hashlib.sha256(ctx.digest()).digest(), "little") > self._target:
                return b'{"id":%d' % mid + self._tpl + b'%08x"]}\n' % nonce

    def flush(self) -> None:
        if self.wbuf:
            try:
                sent = self.sock.send(self.wbuf)
            except (BlockingIOError, InterruptedError):
                return
            self.wbuf = self.wbuf[sent:]

    def handle_input(self, now: float, out: dict) -> None:
        for message in self._read_lines():
            if message.method == "mining.notify":
                self._prepare(message.params)
            elif message.method is None and isinstance(message.id, int) and message.id >= FIRST_SHARE_ID:
                entry = self.pending.pop(message.id, None)
                if entry is None:
                    continue
                scheduled, sent, measured = entry
                if measured:
                    out["latency_ms"].append((now - scheduled) * 1000.0)
                    out["service_ms"].append((now - sent) * 1000.0)
                    out["acked"] += 1


def _worker(args, worker: int, workers: int, n_conn: int, ready, start, t0_value, results) -> None:
    out = {"latency_ms": [], "service_ms": [], "lag_ms": [], "sent": 0, "acked": 0, "lost": 0, "error": ""}
    try:
        conns = [_Conn(args.host, args.port, args.address) for _ in range(n_conn)]
        for index, conn in enumerate(conns):
            try:
                conn.handshake()
            except Exception as exc:
                raise RuntimeError(f"worker {worker} connection {index + 1}/{n_conn}: {exc}") from exc
        selector = selectors.DefaultSelector()
        for conn in conns:
            selector.register(conn.sock, selectors.EVENT_READ, conn)
        ready.put(worker)
        start.wait(120)
        t0 = t0_value.value
        measure_from = t0 + args.warmup
        stop_at = measure_from + args.duration
        drain_until = stop_at + args.drain
        k = 0
        mid = FIRST_SHARE_ID
        next_send = slot_time(t0, args.rate, workers, worker, k)
        while True:
            now = time.perf_counter()
            while next_send < stop_at and now >= next_send:
                conn = conns[k % n_conn]
                measured = next_send >= measure_from
                conn.pending[mid] = (next_send, now, measured)
                conn.wbuf += conn.share(mid)
                conn.flush()
                if measured:
                    out["sent"] += 1
                    out["lag_ms"].append((now - next_send) * 1000.0)
                mid += 1
                k += 1
                next_send = slot_time(t0, args.rate, workers, worker, k)
                now = time.perf_counter()
            if now >= stop_at and (now >= drain_until or not any(
                    measured for c in conns for (_, _, measured) in c.pending.values())):
                break
            wait = (next_send if next_send < stop_at else drain_until) - now
            for key, _ in selector.select(wait - SPIN_SECONDS if wait > SPIN_SECONDS else 0):
                key.data.handle_input(time.perf_counter(), out)
            for conn in conns:
                conn.flush()
        out["lost"] = sum(1 for c in conns for (_, _, measured) in c.pending.values() if measured)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which fails the run
        out["error"] = f"{type(exc).__name__}: {exc}"
        ready.put(worker)
    results.put(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--address", required=True)
    ap.add_argument("--rate", type=float, required=True, help="requests per second (all connections)")
    ap.add_argument("--connections", type=int, default=16)
    ap.add_argument("--workers", type=int, default=0, help="sender processes (0 = auto)")
    ap.add_argument("--warmup", type=float, default=2.0, help="seconds sent but not measured")
    ap.add_argument("--duration", type=float, default=10.0, help="measured seconds")
    ap.add_argument("--drain", type=float, default=5.0, help="max seconds to wait for last acks")
    args = ap.parse_args()

    cpus = len(os.sched_getaffinity(0))
    workers = worker_count(args.rate, cpus, args.connections, args.workers)
    ready, results = mp.Queue(), mp.Queue()
    start = mp.Event()
    t0_value = mp.Value("d", 0.0)
    procs = [
        mp.Process(target=_worker, args=(args, w, workers, n, ready, start, t0_value, results))
        for w, n in enumerate(split_connections(args.connections, workers))
    ]
    for proc in procs:
        proc.start()
    for _ in procs:
        ready.get(timeout=120)
    t0_value.value = time.perf_counter() + 0.5  # CLOCK_MONOTONIC: shared by every process
    start.set()
    merged = {"latency_ms": [], "service_ms": [], "lag_ms": [], "sent": 0, "acked": 0, "lost": 0}
    errors = []
    for _ in procs:
        res = results.get(timeout=args.warmup + args.duration + args.drain + 180)
        if res["error"]:
            errors.append(res["error"])
        for key in ("latency_ms", "service_ms", "lag_ms"):
            merged[key].extend(res[key])
        for key in ("sent", "acked", "lost"):
            merged[key] += res[key]
    for proc in procs:
        proc.join(timeout=10)
    print(encode({
        "rate": args.rate,
        "achieved_rate": round(merged["sent"] / args.duration, 1),
        "connections": args.connections,
        "workers": workers,
        "duration": args.duration,
        "sent": merged["sent"],
        "acked": merged["acked"],
        "lost": merged["lost"],
        "latency_ms": summarize(merged["latency_ms"]),
        "service_ms": summarize(merged["service_ms"]),
        "send_lag_ms": summarize(merged["lag_ms"]),
        "errors": errors,
    }).decode(), flush=True)
    return 0  # worker errors travel in the JSON, so the caller can show them


if __name__ == "__main__":
    raise SystemExit(main())
