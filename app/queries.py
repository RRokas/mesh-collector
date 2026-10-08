"""Read side: overview, per-node history, record listing, alerts."""
from __future__ import annotations

import time
from typing import Optional

from sqlalchemy import BigInteger, Float, and_, cast, func, select
from sqlalchemy.engine import Engine

from .config import Settings
from .db import nodes, quarantine, record_paths, records, routers
from .fmt import fmt_duration

MAX_CHART_POINTS = 600


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


def node_overview(engine: Engine, s: Settings, now: Optional[float] = None) -> list[dict]:
    now = time.time() if now is None else now
    q = (
        select(
            nodes.c.node, nodes.c.record_count, nodes.c.first_received_at,
            nodes.c.last_received_at, nodes.c.last_seen_at,
            records.c.id, records.c.seq, records.c.temp, records.c.hum, records.c.status,
            records.c.hops, records.c.origin, records.c.via_router, records.c.sink,
            records.c.seen_at, records.c.received_at,
        )
        .select_from(nodes.outerjoin(records, records.c.id == nodes.c.last_record_id))
        .order_by(nodes.c.node)
    )
    with engine.connect() as conn:
        out = _rows(conn.execute(q))
    for n in out:
        age = now - min(n["last_seen_at"] or n["last_received_at"], n["last_received_at"])
        n["age_s"] = age
        n["stale"] = age > s.stale_tag_minutes * 60
        n["temp_out"] = n["temp"] is not None and not (s.temp_min <= n["temp"] <= s.temp_max)
        n["hum_out"] = n["hum"] is not None and not (s.hum_min <= n["hum"] <= s.hum_max)
    return out


def router_overview(engine: Engine, s: Settings, now: Optional[float] = None) -> list[dict]:
    now = time.time() if now is None else now
    with engine.connect() as conn:
        out = _rows(conn.execute(select(routers).order_by(routers.c.router_id)))
    for r in out:
        r["is_uplink"] = (r["posts"] or 0) > 0
        r["silent"] = r["is_uplink"] and (now - (r["last_contact"] or 0)) > s.silent_router_minutes * 60
        # sent_at is their clock at POST time; received ~= our clock at the same moment.
        r["clock_skew_s"] = (
            r["last_sent_at"] - r["last_contact"]
            if r["is_uplink"] and r["last_sent_at"] is not None and r["last_contact"] is not None
            else None
        )
        r["clock_bad"] = r["clock_skew_s"] is not None and abs(r["clock_skew_s"]) > s.clock_skew_warn_seconds
    # Uplinks first, then origin-only routers.
    out.sort(key=lambda r: (not r["is_uplink"], r["router_id"]))
    return out


def alerts(engine: Engine, s: Settings, now: Optional[float] = None) -> list[dict]:
    now = time.time() if now is None else now
    out: list[dict] = []
    for n in node_overview(engine, s, now):
        if n["stale"]:
            out.append({"kind": "stale_tag", "subject": n["node"],
                        "message": f"no new reading for {n['age_s'] / 60:.0f} min"})
        if n["temp_out"]:
            out.append({"kind": "temp_out_of_range", "subject": n["node"],
                        "message": f"temperature {n['temp']} °C outside {s.temp_min}..{s.temp_max}"})
        if n["hum_out"]:
            out.append({"kind": "hum_out_of_range", "subject": n["node"],
                        "message": f"humidity {n['hum']} % outside {s.hum_min}..{s.hum_max}"})
    for r in router_overview(engine, s, now):
        if r["silent"]:
            out.append({"kind": "router_silent", "subject": r["router_id"],
                        "message": f"no contact for {(now - r['last_contact']) / 60:.0f} min"})
        if r["clock_bad"]:
            out.append({"kind": "router_clock_skew", "subject": r["router_id"],
                        "message": f"clock off by {fmt_duration(r['clock_skew_s'])} (timestamps from it are unreliable)"})
    with engine.connect() as conn:
        qn = conn.execute(select(func.count()).select_from(quarantine)).scalar_one()
    if qn:
        out.append({"kind": "quarantined_records", "subject": "ingest",
                    "message": f"{qn} record(s) failed validation and were quarantined"})
    return out


def node_series(
    engine: Engine, node: str, since: float, until: float, time_field: str = "seen_at"
) -> list[dict]:
    """Bucketed temp/hum averages so a 30-day chart stays ~600 points."""
    col = records.c.received_at if time_field == "received_at" else records.c.seen_at
    bucket = max(1.0, (until - since) / MAX_CHART_POINTS)
    # CAST(t / bucket AS BIGINT) truncates on SQLite and rounds on Postgres;
    # either way each bucket is a contiguous window of width `bucket`.
    b = cast(col / bucket, BigInteger).label("b")
    q = (
        select(
            b,
            func.min(col).label("t"),
            cast(func.avg(records.c.temp), Float).label("temp"),
            cast(func.min(records.c.temp), Float).label("temp_min"),
            cast(func.max(records.c.temp), Float).label("temp_max"),
            cast(func.avg(records.c.hum), Float).label("hum"),
            func.count().label("n"),
        )
        .where(and_(records.c.node == node, col >= since, col <= until))
        .group_by(b)
        .order_by(b)
    )
    with engine.connect() as conn:
        return [
            {k: v for k, v in r.items() if k != "b"}
            for r in _rows(conn.execute(q))
        ]


def list_records(
    engine: Engine,
    node: Optional[str] = None,
    router: Optional[str] = None,
    since: Optional[float] = None,
    until: Optional[float] = None,
    order: str = "seen_at",
    desc: bool = True,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    order_cols = {
        "seen_at": [records.c.seen_at, records.c.seq],
        "seq": [records.c.seq, records.c.seen_at],
        "received_at": [records.c.received_at, records.c.seen_at],
    }[order]
    time_col = records.c.received_at if order == "received_at" else records.c.seen_at
    # Correlated count (uses ix_paths_record) instead of grouping the whole table.
    paths = (
        select(func.count())
        .where(record_paths.c.record_id == records.c.id)
        .correlate(records)
        .scalar_subquery()
    )
    q = select(records, paths.label("paths"))
    if node:
        q = q.where(records.c.node == node.upper())
    if router:
        q = q.where((records.c.via_router == router) | (records.c.origin == router))
    if since is not None:
        q = q.where(time_col >= since)
    if until is not None:
        q = q.where(time_col <= until)
    q = q.order_by(*[c.desc() if desc else c.asc() for c in order_cols]).limit(limit).offset(offset)
    with engine.connect() as conn:
        return _rows(conn.execute(q))


def record_paths_for(engine: Engine, record_id: str) -> list[dict]:
    q = select(record_paths).where(record_paths.c.record_id == record_id).order_by(record_paths.c.pk)
    with engine.connect() as conn:
        return _rows(conn.execute(q))


def counts(engine: Engine) -> dict:
    with engine.connect() as conn:
        return {
            "records": conn.execute(select(func.count()).select_from(records)).scalar_one(),
            "nodes": conn.execute(select(func.count()).select_from(nodes)).scalar_one(),
            "quarantined": conn.execute(select(func.count()).select_from(quarantine)).scalar_one(),
        }
