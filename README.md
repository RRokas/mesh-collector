# Mesh collector

Internet-side receiver for the RUTX10 BLE mesh. Replaces `mesh_collector.py`.

- `POST /ingest` — the router uplink (contract in `docs/mesh_collector_handoff.md`, unchanged)
- `/` — dashboard: alerts, latest reading per tag, router overview
- `/nodes/<NODE>` — temperature / humidity charts and recent readings for one tag
- `/routers/<ID>` — one router: contact, heartbeats, what it heard and delivered
- `/api/...` — JSON API (interactive docs at `/api/docs`)
- `/healthz` — liveness + DB check

Python 3.12, FastAPI, SQLAlchemy. Storage is **SQLite** (default, one file) or **PostgreSQL** (set `DATABASE_URL`). Same code, same tests, both verified.

---

## How ingest behaves

| Situation | Response | Stored |
|---|---|---|
| Empty `records` (heartbeat) | 200 | nothing; router's `last_heartbeat` updated |
| Valid batch | 200 after the whole batch is committed | new ids inserted |
| Record id already stored (other router, or lost 2xx) | 200 | nothing new; first write wins; the extra path goes to `record_paths` |
| Same id twice in one batch | 200 | first occurrence |
| Not JSON / not an object / bad `router_id` / `records` not an array | 400 | nothing |
| More than `MAX_RECORDS_PER_POST` (1000) records, or body over 2 MB | 413 | nothing |
| Wrong/missing token (when `INGEST_TOKEN` is set) | 401 | nothing |
| DB error anywhere in the batch | 503 | nothing (transaction rolled back) |

**One deliberate addition to the contract:** a record that is inside a structurally valid batch but fails per-record validation (e.g. `seq: "banana"`) is written to a `quarantine` table and the batch still gets 200. Answering 400 there would make the router resend that same batch forever and wedge its uplink. Quarantined records raise an alert on the dashboard and can be listed with `python -m app.cli quarantine`.

Everything else follows the handoff:
- unknown fields are ignored; odd `seen_at` / `sent_at` are stored as-is, never rejected
- our own `received_at` is stored on every record
- ordering is by `seen_at` (or `seq`), never by arrival
- the whole batch is one transaction; Postgres gets a 10 s statement timeout and SQLite a 10 s busy timeout, so a stuck DB fails with 503 inside the router's 15 s window rather than timing out

**Clocks.** "Latest reading" and staleness use `seen_at`, clamped to our receive time, so a router whose clock is in the future can't pin a tag's "latest" forever. Each uplink router's clock skew (`sent_at` minus our clock at receipt) is shown on the dashboard and alerts above `CLOCK_SKEW_WARN_SECONDS`. Charts can switch between "time heard" and "time received" for when a clock is wrong.

---

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest                                       # 22 tests, SQLite
uvicorn app.main:create_app --factory --reload
# -> http://127.0.0.1:8000  (data in ./data/mesh.db)
```

Run the tests against Postgres too:

```bash
TEST_DATABASE_URL=postgresql://user:pw@localhost/mesh_test pytest
```

---

## Deploy on DigitalOcean

There are two paths. **Pick A for now** unless you already know the routers can do HTTPS.

The deciding factor: App Platform only serves HTTPS and 301-redirects plain HTTP, with no way to turn that off. The routers' uplink is plain HTTP until their Python has the `ssl` module. Check on a router:

```bash
python3 -c "import ssl; print(ssl.OPENSSL_VERSION)"
```

If that prints a version, both paths work. If it raises `ModuleNotFoundError`, use A.

### A. One Droplet with Docker Compose (SQLite, Caddy for HTTPS)

Works today with plain-HTTP routers: Caddy serves `/ingest` on both HTTP and HTTPS and redirects everything else (dashboard, API) to HTTPS.

1. Create a Droplet from the **Docker** Marketplace image (Ubuntu + Docker preinstalled). The smallest size is enough for this volume.
2. DNS: point an A record (e.g. `mesh.yourdomain.lt`) at the Droplet's IP. No domain? Use `<droplet-ip>.sslip.io` (e.g. `164.90.1.2.sslip.io`), which resolves to that IP and gets a real certificate.
3. Firewall: allow 22, 80, 443 (Droplet → Networking → Firewalls, or `ufw allow 22,80,443/tcp`).
4. On the Droplet:
   ```bash
   git clone <your repo> mesh-collector && cd mesh-collector
   cp .env.example .env
   nano .env            # DOMAIN, INGEST_TOKEN, DASHBOARD_PASSWORD
   docker compose up -d --build
   docker compose logs -f app
   ```
5. Open `https://<DOMAIN>/` (user `admin`, your `DASHBOARD_PASSWORD`).

**Ship a change:**

```bash
git pull && docker compose up -d --build
```

The router keeps retrying during the few seconds the app restarts, so nothing is lost.

**Backups:** the whole database is `./data/mesh.db`. Turn on Droplet backups, and/or add a nightly cron job:

```bash
docker compose exec -T app python -c "import sqlite3; s=sqlite3.connect('/srv/data/mesh.db'); d=sqlite3.connect('/srv/data/backup.db'); s.backup(d)"
```

**Moving to Postgres later:** create a Managed PostgreSQL database, set `DATABASE_URL=postgresql://...?...sslmode=require` in `.env`, and `docker compose up -d`. Tables are created on startup. Existing SQLite data isn't migrated automatically.

### B. App Platform (auto-deploy on push to `main`, managed Postgres, HTTPS only)

Cost: app `apps-s-1vcpu-0.5gb` ($5/mo) + smallest managed PostgreSQL cluster ($15/mo).

**Why not an App Platform "dev database" ($7/mo)?** They only offer PostgreSQL 15+, where the app's database user may not create tables in the `public` schema, and a dev database gives you no admin login to `GRANT` it. The app would crash on startup. A managed cluster gives you the `doadmin` user, which can create tables.

**1. Create the database** (once): Databases → Create → PostgreSQL 17, region **Frankfurt (FRA1)**, smallest single-node plan, name `mesh-db`. When it's ready, open it → **Connection details** → *Public network*, user `doadmin`, database `defaultdb` → copy the **Connection string** (`postgresql://doadmin:…@…:25060/defaultdb?sslmode=require`).

**2. Create the app** with either method:

- *Control panel:* Create → App Platform → GitHub → authorise DigitalOcean for `RRokas/mesh-collector` → branch `main`, **Autodeploy** ticked → it detects the Dockerfile. Set the region to Frankfurt, size to the $5 plan, HTTP port **8000**. Then Settings → App Spec → Edit, and paste `.do/app.yaml` to pick up the health check and settings.
- *CLI:* `doctl apps create --spec .do/app.yaml` (your DigitalOcean account must already be linked to GitHub, which the control panel asks you to do the first time).

**3. Add the secrets:** App → Settings → `web` → Environment Variables → Edit. Add these, each with **Encrypt** ticked:

| Key | Value |
|---|---|
| `DATABASE_URL` | connection string from step 1 |
| `INGEST_TOKEN` | `python3 -c "import secrets; print(secrets.token_urlsafe(24))"` |
| `DASHBOARD_PASSWORD` | your choice |

Saving redeploys. Until `DATABASE_URL` is set the app refuses to start (`REQUIRE_POSTGRES=1`) instead of quietly writing to the container's disk, which App Platform wipes on every deploy.

**4. Lock down the database:** mesh-db → Settings → Trusted sources → add the `mesh-collector` app.

**5. Check it:** open `https://<app>.ondigitalocean.app/healthz` → `{"ok":true}`. From then on, every `git push` to `main` builds and rolls out a new version. If the new version fails `/healthz`, the old one keeps serving and you get a "deployment failed" email.

Router URL: `https://<app>.ondigitalocean.app/ingest?token=<INGEST_TOKEN>`. This needs `ssl` on the routers, because App Platform redirects plain HTTP.

**Notes:**
- `.do/app.yaml` is read by `doctl` and the spec editor, not on every push. Pushes deploy code; spec changes go through `doctl apps update <id> --spec .do/app.yaml` or the editor.
- If you connect as a non-admin database user instead, the app stops at startup and logs the exact `GRANT` to run as `doadmin`.

---

## Point the routers at it

On each router that has internet:

```bash
python3 ble_web.py --uplink "http://mesh.yourdomain.lt/ingest?token=<INGEST_TOKEN>"
```

Use `https://` once the routers have `ssl` (path B requires it). The token is in the URL because the routers send no auth header today. `Authorization: Bearer <token>` is also accepted, for when `mesh.py` gets an `--uplink-token` option. Over plain HTTP the token travels in clear text: that's no worse than the current no-auth setup, and it goes away once the routers use HTTPS.

Then follow "End-to-end test with real routers" in the handoff. The dashboard's router table should show the router as **online** within a minute (heartbeat).

---

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./data/mesh.db` (`/srv/data/mesh.db` in Docker) | `postgresql://…` also accepted as-is |
| `INGEST_TOKEN` | *(empty = open)* | required `?token=` / Bearer value for `/ingest` |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | `admin` / *(empty = open)* | HTTP Basic auth for dashboard and API |
| `DECODE_MODE` | `router` | `router`, `raw_be` or `raw_le`, see below |
| `STALE_TAG_MINUTES` | 30 | tag alert threshold |
| `SILENT_ROUTER_MINUTES` | 5 | uplink router alert threshold (heartbeat is ~1/min) |
| `CLOCK_SKEW_WARN_SECONDS` | 120 | router clock alert threshold |
| `TEMP_MIN` / `TEMP_MAX` | -40 / 85 | out-of-range alert, °C |
| `HUM_MIN` / `HUM_MAX` | 0 / 100 | out-of-range alert, %RH |
| `MAX_RECORDS_PER_POST` / `MAX_BODY_BYTES` | 1000 / 2000000 | abuse limits (routers send ≤200 / ~70 KB) |

## Byte order of temp/hum

Every record keeps three things: the router's decoded `temp_router` / `hum_router`, the untouched `raw` payload, and the **effective** `temp` / `hum` that the dashboard shows. `DECODE_MODE` decides the effective values for new records.

If readings look absurd (e.g. 268.88 °C), check one payload both ways:

```bash
python -m app.cli decode 00a1b2c3086911a801000003e8000002
# be {"temp": 21.53, "hum": 45.2, ...}
# le {"temp": 268.88, ...}
```

Then switch the deployment and fix the history in one go:

```bash
# set DECODE_MODE=raw_le in .env, then:
docker compose exec app python -m app.cli redecode --mode raw_le
docker compose up -d
```

`redecode --mode router` restores the router's values.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/nodes` | latest reading per tag, with `stale` / out-of-range flags |
| `GET /api/nodes/{node}/series?hours=24&time_field=seen_at` | bucketed averages (~600 points max) for charts |
| `GET /api/records?node=&router=&since=&until=&order=seen_at\|seq\|received_at&desc=true&limit=100&offset=0` | raw records; `router` matches origin or deliverer |
| `GET /api/records/{id}` | one record plus every path it arrived by |
| `GET /api/routers` | uplink and origin routers, last contact/heartbeat, counts, clock skew |
| `GET /api/alerts` | `[]` when healthy, so an uptime monitor can poll it |

## Data model

- `records`: one row per reading `id`, first write wins. Includes `via_router` (who delivered it first) and `received_at`.
- `record_paths`: every distinct (deliverer, origin, sink, hops) a reading arrived by.
- `nodes`, `routers`: summaries maintained in the same transaction as the insert, so the overview stays fast as records grow.
- `quarantine`: per-record validation failures with the original JSON.

## Iterating

- `pytest` covers every acceptance check in the handoff (heartbeat, single record, duplicate via another router, malformed 400, 200-record all-or-nothing, injected mid-batch DB failure → 503 with nothing stored, out-of-order arrival) plus auth, decode modes and page rendering.
- Tables are created on startup (`create_all`), which adds new tables but doesn't alter existing ones. The first time you change a column, add Alembic (`alembic init`, point it at `app.db.metadata`).
- Alerts are computed on request, not pushed. For phone or email notifications, point an uptime monitor at `/api/alerts`, or add a small background task that posts state changes to a webhook.
