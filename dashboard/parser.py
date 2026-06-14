"""Parse polybtc session logs, state.json, and calibration.csv for the dashboard."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


LOG_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+) (\S+)\s+(\S+)\s+(.+)$"
)
FILL = re.compile(
    r"FILL (UP|DN)\s+(\w+)\s+(.+?) ([\d.]+) sh @ ([\d.]+) "
    r"\(\$([\d.]+)(?: \+fee ([\d.]+))?\) \| cash ([\d.-]+)"
)
# payout/cost/pnl can all go negative (net premium collection)
SETTLE = re.compile(
    r"SETTLE (.+?) -> (UP|DOWN) \| payout \$(-?[\d.]+) cost \$(-?[\d.]+) pnl \$([+-]?[\d.]+)"
)
# exposure can go negative after settlements with open orders
# basis suffix: (perp basis +1.0), (perp basis +1.0, cb basis -2.0), or (cb basis -2.0)
STATUS = re.compile(
    r"spot ([\d.]+)(?: \((?:perp basis ([+-][\d.]+)(?:, cb basis ([+-][\d.]+))?"
    r"|cb basis ([+-][\d.]+))\))? \| vol\(1m\) ([\d.]+)% "
    r"\| markets \d+ \[.*?\] \| cash \$(-?[\d.]+) \| equity \$(-?[\d.]+) "
    r"\| exposure \$(-?[\d.]+) \| open orders (\d+)"
)
GUARD = re.compile(r"(FILL BREAKER|JUMP GUARD): (.+)")
# two formats: pre-June-12 "vs fair X (edge Y, $Z)" and the dual-beta gate's
# "vs robust X (blend W, edge Y, $Z)"
SNIPE = re.compile(
    r"SNIPE (.+?) (UP|DN): ask ([\d.]+) \+ fee ([\d.]+) vs (?:fair|robust) ([\d.]+) "
    r"\((?:blend [\d.]+, )?edge ([\d.]+), \$([\d.]+)\)"
)
RESTORED = re.compile(
    r"restored state: cash \$([\d.]+), (\d+) open positions"
)


@dataclass
class Fill:
    ts: datetime
    side: str
    leg: str
    market: str
    shares: float
    price: float
    usd: float
    fee: float
    cash: float


@dataclass
class Settlement:
    ts: datetime
    market: str
    winner: str
    payout: float
    cost: float
    pnl: float


@dataclass
class StatusPoint:
    ts: datetime
    spot: float
    perp_basis: float | None
    cb_basis: float | None
    vol_1m_pct: float
    cash: float
    equity: float
    exposure: float
    open_orders: int


@dataclass
class Snipe:
    ts: datetime
    market: str
    side: str
    ask: float
    fee: float
    fair: float
    edge: float
    usd: float


@dataclass
class LogEvent:
    ts: datetime
    module: str
    level: str
    message: str


@dataclass
class GuardEvent:
    ts: datetime
    kind: str  # "FILL BREAKER" | "JUMP GUARD"
    detail: str


@dataclass
class LegMarketPnl:
    """Settlement P&L attributed to the leg/side that bought the shares."""
    market: str
    leg: str
    side: str
    fills: int
    shares: float
    cost: float
    payout: float
    pnl: float
    won: bool
    settle_ts: datetime


@dataclass
class LogSnapshot:
    path: Path
    mtime: float
    events: list[LogEvent] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    settlements: list[Settlement] = field(default_factory=list)
    status: list[StatusPoint] = field(default_factory=list)
    snipes: list[Snipe] = field(default_factory=list)
    guards: list[GuardEvent] = field(default_factory=list)
    restored_cash: float | None = None
    paper_mode: bool = False


@dataclass
class StateSnapshot:
    path: Path
    mtime: float
    start_cash: float
    cash: float
    exposure: float
    leg_realized: dict[str, float]
    positions: list[dict[str, Any]]


def _parse_ts(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S,%f")


def list_session_logs(log_dir: Path) -> list[Path]:
    if not log_dir.is_dir():
        return []
    logs = sorted(log_dir.glob("session_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs


def parse_log(path: Path) -> LogSnapshot:
    snap = LogSnapshot(path=path, mtime=path.stat().st_mtime)
    text = path.read_text(errors="replace")
    for line in text.splitlines():
        m = LOG_TS.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        module, level, msg = m.group(2), m.group(3), m.group(4)
        snap.events.append(LogEvent(ts=ts, module=module, level=level, message=msg))

        if module == "main" and "paper mode" in msg:
            snap.paper_mode = True

        if module == "exec":
            rm = RESTORED.search(msg)
            if rm:
                snap.restored_cash = float(rm.group(1))
            fm = FILL.search(msg)
            if fm:
                snap.fills.append(Fill(
                    ts=ts, side=fm.group(1), leg=fm.group(2), market=fm.group(3),
                    shares=float(fm.group(4)), price=float(fm.group(5)),
                    usd=float(fm.group(6)), fee=float(fm.group(7) or 0),
                    cash=float(fm.group(8)),
                ))
            sm = SETTLE.search(msg)
            if sm:
                snap.settlements.append(Settlement(
                    ts=ts, market=sm.group(1), winner=sm.group(2),
                    payout=float(sm.group(3)), cost=float(sm.group(4)),
                    pnl=float(sm.group(5)),
                ))

        if module == "strategy":
            st = STATUS.search(msg)
            if st:
                perp = st.group(2)
                cb = st.group(3) or st.group(4)
                snap.status.append(StatusPoint(
                    ts=ts, spot=float(st.group(1)),
                    perp_basis=float(perp) if perp else None,
                    cb_basis=float(cb) if cb else None,
                    vol_1m_pct=float(st.group(5)), cash=float(st.group(6)),
                    equity=float(st.group(7)), exposure=float(st.group(8)),
                    open_orders=int(st.group(9)),
                ))
            sn = SNIPE.search(msg)
            if sn:
                snap.snipes.append(Snipe(
                    ts=ts, market=sn.group(1), side=sn.group(2),
                    ask=float(sn.group(3)), fee=float(sn.group(4)),
                    fair=float(sn.group(5)), edge=float(sn.group(6)),
                    usd=float(sn.group(7)),
                ))

        if module == "guards":
            gm = GUARD.search(msg)
            if gm:
                snap.guards.append(GuardEvent(ts=ts, kind=gm.group(1), detail=gm.group(2)))
    return snap


def attribute_legs(snap: LogSnapshot) -> list[LegMarketPnl]:
    """Split each settled market's P&L by the leg/side that bought the shares.

    Only covers fills present in this log: positions restored from a previous
    session settle without attribution, so the per-leg sum can differ slightly
    from the raw settlement total.
    """
    settled = {s.market: s for s in snap.settlements}
    agg: dict[tuple[str, str, str], dict[str, float]] = {}
    for f in snap.fills:
        if f.market not in settled:
            continue
        a = agg.setdefault((f.market, f.leg, f.side),
                           {"sh": 0.0, "cost": 0.0, "n": 0})
        a["sh"] += f.shares
        a["cost"] += f.usd + f.fee
        a["n"] += 1
    out = []
    for (market, leg, side), a in agg.items():
        s = settled[market]
        won = (side == "UP") == (s.winner == "UP")
        payout = a["sh"] if won else 0.0
        out.append(LegMarketPnl(
            market=market, leg=leg, side=side, fills=int(a["n"]),
            shares=a["sh"], cost=a["cost"], payout=payout,
            pnl=payout - a["cost"], won=won, settle_ts=s.ts,
        ))
    out.sort(key=lambda r: r.settle_ts)
    return out


def load_state(path: Path) -> StateSnapshot | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    positions = []
    meta = data.get("meta", {})
    for slug, pos in data.get("positions", {}).items():
        md = meta.get(slug, {})
        legs = data.get("legpos", {}).get(slug, {})
        leg_breakdown = {leg: lp.get("cost", 0) for leg, lp in legs.items()}
        positions.append({
            "slug": slug,
            "title": md.get("title", slug),
            "kind": md.get("kind", "?"),
            "up": pos.get("up", 0),
            "dn": pos.get("dn", 0),
            "cost": pos.get("cost", 0),
            "legs": leg_breakdown,
            "close_ts": md.get("close_ts"),
        })
    positions.sort(key=lambda p: p.get("close_ts") or 0)
    exposure = sum(p["cost"] for p in positions)
    return StateSnapshot(
        path=path,
        mtime=path.stat().st_mtime,
        start_cash=float(data.get("start_cash", 0)),
        cash=float(data.get("cash", 0)),
        exposure=exposure,
        leg_realized={k: float(v) for k, v in data.get("leg_realized", {}).items()},
        positions=positions,
    )


def load_calibration_summary(path: Path, max_rows: int = 5000) -> dict[str, Any]:
    if not path.is_file():
        return {"rows": 0, "markets": 0, "buckets": []}
    rows = []
    with path.open() as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append(row)
    if not rows:
        return {"rows": 0, "markets": 0, "buckets": []}

    slugs = {r["slug"] for r in rows if r.get("slug")}
    # bucket recent predictions
    buckets: dict[str, list[float]] = {}
    for r in rows:
        if not r.get("p_up"):
            continue
        p = float(r["p_up"])
        b = f"{int(p * 10) / 10:.1f}"
        buckets.setdefault(b, []).append(p)

    bucket_stats = [
        {"bucket": b, "n": len(v), "avg_pred": sum(v) / len(v)}
        for b, v in sorted(buckets.items())
    ]
    return {"rows": len(rows), "markets": len(slugs), "buckets": bucket_stats[-10:]}
