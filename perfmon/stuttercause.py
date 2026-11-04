"""Stutter causes judged against normal gameplay: a condition is a cause only when it shows up around stutters more
often than in ordinary seconds. A process that runs all the time or a counter that is always high explains nothing."""
from collections import Counter

MIN_STUTTERS = 3
MIN_SHARE_PCT = 10     # of stutters, smaller causes are noise
MIN_LIFT = 1.5         # stutter share / baseline share
AMBIENT_PCT = 50       # present in this share of normal seconds = background condition, not a stutter trigger
BASELINE_MAX = 400
QUIET_S = 3            # baseline seconds stay this far from any stutter


def baseline_ts(gameplay_ts, stutter_ts):
    """Evenly spread gameplay seconds away from stutters; all of them when stutters are everywhere."""
    near = {int(t) + d for t in stutter_ts for d in range(-QUIET_S, QUIET_S + 1)}
    quiet = [t for t in gameplay_ts if int(t) not in near]
    base = quiet if len(quiet) >= 30 else list(gameplay_ts)
    return base[::max(1, len(base) // BASELINE_MAX)]


def rank(stutter_tags, base_tags):
    """[{tag, n, stutter_pct, base_pct, lift, verdict: confirmed|ambient|noise}], strongest first.
    Both arguments: one list of tags per stutter / per baseline second."""
    ns, nb = len(stutter_tags), len(base_tags)
    if not ns or not nb:
        return []
    hits = Counter(t for tags in stutter_tags for t in set(tags))
    base = Counter(t for tags in base_tags for t in set(tags))
    out = []
    for tag, n in hits.items():
        sp, bp = 100 * n / ns, 100 * base[tag] / nb
        lift = sp / bp if bp else None
        if n >= MIN_STUTTERS and sp >= MIN_SHARE_PCT and (lift is None or lift >= MIN_LIFT):
            verdict = "confirmed"
        else:
            verdict = "ambient" if bp >= AMBIENT_PCT else "noise"
        out.append({"tag": tag, "n": n, "stutter_pct": round(sp, 1), "base_pct": round(bp, 1),
                    "lift": round(lift, 1) if lift is not None else None, "verdict": verdict})
    order = {"confirmed": 0, "ambient": 1, "noise": 2}
    return sorted(out, key=lambda r: (order[r["verdict"]], -r["n"]))
