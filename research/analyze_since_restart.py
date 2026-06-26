"""Fill rate + win/loss since a restart, broken down by our filters, and the
shadow-candidate counterfactuals (what the filters declined).

Usage:  .venv/bin/python -m research.analyze_since_restart [restart_epoch]
        (defaults to the 2026-06-25 21:18 UTC shadow-candidate restart)
"""
import json
import re
import sys
import datetime
from glob import glob

RESTART = float(sys.argv[1]) if len(sys.argv) > 1 else 1782422289.0
FEE_RATE = 0.07
SETTLE = re.compile(r"SETTLE (.+?) -> (UP|DOWN) \| payout \$[0-9.]+ "
                    r"cost \$[0-9.]+ pnl \$([+-][0-9.]+)")


def load_settles():
    out = {}
    for path in glob("logs/session_*.log"):
        for line in open(path, errors="ignore"):
            m = SETTLE.search(line)
            if m:
                title, outc, pnl = m.groups()
                out[title.strip()] = ("up" if outc == "UP" else "dn", float(pnl))
    return out


def load(path):
    try:
        return [json.loads(l) for l in open(path, errors="ignore") if l.strip()]
    except FileNotFoundError:
        return []


def grp(rows, keyfn, label):
    """fill/win table by a bucketing function."""
    buckets = {}
    for r in rows:
        buckets.setdefault(keyfn(r), []).append(r)
    print(f"  {label:18} {'att':>4} {'fill':>5} {'fill%':>6} "
          f"{'sett':>4} {'win%':>6} {'pnl$':>8}")
    for k in sorted(buckets, key=str):
        rs = buckets[k]
        att = len(rs)
        fl = [r for r in rs if r.get("filled")]
        st = [r for r in fl if r.get("_settled")]
        win = sum(r["_won"] for r in st)
        pnl = sum(r["_pnl"] for r in st)
        fillpct = f"{len(fl) / att * 100:.0f}%" if att else "-"
        winpct = f"{win / len(st) * 100:.0f}%" if st else "-"
        print(f"  {str(k):18} {att:>4} {len(fl):>5} {fillpct:>6} "
              f"{len(st):>4} {winpct:>6} {pnl:>+8.2f}")


def main():
    dt = datetime.datetime.utcfromtimestamp(RESTART).strftime("%Y-%m-%d %H:%M UTC")
    print(f"since restart {dt}  (epoch {RESTART:.0f})\n")
    settles = load_settles()

    att = [d for d in load("logs/shadow_taker.jsonl")
           if d.get("type") == "attempt" and d.get("ts", 0) >= RESTART]
    for r in att:
        s = settles.get((r.get("title") or "").strip())
        r["_settled"] = s is not None
        if s:
            r["_won"] = int(r["side"] == s[0])
            r["_pnl"] = s[1]
        else:
            r["_won"] = 0
            r["_pnl"] = 0.0

    fills = [r for r in att if r.get("filled")]
    settled = [r for r in fills if r["_settled"]]
    print("=" * 64)
    print("REAL FAK ATTEMPTS (the trades we actually placed)")
    print("=" * 64)
    print(f"  attempts:        {len(att)}")
    if att:
        print(f"  filled:          {len(fills)}  "
              f"({len(fills) / len(att) * 100:.0f}% fill rate)")
        print(f"  lost race/kill:  {len(att) - len(fills)}")
    if settled:
        w = sum(r["_won"] for r in settled)
        pnl = sum(r["_pnl"] for r in settled)
        print(f"  settled fills:   {len(settled)}  -> W{w}/{len(settled) - w}  "
              f"win {w / len(settled) * 100:.0f}%  net ${pnl:+.2f}  "
              f"avg ${pnl / len(settled):+.2f}")
    unsettled = len(fills) - len(settled)
    if unsettled:
        print(f"  (awaiting settlement: {unsettled} fills)")

    if att:
        print("\n--- by SIDE ---")
        grp(att, lambda r: r["side"], "side")
        print("\n--- by LEG ---")
        grp(att, lambda r: r.get("leg", "?"), "leg")
        print("\n--- by DISTANCE-to-strike (dist_sigma) ---")

        def dband(r):
            d = r.get("dist_sigma")
            if d is None:
                return "na"
            for lo, hi in [(0, 0.5), (0.5, 1.0), (1.0, 1.5), (1.5, 9)]:
                if lo <= d < hi:
                    return f"[{lo},{hi})"
            return "?"
        grp(att, dband, "dsig band")
        print("\n--- by TREND_Z (fade = momentum against the bet) ---")

        def zband(r):
            z = r.get("trend_z")
            if z is None:
                return "na"
            side = r["side"]
            fade = (side == "up" and z < 0) or (side == "dn" and z > 0)
            mag = abs(z)
            tag = "fade" if fade else "with"
            band = "lo" if mag < 0.5 else ("mid" if mag < 1.0 else "hi")
            return f"{tag}-{band}"
        grp(att, zband, "trend")
        print("\n--- by t_remaining band ---")

        def tband(r):
            t = r.get("t_remaining_s") or 0
            for lo, hi in [(0, 30), (30, 60), (60, 90), (90, 180), (180, 9999)]:
                if lo <= t < hi:
                    return f"[{lo},{hi})"
            return "?"
        grp(att, tband, "t_rem")

    # ---- shadow candidates: what the filters DECLINED ----
    cand = [d for d in load("logs/shadow_candidates.jsonl")
            if d.get("type") == "candidate" and d.get("ts", 0) >= RESTART]
    print("\n" + "=" * 64)
    print("SHADOW CANDIDATES (declined by filters; NO order placed)")
    print("=" * 64)
    if not cand:
        print("  none logged yet.")
    else:
        seen = set()
        uniq = []
        for d in cand:
            k = (d["slug"], d["side"], d.get("reason"))
            if k in seen:
                continue
            seen.add(k)
            s = settles.get((d.get("title") or "").strip())
            d["_settled"] = s is not None
            d["_won"] = int(d["side"] == s[0]) if s else 0
            uniq.append(d)
        print(f"  unique candidates: {len(uniq)}  "
              f"(window={sum(d['reason'] == 'window' for d in uniq)}, "
              f"trend={sum(d['reason'] == 'trend' for d in uniq)})")
        for reason in ("window", "trend"):
            rs = [d for d in uniq if d["reason"] == reason
                  and d["_settled"]]
            if rs:
                w = sum(d["_won"] for d in rs)
                print(f"  {reason:8}: settled {len(rs)}  W{w}/{len(rs) - w}  "
                      f"win {w / len(rs) * 100:.0f}%  "
                      f"(these are trades we did NOT take)")
            else:
                pend = sum(1 for d in uniq if d["reason"] == reason)
                print(f"  {reason:8}: {pend} logged, none settled yet")

    print("\nNOTE: win% needs settlement; very fresh fills/candidates may be "
          "pending. Counts are small over a few hours — read directionally.")


if __name__ == "__main__":
    main()
