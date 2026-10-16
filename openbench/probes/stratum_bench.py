#!/usr/bin/env python3
"""Synthetic Stratum v1 load generator for benchmarking a pool's share hot path.

Opens N connections, subscribes + authorizes, then floods mining.submit shares
so the pool runs its full parse -> rebuild coinbase/merkle/header ->
double-SHA256 -> target-compare on every share. Measures share-validation
throughput and submit->ack latency.

The generator does NOT grind valid work, but it DOES hash each candidate to keep
the share *above the network target* (regtest's target is trivially easy, so a
random nonce solves a "block" ~half the time -- which would make the pool call
submitblock and churn the chain). So every submitted share is structurally valid
and unique, yet always rejected "above target": pure validation cost, no blocks.
Hash math mirrors miner/openbench_miner.py (genesis-verified). stdlib only.

Output: one JSON line of results on stdout.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import random
import struct
import time

from _stratum import DECODER, StratumMessage, encode, frame


def _dsha(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def _prevhash_header_bytes(prevhash_hex: str) -> bytes:
    raw = bytes.fromhex(prevhash_hex)
    return b"".join(raw[i:i + 4][::-1] for i in range(0, 32, 4))


def _bits_to_target(nbits_hex: str) -> int:
    bits = int(nbits_hex, 16)
    exp, mant = bits >> 24, bits & 0x00FFFFFF
    return mant << (8 * (exp - 3)) if exp > 3 else mant >> (8 * (3 - exp))


def _pct(sorted_ms: list[float], p: float) -> float:
    if not sorted_ms:
        return 0.0
    k = (len(sorted_ms) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(sorted_ms) - 1)
    return round(sorted_ms[lo] + (sorted_ms[hi] - sorted_ms[lo]) * (k - lo), 4)


class Client:
    def __init__(self, host: str, port: int, address: str, pipeline: int,
                 stop_evt: asyncio.Event):
        self.host, self.port, self.address = host, port, address
        self.pipeline, self.stop_evt = pipeline, stop_evt
        self.extranonce1 = ""
        self.extranonce2_size = 8
        self.job_id = self.ntime_hex = None
        self.auth_ok = False
        self._mid = self._tail = None
        self._submit_tpl = b""
        self._net_target = 0
        self._nonce = 0
        self._id = 10
        self._pending: dict[int, float] = {}
        self.measuring = False
        self.submits = self.accepts = self.rejects = self.errors = 0
        self.lats: list[float] = []

    def _send(self, obj: dict) -> None:
        self._w.write(frame(obj))

    async def connect(self) -> None:
        self._r, self._w = await asyncio.open_connection(self.host, self.port)
        self._sem = asyncio.Semaphore(self.pipeline)
        self._subscribed = asyncio.Event()
        self._authorized = asyncio.Event()
        self._has_job = asyncio.Event()
        asyncio.create_task(self._read_loop())
        # authorize only after the subscribe reply: pipelined, ckpool can take the authorize first
        # and reject it (the same order real miners use)
        self._send({"id": 1, "method": "mining.subscribe", "params": ["bench/1.0"]})
        await self._w.drain()
        await asyncio.wait_for(self._subscribed.wait(), 30)
        self._send({"id": 2, "method": "mining.authorize", "params": [self.address, "x"]})
        await self._w.drain()
        await asyncio.wait_for(self._authorized.wait(), 30)
        if not self.auth_ok:
            raise RuntimeError(f"pool rejected authorize for {self.address!r}; supply an address valid on the pool's network")
        await asyncio.wait_for(self._has_job.wait(), 30)

    def _prepare(self, p: list) -> None:
        job_id, prevhash, coinbase1, coinbase2, branches, version, nbits, ntime = p[:8]
        if not self.extranonce1:
            return
        extranonce2 = "00" * self.extranonce2_size
        coinbase = bytes.fromhex(coinbase1 + self.extranonce1 + extranonce2 + coinbase2)
        root = _dsha(coinbase)
        for b in branches:
            root = _dsha(root + bytes.fromhex(b))
        prefix = struct.pack("<I", int(version, 16)) + _prevhash_header_bytes(prevhash) + root
        self._mid = hashlib.sha256(prefix[:64])
        self._tail = prefix[64:] + struct.pack("<I", int(ntime, 16)) + struct.pack("<I", int(nbits, 16))
        self._net_target = _bits_to_target(nbits)
        self.job_id, self.ntime_hex = job_id, ntime
        self._submit_tpl = (
            b',"method":"mining.submit","params":['
            + encode(self.address) + b"," + encode(job_id) + b","
            + encode(extranonce2) + b"," + encode(ntime) + b',"'
        )
        self._has_job.set()

    def _next_above_target_nonce(self) -> int:
        while True:
            n = self._nonce
            self._nonce = (self._nonce + 1) & 0xFFFFFFFF
            ctx = self._mid.copy()
            ctx.update(self._tail + struct.pack("<I", n))
            if int.from_bytes(hashlib.sha256(ctx.digest()).digest(), "little") > self._net_target:
                return n

    async def _read_loop(self) -> None:
        try:
            while not self.stop_evt.is_set():
                line = await self._r.readline()
                recv_ts = time.perf_counter()
                if not line:
                    break
                try:
                    message = DECODER.decode(line)
                except ValueError:
                    continue
                if message.method == "mining.notify":
                    self._prepare(message.params)
                elif message.method is None:
                    self._on_response(message, recv_ts)
        except (OSError, asyncio.IncompleteReadError):
            pass

    def _on_response(self, message: StratumMessage, recv_ts: float) -> None:
        mid = message.id
        if mid == 1:
            result = message.result or []
            if len(result) >= 3:
                self.extranonce1 = result[1]
                self.extranonce2_size = int(result[2])
            self._subscribed.set()
        elif mid == 2:
            self.auth_ok = message.result is True
            self._authorized.set()
        elif mid in self._pending:
            t0 = self._pending.pop(mid)
            self._sem.release()
            if self.measuring:
                self.lats.append((recv_ts - t0) * 1000.0)
                self.submits += 1
                if message.result is True:
                    self.accepts += 1
                else:
                    self.rejects += 1

    async def flood(self, go_evt: asyncio.Event) -> None:
        await go_evt.wait()
        while not self.stop_evt.is_set():
            await self._sem.acquire()
            if self.stop_evt.is_set():
                self._sem.release()
                return
            nonce = self._next_above_target_nonce()
            mid = self._id
            self._id += 1
            self._pending[mid] = time.perf_counter()
            try:
                self._w.write(b'{"id":%d' % mid + self._submit_tpl + b'%08x"]}\n' % nonce)
                await self._w.drain()
            except OSError:
                self.errors += 1
                self.stop_evt.set()
                return

    def reset(self) -> None:
        self.submits = self.accepts = self.rejects = 0
        self.lats = []
        self.measuring = True


async def run_local(args, n_conn: int, barrier=None) -> dict:
    """Drive n_conn connections through warmup + a measured window; return raw counts + latencies."""
    stop_evt = asyncio.Event()
    go_evt = asyncio.Event()
    clients = [Client(args.host, args.port, args.address, args.pipeline, stop_evt)
               for _ in range(n_conn)]
    await asyncio.gather(*(c.connect() for c in clients))
    if barrier is not None:
        try:
            barrier.wait(timeout=60)
        except Exception:
            pass
    floods = [asyncio.create_task(c.flood(go_evt)) for c in clients]
    go_evt.set()
    await asyncio.sleep(args.warmup)
    for c in clients:
        c.reset()
    start = time.perf_counter()
    await asyncio.sleep(args.duration)
    for c in clients:
        c.measuring = False
    elapsed = time.perf_counter() - start
    stop_evt.set()
    await asyncio.sleep(0.3)
    for f in floods:
        f.cancel()
    res = {"submits": 0, "accepts": 0, "rejects": 0, "errors": 0, "elapsed": elapsed, "lats": []}
    for c in clients:
        res["submits"] += c.submits
        res["accepts"] += c.accepts
        res["rejects"] += c.rejects
        res["errors"] += c.errors
        res["lats"].extend(c.lats)
    return res


def _worker(args, n_conn, barrier, q):
    try:
        res = asyncio.run(run_local(args, n_conn, barrier))
    except Exception as e:  # noqa: BLE001
        res = {"submits": 0, "accepts": 0, "rejects": 0, "errors": n_conn,
               "elapsed": 0.0, "lats": [], "err": str(e)}
    if len(res["lats"]) > 200000:
        res["lats"] = random.sample(res["lats"], 200000)
    q.put(res)


def _emit(merged: dict, args, workers: int) -> None:
    lats = sorted(merged["lats"])
    elapsed = merged["elapsed"] or 1.0
    print(encode({
        "connections": args.connections,
        "workers": workers,
        "pipeline": args.pipeline,
        "duration_s": round(elapsed, 3),
        "submits": merged["submits"],
        "accepts": merged["accepts"],
        "rejects": merged["rejects"],
        "errors": merged["errors"],
        "validated_per_sec": round(merged["submits"] / elapsed, 1),
        "latency_ms": {"p50": _pct(lats, 0.50), "p95": _pct(lats, 0.95),
                       "p99": _pct(lats, 0.99), "max": round(lats[-1], 4) if lats else 0.0},
    }).decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3333)
    ap.add_argument("--address", required=True, help="payout address valid on the pool's network")
    ap.add_argument("--connections", type=int, default=50)
    ap.add_argument("--pipeline", type=int, default=16, help="max in-flight submits per connection")
    ap.add_argument("--workers", type=int, default=1,
                    help="load-gen processes (>1 lifts the single-core client cap)")
    ap.add_argument("--warmup", type=float, default=3.0)
    ap.add_argument("--duration", type=float, default=20.0)
    args = ap.parse_args()

    if args.workers <= 1:
        _emit(asyncio.run(run_local(args, args.connections)), args, 1)
        return 0

    import multiprocessing as mp
    mp.set_start_method("fork", force=True)
    w = min(args.workers, args.connections)
    barrier = mp.Barrier(w)
    q: mp.Queue = mp.Queue()
    per, rem = divmod(args.connections, w)
    procs = []
    for i in range(w):
        p = mp.Process(target=_worker, args=(args, per + (1 if i < rem else 0), barrier, q))
        p.start()
        procs.append(p)
    results = [q.get() for _ in range(w)]
    for p in procs:
        p.join()
    merged = {"submits": 0, "accepts": 0, "rejects": 0, "errors": 0, "elapsed": 0.0, "lats": []}
    for res in results:
        for k in ("submits", "accepts", "rejects", "errors"):
            merged[k] += res[k]
        merged["elapsed"] = max(merged["elapsed"], res["elapsed"])
        merged["lats"].extend(res["lats"])
    _emit(merged, args, w)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
