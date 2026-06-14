"""polybtc dashboard server.

A single lightweight aiohttp process: serves the static UI and a JSON API
over the bot's session logs, state.json and calibration.csv. No build step,
no extra dependencies.

Run:  .venv/bin/python -m dashboard.server [--port 8787]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from .parser import (
    LogSnapshot,
    attribute_legs,
    list_session_logs,
    load_state,
    parse_log,
)

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
STATE_FILE = ROOT / "state.json"
STATIC = Path(__file__).resolve().parent / "static"

_cache: dict[str, tuple[tuple[float, int], LogSnapshot]] = {}

TIME_RANGE = re.compile(r"(\d{1,2}):(\d{2})(AM|PM)-(\d{1,2}):(\d{2})(AM|PM)")
HOURLY = re.compile(r", \d{1,2}(AM|PM) ET$")
KIND_SEC = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}


def market_kind(title: str) -> str:
    m = TIME_RANGE.search(title)
    if m:
        h1, m1 = int(m.group(1)) % 12, int(m.group(2))
        h2, m2 = int(m.group(4)) % 12, int(m.group(5))
        if m.group(3) == "PM":
            h1 += 12
        if m.group(6) == "PM":
            h2 += 12
        mins = (h2 * 60 + m2) - (h1 * 60 + m1)
        if mins < 0:
            mins += 24 * 60
        return {5: "5m", 15: "15m", 60: "1h", 240: "4h"}.get(mins, f"{mins}m")
    if HOURLY.search(title):
        return "1h"
    return "?"


def cached_parse(path: Path) -> LogSnapshot:
    st = path.stat()
    key = str(path)
    sig = (st.st_mtime, st.st_size)
    hit = _cache.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    snap = parse_log(path)
    _cache[key] = (sig, snap)
    if len(_cache) > 12:
        _cache.pop(next(iter(_cache)))
    return snap


def downsample(points: list, max_n: int = 600) -> list:
    if len(points) <= max_n:
        return points
    step = len(points) / max_n
    out = [points[int(i * step)] for i in range(max_n)]
    if out[-1] is not points[-1]:
        out.append(points[-1])
    return out


def ep(dt: datetime) -> float:
    return dt.timestamp()


def session_kinds(snap: LogSnapshot, kind_stats: dict, state) -> set[str]:
    """Market kinds seen in this session (for close-boundary grid lines)."""
    kinds = {k for k in kind_stats if k in KIND_SEC}
    for f in snap.fills:
        k = market_kind(f.market)
        if k in KIND_SEC:
            kinds.add(k)
    for s in snap.settlements:
        k = market_kind(s.market)
        if k in KIND_SEC:
            kinds.add(k)
    if state:
        for p in state.positions:
            k = p.get("kind")
            if k in KIND_SEC:
                kinds.add(k)
    return kinds


def close_boundary_lines_by_kind(t0: float, t1: float, kinds: set[str]) -> dict[str, list[float]]:
    """UTC epoch-aligned window closes, one list per active market kind."""
    if t1 <= t0 or not kinds:
        return {}
    out: dict[str, list[float]] = {}
    for kind in sorted(kinds):
        sec = KIND_SEC[kind]
        lines: list[float] = []
        t = math.ceil(t0 / sec) * sec
        while t <= t1:
            lines.append(float(t))
            t += sec
        if lines:
            out[kind] = lines
    return out


def build_payload(session: str | None) -> dict:
    logs = list_session_logs(LOG_DIR)
    sessions = [p.name for p in logs]
    if not logs:
        return {"sessions": [], "error": "no session logs found"}
    sel = logs[0]
    if session:
        for p in logs:
            if p.name == session:
                sel = p
                break
    snap = cached_parse(sel)
    state = load_state(STATE_FILE)
    now = time.time()

    # ---- KPIs from the latest status line ----
    last = snap.status[-1] if snap.status else None
    first_eq = snap.status[0].equity if snap.status else None
    kpis = {
        "spot": last.spot if last else None,
        "vol_1m_pct": last.vol_1m_pct if last else None,
        "equity": last.equity if last else None,
        "cash": last.cash if last else None,
        "exposure": last.exposure if last else None,
        "open_orders": last.open_orders if last else None,
        "session_pnl": (last.equity - first_eq) if last and first_eq is not None else None,
        "fees_paid": round(sum(f.fee for f in snap.fills), 2),
        "fills": len(snap.fills),
        "snipes": len(snap.snipes),
        "settlements": len(snap.settlements),
        "settled_pnl": round(sum(s.pnl for s in snap.settlements), 2),
        "paper": snap.paper_mode,
        "log_age_sec": round(now - sel.stat().st_mtime, 1),
        "last_status_ts": ep(last.ts) if last else None,
    }

    # ---- time series ----
    equity_series = downsample(
        [[ep(s.ts), round(s.equity, 2), round(s.exposure, 2)] for s in snap.status])
    spot_series = downsample([[ep(s.ts), s.spot] for s in snap.status])
    cum = 0.0
    settled_series = []
    for s in snap.settlements:
        cum += s.pnl
        settled_series.append([ep(s.ts), round(cum, 2)])

    # ---- per-leg and per-kind stats from settled attributions ----
    legs = attribute_legs(snap)
    leg_stats: dict[str, dict] = {}
    kind_stats: dict[str, dict] = {}
    for r in legs:
        ls = leg_stats.setdefault(r.leg, {"fills": 0, "cost": 0.0, "pnl": 0.0,
                                          "wins": 0, "losses": 0})
        ls["fills"] += r.fills
        ls["cost"] += r.cost
        ls["pnl"] += r.pnl
        ls["wins" if r.won else "losses"] += 1
        ks = kind_stats.setdefault(market_kind(r.market),
                                   {"markets": set(), "cost": 0.0, "pnl": 0.0})
        ks["markets"].add(r.market)
        ks["cost"] += r.cost
        ks["pnl"] += r.pnl
    for ks in kind_stats.values():
        ks["markets"] = len(ks["markets"])
    for d in leg_stats.values():
        d["cost"] = round(d["cost"], 2)
        d["pnl"] = round(d["pnl"], 2)
    for d in kind_stats.values():
        d["cost"] = round(d["cost"], 2)
        d["pnl"] = round(d["pnl"], 2)

    # ---- activity feed (merged, newest first) ----
    feed = []
    for f in snap.fills[-150:]:
        feed.append({"ts": ep(f.ts), "type": "fill", "leg": f.leg, "side": f.side,
                     "market": f.market, "detail": f"{f.shares:.0f} sh @ {f.price:.3f}",
                     "usd": round(f.usd + f.fee, 2)})
    for s in snap.settlements[-150:]:
        feed.append({"ts": ep(s.ts), "type": "settle", "side": s.winner,
                     "market": s.market, "detail": f"payout ${s.payout:.2f}",
                     "usd": round(s.pnl, 2)})
    for g in snap.guards[-50:]:
        feed.append({"ts": ep(g.ts), "type": "guard", "side": "",
                     "market": g.kind, "detail": g.detail[:90], "usd": None})
    feed.sort(key=lambda x: x["ts"], reverse=True)

    # ---- open positions ----
    positions = []
    if state:
        for p in state.positions:
            positions.append({
                "title": p["title"], "kind": p["kind"], "up": round(p["up"], 1),
                "dn": round(p["dn"], 1), "cost": round(p["cost"], 2),
                "legs": p["legs"],
                "expires_in": (p["close_ts"] - now) if p.get("close_ts") else None,
            })

    # ---- settlement pnl distribution ----
    pnl_hist = [round(s.pnl, 2) for s in snap.settlements]

    # ---- market close boundaries (vertical chart guides) ----
    ts_points = [ep(s.ts) for s in snap.status]
    if snap.settlements:
        ts_points.extend(ep(s.ts) for s in snap.settlements)
    t0 = min(ts_points) if ts_points else now
    t1 = max(ts_points) if ts_points else now
    kinds = session_kinds(snap, kind_stats, state)
    close_lines_by_kind = close_boundary_lines_by_kind(t0, t1, kinds)

    return {
        "generated_at": now,
        "sessions": sessions,
        "session": sel.name,
        "kpis": kpis,
        "equity_series": equity_series,
        "spot_series": spot_series,
        "settled_series": settled_series,
        "leg_stats": leg_stats,
        "kind_stats": kind_stats,
        "feed": feed[:120],
        "positions": positions,
        "pnl_hist": pnl_hist,
        "close_lines_by_kind": close_lines_by_kind,
        "state": {
            "cash": state.cash if state else None,
            "start_cash": state.start_cash if state else None,
            "leg_realized": state.leg_realized if state else {},
        },
    }


async def api_data(request: web.Request) -> web.Response:
    session = request.query.get("session")
    payload = build_payload(session)
    return web.json_response(payload)


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/data", api_data)
    print(f"polybtc dashboard: http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
