"""Network quality monitor: ping (gateway / ISP hop / internet / game server), DNS, Wi-Fi link, NIC errors.

Runs in its own thread, one row per second into net_samples, and opens/closes live incidents
(net_down, net_link_down, packet_loss, latency_spike, game_lag, wifi_weak). Each incident says where the
problem most likely is: "home" (Wi-Fi / cable / router / saturated link), "isp" (operator) or "internet".
"""
import ctypes
import ipaddress
import json
import logging
import socket
import statistics
import struct
import threading
import time
import uuid
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes

import psutil

log = logging.getLogger("perfmon.netmon")

iphlpapi = ctypes.WinDLL("iphlpapi")

IP_SUCCESS = 0
IP_TTL_EXPIRED_TRANSIT = 11013


class IP_OPTION_INFORMATION(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class ICMP_ECHO_REPLY(ctypes.Structure):
    _fields_ = [("Address", wintypes.ULONG), ("Status", wintypes.ULONG), ("RoundTripTime", wintypes.ULONG),
                ("DataSize", wintypes.USHORT), ("Reserved", wintypes.USHORT), ("Data", ctypes.c_void_p),
                ("Options", IP_OPTION_INFORMATION)]


iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
iphlpapi.IcmpSendEcho.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.WORD,
                                  ctypes.POINTER(IP_OPTION_INFORMATION), ctypes.c_void_p, wintypes.DWORD,
                                  wintypes.DWORD]
iphlpapi.IcmpSendEcho.restype = wintypes.DWORD
iphlpapi.GetBestRoute.argtypes = [wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD * 14)]
iphlpapi.GetBestRoute.restype = wintypes.DWORD

_PAYLOAD = b"PerfMonitor-netmon-probe-32bytes"
_local = threading.local()


def _ip_to_ulong(ip):
    return struct.unpack("<I", socket.inet_aton(ip))[0]


def _ulong_to_ip(v):
    return socket.inet_ntoa(struct.pack("<I", v))


def ping(ip, timeout_ms=1000, ttl=None):
    """One ICMP echo. Returns (rtt_ms or None, replying address or None, status)."""
    h = getattr(_local, "icmp", None)
    if h is None:
        h = _local.icmp = iphlpapi.IcmpCreateFile()
    buf = ctypes.create_string_buffer(ctypes.sizeof(ICMP_ECHO_REPLY) + len(_PAYLOAD) + 64)
    opts = None
    if ttl:
        opts = ctypes.pointer(IP_OPTION_INFORMATION(Ttl=ttl))
    t0 = time.perf_counter()
    n = iphlpapi.IcmpSendEcho(h, _ip_to_ulong(ip), _PAYLOAD, len(_PAYLOAD), opts, buf, len(buf), timeout_ms)
    dt = (time.perf_counter() - t0) * 1000
    if not n:
        return None, None, None
    rep = ICMP_ECHO_REPLY.from_buffer(buf)
    addr = _ulong_to_ip(rep.Address)
    if rep.Status == IP_SUCCESS:
        # perf_counter gives sub-ms resolution (RoundTripTime is whole ms, 0 on a fast LAN)
        return round(dt, 2), addr, rep.Status
    return None, addr, rep.Status


def best_route(dest="1.1.1.1"):
    """(gateway ip or None, interface index) of the route Windows uses for dest; (None, None) = no route."""
    row = (wintypes.DWORD * 14)()
    if iphlpapi.GetBestRoute(_ip_to_ulong(dest), 0, ctypes.byref(row)) != 0:
        return None, None
    nh = row[3]
    return (_ulong_to_ip(nh) if nh else None), row[4]


def local_ip_for(dest="1.1.1.1"):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((dest, 53))
            return s.getsockname()[0]
    except OSError:
        return None


def is_public(ip):
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


# ---------- Wi-Fi (native WLAN API, independent of the system language) ----------
class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", GUID), ("strInterfaceDescription", ctypes.c_wchar * 256), ("isState", ctypes.c_int)]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", wintypes.DWORD), ("dwIndex", wintypes.DWORD),
                ("InterfaceInfo", WLAN_INTERFACE_INFO * 1)]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", wintypes.ULONG), ("ucSSID", ctypes.c_ubyte * 32)]


class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("dot11Ssid", DOT11_SSID), ("dot11BssType", ctypes.c_int), ("dot11Bssid", ctypes.c_ubyte * 6),
                ("dot11PhyType", ctypes.c_int), ("uDot11PhyIndex", wintypes.ULONG),
                ("wlanSignalQuality", wintypes.ULONG), ("ulRxRate", wintypes.ULONG), ("ulTxRate", wintypes.ULONG)]


class WLAN_SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("bSecurityEnabled", wintypes.BOOL), ("bOneXEnabled", wintypes.BOOL),
                ("dot11AuthAlgorithm", ctypes.c_int), ("dot11CipherAlgorithm", ctypes.c_int)]


class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("isState", ctypes.c_int), ("wlanConnectionMode", ctypes.c_int),
                ("strProfileName", ctypes.c_wchar * 256), ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
                ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES)]


WLAN_CONNECTED = 1
OP_CURRENT_CONNECTION, OP_CHANNEL, OP_RSSI = 7, 8, 0x10000102
PHY = {4: "a", 5: "b", 6: "g", 7: "n", 8: "ac", 9: "ad", 10: "ax", 11: "be"}


class Wifi:
    def __init__(self):
        self.h = None
        try:
            self.api = ctypes.WinDLL("wlanapi")
            neg, h = wintypes.DWORD(), wintypes.HANDLE()
            if self.api.WlanOpenHandle(2, None, ctypes.byref(neg), ctypes.byref(h)) == 0:
                self.h = h
        except Exception as e:
            log.info("WLAN API unavailable: %s", e)

    def _query(self, guid, op):
        size, data = wintypes.DWORD(), ctypes.c_void_p()
        if self.api.WlanQueryInterface(self.h, ctypes.byref(guid), op, None, ctypes.byref(size),
                                       ctypes.byref(data), None) != 0:
            return None
        return data

    def read(self):
        """Connected Wi-Fi interface: {ssid, bssid, signal (%), rssi (dBm), channel, phy, rx/tx Mbps} or None."""
        if self.h is None:
            return None
        lst = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        if self.api.WlanEnumInterfaces(self.h, None, ctypes.byref(lst)) != 0:
            return None
        try:
            n = lst.contents.dwNumberOfItems
            infos = ctypes.cast(ctypes.byref(lst.contents.InterfaceInfo), ctypes.POINTER(WLAN_INTERFACE_INFO))
            for i in range(n):
                info = infos[i]
                if info.isState != WLAN_CONNECTED:
                    continue
                guid = info.InterfaceGuid
                p = self._query(guid, OP_CURRENT_CONNECTION)
                if not p:
                    continue
                try:
                    a = ctypes.cast(p, ctypes.POINTER(WLAN_CONNECTION_ATTRIBUTES)).contents.wlanAssociationAttributes
                    out = {"ssid": bytes(a.dot11Ssid.ucSSID[:a.dot11Ssid.uSSIDLength]).decode("utf-8", "replace"),
                           "bssid": ":".join(f"{b:02x}" for b in a.dot11Bssid),
                           "signal": int(a.wlanSignalQuality), "phy": PHY.get(a.dot11PhyType, str(a.dot11PhyType)),
                           "rx_mbps": a.ulRxRate // 1000, "tx_mbps": a.ulTxRate // 1000}
                finally:
                    self.api.WlanFreeMemory(p)
                for op, key, ctype in ((OP_CHANNEL, "channel", wintypes.DWORD), (OP_RSSI, "rssi", wintypes.LONG)):
                    q = self._query(guid, op)
                    if q:
                        out[key] = ctypes.cast(q, ctypes.POINTER(ctype)).contents.value
                        self.api.WlanFreeMemory(q)
                return out
        finally:
            self.api.WlanFreeMemory(lst)
        return None

    def close(self):
        if self.h is not None:
            self.api.WlanCloseHandle(self.h, None)
            self.h = None


# ---------- probes ----------
def dns_probe(timeout_s=4.0):
    """Resolve a unique name through the system resolver (bypasses the DNS cache).
    NXDOMAIN is the expected, healthy answer. Returns (ms, ok)."""
    name = f"pm-{uuid.uuid4().hex[:12]}.example.com."   # trailing dot: no DNS suffix devolution
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo(name, None, socket.AF_INET)
        ok = True
    except socket.gaierror as e:
        ok = e.errno in (11001, socket.EAI_NONAME)   # WSAHOST_NOT_FOUND = server answered
    except OSError:
        ok = False
    ms = (time.perf_counter() - t0) * 1000
    return round(ms, 1), ok and ms < timeout_s * 1000


def game_server_ip(pid):
    """Most common public remote address the game talks to (UDP preferred, web ports skipped).
    UDP games often use unconnected sockets, so this can return None."""
    try:
        conns = psutil.Process(pid).net_connections(kind="inet4")
    except Exception:
        return None
    udp, tcp = Counter(), Counter()
    for c in conns:
        if not c.raddr or not is_public(c.raddr.ip):
            continue
        if c.type == socket.SOCK_DGRAM:
            udp[c.raddr.ip] += 1
        elif c.raddr.port not in (80, 443):
            tcp[c.raddr.ip] += 1
    for cnt in (udp, tcp):
        if cnt:
            return cnt.most_common(1)[0][0]
    return None


def _median(xs):
    return statistics.median(xs) if xs else None


def _loss(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


INCIDENT_KINDS = ("net_down", "net_link_down", "packet_loss", "latency_spike", "game_lag", "wifi_weak")
WHERE_PL = {"home": "Wi-Fi / router / domowe łącze", "isp": "operator", "internet": "internet za operatorem",
            "game": "serwer gry"}


class NetMonitor:
    """write(row) gets one dict per second; open_incident(kind, severity, title, details, ts) -> id;
    close_incident(id, ts). All called from the monitor thread."""

    def __init__(self, cfg, write, open_incident, close_incident):
        self.timeout_ms = int(cfg.get("net_timeout_ms", 1000))
        self.inet_targets = list(cfg.get("net_targets") or ["1.1.1.1", "8.8.8.8"])
        self.game_servers = {k.lower(): v for k, v in (cfg.get("game_servers") or {}).items()}
        self.write, self.open_incident, self.close_incident = write, open_incident, close_incident
        self.pool = ThreadPoolExecutor(max_workers=len(self.inet_targets) + 4, thread_name_prefix="netprobe")
        self.wifi = Wifi()
        self.running = False
        self.thread = None
        self.game = None                 # (pid, exe, session) set by the collector
        self._game_ip = None
        self._game_probe = {"ip": None, "ok": 0, "tries": 0, "last_lookup": 0.0}
        self.gw = self.isp = None
        self.connected = False
        self.adapter = self.link_type = None
        self.link_speed = None
        self.wifi_info = None
        self.nic_prev = None
        self.nic_errs = None
        self.dns_future = None
        self.last = {"route": 0.0, "isp": 0.0, "link": 0.0, "dns": 0.0}
        self.state_extra = {}            # last extra written (only changes are stored)
        self.hist = {k: deque(maxlen=300) for k in ("gw", "isp", "inet", "game")}     # rtt (successful)
        self.loss = {k: deque(maxlen=30) for k in ("gw", "isp", "inet", "game")}      # loss fraction per second
        self.down_streak = self.up_streak = 0
        self.open = {}                   # kind -> incident id
        self.grace_until = 0.0

    # ---------- lifecycle ----------
    def start(self):
        self.running = True
        self.grace_until = time.time() + 20
        self.thread = threading.Thread(target=self._loop, name="netmon", daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=15)   # a tick with the network down can take several 1 s timeouts
        now = time.time()
        for iid in self.open.values():
            self.close_incident(iid, now)
        self.open.clear()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.wifi.close()

    def set_game(self, pid, exe, session):
        self.game = (pid, exe, session) if pid else None
        self._game_probe = {"ip": None, "ok": 0, "tries": 0, "last_lookup": 0.0}
        self.hist["game"].clear()
        self.loss["game"].clear()

    def _loop(self):
        nxt = last = time.time()
        while self.running:
            now = time.time()
            if now - last > 10:   # resumed from sleep / long stall: network is reconnecting, don't alarm
                self.grace_until = now + 20
                self.down_streak = 0
            last = now
            try:
                self.tick(now)
            except Exception:
                log.exception("netmon tick")
            nxt += 1.0
            delay = nxt - time.time()
            if delay < 0:   # ticks with timeouts run long: re-anchor instead of spinning to catch up
                nxt = time.time()
            time.sleep(max(0.0, delay))

    # ---------- slow refreshes ----------
    def _refresh_route(self, now):
        gw, ifidx = best_route(self.inet_targets[0])
        self.connected = ifidx is not None
        if gw != self.gw:
            log.info("gateway %s -> %s", self.gw, gw)
            self.isp = None
            self.last["isp"] = 0.0
        self.gw = gw
        try:
            self.link_type = socket.if_indextoname(ifidx).split("_")[0] if ifidx else None
        except OSError:
            self.link_type = None
        self.link_type = {"wireless": "wifi", "ethernet": "ethernet", "ppp": "ppp"}.get(self.link_type, self.link_type)

    def _refresh_isp_hop(self):
        """First public router after the gateway (TTL-limited ping towards the internet target)."""
        if not self.gw:
            return
        first = None
        for ttl in range(2, 6):
            _, addr, status = ping(self.inet_targets[0], self.timeout_ms, ttl=ttl)
            if status != IP_TTL_EXPIRED_TRANSIT or not addr:
                continue
            first = first or addr
            if is_public(addr) or ipaddress.ip_address(addr) in ipaddress.ip_network("100.64.0.0/10"):
                first = addr     # CGNAT (mobile/fiber operators) counts as the operator's network
                break
        if first != self.isp:
            log.info("isp hop %s -> %s", self.isp, first)
        self.isp = first

    def _refresh_link(self):
        ip = local_ip_for(self.inet_targets[0])
        self.adapter = None
        if ip:
            for name, addrs in psutil.net_if_addrs().items():
                if any(a.address == ip for a in addrs):
                    self.adapter = name
                    break
        st = psutil.net_if_stats().get(self.adapter) if self.adapter else None
        self.link_speed = st.speed if st else None
        self.wifi_info = self.wifi.read() if self.link_type == "wifi" else None
        self.nic_errs = None
        if self.adapter:
            c = psutil.net_io_counters(pernic=True).get(self.adapter)
            if c:
                cur = (c.errin, c.errout, c.dropin, c.dropout)
                if self.nic_prev and self.nic_prev[0] == self.adapter:
                    d = [a - b for a, b in zip(cur, self.nic_prev[1])]
                    if any(x > 0 for x in d):
                        self.nic_errs = dict(zip(("err_in", "err_out", "drop_in", "drop_out"), d))
                self.nic_prev = (self.adapter, cur)

    def _refresh_game_target(self, now):
        if not self.game:
            self._game_ip = None
            return
        pid, exe, _ = self.game
        gp = self._game_probe
        fixed = self.game_servers.get(exe.lower())
        if fixed:
            if gp["ip"] is None:
                try:
                    gp["ip"] = socket.gethostbyname(fixed)
                except OSError:
                    pass
        elif now - gp["last_lookup"] > 15:
            gp["last_lookup"] = now
            ip = game_server_ip(pid)
            if ip and ip != gp["ip"]:
                log.info("game server %s: %s", exe, ip)
                gp.update(ip=ip, ok=0, tries=0)
                self.hist["game"].clear()
                self.loss["game"].clear()
        # Many game servers drop ICMP: give up on an address that never answered 10 probes
        self._game_ip = gp["ip"] if gp["ip"] and (gp["ok"] or gp["tries"] < 10) else None

    # ---------- per-second ----------
    def tick(self, now):
        if now - self.last["route"] >= 5:
            self.last["route"] = now
            self._refresh_route(now)
            self._refresh_link()
        if self.gw and (self.isp is None and now - self.last["isp"] >= 60 or now - self.last["isp"] >= 600):
            self.last["isp"] = now
            self._refresh_isp_hop()
        self._refresh_game_target(now)

        jobs = {}
        if self.gw:
            jobs["gw"] = [self.pool.submit(ping, self.gw, self.timeout_ms)]
        if self.isp:
            jobs["isp"] = [self.pool.submit(ping, self.isp, self.timeout_ms)]
        jobs["inet"] = [self.pool.submit(ping, t, self.timeout_ms) for t in self.inet_targets]
        if self._game_ip:
            jobs["game"] = [self.pool.submit(ping, self._game_ip, self.timeout_ms)]
        if self.dns_future is None and now - self.last["dns"] >= 30:
            self.last["dns"] = now
            self.dns_future = self.pool.submit(dns_probe)

        res = {}
        for k, futs in jobs.items():
            rtts = []
            for f in futs:
                try:
                    rtts.append(f.result(timeout=self.timeout_ms / 1000 + 1)[0])
                except Exception:
                    rtts.append(None)
            ok = [r for r in rtts if r is not None]
            res[k] = (round(sum(ok) / len(ok), 2) if ok else None, 1 - len(ok) / len(rtts))
        dns = None
        if self.dns_future is not None and self.dns_future.done():
            try:
                dns = self.dns_future.result()
            except Exception:
                dns = (None, False)
            self.dns_future = None

        if "game" in res:
            gp = self._game_probe
            gp["tries"] += 1
            gp["ok"] += res["game"][0] is not None
            if not gp["ok"]:
                del res["game"]   # many servers drop ICMP: no answer yet is not packet loss
        for k in ("gw", "isp", "inet", "game"):
            if k in res:
                rtt, loss = res[k]
                self.loss[k].append(loss)
                if rtt is not None:
                    self.hist[k].append(rtt)

        extra = {"gateway": self.gw, "isp_hop": self.isp, "adapter": self.adapter, "game_ip": self._game_ip}
        if self.wifi_info:   # rx/tx rate and RSSI change every few seconds: not part of the change-only state
            extra.update({f"wifi_{k}": v for k, v in self.wifi_info.items() if k in ("ssid", "bssid", "channel", "phy")})
        else:
            extra["link_mbps"] = self.link_speed
        changed = {k: v for k, v in extra.items() if self.state_extra.get(k) != v}
        self.state_extra = extra
        if self.nic_errs:
            changed["nic_errors"] = self.nic_errs
            self.nic_errs = None
        if dns and not dns[1]:
            changed["dns_failed"] = True

        g = lambda k, i: res[k][i] if k in res else None
        row = dict(ts=now, session=self.game[2] if self.game else None,
                   gw_ms=g("gw", 0), gw_loss=g("gw", 1), isp_ms=g("isp", 0), isp_loss=g("isp", 1),
                   inet_ms=g("inet", 0), inet_loss=g("inet", 1), game_ms=g("game", 0), game_loss=g("game", 1),
                   dns_ms=dns[0] if dns and dns[1] else None,
                   link=(self.link_type or "other") if self.connected else None,
                   wifi_signal=self.wifi_info.get("signal") if self.wifi_info else None,
                   wifi_rssi=self.wifi_info.get("rssi") if self.wifi_info else None,
                   extra=json.dumps(changed, ensure_ascii=False) if changed else None)
        self.write(row)
        if now >= self.grace_until:
            self.detect(row)

    # ---------- incidents ----------
    def _ongoing(self, kind, active, severity, title, ts, details_fn):
        iid = self.open.get(kind)
        if active and iid is None and self.running:
            self.open[kind] = self.open_incident(kind, severity, title, details_fn(), ts)
        elif not active and iid is not None:
            self.close_incident(iid, ts)
            del self.open[kind]

    def _where_loss(self):
        gw, isp = _loss(list(self.loss["gw"])[-20:]), _loss(list(self.loss["isp"])[-20:])
        if gw >= 0.03:
            return "home"
        if isp >= 0.03:
            return "isp"
        return "internet"

    def _elevated(self, k, now_n=5, rel=2.0, abs_ms=40):
        """(is elevated, recent median, baseline median) for a target."""
        h = list(self.hist[k])
        if len(h) < 60:
            return False, None, None
        recent, base = _median(h[-now_n:]), _median(h[:-now_n])
        return recent > max(base * rel, base + abs_ms), recent, base

    def _where_latency(self):
        if self._elevated("gw", abs_ms=25, rel=3)[0]:
            return "home"
        if self.isp and self._elevated("isp", abs_ms=30)[0]:
            return "isp"
        return "internet"

    def _snapshot(self):
        d = {k: {"ms": _median(list(self.hist[k])[-5:]), "loss_pct": round(_loss(list(self.loss[k])[-20:]) * 100)}
             for k in ("gw", "isp", "inet", "game") if self.loss[k]}
        d.update(adapter=self.adapter, link=self.link_type, gateway=self.gw, isp_hop=self.isp)
        if self.wifi_info:
            d["wifi"] = self.wifi_info
        if self.game:
            d["game"] = self.game[1]
            d["game_ip"] = self._game_ip
        return d

    def detect(self, r):
        ts = r["ts"]
        link_down = r["link"] is None
        self._ongoing("net_link_down", link_down, "serious", "Brak połączenia z siecią (Wi-Fi / kabel rozłączony)",
                      ts, self._snapshot)
        if link_down:
            if self.open.get("wifi_weak"):
                self._ongoing("wifi_weak", False, "warning", "", ts, dict)
            return

        inet_lost = r["inet_loss"] == 1
        self.down_streak = self.down_streak + 1 if inet_lost else 0
        self.up_streak = 0 if inet_lost else self.up_streak + 1
        down = self.open.get("net_down") is not None
        if not down and self.down_streak >= 3:
            where = "home" if r["gw_loss"] == 1 else "isp" if r["isp_loss"] == 1 else "internet"
            self._ongoing("net_down", True, "serious", f"Brak internetu (problem: {WHERE_PL[where]})", ts,
                          lambda: {**self._snapshot(), "where": where})
        elif down and self.up_streak >= 2:
            self._ongoing("net_down", False, "serious", "", ts, dict)
        if self.open.get("net_down"):
            return

        loss = _loss(list(self.loss["inet"])[-20:])
        lossy = self.open.get("packet_loss") is not None
        if not lossy and loss >= 0.05 and len(self.loss["inet"]) >= 20:
            where = self._where_loss()
            self._ongoing("packet_loss", True, "warning",
                          f"Utrata pakietów {round(loss * 100)}% (problem: {WHERE_PL[where]})", ts,
                          lambda: {**self._snapshot(), "where": where})
        elif lossy and loss < 0.02:
            self._ongoing("packet_loss", False, "warning", "", ts, dict)

        hi, recent, base = self._elevated("inet")
        spiking = self.open.get("latency_spike") is not None
        if not spiking and hi:
            where = self._where_latency()
            self._ongoing("latency_spike", True, "warning",
                          f"Skok pingu {round(base)} → {round(recent)} ms (problem: {WHERE_PL[where]})", ts,
                          lambda: {**self._snapshot(), "where": where, "baseline_ms": round(base, 1)})
        elif spiking and recent is not None and recent < base + 20:
            self._ongoing("latency_spike", False, "warning", "", ts, dict)

        if self.open.get("game_lag") and not (self.game and self._game_ip):
            self._ongoing("game_lag", False, "warning", "", ts, dict)
        elif self.game and self._game_ip and self.loss["game"]:
            ghi, grecent, gbase = self._elevated("game")
            gloss = _loss(list(self.loss["game"])[-20:])
            bad = ghi or (gloss >= 0.05 and len(self.loss["game"]) >= 20)
            if not self.open.get("game_lag") and bad:
                where = "game" if not self._elevated("inet")[0] and loss < 0.02 else self._where_latency()
                self._ongoing("game_lag", True, "warning",
                              f"Lag do serwera gry {self.game[1]} (problem: {WHERE_PL[where]})", ts,
                              lambda: {**self._snapshot(), "where": where,
                                       "baseline_ms": round(gbase, 1) if gbase else None})
            elif self.open.get("game_lag") and not ghi and gloss < 0.02:
                self._ongoing("game_lag", False, "warning", "", ts, dict)

        sig = r["wifi_signal"]
        weak = self.open.get("wifi_weak") is not None
        if weak and (sig is None or sig >= 50):
            self._ongoing("wifi_weak", False, "warning", "", ts, dict)
        elif not weak and sig is not None and sig < 40:
            self._ongoing("wifi_weak", True, "warning", f"Słaby sygnał Wi-Fi ({sig}%)", ts, self._snapshot)
