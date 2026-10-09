# Handoff: internet-side collector for the BLE router mesh

## What exists already

A "poor man's mesh" moves sensor data from BLE tags to the internet:

```
BLE tags ──(BLE, multi-hop)──> "Sink" BLE device ──(BLE advertisement)──> Teltonika RUTX10 router
RUTX10 router <──(HTTP, store-and-forward between routers)──> other RUTX10 routers
any router that has internet ──(HTTP POST)──> THE APP TO BUILD
```

- Each router runs the same Python server (`ble_web.py` + `mesh.py`, standard library only, Python 3.12 on RutOS). It decodes tag readings from its local Sink and stores them as **records**.
- Routers sync records with manually configured peers (static IPs). Records spread hop by hop until some router with internet can deliver them.
- A router with internet POSTs batches of pending records to its configured **uplink URL**. A 2xx response marks the whole batch as delivered, and that "delivered" state spreads back through the mesh.
- `mesh_collector.py` is a minimal reference receiver (stdlib `http.server`, dedupes by id, appends to a JSONL file, plain HTML table of recent records with hop columns at `/`). The new app replaces it.

**The router side is done. Don't change the wire format below without also changing `mesh.py`.**

## What to build

A web app on the internet that:

1. **Ingests** the routers' POSTs (contract below). This is the critical piece.
2. **Stores** records durably, de-duplicated by `id`.
3. **Shows** the data:
   - latest reading per tag (node)
   - temperature and humidity history per tag (charts)
   - which router heard each reading (`origin`) and which delivered it (`via_router`)
   - **hop counts per reading, each separately and as a sum**: BLE hops (beacon relays), router hops (router-to-router transfers) and total hops, plus the router route (e.g. `rutx10 → rutx11 → rutm16`). Aggregates such as average/max hops per tag or per route are a plus.
   - router overview: last contact, records delivered, last heartbeat
4. *(Optional)* An API to query records, and alerts: stale tags, routers silent for X minutes, out-of-range values.

Tech stack, hosting and database are open decisions for the new session.

## Ingest contract (implemented by the routers)

### Request

```
POST <uplink URL>              e.g. https://example.com/ingest
Content-Type: application/json
User-Agent: rutx-ble-mesh
Connection: close
```

Body:

```json
{
  "router_id": "RUTX10-a3f2",
  "sent_at": 1759930012.4,
  "records": [
    {
      "id": "00A1B2C3-1000-086911a801",
      "node": "00A1B2C3",
      "seq": 1000,
      "temp": 21.53,
      "hum": 45.2,
      "status": 1,
      "hops": 2,
      "raw": "00a1b2c3086911a801000003e8000002",
      "sink": "84:71:27:AA:BB:01",
      "origin": "RUTX10-7c01",
      "seen_at": 1759929987.1,
      "route": ["RUTX10-7c01", "RUTX10-9d22", "RUTX10-a3f2"],
      "ble_hops": 2,
      "router_hops": 2,
      "total_hops": 4
    }
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `router_id` | string (≤64) | Router making **this** POST (the one with internet). |
| `sent_at` | number | Unix seconds on that router's clock. |
| `records` | array | 0–200 records. **An empty array is a connectivity heartbeat**: answer 2xx and store nothing (but you may log router liveness). |
| `id` | string (≤80) | **Unique key.** Format: `<node>-<seq>-<hex of payload bytes 4..8>`. Treat it as an opaque string. |
| `node` | string | Source tag address, 8 hex digits, uppercase (32-bit). |
| `seq` | integer | Tag's packet sequence number (32-bit unsigned). |
| `temp` | number | °C, 2 decimals (decoded from signed hundredths). |
| `hum` | number | %RH, 2 decimals (decoded from hundredths). |
| `status` | integer 0–255 | Sensor status byte. **Bit meanings not yet defined** by the tag developer; store it raw. |
| `hops` | integer 0–255 | BLE hop count from the tag payload (byte 15). Same value as `ble_hops`; kept for compatibility. |
| `raw` | hex string (32 chars) | The 16-byte tag payload exactly as received. Keep it, so readings can be re-decoded later (see caveats). |
| `sink` | string | MAC of the BLE Sink the router heard it from. |
| `origin` | string | Router that first heard the reading over BLE. |
| `seen_at` | number | Unix seconds when `origin` first heard it (origin router's clock). |
| `route` | array of strings | Routers the record passed through, in order: `origin` first, the delivering router last. Each router appends its id when it receives the record from a peer. |
| `ble_hops` | integer | Beacon-to-beacon relays (= `hops`). |
| `router_hops` | integer | Router-to-router transfers = `len(route) - 1`. 0 if the router that heard it over BLE delivered it itself. |
| `total_hops` | integer | `ble_hops + router_hops`. |

The hop fields are computed by the router at send time. If any are missing (older router versions), derive them: `router_hops = len(route or [origin]) - 1`, `ble_hops = hops`. Exactly what the BLE hop count includes (e.g. whether the tag-to-sink hop counts as 1) is defined by the tag firmware. The sink-to-router step is in neither count.

Raw payload layout (16 bytes): 0–3 node address (big-endian), 4–5 temperature (signed, 0.01 °C), 6–7 humidity (0.01 %RH), 8 status, 9–12 sequence (big-endian), 13–14 reserved, 15 hop count.

### Response

- **Any 2xx** = the **whole batch** is delivered. The router marks every record in it as delivered and never sends them again (apart from the duplicate cases below). The response body is ignored.
- **Anything else, a timeout, or a dropped connection** = nothing delivered. The router retries the same records later.
- Therefore: **persist the entire batch (transactionally) before you answer 2xx.** Never answer 2xx on partial failure.
- The router waits **15 s** for a response. Answer well within that, or the router treats it as failed and resends.

### Router behaviour to design for

- **At-least-once delivery.** Duplicates happen when two routers with internet send the same record before syncing, or when a 2xx gets lost. **Upsert / ignore on conflict by `id`.** A duplicate is not an error: still answer 2xx.
- The same reading can arrive via different routers with different `hops`, `route`, `sink`, `origin` or `seen_at`. **First write wins**; optionally log the alternative paths.
- **No ordering guarantee.** Records arrive late (hours or days, after an outage) and out of order. Order by `seq` per node or by `seen_at`, never by arrival.
- **Retry with backoff:** after a failure the router retries after 15 s, doubling up to 4 min. A full batch (200) is followed immediately by the next.
- **Heartbeat:** when nothing is pending, roughly one empty POST per minute per router with internet.
- **Volume** is small: a handful of tags, readings every few seconds to minutes, batches ≤200 records (~70 KB).

## Caveats to handle

- **Clocks:** `seen_at` and `sent_at` come from router clocks, which can be wrong if a router never reached NTP. Always store your own `received_at` as well, and don't reject records with odd timestamps.
- **Byte order of temp/hum is unconfirmed.** The spec only states big-endian for node and seq. Routers decode temp/hum as big-endian. If the tag firmware turns out to be little-endian, values will look absurd (e.g. 268.88 °C). Keeping `raw` lets you re-decode server-side. A per-deployment "decode from raw" option is worth having.
- **Sequence reset:** a tag reboot restarts `seq`. `id` includes the reading bytes, so collisions are unlikely but possible. Don't assume `seq` is monotonic per node over long periods.
- **Unknown fields:** ignore any extra fields (forward compatibility). Validate types; reject only structurally broken requests with 400.

## Security

- **Currently there's no auth on the uplink.** Routers send no auth header. Until that changes, the simplest option is a secret token in the URL (`https://example.com/ingest?token=...`). The routers pass the whole configured URL through unchanged, so this works today.
- If you want a header (e.g. `Authorization: Bearer ...`), `mesh.py` needs a small change: an `--uplink-token` option added to the uplink request headers. Coordinate that change with the router side.
- HTTPS is planned for the uplink. The routers' Python needs the `ssl` module for it. Plain HTTP works now.
- Router-to-router traffic is plain HTTP inside the mesh and is out of scope here.

## Acceptance checks

```bash
# heartbeat -> 2xx, nothing stored
curl -i -X POST $URL -H 'Content-Type: application/json' \
  -d '{"router_id":"R1","sent_at":1759930000,"records":[]}'

# one record -> 2xx, stored
curl -i -X POST $URL -H 'Content-Type: application/json' -d '{"router_id":"R1","sent_at":1759930012.4,"records":[{"id":"00A1B2C3-1000-086911a801","node":"00A1B2C3","seq":1000,"temp":21.53,"hum":45.2,"status":1,"hops":2,"raw":"00a1b2c3086911a801000003e8000002","sink":"84:71:27:AA:BB:01","origin":"R2","seen_at":1759929987.1,"route":["R2","R1"],"ble_hops":2,"router_hops":1,"total_hops":3}]}'

# same record again from another router -> 2xx, still exactly one stored
# same POST with "router_id":"R3" and "hops":4

# malformed body -> 400, nothing stored
curl -i -X POST $URL -H 'Content-Type: application/json' -d '{"records":"nope"}'
```

Also check:
- 200 records in one POST are stored all-or-nothing.
- If storage fails mid-batch, the response is not 2xx.
- Response time is well under 15 s.
- Records sent out of order show in the right `seq` / `seen_at` order on the dashboard.

## End-to-end test with real routers

1. Deploy the app and note the ingest URL.
2. On a router with internet: `python3 ble_web.py --uplink <URL>`. The setting is saved in `mesh.json`; use `--uplink none` to clear it.
3. The router's page (`http://<router>:8080`) shows uplink status under **Mesh**: "online", or the error. Records flip from "pending" to "delivered by <router>".
4. Unplug that router's internet, generate readings, and plug it back. The backlog should arrive without duplicates.
