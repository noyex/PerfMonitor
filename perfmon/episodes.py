"""Repeating incidents of one kind are one episode: start, end, how many times it re-fired, worst values.

Ongoing conditions flap around their threshold (commit at 91-93% of the limit, DwmFlush timing out every ~20 s), so
the old one-incident-per-activation rule wrote 26 commit_full rows for one low-memory episode and 757 dwm_stall rows
for 16 episodes. The collector keeps an incident open until the condition has stayed off for GAP_S (Tracker); the
analysis groups the rows written before that the same way (group), without rewriting history.

dwm_stall counts only while someone uses the PC (or a game runs): on this PC 722 of 757 stalls (95%) happened after
more than a minute without input, in 7 episodes lasting 12-121 min with the user away and the shell responsive
(ui_ms < 1 s), i.e. a display that went to sleep, not a frozen screen anybody saw."""
import bisect

GAP_S = {   # kind -> seconds the condition must stay off before the episode ends
    "dwm_stall": 30, "ui_freeze": 30, "app_not_responding": 30, "commit_full": 120, "vram_full": 60,
    "clock_stretching": 60, "gpu_clock_drop": 60, "vsoc_high": 60,
}
HOT_GAP_S = 60   # hot_<sensor> kinds
PRESENT_S = 60   # dwm_stall: no keyboard/mouse input this long = nobody is looking at the screen
# kind -> (worst-value name, value from a samples row); the incident details keep their own snapshot at the start
_METRIC = {
    "dwm_stall": ("dwm_ms", lambda r: r.get("dwm_ms")),
    "ui_freeze": ("ui_ms", lambda r: r.get("ui_ms")),
    "commit_full": ("commit_pct", lambda r: 100 * r["commit_gb"] / r["commit_limit_gb"]
                    if r.get("commit_gb") and r.get("commit_limit_gb") else None),
    "vram_full": ("vram_mb", lambda r: r.get("gpu_vram_mb")),
}
_DETAIL_WORST = {"dwm_stall": "dwm_ms", "ui_freeze": "ui_ms", "commit_full": "commit_pct", "vram_full": "vram_mb"}
_SEV = {"info": 0, "warning": 1, "serious": 2, "critical": 3}


def gap(kind):
    if kind in GAP_S:
        return GAP_S[kind]
    return HOT_GAP_S if kind.startswith("hot_") else None


def _metric(kind):
    if kind in _METRIC:
        return _METRIC[kind]
    if kind.startswith("hot_"):
        col = kind[4:]
        return "value", lambda r: r.get(col)
    return None


def dwm_visible(user_idle_s, game_session=None):
    """A DWM stall is user-visible while there is input or a game runs (gamepad input is not seen by GetLastInputInfo)."""
    return bool(game_session) or user_idle_s is None or user_idle_s < PRESENT_S


class Tracker:
    """Collector side: per open incident kind, whether the condition is on, when it was last on, count, worst value."""

    def __init__(self):
        self.s = {}

    def start(self, kind, row):
        self.s[kind] = {"last": row["ts"], "off_since": None, "count": 1, "worst": None}
        self._worst(kind, row)

    def hit(self, kind, row):
        """Condition on while the incident is open; a return after an off period is one more occurrence (True)."""
        st = self.s.get(kind)
        if st is None:
            self.start(kind, row)
            return False
        back = st["off_since"] is not None
        if back:
            st["count"] += 1
            st["off_since"] = None
        st["last"] = row["ts"]
        self._worst(kind, row)
        return back

    def just_off(self, kind, ts):
        """True on the first off second of an episode (after off() recorded it)."""
        st = self.s.get(kind)
        return bool(st and st["off_since"] == ts)

    def off(self, kind, ts):
        """Condition off: True when the episode is over (off for its gap; kinds without a gap end at once)."""
        st = self.s.get(kind)
        if st is None:
            return True
        if st["off_since"] is None:
            st["off_since"] = ts
        return ts - st["last"] >= (gap(kind) or 0)

    def close(self, kind, now=None):
        """(end ts, details patch) and forget the kind. End = first off second, as before coalescing."""
        st = self.s.pop(kind, None)
        if st is None:
            return now, {}
        end = st["off_since"] or now or st["last"]
        patch = {"count": st["count"]}
        if st["worst"] is not None:
            patch["worst"] = st["worst"]
        return end, patch

    def _worst(self, kind, row):
        m = _metric(kind)
        if not m:
            return
        try:
            v = m[1](row)
        except (TypeError, ZeroDivisionError):
            v = None
        if v is None:
            return
        st = self.s[kind]
        if st["worst"] is None or v > st["worst"][m[0]]:
            st["worst"] = {m[0]: round(v, 1)}


def _game_spans(con):
    return [(a, b if b is not None else 4e9) for a, b in con.execute(
        "SELECT start_ts, end_ts FROM sessions WHERE COALESCE(kind, 'game')='game' ORDER BY start_ts")]


def _in_spans(spans, starts, ts):
    i = bisect.bisect_right(starts, ts) - 1
    return i >= 0 and spans[i][0] <= ts <= spans[i][1]


def _hidden(r, spans, starts):
    """dwm_stall rows stored before the visibility rule existed that the rule would not have written."""
    if r["kind"] != "dwm_stall":
        return False
    d = r.get("details") if isinstance(r.get("details"), dict) else {}
    if d.get("count"):   # written by the new collector: already judged
        return False
    return not dwm_visible(d.get("user_idle_s"), _in_spans(spans, starts, r["ts"]))


def group(rows, con=None):
    """Analysis side: rows (dicts sorted by ts with parsed details) -> one row per episode, adding `count` (rows merged
    or the collector's count) and `worst` ({name: value}). Kinds without a gap pass through with count 1."""
    spans = _game_spans(con) if con is not None and any(r["kind"] == "dwm_stall" for r in rows) else []
    starts = [s[0] for s in spans]
    out, open_ = [], {}   # kind -> index in out of the episode that can still grow
    for r in rows:
        if _hidden(r, spans, starts):
            continue
        d = r.get("details") if isinstance(r.get("details"), dict) else {}
        n = d.get("count") or 1
        g = gap(r["kind"])
        prev = out[open_[r["kind"]]] if g is not None and r["kind"] in open_ else None
        if prev is not None and r["ts"] - (prev["end_ts"] or prev["ts"]) <= g:
            prev["count"] += n
            if prev["end_ts"] is not None:
                prev["end_ts"] = max(prev["end_ts"], r["end_ts"]) if r["end_ts"] is not None else None
            if _SEV.get(r["severity"], 0) > _SEV.get(prev["severity"], 0):
                prev["severity"] = r["severity"]
            _merge_worst(prev, r["kind"], d)
            continue
        x = dict(r, count=n)
        x["worst"] = None
        _merge_worst(x, r["kind"], d)
        open_[r["kind"]] = len(out)
        out.append(x)
    return out


def _merge_worst(x, kind, d):
    w = d.get("worst") if isinstance(d.get("worst"), dict) else None
    if w is None:
        k = _DETAIL_WORST.get(kind) or ("value" if kind.startswith("hot_") else None)
        v = d.get(k) if k else None
        w = {k: v} if isinstance(v, (int, float)) else None
    if not w:
        return
    cur = x["worst"] or {}
    x["worst"] = {k: max(v, cur.get(k, v)) for k, v in w.items() if isinstance(v, (int, float))} or x["worst"]
