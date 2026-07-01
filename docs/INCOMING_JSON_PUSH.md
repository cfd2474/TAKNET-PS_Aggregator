# Incoming JSON Push — Remote Aggregator Instructions

**Audience:** Operators of a remote TAKNET (or tar1090-compatible) aggregator sending aircraft data to this host.

**Receiving aggregator:** `https://adsb.tak-solutions.com`  
**Assigned push URL (keep private):**

```
https://adsb.tak-solutions.com/ingest/json/jfeed-KjZmuw6nKh2k4-xA9WTzrAPzS5I9gVXsRhJNQpsofKY
```

Treat this URL like a password. Anyone with it can push aircraft into the feed. If it is leaked, ask the receiving admin to **regenerate the token** on Config → Services → Incoming JSON.

---

## 1. What to send

Send a **POST** request every **1–5 seconds** with a **full aircraft snapshot** (not deltas).

Each POST **replaces** the previous snapshot for this feed. If pushes stop, aircraft from this feed drop off the map after about **10 seconds** (stale timeout).

**Payload size:** Snapshots may be up to **50 MB**. If you still hit HTTP **413**, send only aircraft with positions (`lat`/`lon` present) or strip optional trace/history fields before pushing.

**Timeouts (large snapshots over VPN):** A ~8 MB upload can take more than 30 seconds on a slow or VPN path. Use a **client timeout of at least 120 seconds** (e.g. curl `--max-time 120`). **HTTP 000** with `--max-time 30` usually means the **client gave up**, not that the server rejected the body.

---

## 2. HTTP request

| Item | Value |
|------|--------|
| **Method** | `POST` |
| **URL** | `https://adsb.tak-solutions.com/ingest/json/jfeed-KjZmuw6nKh2k4-xA9WTzrAPzS5I9gVXsRhJNQpsofKY` |
| **Content-Type** | `application/json` |
| **Body encoding** | UTF-8 JSON |
| **Max body size** | 50 MB |

**Alternative auth:** Omit the token from the URL and send:

```http
POST /ingest/json/
Authorization: Bearer jfeed-KjZmuw6nKh2k4-xA9WTzrAPzS5I9gVXsRhJNQpsofKY
Content-Type: application/json
```

Full URL: `https://adsb.tak-solutions.com/ingest/json/` (trailing slash, no token in path).

---

## 3. JSON body format

Send **one** of the following.

### Option A — tar1090 `aircraft.json` (preferred)

Same shape as local `aircraft.json` from tar1090/readsb:

```json
{
  "now": 1730000000.123,
  "messages": 1234567,
  "aircraft": [
    {
      "hex": "A12345",
      "flight": "DAL123 ",
      "r": "N123AB",
      "lat": 33.9425,
      "lon": -118.4081,
      "alt_baro": 35000,
      "gs": 420,
      "track": 270,
      "squawk": "1200",
      "seen": 0.5
    }
  ]
}
```

### Option B — ADSBExchange / airplanes.live-style envelope

If you export from `/v2/all` or a similar API, the wrapper is fine — only the `aircraft` array is used:

```json
{
  "msg": "No error",
  "now": 1730000000.123,
  "total": 1,
  "ctime": 1730000000.123,
  "aircraft": [
    {
      "hex": "A12345",
      "lat": 33.9425,
      "lon": -118.4081,
      "flight": "DAL123 "
    }
  ]
}
```

---

## 4. Aircraft object rules

| Field | Required | Notes |
|-------|----------|--------|
| **ICAO hex** | **Yes** | Field name may be `hex`, `icao`, `ICAO`, or `addr`. Must be **6 hexadecimal characters** (e.g. `A12345`). |
| **lat / lon** | Strongly recommended | Required for map positions. |
| **flight, r, alt_baro, gs, track, squawk, seen, …** | Optional | Any tar1090/readsb fields are passed through. |

Rows without a valid hex are **skipped** (not an error).

On the receiving aggregator, accepted aircraft are tagged with a source label like `json:{feed-name}` so they can be distinguished from local Beast feeders and ADSBHub.

---

## 5. Example commands

### curl (from a file)

Use a generous timeout for multi-MB snapshots (especially over VPN):

```bash
curl -X POST \
  "https://adsb.tak-solutions.com/ingest/json/jfeed-KjZmuw6nKh2k4-xA9WTzrAPzS5I9gVXsRhJNQpsofKY" \
  -H "Content-Type: application/json" \
  --max-time 120 \
  --data-binary @aircraft.json
```

### curl (pipe from local aircraft.json)

```bash
curl -sf "http://127.0.0.1/data/aircraft.json" | \
  curl -sf -X POST \
    "https://adsb.tak-solutions.com/ingest/json/jfeed-KjZmuw6nKh2k4-xA9WTzrAPzS5I9gVXsRhJNQpsofKY" \
    -H "Content-Type: application/json" \
    --data-binary @-
```

Adjust the local URL if your sender uses the merger (`http://aircraft-merger:8090/data/aircraft.json`) or another internal endpoint.

### Simple push loop (bash)

```bash
#!/bin/bash
PUSH_URL="https://adsb.tak-solutions.com/ingest/json/jfeed-KjZmuw6nKh2k4-xA9WTzrAPzS5I9gVXsRhJNQpsofKY"
SOURCE="http://127.0.0.1/data/aircraft.json"

while true; do
  curl -sf "$SOURCE" | curl -sf -X POST "$PUSH_URL" \
    -H "Content-Type: application/json" \
    --data-binary @- \
    || echo "push failed $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  sleep 2
done
```

---

## 6. HTTP responses

| Status | Meaning |
|--------|---------|
| **200** | Success. Example: `{"success": true, "feed_id": 1, "accepted": 42}` — `accepted` is the number of valid aircraft in the snapshot. |
| **401** | Invalid or disabled token, or source IP not on allowlist. |
| **400** | Invalid JSON or body is not `{ "aircraft": [ ... ] }` (or v2 envelope with `aircraft` array). |
| **413** | Body larger than 50 MB. Trim the snapshot (e.g. only aircraft with lat/lon) or ask the receiver to raise the limit. |
| **000** (curl) | Client timeout — increase `--max-time` (try **120** or **300**). Common with ~8 MB over VPN when `--max-time 30` is set. |

**Quick test:** A POST with an empty aircraft list should return **200** if the token is valid:

```bash
curl -s -w "\nHTTP %{http_code}\n" -X POST \
  "https://adsb.tak-solutions.com/ingest/json/jfeed-KjZmuw6nKh2k4-xA9WTzrAPzS5I9gVXsRhJNQpsofKY" \
  -H "Content-Type: application/json" \
  -d '{"aircraft":[]}'
```

---

## 7. Merge behavior on the receiving side

When the same ICAO hex exists from multiple sources, priority is:

1. **Local Beast feeders** (direct ADS-B into this aggregator)
2. **Incoming JSON feeds** (this push)
3. **ADSBHub**

Your push **fills gaps**; it does not override aircraft already tracked locally on the receiver.

---

## 8. Checklist

- [ ] POST every 1–5 seconds with a full snapshot
- [ ] Use tar1090 `aircraft.json` or ADSBExchange/v2 envelope with an `aircraft` array
- [ ] Every aircraft row has a valid 6-character hex
- [ ] Include `lat` and `lon` when positions are available
- [ ] Confirm **200** responses; investigate **401** / **400** immediately
- [ ] Receiving admin can verify **Active** status on Config → Services → Incoming JSON

---

## 9. Support

If pushes fail consistently:

1. Confirm the feed is **Enabled** on the receiver (Config → Services → Incoming JSON).
2. Confirm your outbound public IP is allowed if an IP allowlist was configured.
3. Verify JSON with `jq` locally before pushing.
4. Ask the receiver admin to regenerate the token if the URL may have been exposed.
