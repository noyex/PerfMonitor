r"""Adrenalin global 3D settings of the dedicated AMD GPU (read-only: registry + one ADL query).

What Adrenalin's Graphics tab writes for the global profile into the GPU's display class key
HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-...}\000x (verified on RX 9070 XT + Adrenalin 26.10.2, values
matching the UI defaults and the existing Anti-Lag readout):
- KMD_DeLagEnabled (Anti-Lag), KMD_ChillEnabled, KMD_RadeonBoostEnabled, DrvFrameGenEnabled (AFMF),
  Gmx_RSREnabled (Radeon Super Resolution), KMD_FRTEnabled (FRTC) + Gmx_FRTCFrameRateTarget
- UMD\VSyncControl: Wait for Vertical Refresh, AMD's long-standing enum 0 always off, 1 off unless the application
  specifies (default, = VSyncControl_DEF), 2 on unless the application specifies, 3 always on
FreeSync: ADL2_Display_FreeSyncState_Get (current = ADL_FREESYNC_USECASE_* bit mask: 1 static, 2 video, 4 gaming;
min/max refresh in micro-Hz). Only the dGPU's FreeSync monitor answers: here 48-280 Hz = the MO27Q28GR.

Not readable without guessing, so left out: per-game profiles (Adrenalin keeps them in the binary
%LOCALAPPDATA%\AMD\CN\gmdb.blb), Radeon Image Sharpening (no registry value), Enhanced Sync (UMD\TurboSync has no
_DEF twin and its meaning could not be checked against the UI without toggling it). Per-game profiles override
everything here, so these are the global defaults, not necessarily what a game ran with."""
import ctypes

from . import winq
from .ocmon import _MALLOC, REG_CLASS, _AdapterInfo
from .winq import HKLM

VSYNC = {"0": "always_off", "1": "off_unless_app", "2": "on_unless_app", "3": "always_on"}


def _gpu_key(gpu_name=None):
    for sub in winq.reg_subkeys(HKLM, REG_CLASS):
        if not sub.isdigit():
            continue
        desc = winq.reg(HKLM, REG_CLASS + "\\" + sub, "DriverDesc")
        if desc and (desc == gpu_name if gpu_name else "(TM) Graphics" not in desc):
            return REG_CLASS + "\\" + sub, desc
    return None, None


def _flag(v):
    if v is None:
        return None
    try:
        return int(str(v).strip()) != 0
    except ValueError:
        return None


def registry(gpu_name=None):
    key, desc = _gpu_key(gpu_name)
    if not key:
        return {"error": "brak klucza sterownika GPU AMD"}
    v = winq.reg_values(HKLM, key) or {}
    umd = winq.reg_values(HKLM, key + r"\UMD") or {}
    if "RadeonSoftwareVersion" not in v:
        return {"error": f"{desc}: brak Adrenalin"}
    frtc = _flag(v.get("KMD_FRTEnabled"))
    vs = umd.get("VSyncControl")
    return {"gpu": desc, "anti_lag": _flag(v.get("KMD_DeLagEnabled")), "chill": _flag(v.get("KMD_ChillEnabled")),
            "boost": _flag(v.get("KMD_RadeonBoostEnabled")), "afmf": _flag(v.get("DrvFrameGenEnabled")),
            "rsr": _flag(v.get("Gmx_RSREnabled")), "frtc": frtc,
            "frtc_target": v.get("Gmx_FRTCFrameRateTarget") if frtc else None,
            "vsync": VSYNC.get(str(vs).strip()) if vs is not None else None}


def freesync():
    """[{range_hz, gaming, state}] of displays that answer ADL2_Display_FreeSyncState_Get; None = ADL unavailable."""
    try:
        dll = ctypes.WinDLL("atiadlxx.dll")
    except OSError:
        return None
    bufs = []

    def alloc(n):
        b = ctypes.create_string_buffer(n)
        bufs.append(b)
        return ctypes.addressof(b)

    malloc = _MALLOC(alloc)
    ctx = ctypes.c_void_p()
    if dll.ADL2_Main_Control_Create(malloc, 1, ctypes.byref(ctx)) != 0:
        return None
    try:
        n = ctypes.c_int()
        dll.ADL2_Adapter_NumberOfAdapters_Get(ctx, ctypes.byref(n))
        infos = (_AdapterInfo * max(1, n.value))()
        dll.ADL2_Adapter_AdapterInfo_Get(ctx, infos, ctypes.sizeof(infos))
        out, seen = [], set()
        for a in infos[:n.value]:
            for d in range(8):
                cur, dflt, mn, mx = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
                if dll.ADL2_Display_FreeSyncState_Get(ctx, a.iAdapterIndex, d, ctypes.byref(cur), ctypes.byref(dflt),
                                                      ctypes.byref(mn), ctypes.byref(mx)) != 0:
                    continue
                key = (a.iBusNumber, d)   # every logical adapter of one GPU answers for the same display
                if key in seen:
                    continue
                seen.add(key)
                out.append({"name": f"{round(mn.value / 1e6)}-{round(mx.value / 1e6)} Hz",
                            "gpu": a.strAdapterName.decode(errors="replace"), "display_index": d,
                            "range_hz": [round(mn.value / 1e6), round(mx.value / 1e6)], "state": cur.value,
                            "gaming": bool(cur.value & 4)})
        return out
    finally:
        dll.ADL2_Main_Control_Destroy(ctx)


def collect(gpu_name=None):
    out = registry(gpu_name)
    try:
        out["freesync"] = freesync()
    except Exception as e:
        out["freesync"] = None
        out["freesync_error"] = str(e)
    return out
