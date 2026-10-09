"""Schema and engine. Works on SQLite (single-box) and PostgreSQL (managed)."""
from __future__ import annotations

import logging
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
    inspect,
    text,
    update,
)
from sqlalchemy.engine import Engine

log = logging.getLogger("mesh_collector")
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
    Column("hops", Integer),        # BLE hop count from the tag payload (as sent)
    Column("raw", String(64)),
    Column("sink", String(64)),
    Column("origin", String(64)),
    Column("seen_at", Float),       # origin router clock
    Column("sent_at", Float),       # delivering router clock
    Column("via_router", String(64), nullable=False),
    Column("received_at", Float, nullable=False),  # our clock
    # Hop accounting (added later: nullable, see migrate()). route is a JSON
    # array of router ids, origin first, delivering router last.
    Column("route", Text),
    Column("ble_hops", Integer),     # = hops
    Column("router_hops", Integer),  # len(route) - 1; NULL when unknown (old routers)
    Column("total_hops", Integer),   # ble_hops + router_hops
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
    # Router route of the first copy with this (via, origin, sink, hops) key.
    Column("route", Text),
    Column("router_hops", Integer),
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
    Column("last_sent_at", Float),      # their clock at last POST (delivery delay)
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


def migrate(engine: Engine) -> list[str]:
    """Add columns that exist in the code but not yet in the database.

    create_all() creates missing tables but never alters existing ones, so a
    database created by an older version would lack new columns (and every
    insert would fail -> 503 -> routers stuck retrying). Only nullable,
    non-key columns are added this way; anything else needs a real migration.
    Returns the "table.column" names that were added.
    """
    insp = inspect(engine)
    prep = engine.dialect.identifier_preparer
    added: list[str] = []
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if col.primary_key or not col.nullable:
                    raise RuntimeError(
                        f"column {table.name}.{col.name} is missing and can't be added automatically"
                    )
                conn.execute(text(
                    f"ALTER TABLE {prep.format_table(table)} ADD COLUMN "
                    f"{prep.format_column(col)} {col.type.compile(dialect=engine.dialect)}"
                ))
                added.append(f"{table.name}.{col.name}")
        if any(a.startswith("records.") for a in added):
            # Backfill what older rows allow: BLE hops always; router hops only
            # when the origin delivered it itself (otherwise the path is unknown).
            r = records.c
            conn.execute(update(records).where(r.ble_hops.is_(None), r.hops.is_not(None))
                         .values(ble_hops=r.hops))
            conn.execute(update(records).where(r.router_hops.is_(None), r.origin == r.via_router)
                         .values(router_hops=0))
            conn.execute(update(records).where(r.total_hops.is_(None), r.ble_hops.is_not(None),
                                               r.router_hops.is_not(None))
                         .values(total_hops=r.ble_hops + r.router_hops))
    if added:
        log.info("database migrated: added %s", ", ".join(added))
    return added


def init_db(engine: Engine) -> None:
    try:
        metadata.create_all(engine)
        migrate(engine)
    except Exception as e:  # noqa: BLE001
        if "permission denied for schema" in str(e).lower():
            user = engine.url.username or "<app user>"
            raise RuntimeError(
                f"Database user {user!r} may not create tables (PostgreSQL 15+ default). "
                f"Either connect as the cluster's admin user (doadmin), or run as admin: "
                f"GRANT USAGE, CREATE ON SCHEMA public TO {user};"
            ) from e
        raise
