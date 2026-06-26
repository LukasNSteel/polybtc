"""Where in each market's life (seconds-to-close) do we fire, and does the
timing help or harm? Joins shadow attempts (which carry t_remaining_s at fire
time + fill outcome + markout) to realized SETTLE P&L, and buckets by
t_remaining. Secondary: a wall-clock hour-of-day cut.
"""
import json
import re
import statistics as st
from collections import defaultdict
from datetime import datetime, timezone
from glob import glob

TS = re.compile(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})")
SETTLE = re.compile(r"SETTLE (.+?) -> (UP|DOWN) \| payout \$([0-9.]+) "
                    r"cost \$([0-9.]+) pnl \$([+-][0-9.]+)")

# title -> (outcome 'up'/'dn', pnl)
settles = {}
for path in glob("logs/session_*.log"):
    for line in open(path, errors="ignore"):
        m = SETTLE.search(line)
        if m:
            title, outc, _pay, _cost, pnl = m.groups()
            settles[title.strip()] = ("up" if outc == "UP" else "dn", float(pnl))

# shadow attempts + markouts
attempts, mk10 = [], {}
for line in open("logs/shadow_taker.jsonl", errors="ignore"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("type") == "attempt":
        attempts.append(r)
    elif r.get("type") == "markout" and r.get("horizon_s") == 10.0:
        if r.get("drift_vs_fill") is not None:
            mk10[r.get("id")] = r["drift_vs_fill"]


def bucket_trem(t, kind):
    # 5m markets dominate; bucket in seconds-to-close
    if t is None:
        return "na"
    for lo, lab in ((180, ">180s"), (120, "120-180s"), (60, "60-120s"),
                    (30, "30-60s"), (0, "<30s")):
        if t >= lo:
            return lab
    return "<30s"


def report(rows, keyfn, title, order=None):
    print(f"\n== {title} ==")
    print(f"{'bucket':12} {'att':>4} {'fillrate':>9} {'fills':>5} "
          f"{'winrate':>8} {'sum pnl':>9} {'avg pnl':>9} {'mk10':>8}")
    groups = defaultdict(list)
    for r in rows:
        groups[keyfn(r)].append(r)
    keys = order or sorted(groups)
    for k in keys:
        g = groups.get(k)
        if not g:
            continue
        att = len(g)
        fills = [r for r in g if r.get("filled")]
        fr = len(fills) / att if att else 0
        # join to settle for win/pnl
        wins = pnl = 0
        npnl = 0
        for r in fills:
            s = settles.get((r.get("title") or "").strip())
            if not s:
                continue
            outc, p = s
            npnl += 1
            pnl += p
            if r.get("side") == outc:
                wins += 1
        wr = wins / npnl if npnl else float("nan")
        avg = pnl / npnl if npnl else float("nan")
        mks = [mk10[r["id"]] for r in fills if r.get("id") in mk10]
        mk = st.median(mks) if mks else float("nan")
        print(f"{str(k):12} {att:>4} {fr*100:>8.0f}% {len(fills):>5} "
              f"{wr*100:>7.0f}% {pnl:>+9.2f} {avg:>+9.2f} {mk:>+8.4f}")


# focus on snipe attempts on 5m markets (the dominant kind)
snipe = [r for r in attempts if r.get("leg") == "snipe"]
fivem = [r for r in snipe if r.get("kind") == "5m"]
print(f"snipe attempts total={len(snipe)}  5m={len(fivem)}  "
      f"kinds={dict((k, sum(1 for r in snipe if r.get('kind')==k)) for k in set(r.get('kind') for r in snipe))}")

report(fivem, lambda r: bucket_trem(r.get("t_remaining_s"), r.get("kind")),
       "5m markets by seconds-to-close (fire timing)",
       order=[">180s", "120-180s", "60-120s", "30-60s", "<30s"])

report(snipe, lambda r: r.get("kind") or "na", "all snipes by market kind")


def hod(r):
    ts = r.get("ts")
    if not ts:
        return "na"
    h = datetime.fromtimestamp(ts, timezone.utc).hour
    return f"{h:02d}h-{(h+4)%24:02d}h" if False else f"{(h//4)*4:02d}-{(h//4)*4+4:02d}UTC"


report(snipe, hod, "all snipes by wall-clock (UTC 4h blocks)")
