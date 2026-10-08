"""Ingest: validate a router POST and persist it in one transaction.

Contract (from the router side, don't change without changing mesh.py):
  * 2xx  => the whole batch is durably stored (router marks it delivered)
  * else => nothing is considered delivered (router retries the same batch)
So everything below happens inside one transaction, and any exception
propagates to the caller, which answers 5xx.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, or_, update
from sqlalchemy.engine import Engine

from .db import nodes, quarantine, record_paths, records, routers
from .decode import effective_values

U32_MAX = 2**32 - 1


class BadRequest(Exception):
    """Structurally broken request -> 400 (router will keep retrying; that's
    correct, a broken router needs fixing, not silent acceptance)."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class RecordIn(BaseModel):
    """One reading. Extra fields are ignored (forward compatibility)."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=80)
    node: str = Field(min_length=1, max_length=16)
    seq: int = Field(ge=0, le=U32_MAX)
    temp: Optional[float] = None
    hum: Optional[float] = None
    status: Optional[int] = Field(default=None, ge=0, le=255)
    hops: Optional[int] = Field(default=None, ge=0, le=255)
    raw: Optional[str] = Field(default=None, max_length=64)
    sink: Optional[str] = Field(default=None, max_length=64)
    origin: Optional[str] = Field(default=None, max_length=64)
    seen_at: Optional[float] = None

    @field_validator("node")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("raw")
    @classmethod
    def _hex(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip().lower()
        bytes.fromhex(v)  # raises ValueError -> validation error
        return v


@dataclass
class IngestResult:
    router_id: str
    received: int = 0
    new: int = 0
    duplicates: int = 0
    quarantined: int = 0
    heartbeat: bool = False
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "router_id": self.router_id,
            "received": self.received,
            "new": self.new,
            "duplicates": self.duplicates,
            "quarantined": self.quarantined,
            "heartbeat": self.heartbeat,
        }


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def parse_body(body: bytes, max_records: int) -> tuple[str, Optional[float], list]:
    """Structural validation only. Returns (router_id, sent_at, raw records)."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise BadRequest("body is not valid JSON")
    if not isinstance(data, dict):
        raise BadRequest("body must be a JSON object")
    router_id = data.get("router_id")
    if not isinstance(router_id, str) or not (1 <= len(router_id) <= 64):
        raise BadRequest("router_id must be a string of 1-64 chars")
    recs = data.get("records")
    if not isinstance(recs, list):
        raise BadRequest("records must be an array")
    if len(recs) > max_records:
        raise BadRequest(f"too many records ({len(recs)} > {max_records})", status=413)
    sent_at = data.get("sent_at")
    # Odd clocks are expected; a non-numeric sent_at is just dropped, not fatal.
    sent_at = float(sent_at) if _is_number(sent_at) else None
    return router_id, sent_at, recs


def _insert(dialect: str):
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert


def store_batch(
    engine: Engine,
    router_id: str,
    sent_at: Optional[float],
    raw_records: list,
    decode_mode: str,
    client_ip: Optional[str] = None,
    now: Optional[float] = None,
) -> IngestResult:
    now = time.time() if now is None else now
    res = IngestResult(router_id=router_id, received=len(raw_records), heartbeat=not raw_records)
    insert = _insert(engine.dialect.name)

    # --- per-record validation (outside the transaction, pure CPU) ---------
    good: dict[str, dict] = {}   # id -> row; first occurrence in batch wins
    bad: list[dict] = []
    for item in raw_records:
        try:
            if not isinstance(item, dict):
                raise ValueError("record is not an object")
            r = RecordIn.model_validate(item)
        except (ValidationError, ValueError) as e:
            bad.append({
                "router_id": router_id,
                "received_at": now,
                "error": str(e)[:2000],
                "payload": json.dumps(item, default=str)[:20000],
            })
            continue
        if r.id in good:
            continue
        t, h = effective_values(decode_mode, r.raw, r.temp, r.hum)
        good[r.id] = {
            "id": r.id, "node": r.node, "seq": r.seq,
            "temp": t, "hum": h, "temp_router": r.temp, "hum_router": r.hum,
            "status": r.status, "hops": r.hops, "raw": r.raw, "sink": r.sink,
            "origin": r.origin, "seen_at": r.seen_at, "sent_at": sent_at,
            "via_router": router_id, "received_at": now,
        }

    # Stable ordering reduces lock-order deadlocks between concurrent batches.
    rows = [good[k] for k in sorted(good)]

    with engine.begin() as conn:
        inserted: set[str] = set()
        if rows:
            stmt = (
                insert(records)
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(records.c.id)
            )
            inserted = {row[0] for row in conn.execute(stmt, rows)}

            paths = [{
                "record_id": r["id"], "via_router": router_id,
                "origin": r["origin"] or "", "sink": r["sink"] or "",
                "hops": r["hops"] if r["hops"] is not None else -1,
                "seen_at": r["seen_at"], "received_at": now,
            } for r in rows]
            conn.execute(
                insert(record_paths).on_conflict_do_nothing(
                    index_elements=["record_id", "via_router", "origin", "sink", "hops"]
                ),
                paths,
            )

        if bad:
            conn.execute(quarantine.insert(), bad)

        new_rows = [r for r in rows if r["id"] in inserted]
        res.new = len(new_rows)
        res.duplicates = len(rows) - res.new
        res.quarantined = len(bad)

        # A reading's "recency": seen_at, but never later than our own clock,
        # so a router with a clock in the future can't pin "latest" forever.
        def recency(r: dict) -> float:
            return min(r["seen_at"], now) if r["seen_at"] is not None else now

        # --- nodes summary --------------------------------------------------
        by_node: dict[str, list[dict]] = {}
        for r in new_rows:
            by_node.setdefault(r["node"], []).append(r)
        for node in sorted(by_node):
            group = by_node[node]
            latest = max(group, key=recency)
            ins = insert(nodes).values(
                node=node, record_count=len(group), first_received_at=now,
                last_received_at=now, last_record_id=latest["id"], last_seen_at=recency(latest),
            )
            conn.execute(ins.on_conflict_do_update(
                index_elements=["node"],
                set_={
                    "record_count": nodes.c.record_count + ins.excluded.record_count,
                    "last_received_at": ins.excluded.last_received_at,
                },
            ))
            conn.execute(
                update(nodes)
                .where(nodes.c.node == node)
                .where(or_(nodes.c.last_seen_at.is_(None), nodes.c.last_seen_at < recency(latest)))
                .values(last_record_id=latest["id"], last_seen_at=recency(latest))
            )

        # --- uplink router summary -----------------------------------------
        ins = insert(routers).values(
            router_id=router_id, first_contact=now, last_contact=now,
            last_heartbeat=now if res.heartbeat else None, last_sent_at=sent_at,
            last_ip=client_ip, posts=1, records_delivered=res.new,
            duplicates_delivered=res.duplicates, records_originated=0,
        )
        conn.execute(ins.on_conflict_do_update(
            index_elements=["router_id"],
            set_={
                "first_contact": func.coalesce(routers.c.first_contact, ins.excluded.first_contact),
                "last_contact": ins.excluded.last_contact,
                "last_heartbeat": func.coalesce(ins.excluded.last_heartbeat, routers.c.last_heartbeat),
                "last_sent_at": ins.excluded.last_sent_at,
                "last_ip": ins.excluded.last_ip,
                "posts": routers.c.posts + 1,
                "records_delivered": routers.c.records_delivered + ins.excluded.records_delivered,
                "duplicates_delivered": routers.c.duplicates_delivered + ins.excluded.duplicates_delivered,
            },
        ))

        # --- origin router summary -----------------------------------------
        by_origin: dict[str, list[dict]] = {}
        for r in new_rows:
            if r["origin"]:
                by_origin.setdefault(r["origin"], []).append(r)
        for origin in sorted(by_origin):
            group = by_origin[origin]
            last = max(recency(r) for r in group)
            ins = insert(routers).values(
                router_id=origin, posts=0, records_delivered=0, duplicates_delivered=0,
                records_originated=len(group), last_originated_seen_at=last,
            )
            conn.execute(ins.on_conflict_do_update(
                index_elements=["router_id"],
                set_={"records_originated": routers.c.records_originated + ins.excluded.records_originated},
            ))
            conn.execute(
                update(routers)
                .where(routers.c.router_id == origin)
                .where(or_(routers.c.last_originated_seen_at.is_(None),
                           routers.c.last_originated_seen_at < last))
                .values(last_originated_seen_at=last)
            )

    return res
