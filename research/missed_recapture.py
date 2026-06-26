"""Classify the pre-change session's missed FAKs by latency cause, to estimate
how many the P0/P1 fix could plausibly recapture.

A miss is 'no orders found to match' = the ask was gone by the time our order
landed. The fix removed (a) dispatch/queue wait and (b) cold-socket tails (1s
keep-warm caps warm_age ~1s). So a miss is plausibly recapturable ONLY if its
post_ms was inflated by a cold socket / tail; a miss already at the warm ~300ms
server floor would still miss (book moved within the unavoidable venue latency).
"""
import json

PRE_START = 1782359783   # session that ran the new distance rule (old exec code)
PRE_END = 1782370538     # restart with P0/P1

rows = []
for line in open("logs/shadow_taker.jsonl"):
    try:
        r = json.loads(line)
    except Exception:
        continue
    if r.get("type") == "attempt" and PRE_START <= r.get("ts", 0) < PRE_END:
        rows.append(r)

print(f"pre-change session attempts: {len(rows)}\n")
print(f"{'id':>3} {'side':>4} {'post_ms':>8} {'warm_age':>9} {'book_age':>8} {'result':>7}  window")
print("-" * 92)

fills = miss_cold = miss_floor = 0
for r in rows:
    filled = r.get("filled")
    pm = r.get("post_ms") or 0
    wa = r.get("warm_age_ms") or 0
    ba = r.get("book_age_ms") or 0
    tag = ""
    if filled:
        fills += 1
        res = "FILL"
    else:
        res = "miss"
        if wa > 1200 or pm > 600:
            tag = "<- cold/tail  (fix can help)"
            miss_cold += 1
        else:
            tag = "<- warm floor (still miss)"
            miss_floor += 1
    title = (r.get("title", "")
             .replace("Bitcoin Up or Down - June 25, ", "")
             .replace("Bitcoin Up or Down - June 24, ", ""))
    print(f"{r.get('id'):>3} {r.get('side'):>4} {pm:>8.0f} {wa:>9.0f} "
          f"{ba:>8.0f} {res:>7}  {title[:22]:22} {tag}")

print(f"\nfills={fills}  missed={miss_cold + miss_floor}  "
      f"(cold/tail -> fix can help: {miss_cold}; at warm floor -> still miss: {miss_floor})")
