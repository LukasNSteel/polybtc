"""Ad-hoc: FAK attempts since the 2026-06-25 07:58 UTC restart (trend+window)."""
import json
import datetime
import statistics as st

RESTART = 1782374336
rows = []
for line in open("logs/shadow_taker.jsonl"):
    try:
        d = json.loads(line)
    except Exception:
        continue
    if d.get("type") == "attempt" and d.get("ts", 0) >= RESTART:
        rows.append(d)

print(f"FAK attempts since restart: {len(rows)}")
fills = [r for r in rows if r.get("filled")]
print(f"  filled: {len(fills)}   lost-race/killed: {len(rows) - len(fills)}")
if rows:
    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600
    print(f"  first->last attempt span: {span_h:.1f}h "
          f"(last at {datetime.datetime.utcfromtimestamp(rows[-1]['ts']).strftime('%H:%M UTC')})")


def stats(key):
    vals = [r[key] for r in rows if r.get(key) is not None]
    if not vals:
        return "n/a"
    return f"min {min(vals):5.0f}  med {st.median(vals):5.0f}  max {max(vals):5.0f}  mean {st.mean(vals):5.0f}"


print("\nlatency (ms):")
for k in ["latency_ms", "post_ms", "call_ms", "dispatch_ms", "resume_ms",
          "build_ms", "warm_age_ms"]:
    print(f"  {k:12} {stats(k)}")

print("\nper-attempt:")
print(f"  {'time':8} {'sd':>2} {'t_rem':>6} {'dsig':>5} {'tz':>6} "
      f"{'post':>5} {'call':>5} {'disp':>5} {'res':>4} {'warm':>6}  outcome")
for r in rows:
    t = datetime.datetime.utcfromtimestamp(r["ts"]).strftime("%H:%M:%S")
    oc = "FILL" if r.get("filled") else "lost"
    status = r.get("status", "") or ""
    note = ""
    if "no orders found" in status:
        note = "(no match / raced)"
    elif "filled" in status.lower():
        note = ""
    print(f"  {t:8} {r['side']:>2} {r['t_remaining_s']:6.1f} "
          f"{r.get('dist_sigma') or 0:5.2f} {r.get('trend_z') or 0:6.2f} "
          f"{r.get('post_ms') or 0:5.0f} {r.get('call_ms') or 0:5.0f} "
          f"{r.get('dispatch_ms') or 0:5.1f} {r.get('resume_ms') or 0:4.1f} "
          f"{r.get('warm_age_ms') or 0:6.0f}  {oc} {note}")
