"""GPU load / VRAM per process (PDH `GPU Engine`, `GPU Process Memory`) and the game process sampler.

Read every ~5 s from Collector.sample_procs (a read over ~800 engine instances takes ~1 ms).
The wildcard instances are expanded on every collect, so processes started later show up without
re-adding the counters (checked 2026-10-27)."""
import logging
import re
import time

import psutil
import win32pdh

from .config import GPU_PCT_MAX

log = logging.getLogger("perfmon.gpuproc")

ENGINE = r"\GPU Engine(*)\Utilization Percentage"
DEDICATED = r"\GPU Process Memory(*)\Dedicated Usage"
SHARED = r"\GPU Process Memory(*)\Shared Usage"
_INST = re.compile(r"^pid_(\d+)_luid_(0x[0-9a-f]+_0x[0-9a-f]+)_phys_\d+(?:_eng_\d+_engtype_(.*))?$", re.I)
# Engines counted into gpu_pct: 3D, Compute N, Copy, VideoDecode/VideoEncode/Video Codec Engine (AMD)
_COUNTED = re.compile(r"^(3D|Compute|Copy|Video)", re.I)


def parse_instance(inst):
    """'pid_123_luid_0x0_0x16A87_phys_0_eng_0_engtype_3D' -> (123, '0x0_0x16A87', '3D') (engtype None for memory)."""
    m = _INST.match(inst or "")
    if not m:
        return None
    return int(m.group(1)), m.group(2).lower(), m.group(3)


def aggregate(engines, dedicated, shared):
    """Per-PID {gpu, gpu_3d, ded_mb, shared_mb} from PDH arrays {instance: value}.

    Engine load is summed over every adapter; memory is taken from the adapter with the most dedicated
    memory in use (the discrete card - the iGPU's 'shared' memory is ordinary RAM and would mislead).

    PDH occasionally returns absurd engine values (a fresh instance with a zero baseline or a reset counter divided by
    a tiny interval: 3e13 %). An engine above 100 % is dropped as invalid; the per-process sum is capped at 100."""
    out = {}
    for inst, v in engines.items():
        p = parse_instance(inst)
        if not p or not p[2] or not _COUNTED.match(p[2]) or v is None:
            continue
        if not 0 <= v <= GPU_PCT_MAX * 1.05:   # small overshoot is rounding, the rest is garbage (NaN fails the test too)
            continue
        d = out.setdefault(p[0], {"gpu": 0.0, "gpu_3d": 0.0, "ded_mb": None, "shared_mb": None})
        d["gpu"] = min(GPU_PCT_MAX, d["gpu"] + v)
        if p[2].upper() == "3D":
            d["gpu_3d"] = min(GPU_PCT_MAX, d["gpu_3d"] + v)
    per_luid = {}
    for inst, v in dedicated.items():
        p = parse_instance(inst)
        if p and v:
            per_luid[p[1]] = per_luid.get(p[1], 0) + v
    main = max(per_luid, key=per_luid.get) if per_luid else None
    for arr, key in ((dedicated, "ded_mb"), (shared, "shared_mb")):
        for inst, v in arr.items():
            p = parse_instance(inst)
            if not p or p[1] != main or v is None:
                continue
            d = out.setdefault(p[0], {"gpu": 0.0, "gpu_3d": 0.0, "ded_mb": None, "shared_mb": None})
            d[key] = (d[key] or 0) + v / 2**20
    return out


class GpuProcs:
    def __init__(self):
        self.ok = False
        try:
            self.q = win32pdh.OpenQuery()
            self.h = {k: win32pdh.AddEnglishCounter(self.q, p)
                      for k, p in (("eng", ENGINE), ("ded", DEDICATED), ("shared", SHARED))}
            win32pdh.CollectQueryData(self.q)
            self.ok = True
        except Exception as e:
            log.warning("GPU per-process counters unavailable: %s", e)

    def read(self):
        """{pid: {gpu, gpu_3d, ded_mb, shared_mb}}; {} when unavailable."""
        if not self.ok:
            return {}
        fmt = win32pdh.PDH_FMT_DOUBLE | win32pdh.PDH_FMT_NOCAP100
        arrs = {}
        try:
            win32pdh.CollectQueryData(self.q)
            for k, h in self.h.items():
                try:
                    arrs[k] = win32pdh.GetFormattedCounterArray(h, fmt)
                except Exception:
                    arrs[k] = {}   # PDH_CSTATUS_INVALID_DATA right after a process exits
        except Exception as e:
            log.debug("GPU counters read: %s", e)
            return {}
        return aggregate(arrs.get("eng", {}), arrs.get("ded", {}), arrs.get("shared", {}))


class GameProcSampler:
    """Game process metrics every proc interval; thread_max_pct = busiest thread as % of one core."""

    def __init__(self):
        self.prev = {}   # pid -> (t, {thread id: cpu seconds}, read_bytes)

    def sample(self, pid, session, gpu=None, now=None):
        now = now or time.time()
        try:
            pr = psutil.Process(pid)
        except psutil.Error:
            self.prev.pop(pid, None)
            return None
        row = {"ts": now, "session": session, "pid": pid}
        with pr.oneshot():
            try:
                mi = pr.memory_info()
                row["rss_mb"] = round(mi.rss / 2**20, 1)
                priv = getattr(mi, "private", None) or getattr(mi, "pagefile", None)
                row["private_mb"] = round(priv / 2**20, 1) if priv else None
            except psutil.Error:
                pass
            threads, rb = {}, None
            try:
                threads = {t.id: t.user_time + t.system_time for t in pr.threads()}
                row["threads"] = len(threads)
            except psutil.Error:
                pass
            try:
                row["handles"] = pr.num_handles()
            except psutil.Error:
                pass
            try:
                rb = pr.io_counters().read_bytes
            except psutil.Error:
                pass
        p = self.prev.get(pid)
        if p:
            dt = max(0.001, now - p[0])
            busiest = max((c - p[1][tid] for tid, c in threads.items() if tid in p[1]), default=None)
            row["thread_max_pct"] = round(min(100.0, max(0.0, busiest / dt * 100)), 1) if busiest is not None else None
            if rb is not None and p[2] is not None:
                row["io_read_mbs"] = round(max(0, rb - p[2]) / dt / 1e6, 2)
        self.prev[pid] = (now, threads, rb)
        if gpu:
            row["gpu_pct"] = round(gpu["gpu"], 1)
            row["gpu_3d_pct"] = round(gpu["gpu_3d"], 1)
            row["vram_dedicated_mb"] = round(gpu["ded_mb"]) if gpu["ded_mb"] is not None else None
            row["vram_shared_mb"] = round(gpu["shared_mb"]) if gpu["shared_mb"] is not None else None
        return row

    def forget(self, pid):
        self.prev.pop(pid, None)


def top_background(gpu, exclude_pids, n=5, min_pct=1.0):
    """[(pid, data)] of the n processes with the highest GPU load, without the game(s)."""
    items = [(pid, d) for pid, d in gpu.items() if pid not in exclude_pids and pid not in (0, 4) and d["gpu"] >= min_pct]
    items.sort(key=lambda x: -x[1]["gpu"])
    return items[:n]
