## TAKNET-PS Aggregator REST API (v2)

This document describes the public ADS‑B REST API exposed by the TAKNET‑PS Aggregator.  
It is intended as a **practical reference** for engineers integrating against the API for the first time.

The API is:
- **Read‑only**
- **Public** (no authentication, no keys required)
- **JSON** over HTTPS
- **Stateless** – every request is independent

All responses use an envelope compatible with airplanes.live clients.

---

## 1. Base URL and Conventions

- **Base URL (production)**: `https://adsb.tak-solutions.com`
- **API prefix**: all endpoints are under `/v2/`
- **HTTP method**: all endpoints are **GET**
- **Authentication**: **none** – no headers, tokens, or keys required
- **CORS**: `Access-Control-Allow-Origin: *` on all `/v2/` responses (browser JS apps can call directly)

### 1.1 Standard Response Envelope

Every successful endpoint returns JSON with this envelope:

```json
{
  "msg": "No error",
  "now": 1730000000.123,   // server timestamp (seconds, float)
  "total": 42,             // number of aircraft in the 'aircraft' array
  "ctime": 1730000000.123, // same as 'now'
  "ptime": 5.23,           // processing time in milliseconds
  "aircraft": [ ... ]      // array of aircraft objects
}
```

On error, `aircraft` is an empty array and `msg` is a human‑readable error string:

```json
{
  "msg": "Upstream error: ...",
  "now": 1730000001.01,
  "total": 0,
  "ctime": 0,
  "ptime": 0,
  "aircraft": []
}
```

### 1.2 Aircraft Object Shape

The `aircraft` entries are passed through from `tar1090`/`readsb`. Common fields you will see:

- `hex` – ICAO 24‑bit hex address (e.g., `"A12345"`)
- `flight` – callsign / flight number (e.g., `"DAL123 "`)
- `r` – registration / tail number (e.g., `"N123AB"`)
- `t` – ICAO aircraft type designator (e.g., `"B738"`)
- `squawk` – 4‑digit octal squawk code (e.g., `"7500"`)
- `alt_baro`, `alt_geom` – altitudes
- `lat`, `lon` – position (if available)
- `gs`, `ias`, `tas` – speeds
- `dbFlags` – bitmask flags for MIL, PIA, LADD, etc.

When the aggregator uses the **aircraft merger** (local + ADSBHub), each aircraft may also include:

- `source` – `"adsbhub"` when the position came from the ADSBHub feed; omitted or `"local"` for direct feeders.
- `on_ground` – `true` / `false` when known (from ADSBHub MSG,3 or local).

For `/v2/point/...` results, each aircraft is additionally annotated with:

- `dst` – distance in nautical miles from the query point (float, rounded to 2 decimals)

### 1.3 How TIS-B is identified in aircraft.json

The aircraft list is produced by **readsb** (via tar1090 or the aircraft-merger). readsb uses the following to identify TIS-B vs ADS-B:

**`type` (string)**  
The best source of current data for this aircraft. Common values:

| Value | Meaning |
|-------|--------|
| `adsb_icao` | ADS-B from a Mode S transponder with 24-bit ICAO address |
| `tisb_icao` | TIS-B: non-ADS-B target identified by 24-bit ICAO (e.g. Mode S tracked by secondary radar) |
| `tisb_trackfile` | TIS-B: non-ADS-B target using track/file ID (e.g. primary or Mode A/C radar) |
| `tisb_other` | TIS-B: non-ADS-B target with non-ICAO address |
| `mlat` | Position from MLAT (multilateration) |
| `mode_s` | Mode S data, no position |
| `adsr_icao` / `adsr_other` | ADS-B rebroadcast from another link (e.g. UAT) |
| `adsb_icao_nt` | ADS-B “non-transponder” (e.g. ground vehicle) with ICAO address |
| `other` | Miscellaneous / unknown source |

So **TIS-B targets** are those with `type` equal to `tisb_icao`, `tisb_trackfile`, or `tisb_other`.

**`hex` (string)**  
If the ICAO hex starts with `~` (e.g. `"~123456"`), the address is non-ICAO; readsb uses this for some TIS-B or other non-ICAO targets.

**`tisb` (array, optional)**  
When present, lists field names that were derived from TIS-B data for this aircraft.

**Example:** To list only TIS-B aircraft from the API:

```bash
curl -s "https://adsb.tak-solutions.com/v2/all" | jq '[.aircraft[] | select(.type | startswith("tisb_"))]'
```

---

## 2. Health Check

### 2.1 `GET /v2/health`

**Purpose:** Quick check that the REST API container is running and reachable.

**Example:**

```bash
curl -s https://adsb.tak-solutions.com/v2/health | jq .
```

**Typical response:**

```json
{
  "service": "TAKNET-PS Aggregator",
  "status": "ok"
}
```

Use this in monitoring systems or as a first check when debugging connectivity.

---

## 3. Listing All Aircraft

### 3.1 `GET /v2/all`

**Purpose:** Retrieve **all** aircraft currently tracked by the aggregator.

**Example:**

```bash
curl -s https://adsb.tak-solutions.com/v2/all | jq '.total, .aircraft[0]'
```

This prints:
- the total number of aircraft
- the first aircraft object (for inspection)

**Notes:**
- On a busy network this response can be large (hundreds of aircraft).
- There is **no pagination** yet – consumers should be prepared for a multi‑hundred‑KB JSON payload during peak times.

---

## 4. Lookup by ICAO Hex

### 4.1 `GET /v2/hex/<hex>`

**Purpose:** Look up one or more aircraft by ICAO 24‑bit hex address.

**Path parameters:**
- `<hex>` – single hex like `A12345`, or multiple comma‑separated values like `A12345,B67890`.

**Example – single hex:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/hex/A12345" | jq .
```

**Example – multiple hexes:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/hex/A12345,B67890,C11111" | jq '.total'
```

**Behavior:**
- Matching is **case‑insensitive**.
- Leading `~` (used by readsb for non‑ICAO addresses) is stripped automatically.

---

## 5. Lookup by Callsign

### 5.1 `GET /v2/callsign/<callsign>`

**Purpose:** Look up aircraft by ATC callsign / flight number.

**Path parameters:**
- `<callsign>` – one or more callsigns, comma‑separated (e.g., `DAL123,UAL456`).

**Example – single callsign:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/callsign/DAL123" | jq '.total, .aircraft[0].flight'
```

**Example – multiple callsigns:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/callsign/DAL123,UAL456" | jq '.total'
```

**Behavior:**
- Callsigns are upper‑cased and whitespace‑trimmed on both sides before comparison.
- The `flight` field from `aircraft.json` is used as the source field.

---

## 6. Lookup by Registration (Tail Number)

### 6.1 `GET /v2/reg/<reg>`

**Purpose:** Look up aircraft by registration / tail number.

**Path parameters:**
- `<reg>` – one or more registrations, comma‑separated (e.g., `N123AB,G-ABCD`).

**Example – single registration:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/reg/N123AB" | jq '.total, .aircraft[0].r'
```

**Example – multiple registrations:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/reg/N123AB,G-ABCD" | jq '.total'
```

**Behavior:**
- Registrations are upper‑cased and whitespace‑trimmed before comparison.
- The `r` field from `aircraft.json` is used.

---

## 7. Lookup by ICAO Type Code

### 7.1 `GET /v2/type/<type>`

**Purpose:** Filter aircraft by ICAO aircraft type designator (e.g., `B738`, `A321`).

**Path parameters:**
- `<type>` – one or more type codes, comma‑separated (e.g., `B738,A321,B77W`).

**Example – single type:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/type/B738" | jq '.total'
```

**Example – multiple types:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/type/B738,A321,B77W" | jq '.total'
```

**Behavior:**
- Type codes are upper‑cased and trimmed before comparison.
- The `t` field from `aircraft.json` is used.

---

## 8. Lookup by Squawk

### 8.1 `GET /v2/squawk/<squawk>`

**Purpose:** Filter aircraft by Mode A/C squawk code (e.g., `7500`, `7600`, `7700`).

**Path parameters:**
- `<squawk>` – one or more squawk codes, comma‑separated.

**Example – emergency squawks:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/squawk/7500,7600,7700" | jq '.total'
```

**Behavior:**
- Squawks are compared as strings; no normalization is applied beyond trimming.

---

## 9. Flag‑Based Filters (MIL / LADD / PIA)

These endpoints rely on the `dbFlags` field populated by the readsb aircraft database.  
If the upstream database is outdated, results may be empty or incomplete.

### 9.1 `GET /v2/mil`

**Purpose:** Return all aircraft with the **military** flag set.

**Example:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/mil" | jq '.total'
```

### 9.2 `GET /v2/ladd`

**Purpose:** Return all aircraft tagged as **LADD** (Limiting Aircraft Data Displayed).

**Example:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/ladd" | jq '.total'
```

### 9.3 `GET /v2/pia`

**Purpose:** Return all aircraft tagged as **PIA** (Privacy ICAO Address).

**Example:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/pia" | jq '.total'
```

---

## 10. Point / Radius Search

### 10.1 `GET /v2/point/<lat>/<lon>/<radius_nm>`

**Purpose:** Return all aircraft within a given radius (in **nautical miles**) of a latitude/longitude point.

**Path parameters:**
- `<lat>` – latitude in decimal degrees (between −90 and 90)
- `<lon>` – longitude in decimal degrees (between −180 and 180)
- `<radius_nm>` – search radius in **nautical miles**
  - must be `> 0`
  - any value above 250 is **soft‑capped to 250 nm**

**Example – 50 nm around LAX:**

```bash
curl -s "https://adsb.tak-solutions.com/v2/point/33.9425/-118.4081/50" \
  | jq '.total, .aircraft[0] | {hex: .hex, callsign: .flight, dst: .dst}'
```

**Behavior:**
- Aircraft without position (`lat` or `lon` missing) are excluded.
- Distance is computed using great‑circle (haversine) distance in nautical miles.
- Results are **sorted nearest‑first** by `dst`.
- Each aircraft object includes an additional `dst` field (float, nm).

**Validation rules (possible error messages):**
- `lat must be between -90 and 90`
- `lon must be between -180 and 180`
- `radius must be greater than 0`
- Non‑numeric values yield: `lat, lon, and radius must be numeric`

---

## 11. Error Handling and 404s

### 11.1 Common Error Responses

Errors share the standard envelope structure with `aircraft: []` and a non‑default `msg`:

- Upstream issue talking to tar1090/readsb:
  - `{"msg": "Upstream error: ...", "total": 0, "aircraft": []}`
- Invalid numeric parameters to `/v2/point/...`:
  - `{"msg": "lat, lon, and radius must be numeric", ...}`

### 11.2 Unknown Endpoints

Requesting a path that does not exist under `/v2/` (for example `/v2/foo`) returns:

```json
{
  "msg": "Endpoint not found. Available: /v2/all, /v2/hex/<hex>, /v2/callsign/<cs>, /v2/reg/<reg>, /v2/type/<type>, /v2/squawk/<sq>, /v2/mil, /v2/ladd, /v2/pia, /v2/point/<lat>/<lon>/<radius_nm>",
  "now": 1730000000.0,
  "total": 0,
  "ctime": 0,
  "ptime": 0,
  "aircraft": []
}
```

HTTP status code for this case is **404**.

---

## 12. Quick Start: Minimal Working Examples

### 12.1 Check API Health

```bash
curl -s https://adsb.tak-solutions.com/v2/health
```

### 12.2 Count All Aircraft

```bash
curl -s https://adsb.tak-solutions.com/v2/all | jq '.total'
```

### 12.3 Track a Single Aircraft by Hex

Replace `A12345` with the hex you care about:

```bash
curl -s "https://adsb.tak-solutions.com/v2/hex/A12345" | jq '.aircraft[0]'
```

### 12.4 Watch Traffic Around a Location

Example: 50 nm around LAX (33.9425, −118.4081):

```bash
curl -s "https://adsb.tak-solutions.com/v2/point/33.9425/-118.4081/50" \
  | jq '.aircraft[] | {hex, flight, lat, lon, dst}'
```

You can wrap this in a loop for a very simple watcher:

```bash
while true; do
  curl -s "https://adsb.tak-solutions.com/v2/point/33.9425/-118.4081/50" \
    | jq '.now, .total'
  sleep 10
done
```

---

## 13. Integration Notes and Best Practices

- **Rate limiting & caching:**  
  - As of v1.0.93, there is **no server‑side rate limiting or caching** on `/v2/`.  
  - Clients should:
    - Avoid very high request rates (e.g., >1–2 requests per second per client).
    - Implement their own small in‑memory cache if they need to poll frequently.

- **Polling interval guidance:**  
  - For situational awareness displays: 1–5 second polling is typically sufficient.  
  - For low‑bandwidth dashboards: 10–30 seconds can be enough.

- **Handling large responses:**  
  - Use streaming JSON parsers or filter with tools like `jq` to avoid holding the entire payload in memory.  
  - Be careful when logging full `/v2/all` responses in production – logs can grow quickly.

- **Future‑proofing:**  
  - New endpoints may be added under `/v2/` in the future (e.g., `/v2/emergency`, pagination, optional key auth).  
  - Existing fields are expected to remain **backwards compatible**; consumers should ignore unknown additional fields.

