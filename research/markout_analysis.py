"""MARKOUT analysis on REAL fills — the execution-quality signal shadow/backtest
cannot see. The bot logs, per filled FAK, the token mid at 2s and 10s after the
fill (shadow_taker.jsonl type='markout', drift_vs_fill = mid - fill_px). We ONLY
ever buy, so:

    drift > 0  -> the share appreciated after we filled = edge SURVIVED our
                  ~400ms-late entry (latency-robust signal)
    drift < 0  -> the mid fell right after we bought = we got PICKED OFF
                  (adverse selection / chasing a quote that was about to fade)

Plus slippage_vs_seen_ask = avg_fill_px - seen_ask = the in-flight book move we
paid (direct latency cost). We cross-tab both by side / distance / trend / time /
book_age to find WHICH signals keep their edge when we fill late — the only path
to a live-+EV selection rule (tighten toward positive-markout buckets).

Usage: markout_analysis.py [lookback_hours]   (default 96)
"""
import glob
import json
import re
import sys
import time
from collections import defaultdict

LOGDIR = "/home/ubuntu/polybtc/logs"
NOW = time.time()
LOOKBACK = float(sys.argv[1]) * 3600 if len(sys.argv) > 1 else 96 * 3600

# ---- settlement outcomes ----
out_re = re.compile(r"Bitcoin Up or Down - (.+?) -> (UP|DOWN) \| payout")
outcome = {}
for fp in glob.glob(f"{LOGDIR}/session_*.log"):
    try:
        with open(fp, errors="ignore") as f:
            for ln in f:
                m = out_re.search(ln)
                if m:
                    outcome[m.group(1).strip()] = m.group(2)
    except OSError:
        pass

# ---- join attempts + markouts by id ----
att = {}
mk = defaultdict(dict)  # id -> {horizon: drift}
with open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore") as f:
    for ln in f:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        if t == "attempt" and r.get("leg") == "snipe" and r.get("filled"):
            if NOW - r.get("ts", 0) > LOOKBACK:
                continue
            att[r["id"]] = r
        elif t == "markout" and r.get("drift_vs_fill") is not None:
            mk[r["id"]][r["horizon_s"]] = r["drift_vs_fill"]

rows = []
for i, a in att.items():
    key = a.get("title", "").split("Bitcoin Up or Down - ")[-1].strip()
    oc = outcome.get(key)
    won = None if oc is None else ((oc == "UP") == (a["side"] == "up"))
    fpx = float(a.get("avg_fill_px") or 0)
    slip = a.get("slippage_vs_seen_ask")
    rows.append(dict(
        side=a["side"], fpx=fpx,
        dist=float(a.get("dist_sigma") or 0.0), tz=float(a.get("trend_z") or 0.0),
        trem=float(a.get("t_remaining_s") or 0.0),
        bage=float(a.get("book_age_ms") or 0.0),
        slip=(float(slip) if slip is not None else None),
        m2=mk[i].get(2.0), m10=mk[i].get(10.0), won=won, kind=a.get("kind", "?")))


def agg(fs):
    n = len(fs)
    if not n:
        return "  (none)"
    def mean(key, scale=100):
        v = [f[key] for f in fs if f[key] is not None]
        return (sum(v) / len(v) * scale, len(v)) if v else (None, 0)
    m2, n2 = mean("m2"); m10, n10 = mean("m10"); sl, ns = mean("slip")
    wv = [f["won"] for f in fs if f["won"] is not None]
    win = f"{sum(wv)/len(wv):.0%}" if wv else "  -"
    posfrac = (sum(1 for f in fs if f["m10"] is not None and f["m10"] > 0)
               / max(1, n10))
    return (f"n={n:>3}  win={win:>4}  "
            f"mk2s={m2:+5.2f}c  mk10s={(f'{m10:+5.2f}c' if m10 is not None else '  -'):>7} "
            f"(>0:{posfrac:.0%})  slip={(f'{sl:+5.2f}c' if sl is not None else '  -'):>7}")


def cut(name, keyfn, bins):
    g = defaultdict(list)
    for r in rows:
        g[keyfn(r)].append(r)
    print(f"\n[{name}]")
    for b in bins:
        print(f"  {b:14} {agg(g.get(b, []))}")


print(f"=== MARKOUT on REAL fills — last {LOOKBACK/3600:.0f}h, {len(rows)} filled snipes ===")
print("(markout = post-fill mid - fill px; we only buy, so >0 = edge survived, <0 = picked off)")
print(f"\n[ALL] {agg(rows)}")
cut("side", lambda r: r["side"], ["up", "dn"])
cut("outcome", lambda r: ("WON" if r["won"] else "LOST") if r["won"] is not None else "open", ["WON", "LOST", "open"])
cut("distance sigma", lambda r: ("d.5-.8" if r["dist"] < .8 else "d.8-1.1" if r["dist"] < 1.1 else "d>=1.1"), ["d.5-.8", "d.8-1.1", "d>=1.1"])
cut("trend vs bet", lambda r: ("FADE" if ((r["side"]=="up" and r["tz"]<-0.25) or (r["side"]=="dn" and r["tz"]>0.25)) else "aligned" if ((r["side"]=="up" and r["tz"]>0.25) or (r["side"]=="dn" and r["tz"]<-0.25)) else "flat"), ["FADE", "flat", "aligned"])
cut("t_remaining", lambda r: ("t<45" if r["trem"] < 45 else "t45-70" if r["trem"] < 70 else "t>=70"), ["t<45", "t45-70", "t>=70"])
cut("book_age_ms", lambda r: ("<150" if r["bage"] < 150 else "150-400" if r["bage"] < 400 else ">=400"), ["<150", "150-400", ">=400"])
