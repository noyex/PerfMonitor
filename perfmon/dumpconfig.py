r"""Crash-dump configuration for the config snapshot (sysinfo calls read()): can Windows write a dump at a blue screen?

HKLM\SYSTEM\CurrentControlSet\Control\CrashControl  CrashDumpEnabled (0 none, 1 complete, 2 kernel, 3 small, 7 automatic),
MinidumpDir, DumpFile, AutoReboot, Overwrite, DedicatedDumpFile. The dump is written to the pagefile on the boot volume
first (unless a dedicated dump file is set) and copied out at the next boot, so the pagefile there must exist and be
large enough: ~2 MB for a small dump, about a third of RAM for a kernel dump (system-managed size for "automatic").
Values stay stable between snapshots on purpose (no sizes, no free space): a change shows up as a config_change
incident."""
import os
import winreg

import psutil

CC_KEY = r"SYSTEM\CurrentControlSet\Control\CrashControl"
MODES = {0: "none", 1: "complete", 2: "kernel", 3: "small", 7: "automatic"}
SMALL_MIN_MB = 2


def _values(key):
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            i = 0
            while True:
                try:
                    n, v, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                out[n] = v
                i += 1
    except OSError:
        pass
    return out


def check(cc, boot_pf_mb, boot_pf_managed, ram_mb):
    """Problems (Polish) that keep a dump from being written; empty list when the configuration is fine."""
    mode = MODES.get(cc.get("CrashDumpEnabled"), None)
    p = []
    if mode == "none":
        p.append("zapis zrzutów wyłączony (CrashDumpEnabled=0)")
        return p
    if not cc.get("DedicatedDumpFile"):
        if boot_pf_mb is None:
            p.append("brak pliku wymiany na dysku systemowym - Windows nie ma gdzie zapisać zrzutu")
        elif mode == "small" and boot_pf_mb < SMALL_MIN_MB:
            p.append("plik wymiany na dysku systemowym za mały na minizrzut")
        elif mode in ("kernel", "complete") and not boot_pf_managed and boot_pf_mb < (ram_mb if mode == "complete" else ram_mb / 3):
            p.append(f"plik wymiany na dysku systemowym może być za mały na zrzut typu {mode}")
        elif mode == "automatic" and not boot_pf_managed:
            p.append("zrzut automatyczny wymaga pliku wymiany zarządzanego przez system")
    if not cc.get("Overwrite", 1) and mode in ("kernel", "complete", "automatic"):
        p.append("Overwrite=0: istniejący MEMORY.DMP blokuje zapis nowego")
    return p


def read():
    """Crash-dump settings + problems; the advisor uses it to explain an empty Minidump folder."""
    cc = _values(CC_KEY)
    sysdrive = os.environ.get("SystemDrive", "C:").upper()
    from . import memdiag   # memdiag imports analyze: keep sysinfo's import light
    pf = memdiag.pagefile_status()
    boot = next((f for f in pf["files"] if f["path"].upper().startswith(sysdrive)), None)
    ram_mb = psutil.virtual_memory().total / 2**20
    mode = MODES.get(cc.get("CrashDumpEnabled"), cc.get("CrashDumpEnabled"))
    # pagefile size of a system-managed file can only be checked against its maximum, which follows RAM and disk size
    boot_mb = boot["max_gb"] * 1024 if boot and boot["system_managed"] else boot["size_gb"] * 1024 if boot else None
    return {"mode": mode, "minidump_dir": cc.get("MinidumpDir"), "dump_file": cc.get("DumpFile"),
            "auto_reboot": cc.get("AutoReboot"), "overwrite": cc.get("Overwrite"),
            "dedicated_dump_file": cc.get("DedicatedDumpFile"),
            "boot_pagefile": bool(boot), "boot_pagefile_system_managed": bool(boot and boot["system_managed"]),
            "problems": check(cc, boot_mb, bool(boot and boot["system_managed"]), ram_mb)}
