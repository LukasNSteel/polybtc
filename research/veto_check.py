"""Would the max_edge-vetoed snipes have won or lost? (and net $ at live165 sizing)

11 SNIPE VETO events since the config went live. Each is (UTC open/close, side,
modeled edge). Outcome from Binance close-vs-open for that window. P&L assumes
we'd have entered at the live165 flat $5 stake: a loss is the full -$5; a win
returns 5*(1-ask)/ask — we don't log the ask on a veto, so wins are shown as a
range across a plausible favourite ask band [0.55, 0.75].
"""
import json
import ssl
import urllib.request

_SSL = ssl._create_unverified_context()

# (open_utc, close_utc 'YYYY-MM-DD HH:MM', side, edge). ET+4 = UTC.
VETOES = [
    ("2026-06-24 03:35", "2026-06-24 03:40", "up", 0.192),
    ("2026-06-24 04:15", "2026-06-24 04:30", "dn", 0.455),
    ("2026-06-24 04:30", "2026-06-24 04:35", "up", 0.158),
    ("2026-06-24 04:30", "2026-06-24 04:45", "up", 0.351),  # 00:30-00:45 (vetoed twice)
    ("2026-06-24 04:55", "2026-06-24 05:00", "up", 0.204),
    ("2026-06-24 05:05", "2026-06-24 05:10", "dn", 0.178),
    ("2026-06-24 06:20", "2026-06-24 06:25", "up", 0.178),
    ("2026-06-24 06:30", "2026-06-24 06:35", "dn", 0.216),
    ("2026-06-24 10:05", "2026-06-24 10:10", "up", 0.217),
    ("2026-06-24 10:20", "2026-06-24 10:25", "up", 0.239),
]
import calendar, time
def ep(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%d %H:%M"))

FEE = 0.07
STAKE = 5.0


def fetch(start_s, end_s):
    px = {}
    t = start_s * 1000
    while t < end_s * 1000:
        url = ("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s"
               f"&startTime={t}&endTime={end_s*1000}&limit=1000")
        with urllib.request.urlopen(url, timeout=30, context=_SSL) as r:
            rows = json.load(r)
        if not rows:
            break
        for k in rows:
            px[k[0] // 1000] = float(k[4])
        t = rows[-1][0] + 1000
    return px


def at(px, sec):
    for d in range(0, 120):
        if sec - d in px:
            return px[sec - d]
    return None


def main():
    lo = min(ep(o) for o, _, _, _ in VETOES) - 120
    hi = max(ep(c) for _, c, _, _ in VETOES) + 120
    print(f"fetching binance 1s {lo}..{hi} ...", flush=True)
    px = fetch(lo, hi)

    won_n = lost_n = 0
    loss_usd = 0.0
    win_lo = win_hi = 0.0
    print(f"\n{'window (UTC)':22} {'side':>4} {'edge':>6} {'open':>9} {'close':>9} "
          f"{'res':>4} {'bet':>5} {'$ at flat 5':>16}")
    for o, c, side, edge in VETOES:
        op = at(px, ep(o)); cl = at(px, ep(c))
        if op is None or cl is None:
            print(f"{o[5:]:22} {side:>4} {edge:>6.3f}  (no price)")
            continue
        up_won = cl >= op
        won = up_won if side == "up" else (not up_won)
        if won:
            won_n += 1
            wlo = STAKE * (1 - 0.75) / 0.75      # ask 0.75 -> smallest win
            whi = STAKE * (1 - 0.55) / 0.55      # ask 0.55 -> largest win
            win_lo += wlo; win_hi += whi
            money = f"+${wlo:.2f}..+${whi:.2f}"
        else:
            lost_n += 1
            loss_usd += STAKE
            money = f"-${STAKE:.2f}"
        print(f"{o[5:]:22} {side:>4} {edge:>6.3f} {op:>9.1f} {cl:>9.1f} "
              f"{('UP' if up_won else 'DN'):>4} {side.upper():>5} {money:>16}")

    print("\n" + "=" * 70)
    print(f"vetoed events: {won_n + lost_n}  ->  would-win {won_n} | would-lose {lost_n}")
    print(f"losers cost avoided:  +${loss_usd:.2f}  (we did NOT lose this)")
    print(f"winners forgone:      -${win_lo:.2f} .. -${win_hi:.2f}  (we did NOT make this)")
    print(f"NET effect of the veto: +${loss_usd - win_hi:.2f} .. +${loss_usd - win_lo:.2f}")
    print("(positive = the veto helped; range spans favourite ask 0.55-0.75)")


if __name__ == "__main__":
    main()
