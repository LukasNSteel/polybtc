"""Summarise the shadow taker log (logs/shadow_taker.jsonl).

Turns the raw per-FAK records the live bot writes (ShadowTakerLogger) into the
numbers that the paper sim currently *assumes*:

  * real submit->ack LATENCY               (vs paper taker_latency_ms: 420)
  * real FILL RATE and CAPTURE vs displayed (vs paper capture: 0.30,
                                             race_loss_prob: 0.20)
  * SLIPPAGE: avg fill px - the ask we saw  (book move during the order)
  * post-fill MARKOUTS: mid drift vs our fill at each horizon — the direct
    adverse-selection readout (persistently negative = we are being picked off)

This is the P0.2 instrument: run it on a live session's shadow log to decide
whether the taker edge survives real latency/capture, and to recalibrate the
paper knobs from measurement instead of guesses.

Usage: python research/analyze_shadow.py [logs/shadow_taker.jsonl]
"""

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

DEFAULT = Path(__file__).resolve().parent.parent / "logs" / "shadow_taker.jsonl"


def pctl(xs, q):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def fmt_stats(xs, unit=""):
    if not xs:
        return "n=0"
    return (f"n={len(xs)} median {statistics.median(xs):.1f}{unit} "
            f"p10 {pctl(xs, 0.10):.1f}{unit} p90 {pctl(xs, 0.90):.1f}{unit} "
            f"max {max(xs):.1f}{unit}")


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.exists():
        print(f"no shadow log at {path} — run the bot live with shadow.enabled "
              f"(it writes one line per taker FAK)")
        return 1

    attempts = []
    markouts = defaultdict(list)  # id -> [(horizon, drift)]
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("type") == "attempt":
            attempts.append(r)
        elif r.get("type") == "markout" and r.get("drift_vs_fill") is not None:
            markouts[r["id"]].append((r["horizon_s"], r["drift_vs_fill"]))

    if not attempts:
        print(f"{path}: no attempt records yet")
        return 0

    fills = [a for a in attempts if a.get("filled")]
    kills = [a for a in attempts if not a.get("filled")]
    n = len(attempts)

    print("=" * 72)
    print(f"SHADOW TAKER REPORT — {path.name}")
    print("=" * 72)
    print(f"  attempts {n} | fills {len(fills)} | no-fill {len(kills)} "
          f"| fill rate {100 * len(fills) / n:.0f}%")
    print(f"    (paper assumes capture 0.30 + race_loss 0.20 -> ~? fill rate; "
          f"compare directly)")
    print()

    lat = [a["latency_ms"] for a in attempts if a.get("latency_ms") is not None]
    print("LATENCY  submit->ack (real order-out leg; paper taker_latency_ms=420)")
    print(f"  {fmt_stats(lat, 'ms')}")
    print()

    # submit->ack split: BUILD (create_market_order / presign lookup) vs POST
    # (post_order HTTP round trip). Network RTT is ~1ms, so a large BUILD points
    # the finger at the client library — pre-signing moves that off the hot path,
    # which the presigned-vs-live-signed breakdown below quantifies directly.
    build = [a["build_ms"] for a in attempts if a.get("build_ms") is not None]
    post = [a["post_ms"] for a in attempts if a.get("post_ms") is not None]
    if build or post:
        print("LATENCY SPLIT  build (order construct/sign) vs post (HTTP round trip)")
        if build:
            print(f"  build {fmt_stats(build, 'ms')}")
        if post:
            print(f"  post  {fmt_stats(post, 'ms')}")
        pre = [a for a in attempts if a.get("presigned") is True]
        live = [a for a in attempts if a.get("presigned") is False]
        for label, group in (("presigned (fast path)", pre), ("live-signed", live)):
            if not group:
                continue
            b = [a["build_ms"] for a in group if a.get("build_ms") is not None]
            p = [a["post_ms"] for a in group if a.get("post_ms") is not None]
            print(f"  {label}: n={len(group)} | "
                  f"build {fmt_stats(b, 'ms') if b else 'n=0'} | "
                  f"post {fmt_stats(p, 'ms') if p else 'n=0'}")
        print()

    age = [a["book_age_ms"] for a in attempts if a.get("book_age_ms") is not None]
    print("BOOK AGE at submit (how stale our view was; sniper gate now caps 3000ms)")
    print(f"  {fmt_stats(age, 'ms')}")
    print()

    cap = [100 * a["capture_frac"] for a in fills if a.get("capture_frac") is not None]
    print("CAPTURE  filled / displayed-ask size (paper capture=30%)")
    print(f"  {fmt_stats(cap, '%')}")
    print()

    slip = [100 * a["slippage_vs_seen_ask"] for a in fills
            if a.get("slippage_vs_seen_ask") is not None]
    if slip:
        adverse = sum(1 for s in slip if s < -0.5)  # filled >0.5c below seen ask
        print("SLIPPAGE  avg fill px - seen ask (cents; <0 = book cheapened in-flight)")
        print(f"  {fmt_stats(slip, 'c')}")
        print(f"  fills >0.5c BELOW the ask we saw (stale-book / collapse): "
              f"{adverse}/{len(slip)} ({100 * adverse / len(slip):.0f}%)")
        print()

    horizons = sorted({h for v in markouts.values() for h, _ in v})
    if horizons:
        print("MARKOUTS  token-mid drift vs our fill (THE adverse-selection signal)")
        print("  negative = market moved against the side we bought after we filled")
        for h in horizons:
            drifts = [100 * d for v in markouts.values() for hh, d in v if hh == h]
            if not drifts:
                continue
            mean = statistics.mean(drifts)
            neg = sum(1 for d in drifts if d < 0)
            print(f"  +{h:g}s: mean {mean:+.2f}c  median {statistics.median(drifts):+.2f}c "
                  f"  negative {neg}/{len(drifts)} ({100 * neg / len(drifts):.0f}%)")
        print()

    # by leg
    print("BY LEG")
    legs = defaultdict(lambda: [0, 0])
    for a in attempts:
        legs[a.get("leg", "?")][0] += 1
        legs[a.get("leg", "?")][1] += int(bool(a.get("filled")))
    for leg, (att, fl) in sorted(legs.items()):
        print(f"  {leg:6} attempts {att:4} fills {fl:4} ({100 * fl / att:.0f}%)")
    print()
    print("READ: if real latency << 420ms and markouts aren't systematically")
    print("negative, the taker edge survives — recalibrate paper.capture /")
    print("race_loss_prob / taker_latency_ms to these numbers. If markouts are")
    print("persistently negative, the fills are adversely selected and the edge")
    print("is being arbitraged away regardless of headline fill rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
