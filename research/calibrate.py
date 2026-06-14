"""Analyze logs/calibration.csv: how well do the bot's fair-value predictions
match realized outcomes?

Usage: python research/calibrate.py [path/to/calibration.csv]

For each prediction row (slug, t_remaining, p_up) joined with that market's
outcome, reports calibration by predicted-probability bucket and by time
remaining, plus the Brier score. A well-calibrated model has realized
frequency ~= predicted probability in every bucket; systematic overconfidence
near expiry usually means the vol estimate is too low.
"""

import csv
import sys
from collections import defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "logs/calibration.csv"

preds = []          # (slug, t_remaining, blended_p, model_p)
outcomes = {}       # slug -> 1 (up) / 0 (down)
with open(path) as f:
    for row in csv.DictReader(f):
        if row["outcome"] != "":
            outcomes[row["slug"]] = int(row["outcome"])
        elif row["p_up"] != "":
            model = float(row["model_p"]) if row.get("model_p") else float(row["p_up"])
            preds.append((row["slug"], float(row["t_remaining"]), float(row["p_up"]), model))

joined = [(t, p, mp, outcomes[s]) for s, t, p, mp in preds if s in outcomes]
if not joined:
    sys.exit("no settled markets with predictions yet — run the bot longer")

print(f"{len(joined)} predictions across {len(outcomes)} settled markets\n")

brier_blend = sum((p - o) ** 2 for _, p, _, o in joined) / len(joined)
brier_model = sum((mp - o) ** 2 for _, _, mp, o in joined) / len(joined)
print(f"Brier score (blended, what the bot trades on): {brier_blend:.4f}")
print(f"Brier score (raw model, no market blending):   {brier_model:.4f}")
print("(0.25 = coin flip, lower is better; if blended beats raw, the market")
print(" blend is adding value)\n")
joined = [(t, p, o) for t, p, _, o in joined]

print("calibration by predicted probability:")
print(f"{'bucket':>12} {'n':>6} {'predicted':>10} {'realized':>9}")
buckets = defaultdict(list)
for _, p, o in joined:
    buckets[min(int(p * 10), 9)].append((p, o))
for b in sorted(buckets):
    rows = buckets[b]
    avg_p = sum(p for p, _ in rows) / len(rows)
    freq = sum(o for _, o in rows) / len(rows)
    print(f"{b/10:>5.1f}-{(b+1)/10:<5.1f} {len(rows):>6} {avg_p:>10.3f} {freq:>9.3f}")

print("\noverconfidence by time remaining (|p - outcome| for p>0.9 or p<0.1):")
print(f"{'t_remaining':>12} {'n':>6} {'extreme preds wrong':>20}")
tbuckets = defaultdict(list)
for t, p, o in joined:
    if p > 0.9 or p < 0.1:
        if t < 30: tb = "<30s"
        elif t < 60: tb = "30-60s"
        elif t < 180: tb = "1-3m"
        elif t < 600: tb = "3-10m"
        else: tb = ">10m"
        tbuckets[tb].append(1 if (p > 0.5) != (o == 1) else 0)
for tb in ["<30s", "30-60s", "1-3m", "3-10m", ">10m"]:
    if tb in tbuckets:
        rows = tbuckets[tb]
        print(f"{tb:>12} {len(rows):>6} {sum(rows)/len(rows):>19.1%}")
