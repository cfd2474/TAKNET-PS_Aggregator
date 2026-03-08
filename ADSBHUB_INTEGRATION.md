# ADSBHub.org TCP integration

This document describes how the aggregator exchanges data with ADSBHub.org: feeding your Beast stream to them and receiving their aggregated feed, with **local-feeder preference** so duplicate aircraft are deduplicated in favor of your direct feeder data (more accurate time-wise).

## ADSBHub overview

- **Feed TO ADSBHub (outbound):** Your station connects as TCP **client** to `data.adsbhub.org` port **5001** and streams data. Supported formats: **Raw Beast hex** or **SBS/30003**. Your IP must be registered in your ADSBHub profile (Settings → Data Access).
- **Receive FROM ADSBHub (inbound):** After you feed them, they enable your IP to connect as TCP **client** to `data.adsbhub.org` port **5002`** to receive their **aggregated SBS feed** (all contributors’ data).
- **Terms:** You must share at least one ADS-B station to receive the aggregated feed. See [howtofeed](https://www.adsbhub.org/howtofeed.php) and [howtogetdata](https://www.adsbhub.org/howtogetdata.php).

## Architecture in this stack

### 1. Outbound: ADSBHub feeder

- **Service:** `adsbhub-feeder`
- **Role:** Reads Beast from `readsb:30005` and forwards it to `data.adsbhub.org:5001`.
- **Config:** `ADSBHUB_FEED_ENABLED=true`, optional `ADSBHUB_FEED_HOST`, `ADSBHUB_FEED_PORT=5001`.
- **Note:** Your server’s **outbound** IP must be allowed in your ADSBHub profile. Use a fixed IP or DynDNS and add it in Settings.

### 2. Inbound + merge: aircraft merger

- **Service:** `aircraft-merger`
- **Role:**
  - Fetches **local** aircraft from `tar1090` (our readsb → tar1090 pipeline).
  - Connects to `data.adsbhub.org:5002` and parses the **SBS** stream into aircraft state by ICAO.
  - **Merge rule (dedupe):** For each ICAO, **prefer local** if we have that aircraft from our feeders; otherwise use ADSBHub. This keeps your direct feeder data (more accurate time-wise) and fills in aircraft we don’t see locally.
- **Output:** Serves merged `aircraft.json` in the same format as tar1090 (`aircraft`, `now`, `messages`) so the map and REST API work unchanged.
- **Config:** `ADSBHUB_RECEIVE_ENABLED=true`, optional `ADSBHUB_HOST`, `ADSBHUB_PORT=5002`. Optional `MERGER_POLL_MS` for local fetch interval.

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
| `ADSBHUB_FEED_ENABLED` | `false` | Enable Beast feed to ADSBHub (outbound). |
| `ADSBHUB_FEED_HOST` | `data.adsbhub.org` | ADSBHub host for feeding. |
| `ADSBHUB_FEED_PORT` | `5001` | ADSBHub port for feeding (Beast). |
| `ADSBHUB_RECEIVE_ENABLED` | `false` | Enable receiving + merging ADSBHub SBS. |
| `ADSBHUB_HOST` | `data.adsbhub.org` | ADSBHub host for receiving. |
| `ADSBHUB_PORT` | `5002` | ADSBHub port for SBS feed. |
| `TAR1090_URL` | (internal) | URL for local aircraft.json (used by merger). |

## Setup checklist

1. Register at [ADSBHub](https://www.adsbhub.org/register.php) and add your station (Settings → New Station).
2. In Settings → Data Access, add the **IP address** (or DynDNS hostname) you will use to connect to ADSBHub (both feed and receive).
3. Set `ADSBHUB_FEED_ENABLED=true` and `ADSBHUB_RECEIVE_ENABLED=true` in `.env` if you want both; restart stack.
4. Confirm feed: ADSBHub Statistics page should show your station after a few minutes.
5. Confirm receive: Map and `/v2/all` should show more aircraft when ADSBHub feed is connected; local-only aircraft still take precedence when duplicated.
