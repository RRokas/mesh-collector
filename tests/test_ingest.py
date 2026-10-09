"""Acceptance checks from the handoff, plus the edge cases around them.

Run against SQLite by default; set TEST_DATABASE_URL to run on Postgres:
  TEST_DATABASE_URL=postgresql://user:pw@localhost/mesh_test pytest
"""
from __future__ import annotations

import base64
import os
import random
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.config import Settings, _db_url
from app.db import metadata, record_paths, records, routers
from app.decode import decode_raw
from app.main import create_app

EXAMPLE = {
    "id": "00A1B2C3-1000-086911a801", "node": "00A1B2C3", "seq": 1000,
    "temp": 21.53, "hum": 45.2, "status": 1, "hops": 2,
    "raw": "00a1b2c3086911a801000003e8000002", "sink": "84:71:27:AA:BB:01",
    "origin": "R2", "seen_at": 1759929987.1,
}


def settings(url: str, **kw) -> Settings:
    base = dict(
        database_url=_db_url(url), ingest_token="", dashboard_user="admin", dashboard_password="",
        decode_mode="router", max_records_per_post=1000, max_body_bytes=2_000_000,
        stale_tag_minutes=30, silent_router_minutes=5, delivery_delay_warn_seconds=120,
        temp_min=-40, temp_max=85, hum_min=0, hum_max=100,
    )
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def db_url(tmp_path):
    pg = os.environ.get("TEST_DATABASE_URL")
    if not pg:
        yield f"sqlite:///{tmp_path}/test.db"
        return
    # Isolate each test in its own schema-free database state.
    from app.db import make_engine
    eng = make_engine(_db_url(pg))
    metadata.drop_all(eng)
    eng.dispose()
    yield pg
    eng = make_engine(_db_url(pg))
    metadata.drop_all(eng)
    eng.dispose()


def make_client(db_url, **kw):
    app = create_app(settings(db_url, **kw))
    return TestClient(app), app.state.engine


def count(engine, table=records):
    with engine.connect() as c:
        return c.execute(select(func.count()).select_from(table)).scalar_one()


def make_record(node="00A1B2C3", seq=1, temp=21.5, hum=40.0, seen_at=None, origin="R2", hops=1):
    t, h = round(temp * 100), round(hum * 100)
    raw = (bytes.fromhex(node) + t.to_bytes(2, "big", signed=True) + h.to_bytes(2, "big")
           + bytes([1]) + seq.to_bytes(4, "big") + b"\0\0" + bytes([hops])).hex()
    return {
        "id": f"{node}-{seq}-{raw[8:18]}", "node": node, "seq": seq,
        "temp": round(temp, 2), "hum": round(hum, 2), "status": 1, "hops": hops, "raw": raw,
        "sink": "84:71:27:AA:BB:01", "origin": origin,
        "seen_at": seen_at if seen_at is not None else time.time(),
    }


def post(client, router_id, recs, sent_at=None, **params):
    return client.post("/ingest", params=params, json={
        "router_id": router_id, "sent_at": sent_at or time.time(), "records": recs,
    })


# ---- the handoff's curl checks ---------------------------------------------

def test_heartbeat_stores_nothing_but_logs_liveness(db_url):
    client, eng = make_client(db_url)
    r = post(client, "R1", [], sent_at=1759930000)
    assert r.status_code == 200 and r.json()["heartbeat"] is True
    assert count(eng) == 0
    with eng.connect() as c:
        row = c.execute(select(routers).where(routers.c.router_id == "R1")).one()
    assert row.last_heartbeat is not None and row.posts == 1


def test_one_record_stored(db_url):
    client, eng = make_client(db_url)
    r = post(client, "R1", [EXAMPLE], sent_at=1759930012.4)
    assert r.status_code == 200 and r.json()["new"] == 1
    with eng.connect() as c:
        row = c.execute(select(records)).one()
    assert row.id == EXAMPLE["id"] and row.temp == 21.53 and row.hum == 45.2
    assert row.via_router == "R1" and row.origin == "R2" and row.seen_at == 1759929987.1
    assert row.sent_at == 1759930012.4 and row.received_at > 1759930012


def test_duplicate_from_other_router_first_write_wins(db_url):
    client, eng = make_client(db_url)
    assert post(client, "R1", [EXAMPLE]).status_code == 200
    r = post(client, "R3", [{**EXAMPLE, "hops": 4}])
    assert r.status_code == 200 and r.json()["duplicates"] == 1
    assert count(eng) == 1
    with eng.connect() as c:
        row = c.execute(select(records)).one()
        assert (row.via_router, row.hops) == ("R1", 2)
        r3 = c.execute(select(routers).where(routers.c.router_id == "R3")).one()
        assert r3.records_delivered == 0 and r3.duplicates_delivered == 1
    assert count(eng, record_paths) == 2  # the alternative path is logged
    # exact resend (lost 2xx) doesn't add a path
    assert post(client, "R1", [EXAMPLE]).status_code == 200
    assert count(eng, record_paths) == 2


@pytest.mark.parametrize("body", [
    {"records": "nope"},
    {"router_id": "R1", "records": "nope"},
    {"router_id": "", "records": []},
    {"router_id": "x" * 65, "records": []},
    {"router_id": 5, "records": []},
    [1, 2, 3],
])
def test_malformed_400(db_url, body):
    client, eng = make_client(db_url)
    assert client.post("/ingest", json=body).status_code == 400
    assert count(eng) == 0


def test_invalid_json_400(db_url):
    client, eng = make_client(db_url)
    r = client.post("/ingest", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


# ---- batch semantics ---------------------------------------------------------

def test_200_records_all_stored_fast(db_url):
    client, eng = make_client(db_url)
    batch = [make_record(node=f"0000000{i % 5}", seq=i) for i in range(200)]
    t0 = time.perf_counter()
    r = post(client, "R1", batch)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and r.json()["new"] == 200
    assert count(eng) == 200
    assert elapsed < 2, f"took {elapsed:.2f}s"


def test_storage_failure_mid_batch_is_not_2xx_and_stores_nothing(db_url):
    client, eng = make_client(db_url)
    batch = [make_record(seq=i) for i in range(200)]
    poison = sorted(r["id"] for r in batch)[150]  # inserted late in the batch
    with eng.begin() as c:
        if eng.dialect.name == "sqlite":
            c.execute(text(
                f"CREATE TRIGGER boom BEFORE INSERT ON records WHEN NEW.id = '{poison}' "
                "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"))
        else:
            c.execute(text(
                "CREATE OR REPLACE FUNCTION boom() RETURNS trigger AS $$ BEGIN "
                f"IF NEW.id = '{poison}' THEN RAISE EXCEPTION 'simulated failure'; END IF; "
                "RETURN NEW; END $$ LANGUAGE plpgsql"))
            c.execute(text("CREATE TRIGGER boom BEFORE INSERT ON records FOR EACH ROW EXECUTE FUNCTION boom()"))
    r = post(client, "R1", batch)
    assert r.status_code == 503
    assert count(eng) == 0 and count(eng, record_paths) == 0
    with eng.connect() as c:  # router stats also rolled back
        assert c.execute(select(func.count()).select_from(routers)).scalar_one() == 0


def test_bad_record_is_quarantined_not_blocking(db_url):
    client, eng = make_client(db_url)
    good = make_record(seq=1)
    r = post(client, "R1", [good, {**make_record(seq=2), "seq": "banana"}, "not-an-object"])
    assert r.status_code == 200
    j = r.json()
    assert (j["new"], j["quarantined"]) == (1, 2)
    alerts = client.get("/api/alerts").json()
    assert any(a["kind"] == "quarantined_records" for a in alerts)


def test_duplicate_id_inside_one_batch(db_url):
    client, eng = make_client(db_url)
    rec = make_record(seq=7)
    r = post(client, "R1", [rec, {**rec, "hops": 9}])
    assert r.status_code == 200 and count(eng) == 1


def test_unknown_fields_ignored_and_missing_optional_ok(db_url):
    client, eng = make_client(db_url)
    rec = {**make_record(seq=3), "battery": 88, "future": {"x": 1}}
    del rec["sink"]
    r = client.post("/ingest", json={"router_id": "R1", "sent_at": 1, "records": [rec], "fw": "2.0"})
    assert r.status_code == 200 and r.json()["new"] == 1


def test_odd_timestamps_accepted(db_url):
    client, eng = make_client(db_url)
    recs = [make_record(seq=1, seen_at=0), make_record(seq=2, seen_at=4_102_444_800)]  # 1970, 2100
    r = post(client, "R1", recs, sent_at=12345)
    assert r.status_code == 200 and r.json()["new"] == 2
    routers_ = client.get("/api/routers").json()
    assert routers_[0]["delay_bad"] is True
    # a router clock in the future doesn't pin "latest" or hide new data
    later = make_record(seq=3, seen_at=time.time())
    post(client, "R1", [later])
    assert client.get("/api/nodes").json()[0]["seq"] == 3


# ---- ordering --------------------------------------------------------------------

def test_out_of_order_arrival_shows_in_seen_order(db_url):
    client, eng = make_client(db_url)
    base = time.time() - 3600
    recs = [make_record(seq=i, seen_at=base + i * 10, temp=20 + i / 10) for i in range(30)]
    shuffled = recs[:]
    random.Random(1).shuffle(shuffled)
    for i in range(0, 30, 7):  # several late, out-of-order batches
        assert post(client, "R1", shuffled[i:i + 7]).status_code == 200
    got = client.get("/api/records", params={"node": "00A1B2C3", "desc": False, "limit": 100}).json()
    assert [r["seq"] for r in got] == list(range(30))
    by_seq = client.get("/api/records", params={"order": "seq", "desc": True}).json()
    assert by_seq[0]["seq"] == 29
    assert client.get("/api/nodes").json()[0]["seq"] == 29  # latest = latest heard, not latest arrived
    series = client.get("/api/nodes/00A1B2C3/series", params={"hours": 2}).json()
    assert [p["t"] for p in series] == sorted(p["t"] for p in series)
    assert sum(p["n"] for p in series) == 30


# ---- auth ------------------------------------------------------------------------

def test_ingest_token_query_and_bearer(db_url):
    client, eng = make_client(db_url, ingest_token="s3cret")
    assert post(client, "R1", []).status_code == 401
    assert post(client, "R1", [], token="wrong").status_code == 401
    assert post(client, "R1", [], token="s3cret").status_code == 200
    r = client.post("/ingest", headers={"Authorization": "Bearer s3cret"},
                    json={"router_id": "R1", "sent_at": 1, "records": []})
    assert r.status_code == 200


def test_dashboard_basic_auth(db_url):
    client, eng = make_client(db_url, dashboard_password="pw")
    assert client.get("/").status_code == 401
    assert client.get("/api/nodes").status_code == 401
    hdr = {"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()}
    assert client.get("/", headers=hdr).status_code == 200
    assert post(client, "R1", []).status_code == 200  # ingest unaffected
    assert client.get("/healthz").status_code == 200


# ---- decoding ----------------------------------------------------------------------

def test_decode_example_both_orders():
    assert decode_raw(EXAMPLE["raw"], "be") == {
        "node": "00A1B2C3", "temp": 21.53, "hum": 45.2, "status": 1, "seq": 1000, "hops": 2}
    assert decode_raw(EXAMPLE["raw"], "le")["temp"] == 268.88  # the "absurd" value from the handoff
    assert decode_raw("zz", "be") is None and decode_raw("00", "be") is None


def test_decode_mode_raw_le_and_redecode(db_url, monkeypatch):
    client, eng = make_client(db_url, decode_mode="raw_le")
    post(client, "R1", [EXAMPLE])
    with eng.connect() as c:
        row = c.execute(select(records)).one()
    assert row.temp == 268.88 and row.temp_router == 21.53
    from app import cli
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DECODE_MODE", "router")
    assert cli.main(["redecode", "--mode", "router"]) == 0
    with eng.connect() as c:
        assert c.execute(select(records.c.temp)).scalar_one() == 21.53


# ---- dashboard renders ---------------------------------------------------------------

def test_pages_render(db_url):
    client, eng = make_client(db_url)
    assert client.get("/").status_code == 200  # empty state
    post(client, "R1", [EXAMPLE, make_record(seq=5, temp=99),
                        make_record(node="0000BEEF", seq=1, seen_at=time.time() - 7200)])
    post(client, "R3", [])
    html = client.get("/").text
    assert "00A1B2C3" in html and "R1" in html and "R2" in html
    assert client.get("/nodes/00A1B2C3").status_code == 200
    assert client.get("/nodes/00a1b2c3").status_code == 200
    assert client.get("/routers/R1").status_code == 200
    assert client.get("/routers/R2").status_code == 200  # origin-only router
    assert client.get("/nodes/DEADBEEF").status_code == 404
    rec = client.get(f"/api/records/{EXAMPLE['id']}").json()
    assert rec["paths"][0]["via_router"] == "R1"
    r2 = next(r for r in client.get("/api/routers").json() if r["router_id"] == "R2")
    assert r2["records_originated"] == 3 and r2["is_uplink"] is False
    kinds = {a["kind"] for a in client.get("/api/alerts").json()}
    assert "temp_out_of_range" in kinds and "stale_tag" in kinds


# ---- hop accounting (route, BLE / router / total hops) --------------------------

ROUTED = {**EXAMPLE, "route": ["R2", "R4", "R1"], "ble_hops": 2, "router_hops": 2, "total_hops": 99}


def test_hop_fields_derived_from_route(db_url):
    client, eng = make_client(db_url)
    assert post(client, "R1", [ROUTED]).status_code == 200
    with eng.connect() as c:
        row = c.execute(select(records)).one()
        path = c.execute(select(record_paths)).one()
    # router's total_hops (99) is ignored: totals are derived here
    assert (row.ble_hops, row.router_hops, row.total_hops) == (2, 2, 4)
    assert path.router_hops == 2
    rec = client.get(f"/api/records/{EXAMPLE['id']}").json()
    assert rec["route"] == ["R2", "R4", "R1"] and rec["paths"][0]["route"] == ["R2", "R4", "R1"]
    listed = client.get("/api/records").json()[0]
    assert listed["route"] == ["R2", "R4", "R1"] and listed["total_hops"] == 4
    node = client.get("/api/nodes").json()[0]
    assert (node["ble_hops"], node["router_hops"], node["total_hops"]) == (2, 2, 4)


def test_hop_fields_from_older_routers_without_route(db_url):
    client, eng = make_client(db_url)
    own = make_record(seq=1, origin="R1", hops=3)          # origin delivered it itself
    relayed = make_record(seq=2, origin="R2", hops=1)      # path in between unknown
    told = {**make_record(seq=3, origin="R2", hops=1), "router_hops": 1}
    assert post(client, "R1", [own, relayed, told]).status_code == 200
    with eng.connect() as c:
        got = {r.seq: (r.ble_hops, r.router_hops, r.total_hops, r.route)
               for r in c.execute(select(records))}
    assert got[1] == (3, 0, 3, None)
    assert got[2] == (1, None, None, None)
    assert got[3] == (1, 1, 2, None)
    html = client.get("/nodes/00A1B2C3").text
    assert "path in between not reported" in html          # "R2 → ? → R1"


@pytest.mark.parametrize("bad_route", ["R1", [], [1, 2], ["x" * 65], ["R"] * 65, {"a": 1}])
def test_malformed_route_is_dropped_not_quarantined(db_url, bad_route):
    client, eng = make_client(db_url)
    r = post(client, "R1", [{**EXAMPLE, "route": bad_route, "router_hops": "lots"}])
    assert r.status_code == 200 and r.json()["new"] == 1 and r.json()["quarantined"] == 0
    with eng.connect() as c:
        row = c.execute(select(records)).one()
    assert row.route is None and row.router_hops is None and row.ble_hops == 2


def test_hop_display_and_stats(db_url):
    client, eng = make_client(db_url)
    older = {**make_record(seq=7, hops=0, seen_at=EXAMPLE["seen_at"] - 600), "route": ["R1"]}
    post(client, "R1", [ROUTED, older])
    html = client.get("/").text
    for text_ in ("BLE hops", "Router hops", "Total hops", "R4", "→"):
        assert text_ in html
    node_html = client.get("/nodes/00A1B2C3").text
    assert "Hops, all readings" in node_html and "avg 2.0" in node_html
    stats = client.get("/api/nodes/00A1B2C3/hops").json()
    assert stats["n"] == 2 and stats["total_max"] == 4 and stats["router_avg"] == 1.0
    assert "R4" in client.get("/routers/R1").text


def test_migration_adds_hop_columns_to_an_existing_database(tmp_path):
    """A database created by the previous version must keep working after deploy."""
    from sqlalchemy import Column, Float, Integer, MetaData, String, Table, UniqueConstraint, inspect
    from app.db import make_engine
    url = f"sqlite:///{tmp_path}/old.db"
    eng = make_engine(url)
    old = MetaData()
    cols = [Column("id", String(80), primary_key=True), Column("node", String(16), nullable=False),
            Column("seq", Integer, nullable=False), Column("temp", Float), Column("hum", Float),
            Column("temp_router", Float), Column("hum_router", Float), Column("status", Integer),
            Column("hops", Integer), Column("raw", String(64)), Column("sink", String(64)),
            Column("origin", String(64)), Column("seen_at", Float), Column("sent_at", Float),
            Column("via_router", String(64), nullable=False), Column("received_at", Float, nullable=False)]
    old_records = Table("records", old, *cols)
    Table("record_paths", old, Column("pk", Integer, primary_key=True), Column("record_id", String(80)),
          Column("via_router", String(64)), Column("origin", String(64)), Column("sink", String(64)),
          Column("hops", Integer), Column("seen_at", Float), Column("received_at", Float),
          UniqueConstraint("record_id", "via_router", "origin", "sink", "hops", name="uq_record_path"))
    old.create_all(eng)
    with eng.begin() as c:
        c.execute(old_records.insert(), [
            {"id": "a", "node": "N", "seq": 1, "hops": 2, "origin": "R1", "via_router": "R1", "received_at": 1.0},
            {"id": "b", "node": "N", "seq": 2, "hops": 1, "origin": "R2", "via_router": "R1", "received_at": 1.0},
        ])
    eng.dispose()

    client, eng = make_client(url)                       # app start runs the migration
    names = {c["name"] for c in inspect(eng).get_columns("records")}
    assert {"route", "ble_hops", "router_hops", "total_hops"} <= names
    assert {"route", "router_hops"} <= {c["name"] for c in inspect(eng).get_columns("record_paths")}
    with eng.connect() as c:
        got = {r.id: (r.ble_hops, r.router_hops, r.total_hops) for r in c.execute(select(records))}
    assert got == {"a": (2, 0, 2), "b": (1, None, None)}
    assert post(client, "R1", [ROUTED]).status_code == 200   # new-format ingest works
    assert client.get("/").status_code == 200
    # and starting again is a no-op
    from app.db import migrate
    assert migrate(eng) == []


# ---- deployment guards -----------------------------------------------------------

def test_require_postgres_refuses_sqlite(tmp_path):
    with pytest.raises(RuntimeError, match="REQUIRE_POSTGRES"):
        create_app(settings(f"sqlite:///{tmp_path}/x.db", require_postgres=True))


def test_permission_denied_gives_actionable_error(monkeypatch):
    from app import db as dbmod

    class FakeEngine:
        class url:
            username = "db"

    def boom(_):
        raise Exception("(psycopg.errors.InsufficientPrivilege) permission denied for schema public")

    monkeypatch.setattr(dbmod.metadata, "create_all", boom)
    with pytest.raises(RuntimeError, match="GRANT USAGE, CREATE ON SCHEMA public TO db"):
        dbmod.init_db(FakeEngine())
