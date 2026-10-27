"""Hardware sensors via LibreHardwareMonitorLib (pythonnet) + Windows performance counters (PDH)."""
import logging
import os
import sys
import time

import win32pdh

log = logging.getLogger("perfmon.sensors")


class HardwareSensors:
    """Temperatures, clocks, CPU/GPU power. Full CPU data requires administrator rights."""

    def __init__(self, lhm_dir: str):
        self.ok = False
        self.computer = None
        self.cpu = self.gpu = None
        self.storage = []
        self._last_storage = 0.0
        self.storage_temps = {}
        try:
            os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")
            sys.path.append(lhm_dir)
            import clr  # noqa
            clr.AddReference(os.path.join(lhm_dir, "LibreHardwareMonitorLib.dll"))
            from LibreHardwareMonitor.Hardware import Computer, HardwareType
            c = Computer()
            c.IsCpuEnabled = True
            c.IsGpuEnabled = True
            c.IsStorageEnabled = True
            c.Open()
            self.computer = c
            gpus = []
            for h in c.Hardware:
                t = str(h.HardwareType)
                if t == "Cpu":
                    self.cpu = h
                elif t.startswith("Gpu"):
                    h.Update()
                    total = self._find(h, "SmallData", "GPU Memory Total") or 0
                    gpus.append((total, h))
                elif t == "Storage":
                    self.storage.append(h)
            if gpus:
                self.gpu = max(gpus, key=lambda x: x[0])[1]  # dedicated card = most VRAM
            self.ok = True
            log.info("LHM ok: cpu=%s gpu=%s dyski=%d", self.cpu and self.cpu.Name,
                     self.gpu and self.gpu.Name, len(self.storage))
        except Exception as e:
            log.warning("LHM niedostępny: %s", e)

    @staticmethod
    def _find(h, stype, name):
        for s in h.Sensors:
            if str(s.SensorType) == stype and s.Name == name:
                v = s.Value
                return float(v) if v is not None else None
        return None

    @staticmethod
    def _nz(v):
        return v if v else None  # 0.0 without admin rights = no data

    def read(self) -> dict:
        out = {}
        if not self.ok:
            return out
        try:
            if self.cpu is not None:
                self.cpu.Update()
                f = lambda t, n: self._find(self.cpu, t, n)
                out["cpu_temp"] = self._nz(f("Temperature", "Core (Tctl/Tdie)"))
                out["cpu_power_w"] = self._nz(f("Power", "Package"))
                out["cpu_clock_mhz"] = self._nz(f("Clock", "Cores (Average Effective)")) or self._nz(f("Clock", "Cores (Average)"))
            if self.gpu is not None:
                self.gpu.Update()
                g = lambda t, n: self._find(self.gpu, t, n)
                out.update({
                    "gpu_load": g("Load", "GPU Core"),
                    "gpu_3d": g("Load", "D3D 3D"),
                    "gpu_vram_mb": g("SmallData", "GPU Memory Used") or g("SmallData", "D3D Dedicated Memory Used"),
                    "gpu_vram_total_mb": g("SmallData", "GPU Memory Total"),
                    "gpu_clock_mhz": g("Clock", "GPU Core"),
                    "gpu_mem_clock_mhz": g("Clock", "GPU Memory"),
                    "gpu_temp": g("Temperature", "GPU Core"),
                    "gpu_hotspot": g("Temperature", "GPU Hot Spot"),
                    "gpu_mem_temp": g("Temperature", "GPU Memory"),
                    "gpu_power_w": g("Power", "GPU Package"),
                    "gpu_fan_rpm": g("Fan", "GPU Fan"),
                })
            now = time.time()
            if now - self._last_storage > 30:  # SMART is slow – every 30 s
                self._last_storage = now
                for h in self.storage:
                    h.Update()
                    t = self._find(h, "Temperature", "Temperature")
                    if t:
                        self.storage_temps[h.Name] = round(t, 1)
        except Exception as e:
            log.debug("LHM read error: %s", e)
        return out

    def dump(self) -> list:
        """Every LHM sensor (incl. motherboard/Super I/O and memory) - for discovering what is readable."""
        if not self.ok:
            return []
        c = self.computer
        c.IsMotherboardEnabled = True
        c.IsMemoryEnabled = True
        out = []
        try:
            def walk(h, parent=None):
                h.Update()
                for s in h.Sensors:
                    v, mn, mx = s.Value, s.Min, s.Max
                    out.append({"hardware": h.Name, "hw_type": str(h.HardwareType), "parent": parent,
                                "type": str(s.SensorType), "name": s.Name, "id": str(s.Identifier),
                                "value": float(v) if v is not None else None,
                                "min": float(mn) if mn is not None else None,
                                "max": float(mx) if mx is not None else None})
                for sub in h.SubHardware:
                    walk(sub, h.Name)
            for h in c.Hardware:  # first pass primes rate/effective-clock sensors
                h.Update()
                for sub in h.SubHardware:
                    sub.Update()
            time.sleep(0.5)
            for h in c.Hardware:
                walk(h)
        finally:
            c.IsMotherboardEnabled = False
            c.IsMemoryEnabled = False
        return out

    def describe(self) -> dict:
        return {"cpu": self.cpu.Name if self.cpu else None, "gpu": self.gpu.Name if self.gpu else None}

    def close(self):
        try:
            if self.computer:
                self.computer.Close()
        except Exception:
            pass


class PdhCounters:
    """Windows counters (English names, independent of system language)."""

    SINGLE = {
        "dpc_pct": r"\Processor(_Total)\% DPC Time",
        "interrupt_pct": r"\Processor(_Total)\% Interrupt Time",
        "perf_pct": r"\Processor Information(_Total)\% Processor Performance",
        "ctx_switch_s": r"\System\Context Switches/sec",
        "proc_queue": r"\System\Processor Queue Length",
        "hard_faults_s": r"\Memory\Pages Input/sec",
        "commit_bytes": r"\Memory\Committed Bytes",
        "commit_limit": r"\Memory\Commit Limit",
        "pagefile_pct": r"\Paging File(_Total)\% Usage",
    }
    MULTI = {
        "lat": r"\PhysicalDisk(*)\Avg. Disk sec/Transfer",
        "queue": r"\PhysicalDisk(*)\Current Disk Queue Length",
        "idle": r"\PhysicalDisk(*)\% Idle Time",
        "rd": r"\PhysicalDisk(*)\Disk Read Bytes/sec",
        "wr": r"\PhysicalDisk(*)\Disk Write Bytes/sec",
    }

    def __init__(self):
        self.q = win32pdh.OpenQuery()
        self.h = {}
        for k, p in {**self.SINGLE, **self.MULTI}.items():
            try:
                self.h[k] = win32pdh.AddEnglishCounter(self.q, p)
            except Exception as e:
                log.warning("PDH %s: %s", p, e)
        win32pdh.CollectQueryData(self.q)

    def read(self) -> dict:
        win32pdh.CollectQueryData(self.q)
        out = {}
        fmt = win32pdh.PDH_FMT_DOUBLE | win32pdh.PDH_FMT_NOCAP100
        for k in self.SINGLE:
            if k in self.h:
                try:
                    out[k] = win32pdh.GetFormattedCounterValue(self.h[k], fmt)[1]
                except Exception:
                    pass
        disks = {}
        for k in self.MULTI:
            if k not in self.h:
                continue
            try:
                arr = win32pdh.GetFormattedCounterArray(self.h[k], fmt)
            except Exception:
                continue
            for inst, v in arr.items():
                if inst == "_Total":
                    continue
                disks.setdefault(inst, {})[k] = v
        out["disks"] = disks
        return out
