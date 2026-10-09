"""HTTP app: router ingest + dashboard + JSON API."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from starlette.concurrency import run_in_threadpool

from . import queries
from .config import Settings
from .db import init_db, make_engine, records
from .fmt import fmt_duration
from .ingest import BadRequest, parse_body, store_batch

log = logging.getLogger("mesh_collector")
HERE = Path(__file__).parent


def _static_version(static_dir: Path) -> str:
    h = hashlib.sha1()
    for p in sorted(static_dir.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(static_dir).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    s = settings or Settings.from_env()
    if s.require_postgres and not s.database_url.startswith("postgresql"):
        # On App Platform the container disk is wiped on every deploy, so a
        # missing DATABASE_URL must fail loudly rather than fall back to SQLite.
        raise RuntimeError("REQUIRE_POSTGRES is set but DATABASE_URL is not a postgresql:// URL")
    engine = make_engine(s.database_url)
    init_db(engine)

    app = FastAPI(title="Mesh collector", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = s
    app.state.engine = engine
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["dur"] = fmt_duration
    # Versioned static URLs (/static/style.css?v=<hash>): every deploy that
    # changes a static file changes its URL, so phones that cache CSS/JS
    # aggressively (iOS Safari) can't keep showing an old stylesheet.
    static_version = _static_version(HERE / "static")
    templates.env.globals["asset"] = lambda path: f"/static/{path}?v={static_version}"

    if not s.ingest_token:
        log.warning("INGEST_TOKEN is not set: /ingest accepts unauthenticated POSTs")
    if not s.dashboard_password:
        log.warning("DASHBOARD_PASSWORD is not set: dashboard and API are public")

    # ---- auth --------------------------------------------------------------
    basic = HTTPBasic(auto_error=False)

    def dashboard_auth(creds: Optional[HTTPBasicCredentials] = Depends(basic)) -> None:
        if not s.dashboard_password:
            return
        ok = creds is not None and secrets.compare_digest(
            creds.username.encode(), s.dashboard_user.encode()
        ) & secrets.compare_digest(creds.password.encode(), s.dashboard_password.encode())
        if not ok:
            raise HTTPException(401, "auth required", headers={"WWW-Authenticate": 'Basic realm="mesh"'})

    def ingest_token_ok(request: Request) -> bool:
        if not s.ingest_token:
            return True
        supplied = request.query_params.get("token", "")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = supplied or auth[7:].strip()
        return hmac.compare_digest(supplied.encode(), s.ingest_token.encode())

    def client_ip(request: Request) -> Optional[str]:
        for h in ("do-connecting-ip", "x-forwarded-for", "x-real-ip"):
            v = request.headers.get(h)
            if v:
                return v.split(",")[0].strip()[:64]
        return request.client.host if request.client else None

    # ---- ingest --------------------------------------------------------------
    @app.post("/ingest", include_in_schema=False)
    async def ingest(request: Request):
        t0 = time.perf_counter()
        if not ingest_token_ok(request):
            return JSONResponse({"ok": False, "error": "bad token"}, status_code=401)
        clen = request.headers.get("content-length")
        if clen and clen.isdigit() and int(clen) > s.max_body_bytes:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        body = await request.body()
        if len(body) > s.max_body_bytes:
            return JSONResponse({"ok": False, "error": "body too large"}, status_code=413)
        try:
            router_id, sent_at, recs = parse_body(body, s.max_records_per_post)
        except BadRequest as e:
            log.info("ingest rejected (%s): %s", e.status, e)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=e.status)
        try:
            res = await run_in_threadpool(
                store_batch, engine, router_id, sent_at, recs, s.decode_mode, client_ip(request)
            )
        except Exception:  # noqa: BLE001 - any storage failure must be non-2xx
            log.exception("ingest storage failed for %s (%d records)", router_id, len(recs))
            return JSONResponse({"ok": False, "error": "storage failure, retry later"}, status_code=503)
        ms = (time.perf_counter() - t0) * 1000
        log.info(
            "ingest %s: %d received, %d new, %d dup, %d quarantined, %.0f ms",
            router_id, res.received, res.new, res.duplicates, res.quarantined, ms,
        )
        return JSONResponse(res.as_dict())

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": type(e).__name__}, status_code=503)
        return {"ok": True}

    # ---- dashboard -------------------------------------------------------------
    auth = [Depends(dashboard_auth)]

    @app.get("/debug", response_class=HTMLResponse, dependencies=auth, include_in_schema=False)
    def index(request: Request):
        now = time.time()
        return templates.TemplateResponse(request, "index.html", {
            "nodes": queries.node_overview(engine, s, now),
            "routers": queries.router_overview(engine, s, now),
            "alerts": queries.alerts(engine, s, now),
            "counts": queries.counts(engine),
            "s": s,
            "now": now,
        })

    @app.get("/nodes/{node}", response_class=HTMLResponse, dependencies=auth, include_in_schema=False)
    def node_page(request: Request, node: str):
        node = node.upper()
        overview = next((n for n in queries.node_overview(engine, s) if n["node"] == node), None)
        if overview is None:
            raise HTTPException(404, "unknown node")
        return templates.TemplateResponse(request, "node.html", {
            "node": overview,
            "hops": queries.node_hop_stats(engine, node),
            "recent": queries.list_records(engine, node=node, limit=200),
            "s": s,
        })

    @app.get("/routers/{router_id}", response_class=HTMLResponse, dependencies=auth, include_in_schema=False)
    def router_page(request: Request, router_id: str):
        r = next((x for x in queries.router_overview(engine, s) if x["router_id"] == router_id), None)
        if r is None:
            raise HTTPException(404, "unknown router")
        return templates.TemplateResponse(request, "router.html", {
            "r": r,
            "recent": queries.list_records(engine, router=router_id, order="received_at", limit=200),
            "s": s,
        })

    @app.get("/demo", include_in_schema=False)
    def demo_moved():
        return RedirectResponse("/", status_code=308)   # the demo is the front page now

    @app.get("/", response_class=HTMLResponse, dependencies=auth, include_in_schema=False)
    def demo(request: Request):
        """Animated topology: replays each received reading along its real path
        (tag → BLE relays → routers → internet). Data comes from /api/records
        and /api/routers in the browser; with no data it plays a sample route."""
        return templates.TemplateResponse(request, "demo.html", {"s": s})

    # ---- JSON API --------------------------------------------------------------
    @app.get("/api/nodes", dependencies=auth, tags=["api"])
    def api_nodes():
        return queries.node_overview(engine, s)

    @app.get("/api/nodes/{node}/series", dependencies=auth, tags=["api"])
    def api_series(
        node: str,
        hours: float = Query(24, gt=0, le=24 * 366),
        until: Optional[float] = None,
        time_field: Literal["seen_at", "received_at"] = "seen_at",
    ):
        end = until or time.time()
        return queries.node_series(engine, node.upper(), end - hours * 3600, end, time_field)

    @app.get("/api/records", dependencies=auth, tags=["api"])
    def api_records(
        node: Optional[str] = None,
        router: Optional[str] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        order: Literal["seen_at", "seq", "received_at"] = "seen_at",
        desc: bool = True,
        limit: int = Query(100, ge=1, le=5000),
        offset: int = Query(0, ge=0),
    ):
        return queries.list_records(engine, node, router, since, until, order, desc, limit, offset)

    @app.get("/api/records/{record_id}", dependencies=auth, tags=["api"])
    def api_record(record_id: str):
        with engine.connect() as conn:
            row = conn.execute(select(records).where(records.c.id == record_id)).first()
        if row is None:
            raise HTTPException(404, "unknown record")
        rec = dict(row._mapping)
        rec["route"] = queries.decode_route(rec.get("route"))
        return {**rec, "paths": queries.record_paths_for(engine, record_id)}

    @app.get("/api/nodes/{node}/hops", dependencies=auth, tags=["api"])
    def api_node_hops(node: str):
        """Average / max BLE, router and total hops for one tag."""
        return queries.node_hop_stats(engine, node)

    @app.get("/api/routers", dependencies=auth, tags=["api"])
    def api_routers():
        return queries.router_overview(engine, s)

    @app.get("/api/alerts", dependencies=auth, tags=["api"])
    def api_alerts():
        """Poll this from an uptime monitor: it returns [] when all is well."""
        return queries.alerts(engine, s)

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
