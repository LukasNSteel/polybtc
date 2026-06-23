# polybtc

A Polymarket bot for BTC "Up or Down" candle markets (5-minute, 15-minute,
hourly and 4-hour), originally reverse-engineered from wallet
`0xe1d6...907c` ("Idolized-Scallops"), then rebuilt around a June 2026 data
study of 1,853 resolved markets / 3.5M taker executions
(**`research/REPORT.md`** — read it before changing strategy parameters).
It computes the fair probability of Up/Down in real time from the Binance
price feed and trades the gap between fair value and the Polymarket order
book.

## Strategy legs

1. **Stale-quote sniping — the primary leg, favorite-side only.** When BTC
   moves and resting Polymarket asks lag, buy an ask priced ≥ `min_edge`
   below fair value — but **only on the side the market already prices as
   the favorite** (`min_ask: 0.50` ≤ ask ≤ `max_ask: 0.80`). Entries are
   gated on the **dual-β robust edge**: the trade must clear the threshold
   under both fitted market regimes (β ≈ 0.83 mean-reversion days, β ≈ 1.36
   momentum days). Live forensics on 307 settled paper fills (June 12,
   `research/analyze_sessions.py`) found the original backtest split
   cleanly in two: favorite-side buys won 73% (+15%/$1, positive on every
   market kind and both sides), while underdog buys — betting the model's
   sign-flip against the book — won 18% and produced essentially all of a
   −$1,356 drawdown. Where the model and market disagree on *sign*, the
   market wins; the bot now trades repricing lag in magnitude only. The
   `max_edge: 0.25` "too good to be true" veto is back on as a
   winner's-curse guard: an enormous edge still resting 350ms after a move
   is one the faster bots passed on (our edge ≥ 0.15 fills went 0-for-5).
2. **End-of-window scalping — 15m and 1h markets only.** In the final
   15–60 seconds, when the outcome is nearly certain (fair ≥ 99.7%), buy at
   0.90–0.99. Tested +2.2%/$1 on 1h (14 of 15 days green) and mildly
   positive on 15m; **negative on 5m**, which is excluded, as is anything
   above 0.99 (the fee eats it).
3. **Fair-value market making — DISABLED by default.** Simulated passive
   bids lose 2–3c per fill at *every* quoting depth (2–8c) on every market
   kind: fills only happen when the move reaches the quote and keeps going,
   and the ~0.3c maker rebate can't close that gap. Live sessions agreed
   (−$173, 54% of fills on the losing side). The leg and its guards remain
   in the code if you want to experiment — re-enable at your own risk.

## Fair value model

Base: log-price Brownian motion, `P(up) = Φ( ln(S/O) / (σ√τ) )` where `S` =
live spot (top-of-book Binance mid, falling back to last trade), `O` = candle
open, `σ` = realized vol, `τ` = time to close. Ties resolve Up on Polymarket,
matching `close >= open`. Refinements that matter for P&L:

- **Fat tails** — returns are modeled as a two-regime Gaussian mixture
  (`tail_weight`, `tail_scale`), because a pure Gaussian is badly
  overconfident in the tails: it sees "cheap" deep-OTM contracts everywhere
  and buys lottery tickets that lose.
- **Market blending** — final fair value is a weighted blend of the model and
  the Polymarket book's own mid (`blend_model_weight`). The book aggregates
  information we don't have; the model's weight rises toward 0.95 in the
  final minute where distance/vol math is genuinely sharper.
- **Dual-horizon vol** — fast (60s) and slow (10min) EWMAs of 1s returns;
  the larger of the two is used, so quotes widen immediately when a vol
  burst hits.
- **Perp-led spot estimate** — price discovery in BTC happens on Binance
  perpetual futures, which lead spot by milliseconds-to-seconds in fast
  moves. The bot streams the perp top-of-book alongside spot, tracks the
  rolling perp–spot basis, and uses `perp mid − basis` as its spot estimate
  whenever the perp print is fresher (`perp_lead` in config). Markets still
  resolve on spot/Chainlink, so the perp is only a faster *estimator of
  spot*, never the fair-value anchor. If the perp feed drops (or is
  geo-blocked), the bot falls back to spot transparently.
- **Dual-β robust bounds** (`prob_up_bounds`) — fitting `P(up) = Φ(β·d)`
  per day shows the lead-stickiness β flips between ~0.8–0.9
  (mean-reversion regime) and ~1.2–1.4 (momentum regime) on a multi-day
  timescale. The sniper gates on the *worst-case* probability across both
  (`sniper.robust_betas`), which raised backtested edge from +16.0 to
  +19.2c/share by discarding exactly the regime-dependent trades.

The sniper sizes positions by conviction (edge ÷ 2×min_edge, capped),
only buys asks inside `[min_ask, max_ask]` = [0.50, 0.80] (the favorite
band — below it you're fighting the market's sign, above it the fee eats
the edge), and caps per-market cost at `sniper.max_position_usd`. With the
favorite-only gate the live win rate runs ~75%, but sizing should still
respect variance — losers cluster on whipsaw windows.

**Order types**: only MM quotes rest on the book (GTC). Sniper and scalper
orders go out **fill-and-kill (FAK)** — take whatever is available at the
target price, kill the rest. A partially-filled taker order must never be
left resting as exactly the kind of stale quote this bot snipes in others.

### MM adverse-selection guards (dormant while MM is disabled)

A fair-value MM in fast crypto markets has one dominant failure mode: its
resting bids *are* the stale quotes during a BTC jump, picked off by bots
exactly like our own sniper leg in the gap before the next cancel/replace
cycle. The June 2026 study concluded these defenses don't close the gap —
the leg ships disabled — but they remain wired in for experiments. Three
layered defenses (`guards` in config), all MM-only — the sniper and
scalper keep running, since a jump is exactly their moment:

- **Jump guard** — when BTC moves more than `jump_sigma` standard
  deviations (floored at `jump_min_move_bps`) within a few seconds, all MM
  quotes are pulled immediately and re-quoting pauses for a cooldown. The
  EWMA vol estimate lags a burst, so mid-jump is when the model is most
  overconfident.
- **Quote fading** — each MM fill on a side pushes that side's next quote
  further from fair value (escalating, capped, decaying window), so one
  informed trader can't clear us out repeatedly at a stale level.
- **Same-side fill breaker** — too many fills on one side in a short window
  stops quoting that side entirely for a cooldown.

**Markout tracking** measures whether the defenses work: every fill's
post-fill book-mid drift is recorded at 10s/60s horizons and reported per
leg in the session summary. Healthy MM markouts hover near zero or
positive; persistently negative means informed flow is eating the quotes —
widen `market_maker.edge`. Sniper markouts should be strongly positive.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# paper trading (default): live Binance + Polymarket data, simulated fills
python -m bot.main

# live trading: real orders on the CLOB
# put your creds in a .env file (gitignored, loaded automatically):
#   POLYMARKET_PRIVATE_KEY=0x...   # wallet private key
#   POLYMARKET_FUNDER=0x...        # your Polymarket proxy wallet (profile address)
# (or export them as environment variables instead)
python -m bot.main --live

# dashboard: dark-mode web UI over session logs / state.json, auto-refreshes
python -m dashboard.server            # -> http://127.0.0.1:8787
```

The dashboard (plain aiohttp + a single static page, no build step) shows
equity/exposure curves, cumulative settled P&L, per-leg and per-market-kind
attribution, open positions with expiry countdowns, a settlement P&L
histogram, and a live activity feed — with a session selector for older
logs.

Tune sizes, edges, and risk limits in `config.yaml`. The kill switch cancels
everything and halts if session equity drops more than
`risk.kill_switch_loss_usd`.

Cash and open positions persist to `state.json` across restarts (positions
whose windows closed while the bot was down settle on startup). Pass `--fresh`
to start clean.

### Calibration

Every 5 seconds the bot logs its fair-value predictions to
`logs/calibration.csv`, along with each market's eventual outcome. After a few
hours of paper trading, check how honest the model's probabilities are:

```bash
python research/calibrate.py
```

If realized frequencies are systematically less extreme than predicted
(e.g. predictions of 0.95 only come true 85% of the time), the vol estimate is
too low — raise `fair_value.min_vol_per_sec` or shorten `vol_halflife_sec`,
and widen sniper/scalper edges until calibration improves.

### Research pipeline

The strategy parameters come from a reproducible study in `research/`
(findings: **`research/REPORT.md`**). Re-run it periodically — the snipe
edge depends on the competitive landscape and can decay:

```bash
python research/collect.py            # ~30 min: resolved markets, trade tapes,
                                      #   Binance 1s/1m klines -> research/data/
python research/prep_dataset.py       # replicate the model per trade, build features
python research/test_taker_ev.py      # calibration + sniper edge + scalp surfaces
python research/test_mm_sim.py        # passive-quoting simulation
python research/test_signals.py       # walk-forward signal value, markouts
python research/test_strategy_candidates.py   # strategy portfolios w/ daily breakdown
python research/test_snipe_latency.py # second-taker latency haircut
python research/test_dual_beta.py     # fee stress, daily beta fit, robust gate
```

### P&L attribution and safety rails

Every fill is tagged with its strategy leg (`mm` / `snipe` / `scalp`); the
session summary printed on exit (and on kill-switch) shows realized cash flow
per leg, maker vs taker volume, and fees paid — so you know *which leg* makes
money before scaling it. If the Binance feed stalls for more than
`risk.max_feed_age_sec`, all quotes are pulled immediately (never quote
blind); the sniper and scalper also refuse to act on order books older
than 15s.

### Live fill tracking

Live mode streams your own orders and trades over the CLOB **user websocket**
(`/ws/user`), so both taker fills *and* maker fills of resting quotes are
captured in real time, including partial fills and cancellations. Taker fills
are charged the dynamic fee in the bot's accounting.

## Polymarket microstructure (researched June 2026)

- **Taker fees on crypto markets**: `fee = shares × 0.07 × p × (1−p)` USDC,
  peaking at 1.75¢/share at 50¢ and ~0 near the extremes. Makers pay nothing
  and earn ~20% of collected fees as daily rebates. The bot parses each
  market's `feeSchedule`, charges taker fees in the paper simulator, and the
  sniper/scalper only act when the edge clears the fee.
  **Resolved (2026-06-22, live `--fire` smoke test):** a real ~$2.90 taker
  fill (5.18 sh @ 0.56 on an hourly BTC market) was charged a ground-truth fee
  of **$0.0893 = 0.0172/share**, measured as the actual collateral balance
  delta minus fill cost. That matches `0.07 × p(1−p)` exactly (predicted
  0.0172/sh) and **refutes** the third-party `0.07 × min(p, 1−p)` claim (would
  be 0.0308/sh, ≈1.8× higher) and the quadratic `(p(1−p))²` variant (the live
  `feeSchedule` reports `exponent: 1`). The bot's model and
  `fees.assume_taker_rate: null` (trust the advertised schedule) are correct;
  no change to `Market.taker_fee_per_share` needed. The live executor still
  logs a `FEE CHECK` line on every taker fill so any future schedule change is
  caught. The strategy stays +EV under either formula regardless
  (stress-tested in `test_dual_beta.py`).
- **Speed bump removed** (Feb 18, 2026): the old taker order delay on crypto
  markets is gone (`seconds_delay: 0` on the CLOB). This cuts both ways — our
  takes land instantly, but our resting maker quotes can be picked off
  instantly too. The strategy loop runs at 250ms cancel/replace.
- **Pre-expiry trading halts**: empirically tested (June 2026) by polling
  live markets through expiry — both 5-minute and hourly BTC markets accepted
  orders all the way to the close; no 2-minute halt was observed. The bot
  still tracks each market's live `accepting_orders` flag from the CLOB every
  5s (so any future halt is honored automatically) and supports per-kind
  safety cutoffs via `trading.cutoff_sec_*`.
- **On-chain merge/redeem**: complete Up+Down sets are merged into pUSD via
  Polymarket's `CtfCollateralAdapter`
  (`0xAdA100Db00Ca00073811820692005400218FcE1f`), and resolved positions are
  redeemed the same way (waits for the oracle's payout vector before
  sending). This requires `signature_type: 0` — your EOA holds the positions
  directly. Email/browser-wallet accounts keep positions in a proxy contract;
  merge those via the Polymarket UI.

## Caveats — read before going live

- **Competition/latency.** The qualifying prints in the study were taken by
  *someone* — usually a fast bot. The honest latency-adjusted bound on the
  snipe edge (being the second taker, 1–5s late) is +14c/share vs +16c for
  instant capture, so the edge survives realistic latency, but expect the
  realized capture rate to be a fraction of the backtest's.
- **The study sample is one regime.** 3–14 days depending on market kind,
  June 2026 vol conditions. Re-run the research pipeline weekly and after
  any structural change (fee schedule, speed bump, new competitors).
- **Resolution sources differ.** 5- and 15-minute markets resolve on
  **Chainlink BTC/USD**; hourly markets resolve on the **Binance BTC/USDT 1h
  candle**. The bot uses Binance as fair-value proxy for all — fine for
  cents-wide edges, but borderline end-of-window scalps on 5m/15m markets
  carry basis risk.
- **Paper fills model queue position and the itode speed bump but are still
  approximate.** A resting bid only fills after the size displayed ahead of it
  at that level is consumed; trades through the level fill fully. Taker
  (snipe/scalp) orders model Polymarket's speed bump, not a cancellable race:
  the order is committed for the full tick-to-trade budget
  (`paper.taker_latency_ms`, default 400ms — the max identified latency), whose
  last `paper.speed_bump_ms` (250ms) is a **frozen, uncancellable hold**. We
  re-validate against the book only at the *end* of the hold — if the side
  richened (BTC moved our way) the cheap quote is gone and the FAK is rejected;
  if it cheapened (BTC moved against us) we are committed and filled anyway, an
  **adverse-selection** fill the old race model was blind to. There is no
  cancel escape during the hold (a jump-guard pull on a committed order is
  ignored). Resting GTC quotes are still genuinely cancellable
  (`paper.cancel_latency_ms`). The session summary reports the FAK fill rate
  and the adverse-fill count/cost. Hidden/iceberg flow and cancellations ahead
  of us still aren't modeled — discount paper PnL somewhat.
- **Auto merge/redeem needs an EOA wallet** (`signature_type: 0`); proxy
  accounts must merge/redeem via the Polymarket UI. Paper mode settles
  automatically either way.
- **Jurisdiction.** Polymarket geo-blocks trading in some regions (incl. US).
- `research/` contains the scripts and raw data used to reverse-engineer the
  source account.
