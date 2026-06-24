"""Shadow latency / fill-capture logger for the live taker path (ROADMAP P0.2).

Purpose
-------
The paper sim's taker P&L is downstream of four numbers that are currently
*guesses*: `taker_latency_ms` (420), `speed_bump_ms` (250), `capture` (0.30) and
`race_loss_prob` (0.20). They encode how badly the snipe is **adversely
selected** — i.e. how much of the time our marketable FAK only fills because the
book has already moved against us (the favourable prints get away as costless
misses). Nothing measures that on real fills with real latency.

This logger does. For every live taker (snipe/scalp) FAK it records, as
append-only JSONL:
  * what we SAW at submit  — best ask px/size, mid, and how stale our book was;
  * the submit->ack LATENCY (the controllable order-out leg);
  * what we GOT            — filled shares, avg fill px, status;
  * CAPTURE               — filled / displayed-ask-size;
  * SLIPPAGE              — avg fill px - the ask we saw (book move in-flight);
  * post-fill MARKOUTS    — token-mid drift at a few horizons (the direct
                            adverse-selection signal: persistently negative
                            drift vs our fill = we are being picked off).

It is pure observation: every call is wrapped so a logging failure can never
affect an order, and it touches no trading state. Analyse the JSONL offline to
replace the assumed capture/latency/race knobs with measured ones.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import time
from typing import Any

log = logging.getLogger("shadow")


class ShadowTakerLogger:
    def __init__(self, feed, log_dir: str = "logs",
                 markout_horizons_sec: tuple[float, ...] = (2.0, 10.0),
                 filename: str = "shadow_taker.jsonl") -> None:
        self.feed = feed
        self.markout_horizons = tuple(float(h) for h in markout_horizons_sec)
        os.makedirs(log_dir, exist_ok=True)
        self.path = os.path.join(log_dir, filename)
        self._ids = itertools.count(1)
        log.info("shadow taker logger active -> %s (markouts at %s s)",
                 self.path, ", ".join(f"{h:g}" for h in self.markout_horizons))

    # ---------- book snapshot ----------

    def _snapshot(self, token: str) -> dict[str, Any]:
        book = self.feed.books.get(token) if self.feed else None
        if not book:
            return {"ask_px": None, "ask_sz": None, "bid_px": None,
                    "mid": None, "book_age_ms": None}
        ba, bb = book.best_ask(), book.best_bid()
        mid = None
        if ba and bb:
            mid = round((ba[0] + bb[0]) / 2, 5)
        elif ba:
            mid = ba[0]
        elif bb:
            mid = bb[0]
        return {
            "ask_px": ba[0] if ba else None,
            "ask_sz": ba[1] if ba else None,
            "bid_px": bb[0] if bb else None,
            "mid": mid,
            "book_age_ms": round((time.time() - book.ts) * 1000, 1) if book.ts else None,
        }

    # ---------- lifecycle ----------

    def on_submit(self, market, outcome: str, token: str, limit_px: float,
                  shares: float, leg: str,
                  extra: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """Snapshot what we saw and start the latency clock. Returns an opaque
        attempt handle to pass back to on_result (None on any failure).

        `extra` carries pure-observation fields the caller computed at fire time
        (e.g. distance-to-strike: dist_sigma / dist_usd) so they land in the same
        record as the fill outcome. None values are dropped."""
        try:
            seen = self._snapshot(token)
            rec = {
                "id": next(self._ids),
                "_t0": time.monotonic(),
                "ts": round(time.time(), 3),
                "token": token,
                "slug": market.slug,
                "title": market.title,
                "kind": market.kind,
                "side": outcome,
                "leg": leg,
                "t_remaining_s": round(market.close_ts - time.time(), 1),
                "limit_px": round(limit_px, 4),
                "req_shares": round(shares, 2),
                "req_usd": round(shares * limit_px, 2),
                "seen_ask_px": seen["ask_px"],
                "seen_ask_sz": seen["ask_sz"],
                "seen_bid_px": seen["bid_px"],
                "seen_mid": seen["mid"],
                "book_age_ms": seen["book_age_ms"],
            }
            if extra:
                rec.update({k: v for k, v in extra.items() if v is not None})
            return rec
        except Exception as e:  # noqa: BLE001 — never let observation break a trade
            log.debug("shadow on_submit failed: %s", e)
            return None

    def on_result(self, attempt: dict[str, Any] | None, filled_shares: float,
                  avg_fill_px: float, status: str) -> None:
        """Stamp latency + fill outcome, write the attempt record, and (on a
        fill) schedule the post-fill markout reads."""
        if not attempt:
            return
        try:
            latency_ms = round((time.monotonic() - attempt.pop("_t0", time.monotonic())) * 1000, 1)
            filled = filled_shares > 0
            seen_sz = attempt.get("seen_ask_sz")
            seen_ask = attempt.get("seen_ask_px")
            rec = dict(attempt)
            rec.update({
                "type": "attempt",
                "latency_ms": latency_ms,
                "status": str(status),
                "filled": filled,
                "filled_shares": round(filled_shares, 2),
                "avg_fill_px": round(avg_fill_px, 4) if filled else None,
                # fraction of the displayed top-of-book size we actually won
                "capture_frac": (round(filled_shares / seen_sz, 3)
                                 if filled and seen_sz else None),
                # >0 = paid above what we saw; <0 = book cheapened in-flight
                # (the side collapsed: an adverse / stale-book fill)
                "slippage_vs_seen_ask": (round(avg_fill_px - seen_ask, 4)
                                         if filled and seen_ask is not None else None),
            })
            self._write(rec)
            if filled and self.markout_horizons:
                asyncio.ensure_future(
                    self._markouts(attempt["id"], attempt["token"], avg_fill_px))
        except Exception as e:  # noqa: BLE001
            log.debug("shadow on_result failed: %s", e)

    async def _markouts(self, attempt_id: int, token: str, fill_px: float) -> None:
        """Read the token mid at each horizon after the fill. Negative drift vs
        our fill price = the market moved against the side we bought = adverse
        selection made visible."""
        try:
            last = 0.0
            for h in sorted(self.markout_horizons):
                await asyncio.sleep(max(0.0, h - last))
                last = h
                snap = self._snapshot(token)
                mid = snap["mid"]
                self._write({
                    "type": "markout",
                    "id": attempt_id,
                    "horizon_s": h,
                    "mid": mid,
                    "drift_vs_fill": (round(mid - fill_px, 4)
                                      if mid is not None else None),
                })
        except Exception as e:  # noqa: BLE001
            log.debug("shadow markout failed: %s", e)

    # ---------- io ----------

    def _write(self, rec: dict[str, Any]) -> None:
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        except OSError as e:
            log.debug("shadow write failed: %s", e)
