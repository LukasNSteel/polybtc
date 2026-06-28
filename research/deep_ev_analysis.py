"""Deep EV analysis of live snipe fills, with a PRE vs POST book-self-refresh
split (refresh went live 2026-06-27 ~00:13 UTC). Joins shadow_taker fills to
settlement outcomes and buckets EV by every lever: side, distance, favourite
(ask), time-in-window, trend (fade/aligned), and book_age — so we can see WHERE
the losses are and whether the refresh unlocked -EV quiet-book fires.

Usage: deep_ev_analysis.py [lookback_hours]   (default 48)
"""
import calendar
import glob
import json
import re
import sys
import time
from collections import defaultdict

LOGDIR = "/home/ubuntu/polybtc/logs"
NOW = time.time()
LOOKBACK = float(sys.argv[1]) * 3600 if len(sys.argv) > 1 else 48 * 3600
# book self-refresh deploy (1.0s at 00:13Z, 0.5s at 00:25Z 2026-06-27)
REFRESH_TS = calendar.timegm(time.strptime("2026-06-27 00:13:00", "%Y-%m-%d %H:%M:%S"))

# ---- outcomes: window -> "UP"/"DOWN" ----
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

# ---- filled snipe attempts ----
fills = []
with open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore") as f:
    for ln in f:
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if r.get("type") != "attempt" or not r.get("filled") or r.get("leg") != "snipe":
            continue
        if NOW - r.get("ts", 0) > LOOKBACK:
            continue
        key = r.get("title", "").split("Bitcoin Up or Down - ")[-1].strip()
        oc = outcome.get(key)
        if oc is None:
            continue
        side = r["side"]
        sh = float(r.get("filled_shares") or 0)
        px = float(r.get("avg_fill_px") or r.get("limit_px") or 0)
        if sh <= 0 or px <= 0:
            continue
        won = (oc == "UP" and side == "up") or (oc == "DOWN" and side == "dn")
        fee = 0.07 * px * (1 - px) * sh
        pnl = (sh * (1 - px) - fee) if won else (-sh * px - fee)
        fills.append(dict(side=side, sh=sh, px=px, won=won, pnl=pnl,
                          tz=float(r.get("trend_z") or 0.0),
                          dist=float(r.get("dist_sigma") or 0.0),
                          trem=float(r.get("t_remaining_s") or 0.0),
                          bage=float(r.get("book_age_ms") or 0.0),
                          kind=r.get("kind", "?"),
                          ts=r["ts"], post=r["ts"] >= REFRESH_TS))
fills.sort(key=lambda x: x["ts"])


def line(label, fs):
    if not fs:
        print(f"  {label:30} {'0':>4}")
        return
    n = len(fs); w = sum(f["won"] for f in fs); pnl = sum(f["pnl"] for f in fs)
    dep = sum(f["sh"] * f["px"] for f in fs)
    print(f"  {label:30} {n:>4} {w/n:>5.0%} {pnl:>+8.2f} {pnl/n:>+7.2f} {pnl/dep if dep else 0:>+6.1%}")


def bucket(fs, name, keyfn, bins):
    print(f"\n[{name}]  (n / win% / pnl$ / EV/fill / ROI)")
    groups = defaultdict(list)
    for f in fs:
        groups[keyfn(f)].append(f)
    for b in bins:
        line(b, groups.get(b, []))


print(f"=== DEEP EV ANALYSIS — last {LOOKBACK/3600:.0f}h, {len(fills)} settled snipe fills ===")
print(f"(refresh live {time.strftime('%m-%d %H:%MZ', time.gmtime(REFRESH_TS))})")
print("\n[headline]  (n / win% / pnl$ / EV/fill / ROI)")
line("ALL", fills)
line("PRE-refresh", [f for f in fills if not f["post"]])
line("POST-refresh", [f for f in fills if f["post"]])

bucket(fills, "side", lambda f: f["side"], ["up", "dn"])
bucket(fills, "favourite ask", lambda f: ("a<.55" if f["px"] < .55 else "a.55-.65" if f["px"] < .65 else "a.65-.75" if f["px"] < .75 else "a>=.75"), ["a<.55", "a.55-.65", "a.65-.75", "a>=.75"])
bucket(fills, "distance sigma", lambda f: ("d.5-.8" if f["dist"] < .8 else "d.8-1.1" if f["dist"] < 1.1 else "d>=1.1"), ["d.5-.8", "d.8-1.1", "d>=1.1"])
bucket(fills, "t_remaining", lambda f: ("t<40" if f["trem"] < 40 else "t40-60" if f["trem"] < 60 else "t60-90" if f["trem"] < 90 else "t>=90"), ["t<40", "t40-60", "t60-90", "t>=90"])
bucket(fills, "trend vs bet", lambda f: ("FADE" if ((f["side"]=="up" and f["tz"]<-0.25) or (f["side"]=="dn" and f["tz"]>0.25)) else "aligned" if ((f["side"]=="up" and f["tz"]>0.25) or (f["side"]=="dn" and f["tz"]<-0.25)) else "flat~0"), ["FADE", "flat~0", "aligned"])
bucket(fills, "book_age_ms at fire", lambda f: ("<300" if f["bage"] < 300 else "300-800" if f["bage"] < 800 else ">=800"), ["<300", "300-800", ">=800"])
bucket(fills, "market kind", lambda f: f["kind"], ["5m", "15m", "1h", "4h"])

# cross-tabs: don't over-cut. Is DN bad EVERYWHERE, or only in some kind/dist?
print("\n[CROSS: side x kind]")
for sd in ("up", "dn"):
    for k in ("5m", "15m", "1h", "4h"):
        sub = [f for f in fills if f["side"] == sd and f["kind"] == k]
        if sub:
            line(f"{sd.upper()} {k}", sub)
print("\n[CROSS: side x distance]")
for sd in ("up", "dn"):
    for lab, lo, hi in (("d.5-.8", .5, .8), ("d.8-1.1", .8, 1.1), ("d>=1.1", 1.1, 9)):
        sub = [f for f in fills if f["side"] == sd and lo <= f["dist"] < hi]
        if sub:
            line(f"{sd.upper()} {lab}", sub)

print("\n[POST-refresh per-fill]  ts / side / ask / dist / tz / trem / bage / won / pnl")
for f in fills:
    if f["post"]:
        print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(f['ts']))} {f['side'].upper():3} "
              f"@{f['px']:.3f} d{f['dist']:+.2f} tz{f['tz']:+.2f} t{f['trem']:>4.0f} "
              f"b{f['bage']:>5.0f} {'WON ' if f['won'] else 'LOST'} {f['pnl']:+6.2f}")
