# Book freshness & the "manual bet wakes the bot" effect

_2026-06-27_

## TL;DR
When a manual bet is placed from the phone, the bot suddenly trades the same
market. We traced it: it is **not** self-trading (verified — 0 self-matches in
200 trades). It's that the manual order generates the **order-book updates the
bot needs to consider its book "fresh,"** which unblocks its freshness gate. The
same insight gives a clean, legitimate way to make the bot self-sufficient, now
implemented as an optional book self-refresh.

## The mechanism (confirmed in code)
The sniper only fires on a book younger than `sniper.max_book_age_sec` (now
**1s**). The book timestamp (`book.ts`) is updated **only** when the CLOB ws
pushes a `book` / `price_change` event, and those are only broadcast when the
book actually changes (`bot/orderbook.py::_handle`). There is **no periodic
refresh** of `book.ts`.

- **Quiet market** -> no ws updates -> `book.ts` ages out -> freshness gate fails
  -> **bot idle.**
- **Manual $1 bet** -> order emits `price_change` + `last_trade` -> `book.ts =
  now` -> gate passes -> **bot engages** (bonus: the order also nudges the quote,
  sometimes creating the edge it snipes).

## Why it matters
This is the same root cause behind the **low fill rate**: in quiet 5-minute
windows the bot is starved of book-update flow and gates itself off. It isn't
under-trading because the rules are too strict — it's asleep.

Verified non-issue: the bot and the phone share one Polymarket wallet
(`0x4e79…`, signature_type 3), so self-trading was the first thing to rule out.
Checked via shared `transactionHash` and same-market/same-timestamp self-cross
across 200 trades -> **0 self-matches.** No wash trades, no hidden fees.

## How we use it to our advantage

1. **Self-prime the book (implemented).** A new optional loop
   (`LiveExecutor.run_book_refresh`) pulls a fresh REST snapshot for active
   tokens whose ws book has gone **stale** and stamps it current, so the bot can
   act on genuinely-fresh-but-quiet books **without** relaxing the freshness
   gate. A REST snapshot of a *quiet* token is genuinely current, so this is
   *real* freshness, not a fake — the in-flight adverse-selection risk is
   separate and already handled by the distance/edge buffer + feed-lag re-check.
   - Off the latency-critical path (shared `to_thread` pool, never `_submit_pool`).
   - **Stale-only** (never clobbers a fresher ws book).
   - **Capped per cycle** (`book_refresh_max_per_cycle`) to bound REST / rate-limit load.
   - Config: `book_refresh_sec` (set `<= max_book_age_sec` to keep books inside
     the gate; `0` disables).

2. **Honest limit — refreshing != manufacturing fills.** A manual bet does two
   things: refreshes the timestamp **and** adds/moves liquidity. Self-refresh
   only does the first, so it widens the bot's *consideration set* and catches
   fresh-but-quiet liquidity; it cannot conjure fills where no size rests
   (the "no orders to match" case remains). Expect more coverage, not infinite
   fills — and note the structural ceiling is Polymarket's ~250–650ms server-side
   order latency, which no book change fixes.

3. **Treat book-update flow as a free liquidity signal.** Markets that are
   actively updating have real two-sided interest — where a FAK can land. Future
   work could rank/prioritise markets by recent update rate.

## What NOT to do
- Don't loosen `max_book_age_sec` to trade more — that re-imports the stale-book
  adverse selection we tightened against on 2026-06-27.
- Don't rely on manual bets to trigger the bot — it's the shared wallet, it
  muddies the scorecard, and it isn't repeatable. Avoid manual bets on the bot's
  eval markets so the gated sample stays clean.

## Status
- `bot/orderbook.py` — `apply_rest_book()` snapshot applier.
- `bot/execution.py` — `run_book_refresh()` stale-only, capped, off the fire path.
- `bot/main.py` — wired in the live branch (disabled unless `book_refresh_sec>0`).
- `config.live165.yaml` — `book_refresh_sec: 1.0`, `book_refresh_max_per_cycle: 8`.
- `research/test_book_refresh.py` — unit tests (parse, stale-only, cap, disabled).
- **Watch after enabling:** clob rate-limit warnings, and that taker latency
  (`call_ms` in `shadow_taker.jsonl`) is unchanged (the refresh must not contend
  with the submit path).
