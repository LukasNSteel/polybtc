"""Adverse-selection / EV read on live taker fills from shadow_taker.jsonl.

For a BUY, drift_vs_fill = mid(horizon) - fill_px. Positive = the market moved
our way after we filled (good); negative = it moved against us = adverse
selection (we only won the race because the counterparty had better info).
Median drift << 0 on the FILLED set means the taker leg is structurally picked
off regardless of the model's edge."""
import json
import statistics as st
from collections import defaultdict

attempts = {}
markouts = defaultdict(dict)  # id -> {horizon: drift}
fills = []
for line in open("logs/shadow_taker.jsonl"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("type") == "attempt":
        attempts[r.get("id")] = r
        if r.get("filled"):
            fills.append(r)
    elif r.get("type") == "markout":
        d = r.get("drift_vs_fill")
        if d is not None:
            markouts[r.get("id")][r.get("horizon_s")] = d


def stats(name, vals):
    if not vals:
        print(f"  {name}: (none)")
        return
    vals = sorted(vals)
    neg = sum(1 for v in vals if v < 0)
    print(f"  {name}: n={len(vals)}  median {st.median(vals):+.4f}  "
          f"mean {st.mean(vals):+.4f}  share<0 {neg/len(vals)*100:.0f}%")


print(f"FILLED taker orders: {len(fills)}")
print("\nMARKOUT drift_vs_fill (BUY: <0 = market moved AGAINST our fill = adverse):")
for h in (2.0, 10.0):
    vals = [markouts[a["id"]][h] for a in fills
            if a["id"] in markouts and h in markouts[a["id"]]]
    stats(f"horizon {h:>4.0f}s ALL", vals)
    for side in ("up", "dn"):
        sv = [markouts[a["id"]][h] for a in fills
              if a.get("side") == side and a["id"] in markouts and h in markouts[a["id"]]]
        stats(f"horizon {h:>4.0f}s {side}", sv)

print("\nCAPTURE (filled_shares / displayed ask size):")
caps = [a["capture_frac"] for a in fills if isinstance(a.get("capture_frac"), (int, float))]
if caps:
    caps.sort()
    print(f"  median {st.median(caps):.3f}  mean {st.mean(caps):.3f}  "
          f"p90 {caps[int(len(caps)*0.9)-1]:.3f}")

print("\nSLIPPAGE vs seen ask (filled; >0 = paid above what we saw):")
slip = [a["slippage_vs_seen_ask"] for a in fills
        if isinstance(a.get("slippage_vs_seen_ask"), (int, float))]
if slip:
    slip.sort()
    print(f"  median {st.median(slip):+.4f}  mean {st.mean(slip):+.4f}")
