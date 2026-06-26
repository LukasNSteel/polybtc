"""Unit test for the sniper TREND FILTER (bot/strategy.py _snipe).

Drives the real Strategy._snipe through fakes (real _momentum_z, fake book /
binance / executor) and asserts:

  1. a bet that FADES a momentum run beyond trend_filter_sigma is SKIPPED
     (BUY DN into an up-run; BUY UP into a down-run),
  2. the same bet FIRES when momentum is flat or with it,
  3. trend_filter_sigma == 0 disables the gate entirely (still fires),
  4. the order POST path is untouched — the gate only ever PREVENTS a fire,
     so it cannot add latency (no I/O in _momentum_z; checked structurally).

Run:  .venv/bin/python -m research.test_trend_filter
"""
import asyncio
import math
import time

from bot.markets import Market
from bot.strategy import Strategy


class FakeCfg:
    def __init__(self, sniper):
        self._sniper = sniper

    def __getitem__(self, k):
        assert k == "sniper"
        return self._sniper

    def get(self, section, key, default=None):
        return self._sniper.get(key, default) if section == "sniper" else default


class FakeBook:
    def __init__(self, ask_px, ask_sz):
        self._ask = (ask_px, ask_sz)

    def best_ask(self):
        return self._ask


class FakeFeed:
    def __init__(self, books):
        self.books = books


class FakeBinance:
    """recent_return / vol_per_sec only — _momentum_z reads nothing else."""
    def __init__(self, vol_per_sec, recent_return):
        self.vol_per_sec = vol_per_sec
        self._r = recent_return
        self.calls = 0

    def recent_return(self, window_sec):
        self.calls += 1
        return self._r


class FakeExec:
    def __init__(self):
        self.placed = []

    async def place_buy(self, m, side, limit_px, shares, leg=None, extra=None):
        self.placed.append({"side": side, "limit_px": limit_px,
                            "shares": shares, "leg": leg, "extra": extra})


class FakePortfolio:
    def __init__(self):
        self.positions = {}


class FakeShadow:
    """Records log_candidate calls; snapshot returns a fixed book shape."""
    def __init__(self):
        self.candidates = []

    def snapshot(self, token):
        return {"ask_px": 0.55, "ask_sz": 1000.0, "bid_px": 0.53,
                "mid": 0.54, "book_age_ms": 5.0}

    def log_candidate(self, rec):
        self.candidates.append(rec)


def make_market(trem=120):
    now = int(time.time())
    return Market(
        slug="btc-5m-test", title="BTC 5m test", condition_id="cond",
        token_up="TOK_UP", token_down="TOK_DN",
        open_ts=now - 180, close_ts=now + int(trem), tick=0.01, kind="5m",
        interval="5m", fee_rate=0.07, fee_exponent=1.0, accepting=True,
        open_price=100.0,
    )


def build(*, ask_up, ask_dn, trend_sigma, recent_return,
          vol_per_sec=0.001, sniper_extra=None,
          max_t_rem=0, close_buffer=0, shadow_max=0, shadow_on=False):
    """Minimal Strategy with everything permissive except the trend gate, so the
    only thing under test is whether _snipe fires for the eligible side."""
    s = object.__new__(Strategy)
    sniper = {
        "enabled": True, "min_edge": 0.10, "max_edge": 0.30,
        "min_ask": 0.50, "max_ask": 0.80, "flat_size": True, "size_frac": 0.5,
        "trend_window_sec": 45.0, "trend_filter_sigma": trend_sigma,
        "max_inventory_frac": 0.25, "max_book_age_sec": 15.0,
        "limit_slack_ticks": 0,
        "shadow_candidates": shadow_on, "shadow_max_t_rem_sec_5m": shadow_max,
    }
    if sniper_extra:
        sniper.update(sniper_extra)
    s.cfg = FakeCfg(sniper)
    s.binance = FakeBinance(vol_per_sec, recent_return)
    s.feed = FakeFeed({"TOK_UP": FakeBook(ask_up, 1000.0),
                       "TOK_DN": FakeBook(ask_dn, 1000.0)})
    s.exec = FakeExec()
    s.exec.shadow = FakeShadow()
    s.portfolio = FakePortfolio()
    s._fak_tier = 0
    s._fak_adjustments = {}
    # permissive stubs for everything that is NOT the trend gate
    s._tradable = lambda m: True
    s._vol_warm = lambda: True
    s._max_t_rem_sec = lambda m: max_t_rem
    s._close_buffer_sec = lambda m: close_buffer
    s._cooled = lambda *a, **k: True
    s._book_fresh = lambda *a, **k: True
    s._inventory_imbalance = lambda *a, **k: 0.0
    s._distance_to_strike = lambda m, side: (1.0, 50.0)
    s._exposure_ok = lambda usd: True
    s._position_cost = lambda slug: 0.0
    return s


def run_snipe(s, *, p_up, p_lo, p_hi):
    m = make_market()
    caps = {"max_position_usd": 100, "max_take_usd": 20}
    asyncio.run(s._snipe(m, p_up, p_lo, p_hi, caps))
    return s.exec.placed


def run_shadow(s, *, p_up, p_lo, p_hi, trem):
    m = make_market(trem)
    s._shadow_candidates(m, p_up, p_lo, p_hi)
    return s.exec.shadow.candidates


# A move of +1.0102% over 45s against vol_per_sec=0.001 is ~1.52 sigma:
#   z = 0.0102 / (0.001 * sqrt(45)) = 1.52
UP_RUN = 0.0102      # strong up-run  -> trend_z ~ +1.52
DOWN_RUN = -0.0102   # strong down-run-> trend_z ~ -1.52
FLAT = 0.0

# DN-eligible book: UP ask above max_ask (skipped), DN a favourite at 0.55.
# robust DN = 1 - p_hi = 0.75 -> net_edge ~0.18 (in the (0.10,0.30] band).
DN_ELIG = dict(ask_up=0.95, ask_dn=0.55)
DN_PROBS = dict(p_up=0.40, p_lo=0.40, p_hi=0.25)
# UP-eligible book mirror: robust UP = p_lo = 0.75.
UP_ELIG = dict(ask_up=0.55, ask_dn=0.95)
UP_PROBS = dict(p_up=0.60, p_lo=0.75, p_hi=0.75)


def check(name, placed, *, expect_fire, side=None):
    fired = len(placed) > 0
    ok = fired == expect_fire and (not fired or placed[0]["side"] == side)
    detail = (f"fired={fired}"
              + (f" side={placed[0]['side']}" if fired else ""))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail} "
          f"(expected {'fire ' + str(side) if expect_fire else 'skip'})")
    assert ok, name
    return ok


def main():
    print("sanity: trend_z magnitude")
    s0 = build(ask_up=0.95, ask_dn=0.55, trend_sigma=1.5, recent_return=UP_RUN)
    z = s0._momentum_z(45.0)
    print(f"  recent_return={UP_RUN} vol=0.001 -> trend_z={z:.3f} "
          f"(>=1.5 should block a fade)")
    assert z is not None and z >= 1.5

    print("\nTREND FILTER gate (trend_filter_sigma=1.5):")
    # 1. BUY DN into a strong UP run -> fade -> SKIP
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=UP_RUN)
    check("DN fades up-run", run_snipe(s, **DN_PROBS), expect_fire=False)

    # 2. BUY DN with flat momentum -> FIRE
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=FLAT)
    check("DN flat momentum", run_snipe(s, **DN_PROBS),
          expect_fire=True, side="dn")

    # 3. BUY DN with a DOWN run (momentum WITH the bet) -> FIRE
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=DOWN_RUN)
    check("DN with down-run", run_snipe(s, **DN_PROBS),
          expect_fire=True, side="dn")

    # 4. BUY UP into a strong DOWN run -> fade -> SKIP
    s = build(**UP_ELIG, trend_sigma=1.5, recent_return=DOWN_RUN)
    check("UP fades down-run", run_snipe(s, **UP_PROBS), expect_fire=False)

    # 5. BUY UP with an UP run (momentum WITH the bet) -> FIRE
    s = build(**UP_ELIG, trend_sigma=1.5, recent_return=UP_RUN)
    check("UP with up-run", run_snipe(s, **UP_PROBS),
          expect_fire=True, side="up")

    print("\ngate OFF (trend_filter_sigma=0) — must NOT block:")
    # 6. trend_filter_sigma=0 disables: DN fires even into an up-run
    s = build(**DN_ELIG, trend_sigma=0.0, recent_return=UP_RUN)
    check("DN fades up-run, gate off", run_snipe(s, **DN_PROBS),
          expect_fire=True, side="dn")

    print("\nfail-open — momentum unavailable -> trend_z=0 -> must NOT block:")
    # 7. recent_return None (cold feed) -> _momentum_z None -> trend_z 0 -> fire
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=None)
    check("DN, momentum unavailable", run_snipe(s, **DN_PROBS),
          expect_fire=True, side="dn")
    fired = s.exec.placed[0]
    assert fired["extra"]["trend_z"] == 0.0, "trend_z should be logged as 0.0"

    print("\nshadow logging — trend_z is recorded in place_buy extra:")
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=FLAT)
    placed = run_snipe(s, **DN_PROBS)
    assert "trend_z" in placed[0]["extra"], "trend_z missing from shadow extra"
    print(f"  [PASS] extra={placed[0]['extra']}")

    # ---- shadow-candidate evaluator (observe-only, places nothing) ----
    print("\nshadow candidates — live window [30,90], shadow band out to 180:")
    WIN = dict(max_t_rem=90, close_buffer=30, shadow_max=180, shadow_on=True)

    def check_cand(name, cands, *, expect_n, reason=None, in_live=None):
        ok = len(cands) == expect_n
        if ok and expect_n and reason is not None:
            ok = cands[0]["reason"] == reason
        if ok and expect_n and in_live is not None:
            ok = cands[0]["in_live_window"] is in_live
        d = (f"n={len(cands)}" + (f" reason={cands[0]['reason']} "
             f"in_live={cands[0]['in_live_window']}" if cands else ""))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {d}")
        assert ok, name

    # 1. trem=150 -> outside live window [30,90], inside shadow band -> reason window
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=FLAT, **WIN)
    check_cand("trem150 wider-window", run_shadow(s, **DN_PROBS, trem=150),
               expect_n=1, reason="window", in_live=False)
    assert s.exec.placed == [], "shadow eval must NEVER place an order"

    # 2. trem=60 in live window, but trend fades (up-run) -> reason trend
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=UP_RUN, **WIN)
    check_cand("trem60 trend-blocked", run_shadow(s, **DN_PROBS, trem=60),
               expect_n=1, reason="trend", in_live=True)

    # 3. trem=60 in live window, trend OK -> real fire -> NOT a counterfactual
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=FLAT, **WIN)
    check_cand("trem60 real-fire (skip)", run_shadow(s, **DN_PROBS, trem=60),
               expect_n=0)

    # 4. trem=200 -> outside the shadow band -> nothing
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=FLAT, **WIN)
    check_cand("trem200 out-of-band", run_shadow(s, **DN_PROBS, trem=200),
               expect_n=0)

    # 5. shadow_candidates disabled -> nothing even when eligible
    s = build(**DN_ELIG, trend_sigma=1.5, recent_return=FLAT,
              max_t_rem=90, close_buffer=30, shadow_max=180, shadow_on=False)
    check_cand("disabled", run_shadow(s, **DN_PROBS, trem=150), expect_n=0)

    print("\nALL TREND-FILTER + SHADOW-CANDIDATE TESTS PASSED")


if __name__ == "__main__":
    main()
