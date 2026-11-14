"""Read-only WMI (COM, in-process) and registry helpers for the config snapshot.

WMI through SWbemLocator costs ~1 s and a few MB in this process, where the old PowerShell snapshot started a
~220 MB powershell.exe for 4-7 s. Every call is a query: nothing here writes."""
import threading
import winreg

_local = threading.local()
HKLM, HKCU = winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER


def _locator():
    if getattr(_local, "loc", None) is None:
        import pythoncom
        import win32com.client
        pythoncom.CoInitialize()   # per thread; snapshots run on worker threads
        _local.loc = win32com.client.Dispatch("WbemScripting.SWbemLocator")
        _local.svc = {}
    return _local.loc


def wmi(sql, ns=r"root\cimv2"):
    """[{property: value}] for a WQL query. Raises on a missing namespace / access denied."""
    loc = _locator()
    svc = _local.svc.get(ns)
    if svc is None:
        svc = _local.svc[ns] = loc.ConnectServer(".", ns)
    return [{p.Name: p.Value for p in o.Properties_} for o in svc.ExecQuery(sql)]


def wmi_safe(sql, ns=r"root\cimv2"):
    try:
        return wmi(sql, ns)
    except Exception:
        return []


def reg(root, path, name, default=None):
    try:
        with winreg.OpenKey(root, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return default


def reg_values(root, path):
    """{name: value} of a key; None when the key is missing or unreadable."""
    try:
        with winreg.OpenKey(root, path) as k:
            out, i = {}, 0
            while True:
                try:
                    n, v, _ = winreg.EnumValue(k, i)
                except OSError:
                    return out
                out[n] = v
                i += 1
    except OSError:
        return None


def reg_subkeys(root, path):
    try:
        with winreg.OpenKey(root, path) as k:
            out, i = [], 0
            while True:
                try:
                    out.append(winreg.EnumKey(k, i))
                except OSError:
                    return out
                i += 1
    except OSError:
        return []


def wmi_date(s):
    """'20261016000000.000000-000' -> '2026-10-16'."""
    s = str(s or "")
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else None


def us_date(s):
    """'10/14/2026' (QFE InstalledOn, Class key DriverDate '10-28-2026') -> '2026-10-14'; else unchanged."""
    import re
    m = re.fullmatch(r"\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*", str(s or ""))
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else (s or None)
