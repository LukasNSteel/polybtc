"""Probability that the candle closes at/above its open.

Two refinements over plain Gaussian Brownian motion:

1. Fat tails. Crypto returns are leptokurtic; a pure Gaussian *underprices*
   tail moves, which makes the model wildly overconfident near the extremes
   (it sees 1e-4 events where reality has 1e-2). We model returns as a
   mixture of two Gaussians — a core regime and a wider "jump" regime —
   which is cheap, dependency-free, and pushes tail probabilities toward
   sane values.

2. Market blending. The Polymarket order book aggregates information we
   don't have (other traders' models, flow, news). The final fair value
   blends our model with the market mid. The model weight rises as expiry
   approaches, because near expiry the (observable) distance between spot
   and open dominates and the model is genuinely sharper than a wide book.

3. Momentum drift. A zero-drift model prices the side a trend is fading
   *too high*: after a 30-60s directional run the model keeps calling the
   losing side "cheap", which is exactly how session 1781212299 stacked MM
   and snipe inventory on the fading side of every trending window (-$135,
   -$139, -$146 cap-sized losers). The caller passes a `drift` term —
   expected additional log-return before expiry, estimated from recent
   momentum — which shifts the distribution's center instead of leaving
   it pinned at zero.
"""

import math


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_up(spot: float, open_price: float, vol_per_sec: float, t_remaining: float,
            tail_weight: float = 0.25, tail_scale: float = 2.5,
            drift: float = 0.0) -> float:
    """P(close >= open) under a two-regime Gaussian mixture.

    `drift` is the expected additional log-return before expiry (e.g. from
    short-horizon momentum); 0 recovers the symmetric zero-drift model.
    Ties resolve Up on Polymarket, so at expiry spot == open counts as Up.
    """
    if t_remaining <= 0:
        return 1.0 if spot >= open_price else 0.0
    d = (math.log(spot / open_price) + drift) / (vol_per_sec * math.sqrt(t_remaining))
    return (1 - tail_weight) * norm_cdf(d) + tail_weight * norm_cdf(d / tail_scale)


def prob_up_bounds(spot: float, open_price: float, vol_per_sec: float,
                   t_remaining: float, betas: tuple[float, ...] = (0.83, 1.36),
                   tail_weight: float = 0.25, tail_scale: float = 2.5) -> tuple[float, float]:
    """Min/max P(up) across lead-stickiness regimes (dual-beta robust gate).

    Fitting P(up) = Phi(beta * d) per day on 1,847 markets shows beta is
    regime-dependent: ~1.2-1.4 on momentum days (Binance leads stick), ~0.8-0.9
    on mean-reversion days (leads fade). A taker entry that is only +EV under
    one regime assumption is a regime bet, not a mispricing. Gating the sniper
    on the WORST-case probability across both observed regimes raised realized
    edge from +16.0c to +19.2c/share in the June 2026 backtest
    (research/test_dual_beta.py), discarding exactly the regime-dependent
    trades. No drift term here on purpose: the bounds were validated on the
    pure distance signal.
    """
    if t_remaining <= 0:
        p = 1.0 if spot >= open_price else 0.0
        return p, p
    d = math.log(spot / open_price) / (vol_per_sec * math.sqrt(t_remaining))
    ps = [(1 - tail_weight) * norm_cdf(b * d) + tail_weight * norm_cdf(b * d / tail_scale)
          for b in betas]
    return min(ps), max(ps)


def blend_with_market(model_p: float, market_mid: float | None, t_remaining: float,
                      base_model_weight: float = 0.65) -> float:
    """Combine model fair value with the order book's mid.

    Far from expiry the market's aggregated information deserves real weight;
    in the final minute the model's distance/vol calculation dominates.
    """
    if market_mid is None:
        return model_p
    w = base_model_weight + (0.95 - base_model_weight) * math.exp(-max(t_remaining, 0.0) / 60.0)
    return w * model_p + (1 - w) * market_mid
