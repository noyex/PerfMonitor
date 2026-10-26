"""Reading the Windows event log (System + Application) via EvtQuery."""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import win32evtlog

log = logging.getLogger("perfmon.events")
NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

# Informational events we also want (level 4)
INFO_KEEP = {
    ("System", 1074),   # who initiated shutdown/restart
    ("System", 6005), ("System", 6006), ("System", 6008),  # event log start/stop, unexpected shutdown
    ("System", 12), ("System", 13),  # Kernel-General: system start/stop
    ("System", 42), ("System", 107), ("System", 109),  # Kernel-Power: sleep / resume / shutdown
    ("System", 19),     # update installed
    ("System", 7045),   # new service installed
    ("Application", 10001),  # Winsrv: application blocked shutdown
    ("Application", 1001),   # WER: error report (crash details)
}
NOISE = {
    ("Microsoft-Windows-DistributedCOM", 10016),
    ("BTHUSB", 18),
    ("Microsoft-Windows-Security-SPP", 16384),
    ("Microsoft-Windows-Security-SPP", 16394),
}


def _ts(s):
    s = s.rstrip("Z")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).timestamp()


class EventReader:
    def __init__(self):
        self._meta = {}

    def _message(self, provider, h):
        try:
            if provider not in self._meta:
                try:
                    self._meta[provider] = win32evtlog.EvtOpenPublisherMetadata(provider)
                except Exception:
                    self._meta[provider] = None
            m = self._meta[provider]
            if m is None:
                return None
            return win32evtlog.EvtFormatMessage(m, h, win32evtlog.EvtFormatMessageEvent)
        except Exception:
            return None

    def read_since(self, logname: str, since_ts: float, max_events: int = 2000):
        iso = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        xpath = f"*[System[TimeCreated[@SystemTime>'{iso}']]]"
        out = []
        try:
            q = win32evtlog.EvtQuery(logname, win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryForwardDirection, xpath)
        except Exception as e:
            log.warning("EvtQuery %s: %s", logname, e)
            return out
        while len(out) < max_events:
            try:
                hs = win32evtlog.EvtNext(q, 100)
            except Exception:
                break
            if not hs:
                break
            for h in hs:
                try:
                    x = ET.fromstring(win32evtlog.EvtRender(h, win32evtlog.EvtRenderEventXml))
                    sysn = x.find(NS + "System")
                    provider = sysn.find(NS + "Provider").get("Name")
                    eid = int(sysn.find(NS + "EventID").text)
                    level = int(sysn.find(NS + "Level").text or 4)
                    rec = int(sysn.find(NS + "EventRecordID").text)
                    ts = _ts(sysn.find(NS + "TimeCreated").get("SystemTime"))
                    if (provider, eid) in NOISE:
                        continue
                    keep = level in (1, 2, 3) or (logname, eid) in INFO_KEEP or provider.startswith("Microsoft-Windows-WHEA")
                    if not keep:
                        continue
                    msg = self._message(provider, h) or ""
                    named = [f"{d.get('Name')}={d.text}" for d in x.iter(NS + "Data") if d.get("Name") and d.text]
                    if named:  # structured fields (e.g. Kernel-Power 41 BugcheckCode) are not in the text
                        msg = (msg + " [" + "; ".join(named) + "]").strip()
                    elif not msg:
                        msg = " | ".join(d.text for d in x.iter(NS + "Data") if d.text)
                    out.append(dict(ts=ts, log=logname, provider=provider, event_id=eid, level=level,
                                    record_id=rec, message=(msg or "").strip()[:4000]))
                except Exception as e:
                    log.debug("event parse: %s", e)
        return out


_APPNAME = re.compile(r"\bAppName=([^;\]]+)")
_EXE = re.compile(r"([^\s:,\"'\[\]]+\.exe)\b", re.I)


def exe_from_message(m: str) -> str:
    """Faulting/hung exe from a WER 1000/1002 message, independent of the system language
    ("Faulting application name: x.exe, ..." / "Nazwa aplikacji powodującej błąd: x.exe, ...")."""
    s = _APPNAME.search(m or "")
    if s and s.group(1).strip():
        return s.group(1).strip()
    s = _EXE.search(m or "")
    return s.group(1) if s else "?"


def classify(ev: dict):
    """Returns (kind, severity, title) if the event is an incident, otherwise None."""
    p, i, m = ev["provider"], ev["event_id"], ev["message"] or ""
    if p == "Microsoft-Windows-Kernel-Power" and i == 41:
        return "unclean_shutdown", "critical", "Nieoczekiwane wyłączenie / twardy reset (Kernel-Power 41)"
    if i == 6008 and p == "EventLog":
        return "unclean_shutdown", "critical", "Poprzednie zamknięcie systemu było nieoczekiwane (6008)"
    if p.startswith("Microsoft-Windows-WHEA"):
        return "whea", "critical", f"Błąd sprzętowy WHEA ({i})"
    if i == 1001 and p in ("Microsoft-Windows-WER-SystemErrorReporting", "BugCheck"):
        return "bsod", "critical", "Blue screen (BugCheck)"
    if (p == "Display" and i == 4101) or p.lower() in ("amdkmdag", "amdkmdap", "nvlddmkm"):
        return "gpu_driver", "serious", f"Problem sterownika GPU: {p} {i}"
    if i == 1000 and p == "Application Error":
        return "app_crash", "serious", f"Crash aplikacji: {exe_from_message(m)}"
    if i == 1002 and p == "Application Hang":
        return "app_hang", "serious", f"Aplikacja przestała odpowiadać: {exe_from_message(m)}"
    if p == "Microsoft-Windows-Kernel-Processor-Power" and i == 37:
        return "throttle", "warning", "Firmware ograniczył taktowanie CPU (throttling)"
    if p in ("disk", "Ntfs", "stornvme", "storahci", "volmgr", "Microsoft-Windows-Ntfs") and ev["level"] <= 3:
        return "disk", "serious", f"Błąd/ostrzeżenie dysku: {p} {i}"
    if p == "Microsoft-Windows-Resource-Exhaustion-Detector":
        return "low_memory", "serious", "Windows: kończy się pamięć wirtualna"
    if (p == "Microsoft-Windows-Kernel-PnP" and i == 219) or (p == "Microsoft-Windows-USB-USBHUB3" and ev["level"] <= 2):
        return "device", "warning", f"Problem urządzenia: {p} {i}"
    return None
