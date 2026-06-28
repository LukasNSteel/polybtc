"""Prototype the vol-inflation multiplier (calibration found vol_per_sec understated
~1.26x in the fire window). Backtest on the 13-week archive whether inflating model
vol changes fill count / EV — and disentangle the two channels it acts through:

  TABLE A  floor held at the live 0.7 numeric value. Inflating vol shrinks dist_sigma,
           so 0.7 now demands a BIGGER true move AND lowers favourite edge. This is the
           net effect of just turning the multiplier on with today's config.

  TABLE B  floor scaled DOWN by 1/mult so the TRUE displacement selected is held
           constant (0.7/1.25 = 0.56, etc). This isolates the pure prob_up/edge-calc
           channel: does fattening tails in the edge math pick better trades at the
           same physical cushion?
"""
import numpy as np
import replay_binance as R


def stats(fs):
    if not fs:
        return (0, 0.0, 0.0, 0.0, 0.0)
    dep = sum(f[0] for f in fs); pnl = sum(f[2] for f in fs)
    return (len(fs), float(np.mean([f[3] for f in fs])), pnl,
            pnl / dep if dep else 0.0, R.drawdown(fs))


def row(lab, st):
    print(f"  {lab:22} {st[0]:>6} {st[1]:>6.1%} {st[2]:>+8.0f} {st[3]:>+7.1%} {st[4]:>7.0f}")


def main():
    print("loading...", flush=True)
    base, price, vol, obi = R.load_binance()
    ticks = R.load_ticks()
    live = dict(min_ask=0.50, max_ask=0.80, min_edge=0.10, max_edge=0.30,
                max_take_usd=10, max_position_usd=10, max_exposure_usd=60,
                cooldown=10, no_scale=True, kind_only="5m", dist_sigma_min=0.7,
                min_t_rem=30, max_t_rem=90, trend_filter_sigma=1.0,
                race_loss=0.20, capture=0.30, contention=True, feedlag=True)
    hdr = f"  {'config':22} {'fills':>6} {'win%':>6} {'pnl$':>8} {'ROI/$':>7} {'maxDD$':>7}"

    print("\n=== TABLE A: vol_mult ON, floor held at 0.7 (net live effect) — UP-only ===")
    print(hdr)
    for mult in (1.0, 1.1, 1.25, 1.4):
        cfg = {**live, "vol_mult": mult}
        f, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
        f = [x for x in f if x[5] == "up"]
        row(f"mult {mult} floor0.70", stats(f))

    print("\n=== TABLE B: floor scaled 0.7/mult (true cushion held) — isolates edge calc ===")
    print(hdr)
    for mult in (1.0, 1.1, 1.25, 1.4):
        cfg = {**live, "vol_mult": mult, "dist_sigma_min": round(0.7 / mult, 3)}
        f, _ = R.run(cfg, base, price, vol, ticks, obi=obi)
        f = [x for x in f if x[5] == "up"]
        row(f"mult {mult} floor{0.7/mult:.2f}", stats(f))


if __name__ == "__main__":
    main()
