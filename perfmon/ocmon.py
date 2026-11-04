"""UV/OC helpers for the collector: AMD GPU tuning readout (ADL Overdrive8 + Adrenalin registry presets),
WHEA-Logger parsing (APIC ID -> core, MCA bank -> unit) and stress-test detection.

Nothing here ever changes a hardware setting: it only reads."""
import ctypes
import logging
import re
import winreg

log = logging.getLogger("perfmon.ocmon")

# ---------------------------------------------------------------------------
# GPU: ADL Overdrive8 (atiadlxx.dll)
# ---------------------------------------------------------------------------

# ADLOD8SettingId indices that are known to mean the same thing on RDNA4. Everything else stays in "raw":
# on RDNA4 the voltage offset index is not documented, so any raw change is reported as a settings change.
OD8_NAMES = {
    0: "gfxclk_fmax", 1: "gfxclk_fmin", 8: "vram_mhz", 9: "power_limit_pct", 14: "vram_fast_timing",
    15: "zero_rpm", 16: "auto_uv", 17: "auto_oc_gpu", 18: "auto_oc_vram",
}
FAN_CURVE = range(19, 29)   # 5 x (temperature, speed %)
REG_CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


class _AdapterInfo(ctypes.Structure):
    _fields_ = [("iSize", ctypes.c_int), ("iAdapterIndex", ctypes.c_int), ("strUDID", ctypes.c_char * 256),
                ("iBusNumber", ctypes.c_int), ("iDeviceNumber", ctypes.c_int), ("iFunctionNumber", ctypes.c_int),
                ("iVendorID", ctypes.c_int), ("strAdapterName", ctypes.c_char * 256),
                ("strDisplayName", ctypes.c_char * 256), ("iPresent", ctypes.c_int), ("iExist", ctypes.c_int),
                ("strDriverPath", ctypes.c_char * 256), ("strDriverPathExt", ctypes.c_char * 256),
                ("strPNPString", ctypes.c_char * 256), ("iOSDisplayIndex", ctypes.c_int)]


class _OD8Current(ctypes.Structure):
    _fields_ = [("count", ctypes.c_int), ("table", ctypes.c_int * 128)]


_MALLOC = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int)


class GpuOverdrive:
    """Current Adrenalin tuning of the dedicated AMD GPU. read() returns None when unavailable."""

    def __init__(self, gpu_name=None):
        self.ok = False
        self.index = None
        self._bufs = []
        self._malloc = _MALLOC(self._alloc)   # keep a reference: ADL calls it later
        try:
            self.dll = ctypes.WinDLL("atiadlxx.dll")
            self.ctx = ctypes.c_void_p()
            if self.dll.ADL2_Main_Control_Create(self._malloc, 1, ctypes.byref(self.ctx)) != 0:
                return
            n = ctypes.c_int()
            self.dll.ADL2_Adapter_NumberOfAdapters_Get(self.ctx, ctypes.byref(n))
            infos = (_AdapterInfo * max(1, n.value))()
            self.dll.ADL2_Adapter_AdapterInfo_Get(self.ctx, infos, ctypes.sizeof(infos))
            for a in infos:
                name = a.strAdapterName.decode(errors="replace")
                if gpu_name and name != gpu_name:
                    continue
                if self._current(a.iAdapterIndex) is not None:
                    self.index, self.name = a.iAdapterIndex, name
                    break
            self.ok = self.index is not None
            log.info("ADL Overdrive8: %s", f"adapter {self.index} {self.name}" if self.ok else "niedostępne")
        except Exception as e:
            log.info("ADL niedostępne: %s", e)

    def _alloc(self, n):
        b = ctypes.create_string_buffer(n)
        self._bufs.append(b)
        return ctypes.addressof(b)

    def _current(self, idx):
        s = _OD8Current()
        s.count = 128
        if self.dll.ADL2_Overdrive8_Current_Setting_Get(self.ctx, idx, ctypes.byref(s)) != 0:
            return None
        return list(s.table[:max(0, min(128, s.count))])

    def read(self):
        out = {}
        if self.ok:
            try:
                raw = self._current(self.index)
                if raw:
                    named = {k: raw[i] for i, k in OD8_NAMES.items() if i < len(raw)}
                    named["fan_curve"] = [raw[i] for i in FAN_CURVE if i < len(raw)]
                    out.update(named=named, raw=raw)
            except Exception as e:
                log.debug("ADL read: %s", e)
        presets = registry_presets()
        if presets:
            out["presets"] = presets
        return out or None


def registry_presets():
    """Adrenalin auto-tuning presets (PP_Auto*) of the dedicated AMD GPU from the display class key."""
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, REG_CLASS) as cls:
            for i in range(32):
                try:
                    sub = winreg.EnumKey(cls, i)
                except OSError:
                    break
                if not sub.isdigit():
                    continue
                try:
                    with winreg.OpenKey(cls, sub) as k:
                        desc = winreg.QueryValueEx(k, "DriverDesc")[0]
                        vals = {}
                        j = 0
                        while True:
                            try:
                                n, v, _ = winreg.EnumValue(k, j)
                            except OSError:
                                break
                            j += 1
                            if n.startswith("PP_Auto") and isinstance(v, int):
                                vals[n[3:]] = v
                        if vals:
                            out[desc] = vals
                except OSError:
                    continue
    except OSError:
        pass
    return out


def describe_change(old, new):
    """Human-readable list of differences between two GpuOverdrive.read() results."""
    old, new = old or {}, new or {}
    diffs = []
    on, nn = old.get("named") or {}, new.get("named") or {}
    for k in sorted(set(on) | set(nn)):
        if on.get(k) != nn.get(k):
            diffs.append(f"{k}: {on.get(k)} -> {nn.get(k)}")
    known = set(OD8_NAMES) | set(FAN_CURVE)
    oraw, nraw = old.get("raw") or [], new.get("raw") or []
    for i in range(max(len(oraw), len(nraw))):
        a = oraw[i] if i < len(oraw) else None
        b = nraw[i] if i < len(nraw) else None
        if i not in known and a != b:
            diffs.append(f"od8[{i}]: {a} -> {b}")
    op, np_ = old.get("presets") or {}, new.get("presets") or {}
    for gpu in sorted(set(op) | set(np_)):
        a, b = op.get(gpu) or {}, np_.get(gpu) or {}
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                diffs.append(f"{k}: {a.get(k)} -> {b.get(k)}")
    return diffs


# ---------------------------------------------------------------------------
# WHEA-Logger
# ---------------------------------------------------------------------------

_KV = re.compile(r"(\w+)=([^;\]]*)")
_APIC_TEXT = re.compile(r"APIC\D{0,20}(\d+)", re.I)
_BANK_TEXT = re.compile(r"(?:MCA ?Bank|Bank)\D{0,10}(\d+)", re.I)


def _int(v):
    try:
        return int(str(v).strip(), 0)
    except (TypeError, ValueError):
        return None


def mca_unit(bank):
    """Zen 4 MCA bank -> which part of the CPU reported the error (approximate, from AMD PPR bank layout)."""
    if bank is None:
        return None
    if bank <= 6:   # LS, IF, L2, DE, -, EX, FP: per-core banks
        return "core"
    if bank <= 14:  # L3 slices of the CCX
        return "l3"
    return "soc"    # UMC (memory controller), Infinity Fabric / PIE, IO die: EXPO / VSoC / FCLK, not Curve Optimizer


def parse_whea(message, apic_per_core=2):
    """Fields of a WHEA-Logger 18/19 event: apic, core, bank, unit, error_type, source."""
    m = message or ""
    kv = {k.lower(): v.strip() for k, v in _KV.findall(m[m.rfind("["):] if "[" in m else "")}
    apic = _int(kv.get("apicid"))
    if apic is None:
        t = _APIC_TEXT.search(m)
        apic = int(t.group(1)) if t else None
    bank = _int(kv.get("mcabank"))
    if bank is None:
        t = _BANK_TEXT.search(m)
        bank = int(t.group(1)) if t else None
    out = {"apic": apic, "core": apic // apic_per_core if apic is not None and apic_per_core else None,
           "bank": bank, "unit": mca_unit(bank)}
    for k in ("errortype", "errorsource", "transactiontype", "memhierarchylvl"):
        if kv.get(k):
            out[k] = kv[k]
    return out


# ---------------------------------------------------------------------------
# stress tests
# ---------------------------------------------------------------------------

def stress_kind(name, path, cfg):
    """(display name, uses_3d) when the process is a known stress test / benchmark, else None."""
    pl = (path or "").lower()
    for marker, label in cfg.get("stress_path_markers", {}).items():
        if marker in pl and (name or "").lower().endswith(".exe"):
            return label, False
    s = cfg.get("stress_exes", {}).get((name or "").lower())
    if s:
        return s.get("name") or name, bool(s.get("3d"))
    return None
