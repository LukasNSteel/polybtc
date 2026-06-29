"""Unit test for the tiered far-band snipe gate (bot/strategy.py).

Exercises _max_t_rem_sec_far + _dist_sigma_floor + the effective-window logic
against the REAL config.live165.yaml, and proves the fail-safe: with the far
keys unset, behaviour is byte-identical to the legacy single-window gate.

Run:  .venv/bin/python -m research.test_far_band
"""
from dataclasses import dataclass

from bot.config import Config
from bot.strategy import Strategy


@dataclass
class FakeMarket:
    kind: str = "5m"
    t_remaining: float = 0.0


class Fake:
    """Minimal carrier so we can call the unbound Strategy gate helpers without
    standing up feeds/exec/portfolio (the helpers only touch self.cfg)."""
    def __init__(self, cfg):
        self.cfg = cfg

    _max_t_rem_sec = Strategy._max_t_rem_sec
    _max_t_rem_sec_far = Strategy._max_t_rem_sec_far
    _dist_sigma_floor = Strategy._dist_sigma_floor


def effective_max(f, m):
    return max(f._max_t_rem_sec(m), f._max_t_rem_sec_far(m))


def would_fire(f, m, dist_sigma):
    """Reproduce the _snipe window + dist-floor decision (the parts the far band
    changes). True == the timing+conviction gates admit the fire."""
    em = effective_max(f, m)
    if em and m.t_remaining > em:
        return False
    floor = f._dist_sigma_floor(m, m.t_remaining)
    if floor and dist_sigma is not None and dist_sigma < floor:
        return False
    return True


def check(name, got, want):
    ok = got == want
    print(f"  [{'ok ' if ok else 'FAIL'}] {name}: got {got!r} want {want!r}")
    assert ok, name


def main():
    cfg = Config.load("config.live165.yaml")
    f = Fake(cfg)
    m = FakeMarket(kind="5m")

    print("config: max_t_rem_sec_5m=%s far=%s dist_sigma_min=%s far=%s" % (
        cfg.get("sniper", "max_t_rem_sec_5m"),
        cfg.get("sniper", "max_t_rem_sec_5m_far"),
        cfg.get("sniper", "dist_sigma_min"),
        cfg.get("sniper", "dist_sigma_min_far")))

    print("\n=== window ceiling ===")
    check("inner ceiling", f._max_t_rem_sec(m), 90)
    check("far ceiling", f._max_t_rem_sec_far(m), 170)
    check("effective max = far", effective_max(f, m), 170)

    print("\n=== band-aware dist floor ===")
    m.t_remaining = 60
    check("inner band floor", f._dist_sigma_floor(m, 60), 0.7)
    m.t_remaining = 150
    check("far band floor", f._dist_sigma_floor(m, 150), 1.0)

    print("\n=== fire decisions ===")
    # inner band: 0.7 floor
    check("inner 60s dσ0.8 fires", would_fire(f, FakeMarket("5m", 60), 0.8), True)
    check("inner 60s dσ0.6 blocked", would_fire(f, FakeMarket("5m", 60), 0.6), False)
    # far band: needs 1.0
    check("far 150s dσ1.2 fires", would_fire(f, FakeMarket("5m", 150), 1.2), True)
    check("far 150s dσ0.8 blocked", would_fire(f, FakeMarket("5m", 150), 0.8), False)
    # beyond far ceiling: always blocked on timing
    check("180s blocked (beyond far)", would_fire(f, FakeMarket("5m", 180), 5.0), False)
    # fail-open when dσ unknown (missing open/vol) even in far band
    check("far 150s dσ=None fires (fail-open)",
          would_fire(f, FakeMarket("5m", 150), None), True)

    print("\n=== FAIL-SAFE: far keys unset -> legacy single-window behaviour ===")
    legacy = Config(raw={k: dict(v) if isinstance(v, dict) else v
                         for k, v in cfg.raw.items()})
    for kk in ("max_t_rem_sec_5m_far", "max_t_rem_sec_far", "dist_sigma_min_far"):
        legacy.raw["sniper"].pop(kk, None)
    g = Fake(legacy)
    check("legacy effective max == inner 90", effective_max(g, FakeMarket("5m")), 90)
    check("legacy far 150s blocked", would_fire(g, FakeMarket("5m", 150), 1.2), False)
    check("legacy inner 60s dσ0.8 fires", would_fire(g, FakeMarket("5m", 60), 0.8), True)
    check("legacy floor everywhere = 0.7", g._dist_sigma_floor(FakeMarket("5m"), 60), 0.7)

    print("\nALL PASS")


if __name__ == "__main__":
    main()
