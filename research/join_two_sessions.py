import re

SETTLE = re.compile(
    r"SETTLE (?P<title>.+?) -> (?P<res>UP|DOWN) \| payout \$(?P<payout>[\d.]+) "
    r"cost \$(?P<cost>[\d.]+) pnl \$(?P<pnl>[+\-][\d.]+)"
)


def load(path):
    d = {}
    with open(path) as f:
        for line in f:
            if (m := SETTLE.search(line)):
                d[m["title"]] = (float(m["cost"]), float(m["pnl"]), m["res"])
    return d


big = load("logs/session_1781925965.log")   # 1000 cap
small = load("logs/session_1781925954.log")  # 250 cap
common = sorted(set(big) & set(small))

print(f"markets settled in both: {len(common)}\n")
print(f"{'market':40} {'1000:cost':>10} {'1000:pnl':>9} {'250:cost':>9} {'250:pnl':>8}")
print("-" * 80)
b_net = s_net = 0.0
rows = []
for t in common:
    bc, bp, _ = big[t]
    sc, sp, _ = small[t]
    b_net += bp
    s_net += sp
    rows.append((bp, t, bc, bp, sc, sp))
for bp, t, bc, bpp, sc, sp in sorted(rows):
    short = t.replace("Bitcoin Up or Down - ", "")
    print(f"{short:40} {bc:>10.2f} {bpp:>+9.2f} {sc:>9.2f} {sp:>+8.2f}")
print("-" * 80)
print(f"{'NET (common markets)':40} {'':>10} {b_net:>+9.2f} {'':>9} {s_net:>+8.2f}")

# same direction every time?
same_dir = all(big[t][2] == small[t][2] for t in common)
print(f"\nsame resolution direction in both sessions for every common market: {same_dir}")
# how often did both win / both lose / split
bw = sum(big[t][1] > 0 for t in common)
sw = sum(small[t][1] > 0 for t in common)
split = sum((big[t][1] > 0) != (small[t][1] > 0) for t in common)
print(f"1000 wins {bw}/{len(common)}, 250 wins {sw}/{len(common)}, "
      f"win/loss disagreements: {split}")
