"""READ-ONLY probe: when does a 5m BTC market stop accepting orders?

No orders are placed, no money at risk. For each live 5-minute window it polls
Polymarket's public `acceptingOrders` flag (Gamma) and the CLOB best ask/bid
for the favorite, ~2x/sec through the final seconds, and reports the exact
t_remaining at which `acceptingOrders` flips True->False (and when the book
goes one-sided).

    python research/probe_5m_accepting.py            # watch 3 windows
    python research/probe_5m_accepting.py --windows 5 --watch 45

This directly answers "is there a fixed lockout?" by observing the venue's own
acceptance flag across several windows.
"""
import argparse
import json
import time

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def current_5m_slug(now):
    epoch = now // 300 * 300
    return f"btc-updown-5m-{epoch}", epoch, epoch + 300


def fetch_event(slug):
    try:
        r = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=5)
        r.raise_for_status()
        evs = r.json()
        return evs[0] if evs else None
    except Exception as e:  # noqa: BLE001
        return None


def best_levels(token):
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=5)
        ob = r.json()
        asks = ob.get("asks") or []
        bids = ob.get("bids") or []
        a = min((float(x["price"]) for x in asks), default=None)
        b = max((float(x["price"]) for x in bids), default=None)
        return a, b
    except Exception:  # noqa: BLE001
        return None, None


def watch_window(watch, poll):
    now = int(time.time())
    slug, open_ts, close_ts = current_5m_slug(now)
    ev = fetch_event(slug)
    if not ev:
        return None
    m = (ev.get("markets") or [{}])[0]
    try:
        outcomes = json.loads(m["outcomes"]); tokens = json.loads(m["clobTokenIds"])
    except (KeyError, json.JSONDecodeError):
        return None
    up_idx = outcomes.index("Up")
    tok_up, tok_dn = tokens[up_idx], tokens[1 - up_idx]
    print(f"\nwatching {slug}  (close in {close_ts-now}s)")

    last_accepting_trem = None
    first_reject_trem = None
    book_oneside_trem = None
    while True:
        now = time.time()
        t_rem = close_ts - now
        if t_rem > watch:
            time.sleep(min(poll * 4, t_rem - watch))
            continue
        if t_rem < -3:
            break
        ev = fetch_event(slug)
        m = (ev.get("markets") or [{}])[0] if ev else {}
        accepting = bool(m.get("acceptingOrders", False))
        a_up, b_up = best_levels(tok_up)
        a_dn, b_dn = best_levels(tok_dn)
        fav = "UP" if (a_up or 1) <= (a_dn or 1) else "DN"  # lower ask = higher-prob favorite? use prices
        # favorite = higher mid; use available asks
        twosided = all(x is not None for x in (a_up, b_up, a_dn, b_dn))
        if accepting:
            last_accepting_trem = t_rem
        elif first_reject_trem is None:
            first_reject_trem = t_rem
        if not twosided and book_oneside_trem is None:
            book_oneside_trem = t_rem
        print(f"  t_rem {t_rem:6.1f}s | accepting={accepting!s:5} | "
              f"up ask/bid {a_up}/{b_up}  dn ask/bid {a_dn}/{b_dn}", flush=True)
        time.sleep(poll)
    return dict(slug=slug, last_accepting=last_accepting_trem,
                first_reject=first_reject_trem, book_oneside=book_oneside_trem)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=3)
    ap.add_argument("--watch", type=float, default=40.0, help="start polling this many s before close")
    ap.add_argument("--poll", type=float, default=0.5)
    args = ap.parse_args()
    results = []
    seen = set()
    while len(results) < args.windows:
        now = int(time.time())
        slug, *_ = current_5m_slug(now)
        if slug in seen:
            time.sleep(2)
            continue
        seen.add(slug)
        r = watch_window(args.watch, args.poll)
        if r:
            results.append(r)
    print("\n" + "=" * 64)
    print("SUMMARY — t_remaining (s) at key events per window")
    print("=" * 64)
    print(f"{'slug':28} {'last accept':>12} {'first reject':>13} {'book 1-sided':>13}")
    for r in results:
        print(f"{r['slug']:28} {str(round(r['last_accepting'],1) if r['last_accepting'] is not None else '-'):>12} "
              f"{str(round(r['first_reject'],1) if r['first_reject'] is not None else 'never'):>13} "
              f"{str(round(r['book_oneside'],1) if r['book_oneside'] is not None else 'never'):>13}")
    print("\nIf 'first reject' is 'never', the venue accepted orders to the wire;")
    print("a number there is the real lock-out point that window.")


if __name__ == "__main__":
    main()
