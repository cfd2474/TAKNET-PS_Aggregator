# MediaMTX → TAKNET Aggregator: KLV Ingest Requirements

**Audience:** MediaMTX config editor / `klv-to-cot` sidecar developers  
**Aggregator version:** 1.0.377+  
**Status:** Consumer implemented on aggregator; producer spec for MediaMTX integration

---

## 1. Summary

The TAKNET-PS Aggregator receives **decoded MISB ST0601 KLV metadata** from external MediaMTX boxes and uses it to drive TAK map markers (platform position + SPI point) for aircraft that have a COTProxy video URL configured.

**MediaMTX responsibility (producer):**

1. Ingest aircraft video (existing External Sources flow).
2. Tap KLV from the local rebroadcast path.
3. Decode ST0601 with `klvdata` (or equivalent).
4. Send **newline-delimited JSON (NDJSON)** over **TCP** to the aggregator’s NetBird address on **port 9000**.

**Aggregator responsibility (consumer — already built):**

- Validate per-aircraft **ingest tokens**.
- Merge KLV position onto the existing COTProxy transform (callsign, icon, CoT type, video URL).
- Emit SPI point CoT when frame-center is present.
- Fall back to ADS-B when the stream stops (~5 s).

MediaMTX does **not** generate CoT, connect to TAK Server, or open RTSP on the aggregator.

---

## 2. End-to-end flow

```
┌─────────────────────┐         NetBird mesh          ┌──────────────────────────┐
│  MediaMTX box       │  TCP NDJSON :9000             │  TAKNET aggregator       │
│                     │ ────────────────────────────► │                          │
│  SRT/RTSP ingest    │  {event, hex, token, ...}    │  klv_ingest listener     │
│  runOnReady sidecar │                               │  → cot_pipeline → TAK    │
│  klv-to-cot.py      │                               │                          │
└─────────────────────┘                               └──────────────────────────┘
```

**Operator handoff (out of band):**

| Item | Source | Used by MediaMTX |
|------|--------|------------------|
| NetBird setup key | Aggregator COTProxy UI (“Generate NetBird setup key”) | Enroll MediaMTX box on mesh (one-time) |
| Target host:port | Aggregator COTProxy connection info | `host:9000` (aggregator NetBird overlay IP) |
| Aircraft hex | COTProxy transform row | `--hex` / message `hex` field |
| Ingest token | COTProxy transform row (owner-only) | **Required** on every NDJSON message |

---

## 3. Network & transport

| Requirement | Value |
|-------------|-------|
| VPN | **NetBird** (WireGuard mesh). Plain UDP/TCP inside the tunnel; no app-layer TLS on the sidecar. |
| Protocol | **TCP** (recommended). Aggregator listens on fixed port **9000**. |
| Target | Aggregator **NetBird overlay IP**, port **9000** (not the public internet). |
| Framing | **NDJSON** — one JSON object per line, UTF-8, terminated with `\n`. |
| Direction | **Outbound only** from MediaMTX to aggregator. No inbound listener required on MediaMTX for KLV. |
| Fan-out | **One target per external source** for v1. Routing on aggregator is by in-band `token`, not by port. |

**NetBird enrollment (MediaMTX box):**

- Management URL: `https://netbird.tak-solutions.com` (fixed; same as TAKNET feeders).
- Setup key: provided by the aggregator output owner (ephemeral, one-off, scoped to **`mediamtx_video`** group).
- After enroll, MediaMTX peer must be allowed by NetBird ACL to reach aggregator `:9000` (policy created by **VPN → Enable secure KLV** when the aggregator is enrolled).

**mTLS fallback:** Not required for v1. NetBird is the supported path.

---

## 4. Sidecar lifecycle (MediaMTX `runOnReady`)

When “Extract KLV → CoT” (or equivalent) is enabled on an External Source path, append to the path block in `mediamtx.yml`:

```yaml
paths:
  <path_name>:
    source: srt://upstream:port/...
    sourceOnDemand: yes
    runOnReady: /opt/mediamtx-webeditor/klv-to-cot.py --path $MTX_PATH --hex <hex> --token <ingest-token> --target <netbird-ip:9000> --proto tcp
    runOnReadyRestart: yes
    runOnNotReady: /opt/mediamtx-webeditor/klv-to-cot.py --path $MTX_PATH --hex <hex> --token <ingest-token> --target <netbird-ip:9000> --proto tcp --downlink
```

| Hook | Behavior |
|------|----------|
| `runOnReady` | Start decoder sidecar when path goes live. |
| `runOnReadyRestart` | `yes` — restart sidecar if it exits while path is still ready. |
| `runOnNotReady` | Run once with `--downlink` to send `{"event":"down",...}` then exit. |

**Pipeline (sidecar):**

```
ffmpeg -loglevel error -rtsp_transport tcp -i rtsp://127.0.0.1:8554/$MTX_PATH \
       -map 0:d -c copy -f data pipe:1
  | klvdata (MISB ST0601)
  | extract fields → NDJSON → TCP write to target (one line per message)
```

- Read from **local rebroadcast** (`127.0.0.1:8554/<path>`), not the upstream URL.
- Rate-limit samples to KLV cadence (~1–5 Hz); cap at **10 Hz** (log when capped).
- On first successful decode: send `{"event":"up",...}`.
- On `--downlink`: send single `{"event":"down",...}`.

---

## 5. Wire format — NDJSON schema

Payload is **transport-agnostic** JSON. One object per line.

### 5.1 Required on every message

| Field | Type | Description |
|-------|------|-------------|
| `event` | string | `up`, `down`, or `sample` |
| `hex` | string | ICAO 24-bit hex (merge key; must match COTProxy transform) |
| `token` | string | **Ingest token** from aggregator COTProxy (authenticates tenant/aircraft) |

Messages with missing or unknown `token` are **dropped** by the aggregator.

### 5.2 Lifecycle messages

```json
{"event":"up","hex":"A1B2C3","token":"<ingest-token>","path":"chp_air1","ts":1733600000.0}
{"event":"down","hex":"A1B2C3","token":"<ingest-token>","path":"chp_air1","ts":1733600090.0}
```

| Field | Required | Notes |
|-------|----------|-------|
| `path` | Recommended | MediaMTX path name (debugging) |
| `ts` | Recommended | Unix seconds (float). Sidecar wall clock OK if KLV tag 2 absent. |

### 5.3 Sample message (one per decoded KLV frame)

```json
{
  "event": "sample",
  "hex": "A1B2C3",
  "token": "<ingest-token>",
  "path": "chp_air1",
  "ts": 1733600000.123,
  "platform": {
    "lat": 34.123456,
    "lon": -118.123456,
    "alt_m": 1875.0,
    "heading_deg": 92.4,
    "pitch_deg": -1.2,
    "roll_deg": 0.4
  },
  "sensor": {
    "rel_az_deg": 145.0,
    "rel_el_deg": -23.5,
    "rel_roll_deg": 0.0,
    "slant_range_m": 4200.0,
    "hfov_deg": 2.3,
    "vfov_deg": 1.3
  },
  "spi": {
    "lat": 34.150000,
    "lon": -118.150000,
    "elev_m": 410.0
  }
}
```

### 5.4 Field rules

- **Sub-objects may be partial** — omit any tag not present in the KLV frame.
- Consumer treats missing fields as **unknown**, not zero.
- **Aggregator uses for TAK (v1):**
  - `platform` → aircraft marker position (lat, lon, alt_m, heading_deg).
  - `spi` → SPI point (`b-m-p-s-p-i`) at lat/lon/elev_m.
  - `sensor` → decoded and accepted but **not** used for slant geometry in v1.
- **Units:** lat/lon decimal degrees WGS84; altitudes/ranges in **meters**; angles in **degrees**.
- **Schema versioning:** v1 frozen as above; additive fields only in later versions.

### 5.5 Hex binding

ST0601 does not carry ICAO hex. Bind via configuration:

- **Auto:** path name equals hex → `--hex $MTX_PATH`
- **Explicit:** “Aircraft hex / ID” form field when path name ≠ hex

Hex must match the COTProxy transform row on the aggregator.

---

## 6. Aggregator consumer behavior (for shared understanding)

When `token` is valid and samples are fresh (< **5 s**):

1. **Platform marker** — position from KLV `platform`; styling from COTProxy transform (callsign, icon, CoT type, `__video` URL).
2. **SPI marker** — separate UID `{hex}-spi` when `spi` lat/lon present.
3. **ADS-B suppressed** for that hex on that output while live.

When stream ends (`down` or stale > **5 s**):

- Resume ADS-B for platform UID.
- SPI allowed to expire via CoT stale.

When video URL is configured but no usable KLV:

- Aggregator reverts to ADS-B + video URL (current behavior); UI shows **No KLV**.

---

## 7. MediaMTX UI / API changes requested

### External Source form (when KLV extraction enabled)

| Field | Required | Notes |
|-------|----------|-------|
| Enable KLV → aggregator | Yes | Checkbox |
| Transport | Yes | **Netbird + TCP** (default for TAKNET) |
| Aggregator target | Yes | `host:port` — NetBird IP:9000 |
| Aircraft hex / ID | Optional | Default = stream/path name |
| **Ingest token** | **Yes** | Pasted from aggregator operator |
| Protocol | Yes | `tcp` (default) |

### List / badge

- Show badge when KLV→aggregator is enabled on a source (mirror demux / simulate pattern).

### Backend / installer

- Persist `token` in external source metadata.
- Interpolate `token` into `runOnReady` / `runOnNotReady` command lines.
- Ship `klv-to-cot.py` + `klvdata` dependency.
- Document operator handoff checklist (see §2).

### Optional (later)

- API button on MediaMTX UI to request NetBird setup key from aggregator (`POST /api/outputs/<id>/klv/netbird-setup-key`). v1 can be manual copy from aggregator COTProxy.

---

## 8. Acceptance test

**Prereqs:** Feed with MISB ST0601 KLV (test stream or live aircraft).

```bash
# 1. On aggregator: COTProxy transform with Video URL → copy token, hex, NetBird IP:9000.
# 2. On MediaMTX: enroll NetBird, enable KLV extraction with target + hex + token, TCP.
# 3. Confirm mediamtx.yml hooks:
grep -A6 '<path_name>:' /etc/mediamtx/mediamtx.yml
#    expect: runOnReady, runOnReadyRestart: yes, runOnNotReady (with --token, --proto tcp)

# 4. Stand-in listener (sanity check without aggregator):
nc -l 9000

# 5. Start feed. Expect:
#    {"event":"up",...,"token":"..."}
#    stream of {"event":"sample",...,"platform":{...}}
# 6. Stop feed. Expect single {"event":"down",...}
# 7. Verify HLS/RTSP playback unchanged on same path.
```

**PASS:** `up` / `down` bracket samples with plausible `platform` (+ `spi` when tag present), every message includes matching `hex` + `token`, video playback unaffected.

**End-to-end PASS (with aggregator):** COTProxy row shows **Live**; TAK marker tracks KLV position with proxy icon/callsign; SPI appears when `spi` present.

---

## 9. Out of scope (MediaMTX)

- CoT XML generation or TAK Server connection
- UID merge / ADS-B mute logic (aggregator)
- Slant line / sensor wedge visualization (aggregator v1 uses SPI point only)
- Cert issuance for mTLS
- Multi-aggregator fan-out per source
- Plain-text UDP/TCP on public internet (NetBird required)

---

## 10. Aggregator API reference (for optional integration)

These endpoints exist on the TAKNET dashboard API (output owner auth required):

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/outputs/<output_id>/klv/connection-info` | Returns `netbird_ip`, `port` (9000), `management_url`, `protocol` |
| `POST` | `/api/outputs/<output_id>/klv/netbird-setup-key` | Returns ephemeral NetBird `setup_key` for MediaMTX enrollment |
| `POST` | `/api/outputs/<output_id>/cot-transforms/<id>/klv/regenerate-token` | Invalidates old token; returns new `klv_ingest_token` |

Ingest token is visible on `GET /api/outputs/<output_id>/cot-transforms/<id>` (owner only). It is **not** included in CSV export or list API responses.

---

## 11. Contact / coordination

- Confirm `token` field is implemented in sidecar and form before field trials.
- Confirm TCP reconnect with backoff on aggregator connection drop.
- Report any ST0601 tags missing from §5.3 that you decode routinely — aggregator can add consumers additively.
