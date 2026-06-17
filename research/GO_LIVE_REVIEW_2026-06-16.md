# Go-Live Readiness Review — 2026-06-16

**Session reviewed:** `logs/session_1781421149.log`
(Jun 14 17:12 → Jun 16 23:45 ET, ~54h continuous paper trading)
**Scripts:** `research/analyze_sessions.py`, `research/analyze_live_hypotheses.py`,
`research/calibrate.py`, `research/test_strategy_candidates.py`,
`research/test_dual_beta.py`
**Verdict (TL;DR): Not yet. The edge is real and statistically positive in
paper, but the evidence is one ~2-day paper sample with optimistic-fill bias,
concentrated in a single day, and two material live risks (fee formula,
Chainlink basis) are still unverified. Recommended next step is a small,
capped LIVE pilot — not full deployment.**

---

## 1. What the most recent log shows

Started from a restored balance of **$468**, ended at **$2,548** — a paper gain
of **~$2,081 over 54 hours**. Joining every fill to its settlement:

| leg | n | win% | cost | pnl | ret/$1 |
|---|---|---|---|---|---|
| snipe | 202 | 67% | $7,492 | **+$2,078** | **+27.7%** |
| scalp | 5 | 100% | $356 | +$3 | +0.9% |
| mm | 0 (disabled) | — | — | — | — |

- **Market-clustered bootstrap CI on the snipe: ret/$1 = +27.7% [+14.0%, +40.0%]** —
  the lower bound is well above zero, so the edge is statistically real *in this sample*.
- **FAK fill rate ~61%** (133 of ~340 taker attempts lost the race) — the bot is
  already losing 4 of every 10 races to faster takers, in a simulator that is
  *kinder* than live.
- **Max drawdown $407 (16% of peak).** The kill switch sits at 30% / $300–500;
  this session came within striking distance of it.

### Profit is not broad-based — it's a fat right tail
| day | n | pnl | ret/$1 |
|---|---|---|---|
| 06-14 | 44 | **−$83** | −5.0% |
| 06-15 | 91 | +$728 | +24.2% |
| 06-16 | 67 | **+$1,432** | +50.3% |

**Only 2 of 3 days were green, and ~69% of the entire profit came from one day
(06-16).** This matches the original study's warning ("51% of markets net
positive; top-10 markets ≈ half the PnL"). The headline return is driven by a
handful of windows, not a steady drip.

---

## 2. Current-strategy tests (June-12 study data, re-run)

The historical backtests still support the core thesis:

- **Late snipe (5m+15m, τ≤60, edge≥0.15): +55.5%/$1 [+34.6, +75.9]**, robust to
  either fee formula (`fee_min` gives +54.6%).
- **Dual-β robust gate** lifts edge from +15.95c/share to **+19.22c/share** at
  edge≥0.15 — the regime insurance still pays.
- **MM leg:** still structurally −EV; correctly disabled.
- **Scalp:** ~breakeven on the current-ish config (−0.18%/$1), modestly +EV only
  on 15m/1h. The live session agrees (5 fills, +$3, immaterial).

The snipe is the whole business. Everything else is rounding error.

---

## 3. Calibration (model honesty, 300k predictions / 1,842 markets)

- **Brier 0.1666 blended vs 0.1686 raw** — the market blend adds value, model is
  reasonably honest.
- **But mild overconfidence in the 0.70–0.90 band** (predicted 0.749 → realized
  0.702; 0.849 → 0.827). That is *exactly* the favorite band the sniper buys
  (`min_ask 0.50`, `max_ask 0.80`). The model slightly overstates edge on the
  priciest favorites it trades.

---

## 4. Hypothesis tests on the live fills (counterfactual filters)

All run on the same 202 live snipe fills, market-clustered bootstrap CIs:

| hypothesis | n | win% | ret/$1 | CI | call |
|---|---|---|---|---|---|
| **current** | 202 | 67% | +27.7% | [+14.6, +40.2] | baseline |
| **H1 edge ≤ 0.20** | 189 | 69% | **+31.5%** | [+18.1, +43.9] | **adopt** |
| H1 edge 0.20–0.25 band | 13 | 31% | −37.1% | [−101.7, +29.3] | toxic (small n) |
| H2 5m only | 169 | 63% | +22.3% | [+7.3, +36.8] | keep, weakest |
| H2 15m+1h only | 33 | 88% | +56.1% | [+34.7, +72.7] | best, low volume |
| H3 ask ≤ 0.65 | 180 | 67% | +31.9% | [+17.8, +45.4] | minor win |
| H3 ask 0.70–0.80 band | 9 | 78% | +4.4% | [−35.6, +32.0] | marginal |
| H4 cost ≥ $10 | 165 | 70% | +28.6% | [+15.3, +41.1] | adopt floor |
| H4 cost < $10 | 37 | 54% | −5.3% | [−41.0, +31.5] | noise |
| **H5 combined** (edge≤0.20, ask≤0.70, cost≥$10) | 147 | 71% | **+33.8%** | [+20.0, +46.8] | **best CI** |

**Read-out:** the current config leaves money on the table at both extremes of
its own gates. The high-edge (0.20–0.25) band lost again — consistent with the
"winner's-curse" rationale that reinstated `max_edge`; the live data says the
veto should be **0.20, not 0.25**. The 0.70–0.80 ask band and sub-$10 fills are
both near-zero / negative drag. The combined filter (H5) keeps 73% of the fills,
raises win rate to 71%, and tightens the CI lower bound to +20%.

### Suggested config tweaks (paper-test first)
```yaml
sniper:
  max_edge: 0.20        # was 0.25 — the 0.20-0.25 band lost live, twice
  max_ask: 0.70         # was 0.80 — 0.70-0.80 was +4.4% (fee-dead band)
  min_take_usd: 10      # (new) skip sub-$10 fills — pure noise, -5.3% live
```
These are *refinements*, not the reason to wait. Even the un-tweaked config is
positive.

---

## 5. Is this enough proof to go live? — No (with a clear path to yes)

**Why the paper result is encouraging:**
- Edge is positive and statistically significant (CI lower bound +14%).
- Robust to every sensible parameter variation tested.
- Independently corroborated by the 1,853-market historical study.
- MM correctly off; risk rails (kill switch, FAK monitor, equity scaling) active.

**Why it is not yet sufficient for real capital:**
1. **It's paper, and paper here is optimistic by construction.** The README is
   explicit: hidden/iceberg liquidity and queue-jumping cancels are not modeled;
   "discount paper PnL somewhat." With a 61% fill rate already, live capture will
   be a *fraction* of this. The honest latency-adjusted bound is +14c/share vs
   +16–19c — the edge survives, but realized PnL will be materially lower.
2. **Tiny, concentrated sample.** ~54 hours, 185 markets, **69% of profit from a
   single day**, one June vol regime. This is nowhere near enough to bound the
   downside of a fat-tailed strategy.
3. **Two unverified live-only risks:**
   - **Fee formula** — docs say `0.07·p(1−p)`, a third-party live fill implied
     `0.07·min(p,1−p)` (≈2× at mid-prices, exactly where we trade). Unresolved;
     must be checked on the first live fill (`FEE CHECK` log line).
   - **Chainlink basis** — 5m/15m settle on Chainlink, the bot prices/settles on
     Binance. **83% of live volume was 5m.** Paper cannot see this risk; close
     calls will settle differently live.
4. **Model overconfidence in the 0.7–0.9 favorite band** it actually trades.
5. **Drawdown already hit 16% of peak** in a *winning* session — variance is large
   relative to the kill-switch floor.

### Recommended path to "yes"
1. Apply the H1/H4 tweaks (`max_edge 0.20`, `min_take_usd $10`); optionally
   `max_ask 0.70`. Paper-trade 3–5 more days to grow the sample past one regime
   and confirm the tweaks hold.
2. **Then run a small capped LIVE pilot** — e.g. `max_take_usd $20–25`,
   `max_position_usd $50`, exposure cap $100, kill switch tight — for the sole
   purpose of validating: (a) the fee formula, (b) real FAK capture rate vs
   paper, (c) Chainlink-vs-Binance settlement on 5m. Compare realized vs paper
   per-leg attribution.
3. Re-run `research/collect.py` + the test suite (edge decays with competition).
4. Scale only after live capture and fees match the paper model within tolerance
   over a few hundred fills.

**Bottom line:** the strategy looks genuinely +EV and the engineering/risk
controls are sound, but going live now would be sizing real money on a 2-day
optimistic-fill paper sample with two unpriced live risks. Validate cheaply with
a capped live pilot first; the data so far justifies *that* step, not full
deployment.
