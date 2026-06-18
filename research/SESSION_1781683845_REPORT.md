# Session 1781683845 — Forensic Report

**Window:** 2026-06-17 18:19 → 2026-06-18 20:14 (26.0 h paper run)
**Capital:** start $1,000 · realized cash end **$1,418.68 (+41.9%)** · mark-to-market equity end **$1,660 (+66%)**
**Reproduce:** `python3 research/analyze_session_1781683845.py`

---

## 1. Executive summary

- The session was **+EV**, driven **entirely by the sniper leg** (favorite-side, book-lags-Binance lag capture). MM is disabled; the scalper was effectively dormant (2 fills, +$2).
- The regime was **low-volatility, mildly trending down, and lead-sticky (momentum-favorable)** — the sniper's ideal habitat.
- The edge is real but **single-session settled-dollar P&L is too noisy to prove significance**; the high-power evidence is the **post-fill markout (+7.3c @10s, n=114)**.
- The biggest free money is **structural, not directional**: ~44% of capital was spent on coin-flip 0.50 favorites that returned ~zero, and position size was scaled by equity rather than by edge.

---

## 2. P&L reconciliation

| Metric | Value |
|---|---|
| Fills | 124 (121 joined to a settlement, 3 still open at end) |
| Settled P&L | **+$576.37** on $6,198 taker cost (**+9.3%/$**) |
| Realized cash | +$418.68 (snipe +$416.53, scalp +$2.15) |
| Open cost basis at end | $152.18 |
| Fees paid | **$176.10 (2.9% of notional)** |

Reconciliation: settled-P&L $576 − $152 open ≈ **+$424 realized cash ≈ summary +$418.68**. Books tie out.

---

## 3. Regime identification

```
spot   open 65,406 → close 64,008    net −2.14%    intraday range 417 bps
1m realized vol  median 0.041% · mean 0.052% · p95 0.127%      → LOW vol
perp basis  −32 (stable, range −42…−24)
jump-guard trips  73 (mean 7.3 bps)                            → frequent small jumps
settlements  111: UP 59 / DOWN 52 (53/47)                      → no strong outcome skew
```

**Classification: low-vol, mild-downtrend, lead-sticky / momentum regime (high-β ≈ 1.36 side of `robust_betas`).**

The decisive tell is not the price path but that *the favorite-side sniper was profitable*. That leg only earns when Polymarket's book lags Binance **and the move continues**. In a mean-reversion regime the favorite fades and this leg bleeds. Confirming signals:

- **DN side +14.7%/$ vs UP +6.1%/$** — trend-aligned (down) bets carried the book.
- **Low background vol + frequent ~7 bps jumps** = a clean, followable lead signal.
- The lone mean-reverting pocket (**19:00 UTC: −$202, 44% win**) is the visible exception where the favorite whipsawed.

---

## 4. Is the strategy +EV? Evidence table

| Evidence | Reading | Strength |
|---|---|---|
| Snipe markout **+7.29c @10s (n=114)**, +9.45c @60s (n=95) | Mid drifts our way post-fill → structural lag capture | **Strong / high-power** |
| Win rate **67% [58–75%]** vs breakeven **~58.6%** | Above breakeven; lower CI ≈ breakeven | Positive, borderline-sig |
| EV/$ **+9.6%**, bootstrap 95% CI **[−6.7%, +26%]** | Straddles zero | Not sig from settled-$ alone |

**Verdict: genuinely +EV.** The settled-dollar CI includes zero only because 119 binary outcomes are inherently high-variance — a single session cannot establish significance. The markout is the clean, settlement-independent signal and is firmly positive, consistent with the prior 9-session / 307-fill forensics.

---

## 5. Where the edge lives — and where it leaks

### 5.1 By ask price (this session only — see WARNING below)

```
ask 0.50   n=51  win 57%  cost $2,636 (44% of capital)  +0.6%/$
ask 0.55   n=25  win 76%                                +18.0%/$
ask 0.60   n=15  win 67%                                 +9.4%/$
ask 0.65   n=12  win 75%                                +19.0%/$
ask 0.70   n=7   win 86%                                +21.1%/$
ask 0.75   n=6   win 83%                                +16.1%/$
```

In **this session**, the 0.50 bucket was flat (+0.6%/$, n=51) while 0.55–0.80 returned +9–21%/$.

> **⚠️ DO NOT ACT ON THE 0.50 READ ABOVE — it does not hold out-of-sample.**
> Tested across all 6 sessions (723 snipe fills, `research/test_min_ask.py`):
> the **0.50–0.54 bucket is the single most profitable zone**, +21.3%/$ on
> n=304 (CI [+8.7%, +33.5%]), vs 0.55+ at +12.4%/$. This session's flat 0.50
> bucket (n=51) was noise, overlapping the one mean-reverting hour. **Raising
> `min_ask` 0.50 → 0.55 would LOSE ~$2,639 of realized edge. Leave min_ask at 0.50.**
> The genuinely soft zone in aggregate is the 0.60–0.65 middle and 0.75+, but
> those CIs all include zero, so there is no clean case to cut them either.

### 5.2 By market kind

| Kind | n | EV/$ | P&L |
|---|---|---|---|
| 5m | 79 | **+18.2%** | +$695 |
| 15m | 35 | **−4.9%** | −$99 |
| 1h | 3 | −25.5% | −$33 |
| 4h | 2 | +61.2% | +$11 |

5m is the engine; **15m was a net drag** on both sides this session.

### 5.3 By size

| Bucket | n | EV/$ | mean edge |
|---|---|---|---|
| small (<$60) | 85 | **+15.3%** | 0.115 |
| big (≥$60) | 34 | **+1.8%** | 0.126 |

Big lots earned ~nothing at **the same edge** as small lots — sqrt-equity scaling is amplifying variance without amplifying EV. The largest lots (x220, x161, x157) are the biggest losers.

### 5.4 Winner's-curse cap leak

The single worst fill (**−$113.85**) was `15m UP @0.50, edge = 0.25` — an edge sitting *exactly at* `sniper.max_edge`. The 0.25+ edge bucket = −103%/$. Giant residual edges are stale/toxic; the cap should be strictly exclusive.

---

## 6. Execution quality

- **FAK fill rate 67% session / 87% over last 30**; 61 races lost; FAK monitor tripped tier 0→1 once early and self-recovered. Healthy.
- **Lost-race rejects had mean fair-drift +4.1c in our favor** — we systematically miss trades that immediately richen and keep the ones that go adverse (9 adverse fills, mean −2.6c). Realized EV is therefore a **floor**; the 410ms/250ms speed-bump caps it.
- Ops noise handled correctly: one Binance feed stall (5–8 s ~20:03), several CLOB ws reconnects, one Coinbase basis spike to −291 — all absorbed by guards / basis-adjustment with no directional damage.

---

## 7. Recommendations (ranked by conviction)

1. **~~Raise `sniper.min_ask` 0.50 → 0.55.~~ RETRACTED — do NOT do this.** The single-session read was noise; across all 6 sessions the 0.50–0.54 bucket is the *most* profitable zone (+21.3%/$, n=304). Keep `min_ask` at 0.50. (See §5.1 warning and `research/test_min_ask.py`.)
2. **Make `sniper.max_edge` strictly exclusive and down-size on edge ≥ 0.15.** Treat edge ≥ 0.20 as a *fade* signal, not a size-up signal.
3. **Divorce position size from equity; tie it to edge.** Cap per-trade size and let edge drive sizing. Cuts the −20% mark-to-market drawdown without touching the EV engine.
4. **Demote 15m for the sniper in this regime** (−$99). Consider 5m-only, or gate 15m behind a higher edge / momentum-alignment filter.
5. **Lean directional with the confirmed trend.** DN +14.7% vs UP +6.1%; weight the trend-aligned favorite harder and tighten the counter-trend side via `momentum_beta`.
6. **Latency reduction converts directly to P&L.** Anything shaving decide/submit time, or pre-positioning ahead of the uncancellable bump, recovers the +4.1c currently lost to richen-and-gone races.

---

## 8. Risk notes

- The **−20.4% max drawdown is mark-to-market on open inventory, not realized** — realized cash trended up steadily. Recommendation #3 tightens it further.
- No kill-switch events; exposure stayed within the $500 ceiling throughout.

---

## 9. Caveats

- **Single session, n = 119 settled snipe fills.** Sub-buckets (1h, 4h, hour-of-day, edge ≥ 0.15) have small n; treat their point estimates as directional, not conclusive.
- Paper settlement uses Binance klines as a proxy for Chainlink on 5m/15m/4h — near-50/50 closes may settle differently live (per `config.yaml` note).
- All recommendations should be confirmed out-of-sample across the full `logs/` set before going live.
