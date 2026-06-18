"""Re-evaluate session_1781421149 under the new speed-bump execution model.

IMPORTANT — what this can and cannot do
---------------------------------------
The session log is the *output* of a paper run under the OLD executor
(taker latency 350ms, cancel latency 150ms, and kills treated as costless
"lost the race"). It records, per snipe: the ask at decision time, our fair
estimate, the net edge, the staked notional, and (on fills) the entry price —
plus an aggregate post-fill markout (mid drift at 10s / 60s).

The NEW executor (bot/execution.PaperExecutor._speed_bump_fill) keys off
something the log never recorded: how the Polymarket *ask* moves during the
250ms uncancellable hold. Reject-vs-fill and the adverse-selection drift are
decided by the sub-250ms book tape, which is not in a text log. So a
tick-faithful replay is impossible from this artifact alone.

What we CAN do is a transparent, parameterised projection anchored on the
real per-snipe edge distribution and the real markouts, with the single
dominant unknown made explicit: phi = the fraction of the snipe's forward
"alpha" (book catching up to Binance) that lands inside the 250ms hold.
That fraction is exactly what the speed bump converts from profit into a
costless miss (quote richens -> FAK rejected) while leaving the flat/adverse
prints to fill. We sweep phi and report the range.
"""

import re
import sys
from pathlib import Path

LOG = Path(__file__).resolve().parent.parent / "logs" / "session_1781421149.log"

# ---- parse -----------------------------------------------------------------

SNIPE_RE = re.compile(
    r"SNIPE (?P<title>.+?) (?P<side>UP|DN): ask (?P<ask>[\d.]+) \+ fee "
    r"(?P<fee>[\d.]+) vs robust (?P<robust>[\d.]+) \(blend (?P<blend>[\d.]+), "
    r"edge (?P<edge>[\d.]+), \$(?P<usd>[\d.]+)\)"
)
FILL_RE = re.compile(
    r"FILL (?P<side>UP|DN)\s+snipe (?P<title>.+?) (?P<sh>[\d.]+) sh @ "
    r"(?P<px>[\d.]+) \(\$(?P<cost>[\d.]+)(?: \+fee (?P<fee>[\d.]+))?\)"
)


def parse(path):
    snipes, fills = [], []
    with open(path) as f:
        for line in f:
            m = SNIPE_RE.search(line)
            if m:
                d = m.groupdict()
                snipes.append({
                    "side": d["side"], "ask": float(d["ask"]),
                    "fee": float(d["fee"]), "robust": float(d["robust"]),
                    "blend": float(d["blend"]), "edge": float(d["edge"]),
                    "usd": float(d["usd"]),
                })
                continue
            m = FILL_RE.search(line)
            if m:
                d = m.groupdict()
                fills.append({
                    "side": d["side"], "sh": float(d["sh"]),
                    "px": float(d["px"]), "cost": float(d["cost"]),
                    "fee": float(d["fee"] or 0.0),
                })
    return snipes, fills


def pct(x):
    return f"{x:6.1f}%"


def main():
    snipes, fills = parse(LOG)

    # ground-truth (old model) from the log summary
    OLD_SNIPE_PNL = 2421.09       # leg snipe realized cash flow
    OLD_FILLS = 288               # total fills (283 snipe + 5 scalp)
    SNIPE_FILLS = len(fills)
    FAK_ATTEMPTS = 460
    FAK_FILLS = 288
    FAK_KILLS = 172
    MK10 = 0.1031                 # +10.31c avg markout @10s (n=265)
    MK60 = 0.1309                 # +13.09c @60s (n=204)
    TAKER_VOL = 13436.02
    FEES = 376.18

    notional = sum(f["cost"] for f in fills)
    avg_edge = sum(s["edge"] for s in snipes) / len(snipes)

    edges = sorted(s["edge"] for s in snipes)
    n = len(edges)
    q = lambda p: edges[min(n - 1, int(n * p))]

    print("=" * 72)
    print("GROUND TRUTH — old model (taker 350ms, cancel 150ms, costless kills)")
    print("=" * 72)
    print(f"  snipe decision lines parsed : {len(snipes)}")
    print(f"  snipe fills parsed          : {SNIPE_FILLS}  (notional ${notional:,.0f})")
    print(f"  FAK attempts/fills/kills    : {FAK_ATTEMPTS} / {FAK_FILLS} / {FAK_KILLS}"
          f"  (fill rate {pct(100*FAK_FILLS/FAK_ATTEMPTS)})")
    print(f"  snipe realized P&L          : ${OLD_SNIPE_PNL:,.2f}")
    print(f"  net edge at decision        : median {q(0.5)*100:.1f}c  "
          f"p25 {q(0.25)*100:.1f}c  p75 {q(0.75)*100:.1f}c  (avg {avg_edge*100:.1f}c)")
    print(f"  post-fill markout           : +{MK10*100:.2f}c @10s   +{MK60*100:.2f}c @60s")
    print(f"  taker volume / fees         : ${TAKER_VOL:,.0f} / ${FEES:,.0f}")
    print()
    print("  -> Every snipe fires on a LARGE apparent edge (>=10c). A 10c+ ask")
    print("     mispricing that is still resting is exactly what the fastest")
    print("     bots already declined — the winner's-curse zone. The +$2,421")
    print("     shows up as the strongly positive markout: under 350ms latency")
    print("     we still won the race to those prints before the book caught up.")
    print()

    # ---- new-model projection ---------------------------------------------
    # Mechanism: during the 250ms uncancellable hold the order is committed.
    #   * side richened (BTC moved our way fast) -> cheap quote gone -> REJECT
    #     (costless miss). These are precisely the fastest-converging, i.e.
    #     highest-alpha prints. Fraction lost ~ phi.
    #   * side flat/cheapened -> we fill, and the slice of convergence that
    #     already happened in the hold is value we DON'T capture (we paid up
    #     to it); if BTC moved against us we fill adverse.
    #
    # phi = fraction of the 10s markout alpha that is realized within 250ms.
    # For a "book-lags-Binance" signal the lag is short, so phi is not small.
    #
    # Captured snipe alpha multiplier under the bump, first order:
    #   keep fills that don't richen (~ 1-phi of the good ones) AND on the
    #   fills we keep we forfeit the in-hold slice. Net surviving profit
    #   fraction ~ (1-phi)^2 on the profitable book, plus an adverse drag from
    #   old kills that now fill into a move against us.
    ADVERSE_DRAG_PER_C = {  # extra loss as a fraction of old gross, by phi
        0.00: 0.00, 0.25: 0.05, 0.50: 0.12, 0.75: 0.22,
    }

    print("=" * 72)
    print("PROJECTION — new model (250ms uncancellable hold, reject-on-richen)")
    print("=" * 72)
    print("  phi = share of snipe alpha that lands inside the 250ms hold")
    print("        (and is therefore rejected away as a costless miss)")
    print()
    print(f"  {'phi':>5} {'surviving':>10} {'adverse':>9} {'proj snipe':>12} "
          f"{'vs old':>9}")
    print(f"  {'':>5} {'profit':>10} {'drag':>9} {'P&L':>12} {'':>9}")
    print("  " + "-" * 52)
    for phi in (0.00, 0.25, 0.50, 0.75):
        survive = (1 - phi) ** 2
        gross = OLD_SNIPE_PNL * survive
        drag = OLD_SNIPE_PNL * ADVERSE_DRAG_PER_C[phi]
        proj = gross - drag
        delta = proj - OLD_SNIPE_PNL
        print(f"  {phi:>5.2f} {survive*100:>9.0f}% ${drag:>8,.0f} "
              f"${proj:>11,.0f} {delta:>+8,.0f}")
    print()
    print("  Fees scale with surviving fills; the kill arm is costless either")
    print("  way, so the swing is almost entirely in captured snipe alpha.")
    print()
    print("  READ: if the Polymarket ask catches up to Binance mostly AFTER")
    print("  250ms (phi low) the bump barely bites. If catch-up is mostly")
    print("  sub-250ms (phi high) the edge is structurally arbitraged away —")
    print("  the speed bump keeps the prints faster bots declined.")
    print()
    print("  This is a projection, not a replay. A faithful number needs the")
    print("  sub-250ms Polymarket book tape for this window (not in the log),")
    print("  or a fresh paper run on the live executor already wired up.")


if __name__ == "__main__":
    sys.exit(main())
