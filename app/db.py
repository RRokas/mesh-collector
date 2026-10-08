"""Schema and engine. Works on SQLite (single-box) and PostgreSQL (managed)."""
from __future__ import annotations

import os

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

# One row per reading id. First write wins: later copies never overwrite it.
records = Table(
    "records",
    metadata,
    Column("id", String(80), primary_key=True),
    Column("node", String(16), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("temp", Float),          # effective value (see DECODE_MODE)
    Column("hum", Float),           # effective value
    Column("temp_router", Float),   # exactly as the router decoded it
    Column("hum_router", Float),
    Column("status", Integer),
    Column("hops", Integer),
    Column("raw", String(64)),
    Column("sink", String(64)),
    Column("origin", String(64)),
    Column("seen_at", Float),       # origin router clock
    Column("sent_at", Float),       # delivering router clock
    Column("via_router", String(64), nullable=False),
    Column("received_at", Float, nullable=False),  # our clock
)
Index("ix_records_node_seen", records.c.node, records.c.seen_at)
Index("ix_records_node_seq", records.c.node, records.c.seq)
Index("ix_records_received", records.c.received_at)
Index("ix_records_via", records.c.via_router)
Index("ix_records_origin", records.c.origin)

# Every distinct path a reading arrived by (including the first one).
record_paths = Table(
    "record_paths",
    metadata,
    Column("pk", Integer, primary_key=True, autoincrement=True),
    Column("record_id", String(80), nullable=False),
    Column("via_router", String(64), nullable=False),
    Column("origin", String(64), nullable=False, default=""),
    Column("sink", String(64), nullable=False, default=""),
    Column("hops", Integer, nullable=False, default=-1),
    Column("seen_at", Float),
    Column("received_at", Float, nullable=False),
    UniqueConstraint("record_id", "via_router", "origin", "sink", "hops", name="uq_record_path"),
)
Index("ix_paths_record", record_paths.c.record_id)

# Per-tag summary, maintained on ingest so the overview is O(tags).
nodes = Table(
    "nodes",
    metadata,
    Column("node", String(16), primary_key=True),
    Column("record_count", BigInteger, nullable=False, default=0),
    Column("first_received_at", Float, nullable=False),
    Column("last_received_at", Float, nullable=False),
    Column("last_record_id", String(80)),
    Column("last_seen_at", Float),
)

# Per-router summary. A router can be an uplink (POSTs to us), an origin
# (heard readings over BLE), or both.
routers = Table(
    "routers",
    metadata,
    Column("router_id", String(64), primary_key=True),
    Column("first_contact", Float),
    Column("last_contact", Float),
    Column("last_heartbeat", Float),
    Column("last_sent_at", Float),      # their clock at last POST (skew check)
    Column("last_ip", String(64)),
    Column("posts", BigInteger, nullable=False, default=0),
    Column("records_delivered", BigInteger, nullable=False, default=0),  # new records only
    Column("duplicates_delivered", BigInteger, nullable=False, default=0),
    Column("records_originated", BigInteger, nullable=False, default=0),
    Column("last_originated_seen_at", Float),
)

# Records that were structurally inside a valid batch but failed per-record
# validation. Kept (not dropped) and the batch still gets 2xx, so one bad
# record can never wedge a router's retry loop.
quarantine = Table(
    "quarantine",
    metadata,
    Column("pk", Integer, primary_key=True, autoincrement=True),
    Column("router_id", String(64)),
    Column("received_at", Float, nullable=False),
    Column("error", Text),
    Column("payload", Text),
)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

        return engine
    # Statement timeout keeps us well inside the router's 15 s budget: a stuck
    # query fails fast (-> 5xx -> router retries) instead of timing out.
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"options": "-c statement_timeout=10000", "connect_timeout": 5},
    )


def init_db(engine: Engine) -> None:
    metadata.create_all(engine)
