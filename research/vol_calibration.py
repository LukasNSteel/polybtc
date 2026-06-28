"""Is the model's vol_per_sec calibrated, or does it UNDERESTIMATE realized move?

dist_sigma = log(spot/open) / (vol_per_sec * sqrt(t_remaining))  [bot/strategy.py].
For a favourite to LOSE, the future move over t_remaining must erase `dist`, so
P(lose) ~= Phi(-dist_sigma) IFF the future move's std == vol_per_sec*sqrt(t_rem).

Test: reconstruct the bot's exact vol EWMA (fast hl 60 / slow hl 600, max, floor
2e-5 — identical to load_binance), then for fire points at the live t_remaining
horizons, standardize the realized move to close:
        z = log(price_close / price_fire) / (vol_fire * sqrt(t_rem))
If vol is calibrated, std(z) ~= 1 and the tail fractions match a Normal. std(z) > 1
means vol is UNDERSTATED by that factor -> a reported "1.5 sigma" cushion is really
1.5/std(z) sigma, and the true loss prob is much higher than Phi(-1.5)=6.7%.
"""
import numpy as np
from scipy.special import ndtr
import replay_binance as R

RECENT_DAYS = 14
HORIZONS = [30, 60, 90, 300]   # 30-90 = live fire window; 300 = full candle


def main():
    base, price, vol, _ = R.load_binance()
    n = len(price)
    end_ep = base + n - 1
    cutoff = end_ep - RECENT_DAYS * 86400  # "recent" slice only

    # 5m windows aligned to epoch multiples of 300 (Polymarket btc-updown-5m grid)
    first_w = ((base + 299) // 300) * 300
    last_w = ((end_ep) // 300) * 300 - 300
    windows = np.arange(first_w, last_w + 1, 300)
    windows = windows[windows >= cutoff]
    print(f"data {base}..{end_ep}  |  recent {RECENT_DAYS}d  |  {len(windows)} 5m windows\n")

    # Normal reference tail probabilities P(|z| > k)
    def ptail(k):
        return 2 * (1 - ndtr(k))
    print(f"{'horizon':>8} {'n':>6} {'std(z)':>7} {'mean':>7} "
          f"{'|z|>1':>12} {'|z|>1.5':>12} {'|z|>2':>12}")
    print(f"{'(Normal)':>8} {'':>6} {'1.00':>7} {'0.00':>7} "
          f"{ptail(1):>11.1%} {ptail(1.5):>11.1%} {ptail(2):>11.1%}")
    print("-" * 70)

    results = {}
    for tr in HORIZONS:
        zs = []
        for w in windows:
            fire = w + 300 - tr
            ci = w + 300 - base
            fi = fire - base
            if fi < 0 or ci >= n:
                continue
            pf, pc, vf = price[fi], price[ci], vol[fi]
            if pf <= 0 or pc <= 0 or vf <= 0:
                continue
            pred = vf * np.sqrt(tr)
            if pred <= 0:
                continue
            zs.append(np.log(pc / pf) / pred)
        z = np.array(zs)
        sd = z.std()
        results[tr] = sd
        f1 = np.mean(np.abs(z) > 1); f15 = np.mean(np.abs(z) > 1.5)
        f2 = np.mean(np.abs(z) > 2)
        print(f"{tr:>7}s {len(z):>6} {sd:>7.2f} {z.mean():>7.2f} "
              f"{f1:>11.1%} {f15:>11.1%} {f2:>11.1%}")

    # translate the fire-window inflation into the loss-prob story
    print("\n=== what this does to dist_sigma (using 60s horizon std) ===")
    infl = results[60]
    print(f"vol understated by ~{infl:.2f}x  ->  a REPORTED dist_sigma is really "
          f"dist_sigma/{infl:.2f} of true protection")
    for rep in (0.7, 1.0, 1.5, 2.0):
        true = rep / infl
        p_assumed = 1 - ndtr(rep)
        p_real = 1 - ndtr(true)
        print(f"  reported {rep:>4.1f}σ -> true {true:>4.2f}σ | "
              f"assumed loss {p_assumed:>5.1%}  vs  REAL loss {p_real:>5.1%}")


if __name__ == "__main__":
    main()
