"""PRELIMINARY (n~20) exploration of a markout-conditioned SELECTION rule for the
5m UP snipe. Markout is measured POST-fill so it can't be a pre-trade gate; the
job is to find which PRE-TRADE features (ask, distance, trend_z, t_remaining,
book_age, hour-of-day) precede the negative-markout LOSERS, so a future rule can
gate on them. Univariate only — at this sample any multivariate/ML fit overfits.

CAVEAT: tiny sample. Treat every cut below as a HYPOTHESIS to re-test at n>=40,
not a deployable rule. In-sample 'best cuts' look better than they are.
"""
import calendar, glob, json, re, time, urllib.request

LOGDIR = "/home/ubuntu/polybtc/logs"
DEPLOY = calendar.timegm(time.strptime("2026-06-27 08:09:31", "%Y-%m-%d %H:%M:%S"))

out_re = re.compile(r"Bitcoin Up or Down - (.+?) -> (UP|DOWN) \| payout")
outcome = {}
for fp in glob.glob(f"{LOGDIR}/session_*.log"):
    try:
        for ln in open(fp, errors="ignore"):
            m = out_re.search(ln)
            if m:
                outcome[m.group(1).strip()] = m.group(2)
    except OSError:
        pass

att = []
mk = {}
for ln in open(f"{LOGDIR}/shadow_taker.jsonl", errors="ignore"):
    try:
        r = json.loads(ln)
    except json.JSONDecodeError:
        continue
    if r.get("type") == "markout" and r.get("drift_vs_fill") is not None:
        mk.setdefault(r["id"], {})[r["horizon_s"]] = r["drift_vs_fill"]
    elif r.get("type") == "attempt" and r.get("leg") == "snipe" and r.get("filled") and r.get("ts", 0) >= DEPLOY:
        att.append(r)


def wstart(slug):
    try:
        return int(slug.rsplit("-", 1)[1])
    except (ValueError, AttributeError, IndexError):
        return None

starts = [wstart(a.get("slug")) for a in att if wstart(a.get("slug"))]
kl = {}
if starts:
    url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m"
           f"&startTime={min(starts)*1000}&endTime={(max(starts)+300)*1000}&limit=1000")
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            for k in json.load(resp):
                kl[k[0] // 1000] = "UP" if float(k[4]) >= float(k[1]) else "DOWN"
    except Exception as e:  # noqa: BLE001
        print("(kline fail", e, ")")

rows = []
for a in att:
    key = a.get("title", "").split("Bitcoin Up or Down - ")[-1].strip()
    oc = outcome.get(key) or kl.get(wstart(a.get("slug")))
    if oc is None:
        continue
    px = float(a.get("avg_fill_px") or 0); sh = float(a.get("filled_shares") or 0)
    won = (oc == "UP") == (a["side"] == "up")
    fee = 0.07 * px * (1 - px) * sh
    pnl = (sh * (1 - px) - fee) if won else (-sh * px - fee)
    rows.append(dict(won=won, pnl=pnl, ask=px, dist=float(a.get("dist_sigma") or 0),
                     tz=float(a.get("trend_z") or 0), trem=float(a.get("t_remaining_s") or 0),
                     bage=float(a.get("book_age_ms") or 0),
                     hour=time.gmtime(a["ts"]).tm_hour,
                     m60=mk.get(a["id"], {}).get(60.0)))

W = [r for r in rows if r["won"]]; L = [r for r in rows if not r["won"]]
n = len(rows)
print(f"=== markout-rule exploration — {n} settled fills since deploy "
      f"({len(W)} won / {len(L)} lost) — PRELIMINARY, n is tiny ===")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")

print("\n[winner vs loser PRE-TRADE feature profile]  (mean)")
print(f"  {'feature':10} {'WON':>9} {'LOST':>9}")
for f, lab in [("ask", "ask px"), ("dist", "dist σ"), ("tz", "trend_z"),
               ("trem", "t_rem s"), ("bage", "book_age"), ("hour", "hour UTC"),
               ("m60", "mk60 (c→×100)")]:
    wv, lv = mean([r[f] for r in W]), mean([r[f] for r in L])
    sc = 100 if f == "m60" else 1
    print(f"  {lab:10} {wv*sc:>9.2f} {lv*sc:>9.2f}")


def cut(name, keyfn, bins):
    print(f"\n[{name}]  (n / win% / pnl$)")
    for lab, pred in bins:
        sub = [r for r in rows if pred(r)]
        if sub:
            w = sum(x["won"] for x in sub); p = sum(x["pnl"] for x in sub)
            print(f"  {lab:16} {len(sub):>3}  {w/len(sub):>4.0%}  {p:>+7.2f}")


cut("ask band", None, [("ask<0.55", lambda r: r["ask"] < .55),
                       ("0.55-0.68", lambda r: .55 <= r["ask"] < .68),
                       ("ask>=0.68", lambda r: r["ask"] >= .68)])
cut("distance σ", None, [("d<0.8", lambda r: r["dist"] < .8),
                         ("0.8-1.2", lambda r: .8 <= r["dist"] < 1.2),
                         ("d>=1.2", lambda r: r["dist"] >= 1.2)])
cut("trend_z (UP bet)", None, [("tz<0 (fading)", lambda r: r["tz"] < 0),
                               ("tz 0-0.5", lambda r: 0 <= r["tz"] < .5),
                               ("tz>=0.5 (with run)", lambda r: r["tz"] >= .5)])
cut("hour-of-day UTC", None, [("08-15 (EU/AM)", lambda r: 8 <= r["hour"] < 15),
                              ("15-24 (US PM)", lambda r: 15 <= r["hour"] < 24),
                              ("00-08 (overnight)", lambda r: r["hour"] < 8)])
cut("book_age ms", None, [("<150", lambda r: r["bage"] < 150),
                          (">=150", lambda r: r["bage"] >= 150)])
