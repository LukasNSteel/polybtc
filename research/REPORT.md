# Polymarket BTC Up/Down — Data Study & Strategy Findings

**Date:** 2026-06-12
**Data:** 1,853 resolved markets (862×5m over 3d, 574×15m over 6d, 335×1h over 14d, 82×4h over 14d),
3.53M in-window taker executions ($47.4M notional), Binance 1s klines (4d) + 1m klines (15d),
plus the bot's own `calibration.csv` (200 markets) and session logs.

All EV numbers are **after the 7% × p(1−p) taker fee**. Confidence intervals are
95% market-cluster bootstraps (trades within a window are correlated, so we
resample whole markets). Every print in the tape is normalized to "a taker
bought side X at price q" (a SELL of Up at π is a buy of Down at 1−π — the
books are mirrored), so each print is an executable taker price.

Scripts: `collect.py` → `prep_dataset.py` → `test_taker_ev.py`,
`test_signals.py`, `test_mm_sim.py`, `test_strategy_candidates.py`,
`test_snipe_latency.py`. Raw outputs in `research/data/out_*.txt`.

---

## Headline findings

### 1. The late-window snipe is the only large, robust edge — and it's bigger than the bot currently allows

Replicating the bot's exact fair-value model (Gaussian mixture + momentum
drift, no lookahead: Binance state at t−1s) and bucketing every real taker
execution by model edge:

| model edge (after fee) | 5m, τ≤60s | 15m, τ≤60s |
|---|---|---|
| −0.15 .. −0.08 | −6.5c* | −5.1c |
| −0.03 .. +0.03 | +0.2c | −0.1c |
| +0.08 .. +0.15 | **+5.9c*** | **+6.2c*** |
| +0.15 .. +0.25 | **+12.1c*** | +6.7c |
| **> +0.25** | **+18.1c*** | **+33.3c*** |

Monotonic, significant, and the `edge > 0.25` bucket — which the current
sniper **vetoes** (`max_edge: 0.25`, "too good to be true") — is the most
profitable bucket in the entire study. The veto is discarding the best trades.

As a portfolio (take every qualifying print, $100 cap/market, hold to settle):

- **5m+15m, τ≤60s, edge≥0.15: +55% per $1 staked [CI +36, +78], positive 6 of 7 days, 492 markets.**
- edge≥0.08 variant: +35% [+18, +54] on more volume.

Latency robustness (the worry: these prints were won by faster bots):
- Prints where the *same opportunity had already been hit 1–5s earlier* (i.e.
  you only get to be the **second** taker): still **+14.1c/share** [+8.3, +20.2].
- Prints where the edge was still there 3s later: +15.7c. 94% of qualifying
  prints have follow-on prints within 2s. A 350ms bot captures most of this.
- Positive in every 4h UTC block; best during US hours (12–20 UTC: +16–28c).
- Caveat: high variance — only 51% of markets are net positive; the top-10
  markets are ~49% of profits. The edge is a fat right tail, not a steady drip.
  Size for ~50 concurrent losers, not for smooth PnL.

Capacity in the bucket: median ~$650 of printed notional per qualifying 5m
market (mean $1.3k), ~$200–400 on 15m. Plenty relative to current caps.

### 2. The market-maker leg is structurally −EV. Turn it off.

Simulated passive bids on both sides at δ = 2..8c below the side price,
repriced every grid step, fills detected from actual sweeps (conservative:
price traded *through* the level):

| δ below price | 5m pnl/fill | 15m | 1h | 4h |
|---|---|---|---|---|
| 2c | −2.2c* | −2.4c* | −2.4c* | −2.3c* |
| 4c | −2.3c* | −2.6c* | −1.9c* | −2.9c* |
| 8c | −2.3c* | −2.2c* | −2.9c* | −2.8c* |

**Loss per fill is ~−2 to −3c at every depth, every market kind.** Quoting
deeper doesn't help: you only get filled when the move is big enough to reach
you, and it keeps going. The maker rebate (~20% of taker fee ≈ 0.3c/share at
mid prices) cannot close a 2–3c gap. The bot's own live sessions agree: MM leg
−$173 realized, 251 losing-side vs 211 winning-side fills, −3 to −5c markouts.
This is a market where the marginal taker is a Binance-watching bot; resting
symmetric quotes is donating to them.

(Average markouts after taker prints are ≈0c — flow is *not* toxic on average.
It's toxic *conditional on reaching a resting quote*. That's exactly adverse
selection, and it's why MM loses while overall taker flow roughly breaks even.)

### 3. Scalper: −EV on 5m, mildly +EV on 1h/15m

Late-window high-price buys (per $1 staked, after fees):

- **5m: negative.** τ 30–60s, q 0.97–0.99: **−4.6%*** ; nothing significantly
  positive anywhere in the 5m scalp grid. The current config scalps 5m hardest
  (most windows). Strategy backtest of ~current config: −0.2%/$1 ≈ breakeven
  at best.
- **15m: τ 5–30s, q 0.90–0.97: +3 to +6%***.
- **1h: τ 15–60s, q 0.90–0.99: +0.6 to +7%***; 14 of 15 days positive
  (+2.2%/$1 overall).
- q ≥ 0.99 is dead everywhere after fees (+0.3–0.6% at best, the fee eats it).
- Never buy the cheap side late on 1h (q<0.10, τ<60: −98.5%*** — it wins 0.5%
  of the time).

### 4. The model adds no value at steady state — only in dislocations

Walk-forward logistic regressions on per-(market, τ) snapshots: adding
model_p / momentum / 60s taker-flow to the market price improves out-of-sample
logloss by ≈0.000–0.002 (nothing) on 5m/15m/1h. The market price is efficient
at rest. All the model's value is concentrated in the moments the book lags a
fast Binance move — which is the snipe, and is why fattening the snipe and
killing the passive legs is the right shape. The bot's own calibration log
says the same thing: blended price beats raw model in 17 of 19 (kind × τ)
buckets, and when model and market disagreed at 5s sampling, the *market* side
of the disagreement was right (e.g. 15m model>mkt: model 0.39, market 0.36,
empirical 0.08).

Taker flow (60s imbalance) has no predictive value beyond price on any kind —
don't bother with flow-following.

### 5. Favorite–longshot bias exists but is too small to trade

Early-window favorites (q 0.90–0.98, τ ≥ 40% of window) are slightly
underpriced: +0.1%/+0.8%/+1.3% per $1 on 5m/15m/1h — right sign every kind,
but CIs straddle zero after fees at realistic caps. Longshots (q 0.2–0.4 early)
are reliably overpriced (−4.3c* on 5m) — worth knowing as a *don't-buy* rule,
which the snipe edge filter already enforces.

---

## Recommended config changes

```yaml
market_maker:
  enabled: false        # structurally -EV at every depth tested (Test 6)

sniper:
  min_edge: 0.10        # keep a little stricter than 0.08 for safety margin
  max_edge: 1.0         # REMOVE the too-good-to-be-true veto (edge>0.25 is the
                        # best bucket: +18c/5m, +33c/15m per share). The deep-tail
                        # protection belongs to min_ask, keep that at 0.05.
  max_take_usd: 100     # capacity is there (median $650/market in-bucket);
  late_boost: true      # concept: allow up to 2x size when tau<=60 and edge>=0.15
  # also: drop the same-side inventory fraction tie to the (now disabled) MM cap;
  # cap snipe inventory per market at ~$150 outright.

scalper:
  # restrict to where it's actually +EV:
  kinds: [15m, 1h]      # disable on 5m (-1.7%/$1) and 4h (no sample)
  window_sec: 60        # 1h: act in final 15-60s; skip the final <15s on 1h
  min_tau_sec: 15       #   (Binance close-print risk), 15m can go to ~5s
  min_price: 0.90
  max_price: 0.99       # 0.99+ is fee-dead everywhere
  min_prob: 0.997       # unchanged

risk:
  # snipe variance is fat-tailed: 51% of markets net-positive, top-10 markets
  # ~half the pnl. Keep kill switch generous or you'll stop out of variance:
  kill_switch_loss_usd: 300
```

Trading-loop implications beyond config:
1. With MM off, the guards (fade/breaker/jump) become snipe-irrelevant; jump
   guard should NOT block the sniper (it already doesn't — keep it that way).
2. Hold-to-settlement stays correct for 5m/15m snipes (τ≤90s). No exit logic needed.
3. If you want passive exposure at all, the only defensible form is
   *one-sided resting bids on the side the model already favors by ≥5c*
   (passive snipe: collect spread + rebate while expressing the same signal).
   Untested here — paper-trade it separately before risking money.

## Addendum (cross-validation against a third-party 5m bot study)

We obtained logs from another team's independent 5m study (207 markets,
June 8–9, order-book snapshots + Binance agg-trades). Tested their claims on
our 1,853-market dataset (`test_dual_beta.py`):

1. **Fee formula discrepancy — flagged, not resolved.** Their live fill implies
   `fee = 0.07 × min(p, 1−p)` (old formula); current official docs and the
   Gamma feeSchedule say `0.07 × p(1−p)` (what our bot uses). Stress test:
   the late snipe survives either way (+54.6% vs +55.5% per $1 at edge≥0.15).
   **Action: verify against our first live fill;** if min() is real, fix
   `Market.taker_fee_per_share` — at mid prices the bot would be understating
   fees ~2x and overstating sniper net edge by up to 1.75c.
2. **β regime-dependence — confirmed, almost exactly.** Fitting
   P(up)=Φ(β·d) per day on our snapshots: June 6–8 fits β≈1.23–1.36
   (momentum regime — Binance leads stick), June 9–12 fits β≈0.81–0.91
   (mean-reversion regime). Their fitted pair (0.83 / 1.36) matches our range.
   A fixed model is mis-calibrated roughly half the days.
3. **Their dual-β robust gate improves the snipe.** Requiring the edge to
   clear the threshold under BOTH β=0.83 and β=1.36 (take min of the two
   model probabilities): +19.2c/share vs +16.0c at edge≥0.15 (5m+15m, τ≤60s),
   at ~60% of the volume. Vetoed prints still average +10.7c, so the gate
   costs some profit in total prints — but with per-market caps the volume
   loss matters less than the regime insurance. Recommended for live:
   **edge_robust ≥ 0.10** (+16.7c/share, 385 markets) as the entry gate.
4. Their negative results (naive buy-favorites vanishes under market-level
   clustering; momentum-following loses) independently match Tests 1/4/B.

## Caveats

- Sample: 3–14 days depending on kind, one BTC regime (vol ~0.05–0.15%/min).
  Re-run `collect.py` + the test suite weekly; the snipe edge depends on the
  competitive landscape and can decay.
- The edge buckets are computed from *prints that occurred* — we assume we can
  take the same liquidity. The P1 second-taker test (+14c) is the honest
  bound; the true capture is between P1 and the full +16c.
- 5m/15m/4h resolve on Chainlink, model uses Binance: near-tie windows carry
  reference-price risk that this study (which uses actual resolutions) prices
  in correctly on average, but individual close calls will sting.
- Trades API caps history at 3,500 prints/market: busiest 1h/4h windows lose
  early-window prints (late-window analysis unaffected; that's where all
  recommendations live).
