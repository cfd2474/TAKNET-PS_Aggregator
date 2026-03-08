# ADSBHub.org TCP integration

This document describes how the aggregator exchanges data with ADSBHub.org: feeding your **SBS** stream to them and receiving their aggregated feed, with **local-feeder preference** so duplicate aircraft are deduplicated in favor of your direct feeder data (more accurate time-wise).

## ADSBHub overview

- **Feed TO ADSBHub (outbound):** Your station connects as TCP **client** to `data.adsbhub.org` port **5001**. Data format is **SBS/BaseStation (30003) plain text**. You must send your **Station dynamic IP key (CLIENTKEY)** as the first line after connection, then SBS lines. Your connecting IP must match the station IP in your ADSBHub profile (Settings → Data Access).
- **Receive FROM ADSBHub (inbound):** Connect as TCP **client** to `data.adsbhub.org` port **5002** to receive their **aggregated SBS feed** (all contributors’ data). Access is by source IP only (no token); your IP must be in Settings → Data Access.
- **Terms:** You must share at least one ADS-B station to receive the aggregated feed. See [howtofeed](https://www.adsbhub.org/howtofeed.php) and [howtogetdata](https://www.adsbhub.org/howtogetdata.php).

## Architecture in this stack

### 1. Outbound: ADSBHub feeder

- **Service:** `adsbhub-feeder`
- **Role:** Reads **SBS** from `readsb:30003`, connects to `data.adsbhub.org:5001`, sends **CLIENTKEY** as the first line (per ADSBHub Connection Guide), then forwards SBS lines. Reconnects on disconnect; resends CLIENTKEY on each new connection.
- **Config:** `ADSBHUB_FEED_ENABLED=true`, **`ADSBHUB_CLIENT_KEY`** (Station dynamic IP key from ADSBHub Settings → New Station; required for feed). Optional: `ADSBHUB_FEED_HOST`, `ADSBHUB_FEED_PORT=5001`. If CLIENTKEY contains `$`, use single quotes in `.env` or double `$` in Docker Compose.
- **Note:** Your server’s **outbound** IP must match the Station Host/IP in your ADSBHub station settings.

### 2. Inbound + merge: aircraft merger

- **Service:** `aircraft-merger`
- **Role:**
  - Fetches **local** aircraft from `tar1090` (our readsb → tar1090 pipeline).
  - Connects to `data.adsbhub.org:5002` and parses the **SBS** stream into aircraft state by ICAO.
  - **Merge rule (dedupe):** For each ICAO, **prefer local** if we have that aircraft from our feeders; otherwise use ADSBHub. This keeps your direct feeder data (more accurate time-wise) and fills in aircraft we don’t see locally.
- **Output:** Serves merged `aircraft.json` in the same format as tar1090 (`aircraft`, `now`, `messages`) so the map and REST API work unchanged.
- **Config:** `ADSBHUB_RECEIVE_ENABLED=true`, optional `ADSBHUB_HOST`, `ADSBHUB_PORT=5002`. Optional `MERGER_POLL_MS` for local fetch interval. Optional `MERGER_STALE_SECONDS=10` (drop aircraft not seen in this many seconds; map and API get no stale data).
- **When Receive is disabled:** The dashboard writes `receive_enabled` to the shared volume on save; the merger reads it each cycle and immediately drops all ADSBHub-sourced aircraft, so the map and API clear without waiting for a container restart.
- **Staleness:** Any aircraft (local or ADSBHub) not updated within `MERGER_STALE_SECONDS` (default 10s) is removed from the merged output and is not served to the map or REST API.

### 3. Nginx

- **`/data/aircraft.json`** → proxied to **aircraft-merger** (merged output).
- **`/data/*`** (all other paths, e.g. traces) → tar1090 as today.
- **REST API** and **tar1090 map** both read aircraft from the same URL; they now get merged data with local preference.

### 4. Deduplication (sanitization for accuracy)

- **By ICAO:** One record per ICAO in the merged list.
- **Prefer local:** If an ICAO appears in **local** (tar1090 = our feeders + readsb), that record is used. Otherwise the record from the ADSBHub SBS stream is used.
- **No blending:** We do not average or mix fields from both sources for the same ICAO; we choose one source to avoid inconsistent or delayed fields. Local is chosen when available for better time accuracy.

## Env vars (summary)

| Variable | Default | Description |
|----------|---------|-------------|
| `ADSBHUB_FEED_ENABLED` | `false` | Enable SBS feed to ADSBHub (outbound). |
| `ADSBHUB_CLIENT_KEY` | — | **Required for feed.** Station dynamic IP key from ADSBHub Settings → New Station. Sent as first line after connect. |
| `ADSBHUB_FEED_HOST` | `data.adsbhub.org` | ADSBHub host for feeding. |
| `ADSBHUB_FEED_PORT` | `5001` | ADSBHub port for feeding (SBS). |
| `ADSBHUB_RECEIVE_ENABLED` | `false` | Enable receiving + merging ADSBHub SBS. |
| `ADSBHUB_HOST` | `data.adsbhub.org` | ADSBHub host for receiving. |
| `ADSBHUB_PORT` | `5002` | ADSBHub port for SBS feed. |
| `TAR1090_URL` | (internal) | URL for local aircraft.json (used by merger). |
| `ADSBHUB_STATUS_DIR` | `/status` | Path inside feeder/merger containers for feed.json, receive.json, receive_enabled (dashboard reads via its own mount). |
| `MERGER_STALE_SECONDS` | `10` | Drop aircraft not seen in this many seconds; map/API get no stale data. |

## Data from ADSBHub receive feed

ADSBHub sends SBS message types 1, 3, and 4. The aircraft-merger parses and merges them by ICAO into one record per aircraft. Available fields (when present) for ADSBHub-sourced aircraft:

| Field | Source | Description |
|-------|--------|-------------|
| `hex` | all | ICAO 24-bit address |
| `flight` | MSG,1 | Callsign |
| `alt_baro` | MSG,3 | Barometric altitude (ft) |
| `lat`, `lon` | MSG,3 | Position |
| `gs` | MSG,4 | Ground speed (kt) |
| `track` | MSG,4 | Track (degrees) |
| `baro_rate` | MSG,4 | Vertical rate (ft/min) |
| `squawk` | MSG,6 if present | Transponder code |
| `on_ground` | MSG,3 | Whether aircraft is on ground |
| `source` | (set by merger) | `"adsbhub"` for feed aircraft |

Local (feeder) aircraft can include additional fields from readsb/tar1090 (e.g. messages, seen, rssi, type, registration). The Merged map sidebar shows all of the above when you click an aircraft.

## Verification: receive off and 10s staleness

**1. ADSBHub data discarded when Receive is turned off**

- On save (Config → Services), the dashboard writes `receive_enabled` (`true`/`false`) to the shared volume at `ADSBHUB_STATUS_PATH/receive_enabled` (same volume the merger sees as `STATUS_DIR/receive_enabled`).
- Each merge cycle (~1.5s), the merger calls `_is_receive_enabled()`. If the file is `false`: it uses an empty ADSBHub map, clears `_state["_adsbhub"]` and `_adsbhub_last_seen`, so no ADSBHub aircraft are added to the merged list. Map and API then show only local aircraft within one or two cycles.

**2. Data stales out after 10 seconds (MERGER_STALE_SECONDS)**

- **Local aircraft:** Tar1090 provides a `seen` field (seconds since last message). The merger drops any local aircraft with `seen > MERGER_STALE_SECONDS` (default 10).
- **ADSBHub aircraft:** Each SBS update sets `_adsbhub_last_seen[hex] = time.time()`. In the merge loop, `cutoff = time.time() - STALE_SECONDS`; an ADSBHub aircraft is included only if `_adsbhub_last_seen.get(hex, 0) >= cutoff`. So if an aircraft has had no update in the last 10 seconds, it is omitted. Old hexes are purged from `_adsbhub_last_seen` to avoid unbounded growth.

**Manual checks**

- **Receive off:** With receive on, note aircraft count (dashboard “Aircraft Tracked” or Map). Turn off “Receive from ADSBHub” in Config → Services and save. Within a few seconds the map and `/api/aircraft.json` or `/v2/all` should show fewer aircraft (only direct); any previously ADSBHub-only targets should disappear.
- **10s staleness:** With receive on, call `/api/aircraft.json` or `/v2/all` and note aircraft with `"source": "adsbhub"`. Stop receiving (turn off receive and save, or disconnect ADSBHub). Within ~10 seconds those ADSBHub-sourced aircraft should disappear from the response. Local aircraft without recent messages (tar1090 `seen` > 10) also disappear from the merged output.

## Setup checklist

1. Register at [ADSBHub](https://www.adsbhub.org/register.php) and add your station (Settings → New Station). Set Data Protocol to **SBS**, Station mode to **Client**, Station Host/IP to your public IP. Copy the **Station dynamic IP key (CLIENTKEY)**.
2. In Settings → Data Access, add the **IP address** you use to connect to ADSBHub (required for receive; feed uses the same IP).
3. In `.env`: set `ADSBHUB_CLIENT_KEY=<your CLIENTKEY>`. If you want to feed, set `ADSBHUB_FEED_ENABLED=true`. For receive, set `ADSBHUB_RECEIVE_ENABLED=true`. Restart stack (or use Config → Services ADSBHub toggles).
4. Confirm feed: ADSBHub Statistics page should show your station after a few minutes.
5. Confirm receive: Map and `/v2/all` should show more aircraft when ADSBHub feed is connected; local-only aircraft still take precedence when duplicated.
