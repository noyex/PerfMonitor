"""Hardware sensors via LibreHardwareMonitorLib (pythonnet) + Windows performance counters (PDH)."""
import logging
import os
import sys
import time

import win32pdh

log = logging.getLogger("perfmon.sensors")


STORAGE_FAST_S = 10   # NVMe temperature cadence in game/stress sessions (a hot game disk throttles within seconds)


def _rd(v, n=0):
    return round(v, n) if v is not None else None


class HardwareSensors:
    """Temperatures, clocks, CPU/GPU power. Full CPU data requires administrator rights."""

    def __init__(self, lhm_dir: str):
        self.ok = False
        self.computer = None
        self.cpu = self.gpu = self.board = None
        self.dimms = []
        self.storage = []
        self._last_dimm = 0.0
        self.dimm_temp = None
        self._last_storage = self._last_nvme = 0.0
        self.storage_temps = {}         # disk -> composite temperature (NVMe) / the only one (SATA)
        self.storage_hot = {}           # NVMe disk -> hottest of its sensors (controller / NAND), when above composite
        self.storage_limits = {}        # NVMe disk -> {"warn", "crit"}: the drive's own throttle thresholds
        self.storage_fast = False       # collector: game/stress session -> NVMe temperatures every STORAGE_FAST_S
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
            c.IsMotherboardEnabled = True   # Super I/O: Vcore, VSoC, VRM temperatures (UV/OC detail)
            c.IsMemoryEnabled = True        # DIMM temperatures
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
                elif t == "Motherboard":
                    self.board = h
                elif t == "Memory" and h.Name not in ("Virtual Memory", "Total Memory"):   # DIMM SPD hubs
                    self.dimms.append(h)
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

    @staticmethod
    def _map(h):
        """{(sensor type, name): value} for one hardware node (values None when unreadable)."""
        m = {}
        for s in h.Sensors:
            v = s.Value
            m[(str(s.SensorType), s.Name)] = float(v) if v is not None else None
        return m

    def read(self) -> dict:
        """Per-second values for `samples` plus out["detail"]: the UV/OC telemetry for `hw_detail`."""
        out = {}
        if not self.ok:
            return out
        try:
            det = {}
            if self.cpu is not None:
                self.cpu.Update()
                cm = self._map(self.cpu)
                f = lambda t, n: cm.get((t, n))
                out["cpu_temp"] = self._nz(f("Temperature", "Core (Tctl/Tdie)"))
                out["cpu_power_w"] = self._nz(f("Power", "Package"))
                out["cpu_clock_mhz"] = self._nz(f("Clock", "Cores (Average Effective)")) or self._nz(f("Clock", "Cores (Average)"))
                n = 0
                while ("Clock", f"Core #{n + 1}") in cm:
                    n += 1
                eff = [_rd(f("Clock", f"Core #{i} (Effective)")) for i in range(1, n + 1)]
                det.update(
                    cpu_eff=eff, cpu_clk=[_rd(f("Clock", f"Core #{i}")) for i in range(1, n + 1)],
                    cpu_core_w=[_rd(f("Power", f"Core #{i} (SMU)"), 2) for i in range(1, n + 1)],
                    cpu_pkg_w=self._nz(f("Power", "Package")), tctl=self._nz(f("Temperature", "Core (Tctl/Tdie)")),
                    tccd=self._nz(f("Temperature", "CCD1 (Tdie)")), bus_mhz=self._nz(f("Clock", "Bus Speed")))
                out["cpu_eff_max"] = max((e for e in eff if e), default=None)
            if self.gpu is not None:
                self.gpu.Update()
                gm = self._map(self.gpu)
                g = lambda t, n: gm.get((t, n))
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
                    "gpu_fan_pct": g("Control", "GPU Fan"),
                    "gpu_voltage": self._nz(g("Voltage", "GPU Core")),
                })
                det.update(gpu_clk=out["gpu_clock_mhz"], gpu_mem_clk=out["gpu_mem_clock_mhz"], gpu_v=out["gpu_voltage"],
                           gpu_w=out["gpu_power_w"], gpu_temp=out["gpu_temp"], gpu_hot=out["gpu_hotspot"],
                           gpu_mem_temp=out["gpu_mem_temp"], gpu_fan=out["gpu_fan_rpm"], gpu_load=out["gpu_load"],
                           gpu_fan_pct=out["gpu_fan_pct"])
            if self.board is not None:
                bm = {}
                for sub in self.board.SubHardware:   # the Super I/O chip is a sub-node of the board
                    sub.Update()
                    bm.update(self._map(sub))
                b = lambda t, n: self._nz(bm.get((t, n)))
                det.update(cpu_vcore=b("Voltage", "Vcore"), cpu_vsoc=b("Voltage", "Vcore SoC"),
                           cpu_vmisc=b("Voltage", "Vcore Misc"), vrm_temp=b("Temperature", "VRM MOS"),
                           vsoc_temp=b("Temperature", "VSoC MOS"), cpu_fan=b("Fan", "CPU Fan"),
                           # PSU rails and board temperatures (hard-reset forensics): same Super I/O read, no extra cost
                           v12=b("Voltage", "+12V"), v5=b("Voltage", "+5V"), v33=b("Voltage", "+3.3V"),
                           v3sb=b("Voltage", "+3V Standby"), sys_temp=b("Temperature", "System"),
                           pch_temp=b("Temperature", "PCH"), pcie_temp=b("Temperature", "PCIe x16"))
                out["cpu_vcore"], out["vrm_temp"] = det["cpu_vcore"], det["vrm_temp"]
            now = time.time()
            if self.dimms and now - self._last_dimm > 10:   # SPD over SMBus: no need every second
                self._last_dimm = now
                temps = []
                for h in self.dimms:
                    h.Update()
                    temps += [v for (t, n), v in self._map(h).items() if t == "Temperature" and n.startswith("DIMM") and v]
                self.dimm_temp = max(temps) if temps else None
            det["dimm_temp"] = self.dimm_temp
            out["detail"] = det
            full = now - self._last_storage > 30   # SMART is slow: every 30 s, NVMe every 10 s in sessions
            if full or (self.storage_fast and now - self._last_nvme > STORAGE_FAST_S):
                self._last_nvme = now
                if full:
                    self._last_storage = now
                for h in self.storage:
                    if full or self._nvme(h):
                        h.Update()
                        self._storage_temps(h)
        except Exception as e:
            log.debug("LHM read error: %s", e)
        return out

    @staticmethod
    def _nvme(h):
        return "/nvme/" in str(h.Identifier)

    def _storage_temps(self, h):
        """NVMe drives expose Composite Temperature + Temperature #1..#n (controller, NAND) + their own Warning/Critical
        thresholds; SATA drives a single Temperature."""
        temps = {n: v for (t, n), v in self._map(h).items() if t == "Temperature" and v}
        lim = {k: temps.pop(n) for k, n in (("warn", "Warning Temperature"), ("crit", "Critical Temperature")) if n in temps}
        comp = temps.get("Composite Temperature") or temps.get("Temperature")
        if not comp:
            return
        self.storage_temps[h.Name] = round(comp, 1)
        hot = max(temps.values())
        if hot > comp + 0.5:
            self.storage_hot[h.Name] = round(hot, 1)
        else:
            self.storage_hot.pop(h.Name, None)
        if lim:
            self.storage_limits[h.Name] = lim

    def dump(self) -> list:
        """Every LHM sensor (incl. motherboard/Super I/O and memory) - for discovering what is readable."""
        if not self.ok:
            return []
        c = self.computer
        out = []

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
        # memory lists (perfmon/memdiag.py): what the RAM not in use by processes holds
        "standby_core": r"\Memory\Standby Cache Core Bytes",
        "standby_normal": r"\Memory\Standby Cache Normal Priority Bytes",
        "standby_reserve": r"\Memory\Standby Cache Reserve Bytes",
        "modified_bytes": r"\Memory\Modified Page List Bytes",
        "free_zero_bytes": r"\Memory\Free & Zero Page List Bytes",
        "pool_np_bytes": r"\Memory\Pool Nonpaged Bytes",
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
