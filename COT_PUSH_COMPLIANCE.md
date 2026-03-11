# CoT Push to TAK Server — PyTAK-Compliant Protocol

This document describes how to send Cursor on Target (CoT) to a TAK Server **without** using a REST/call API: direct TCP or TLS streaming, compatible with [PyTAK](https://github.com/snstac/pytak) and TAK Server.

## Reference

- **PyTAK**: [https://github.com/snstac/pytak](https://github.com/snstac/pytak) — Python TAK clients, servers & gateways; CoT parse/serialize and network transport.
- **PyTAK docs**: [https://pytak.readthedocs.io/](https://pytak.readthedocs.io/) — examples, configuration, troubleshooting.

## Transport (COT_URL)

- **TCP (plain):** `tcp://host:port` — e.g. `tcp://takserver.example.com:8087`
- **TLS:** `tls://host:port` — e.g. `tls://takserver.example.com:8089`  
  TLS requires client certificate and private key; the aggregator can store these encrypted per output (see CoT push TLS in the Outputs UI).

PyTAK uses the same schemes; see [PyTAK configuration](https://pytak.readthedocs.io/en/stable/configuration/).

## CoT message format (XML)

Each CoT message is a single XML document with a root `<event>` element. Minimum compliant shape:

- **Root:** `<event>`
- **Attributes:** `version="2.0"`, `type`, `uid`, `how`, `time`, `start`, `stale`
- **Event type (untransformed):** The aggregator derives `type` from ADS-B data to match the [FTS CoT table](https://freetakteam.github.io/FreeTAKServer-User-Docs/About/architecture/cot_table/): **civil vs military** from `dbFlags` (bit 1 = military). **Fixed / rotor / LTA / UAV** come from (1) **type description** when present (tar1090/ADS-B Exchange 3–4 char code: first letter L/S/A/T = fixed, H/G = rotor; see [ADS-B Exchange map help](https://www.adsbexchange.com/map-help/) and [type description filter values](https://www.reddit.com/r/ADSB/comments/1161thj/)), then (2) ICAO emitter category (7=rotor, 10=LTA, 14/11=UAV, 2–6/9/12=fixed). Aircraft dict fields checked for type description: `t_adsb`, `type_desc`, `desc` (only if 3–4 chars with first char in L,H,G,S,A,T). If none present, the aggregator derives the first letter from **ICAO type (`t`)** using `web/data/icao_type_to_desc_first.json` (from [tar1090-db](https://github.com/wiedehopf/tar1090-db) icao_aircraft_types), so fixed/rotor/etc. work when readsb provides `t` (e.g. B738, A109) even without a 3-char type description in the feed. Types used: **a-f-A-C-F** (civil fixed), **a-f-A-C-H** (civil rotary), **a-f-A-C-L** (civil LTA), **a-f-A-C-F-q** (civil UAV), **a-f-A-M-F** (military fixed), **a-f-A-M-H** (military rotary), **a-f-A-M-L** (military LTA), **a-f-A-M-F-Q** (military UAV), or generic **a-f-A-C** / **a-f-A-M**. See [FTS MIL-STD-2525](https://freetakteam.github.io/FreeTAKServer-User-Docs/About/architecture/mil_std_2525/). Transforms may override with a custom CoT type string.
- **Child:** `<point lat="..." lon="..." le="..." hae="..." ce="..."/>` — `lat`/`lon` in degrees; `hae` (height above ellipsoid), `le` (linear/vertical error), `ce` (circular error) in **meters** (TAK/PyTAK/node-cot standard). Use a fixed vertical accuracy for `le` (e.g. 50) or 9999999 when unknown; do not set `le` to altitude.
- **Optional:** `<detail>` — the aggregator sends (COTProxy/adsbcot parity): `detail@callsign`; `<contact callsign="..." name="..."/>` (name from transform reg when set); `<usericon iconsetpath="..."/>` when transform has icon and “Include icon in CoT” is on; `<remarks>` when transform has remarks; `<track speed="..." course="..." slope="..."/>` when aircraft has gs/track (and optional `slope` in degrees from baro_rate when gs present). Remarks always include source: "taknet-ps", feed type ("ADSBHub" or "direct feed"), and "CoT-Proxy" when a transform was applied; then transform remarks text or (when no transform remarks) squawk/category. When transform has a video URL, `<__video url="..."/>` is added as a child of the event root (COTProxy parity).

Time fields use **W3C XML Schema dateTime** in UTC, e.g. `2025-03-09T12:00:00.000000Z` (see PyTAK `cot_time()`).

Example (minimal):

```xml
<event version="2.0" type="a-f-A" uid="ICAO_HEX" how="m-g" time="..." start="..." stale="...">
  <point lat="40.78" lon="-73.96" le="50" hae="0" ce="10"/>
  <detail><contact callsign="N12345"/></detail>
</event>
```
(Untransformed aircraft use a derived type from the FTS CoT table per category and dbFlags, e.g. **a-f-A-C-F** for civil fixed wing.)

## Message framing (wire format)

TAK Server / PyTAK expect **one CoT message per connection** or a stream where messages are delimited. PyTAK’s receiver uses **space (0x20)** as the end-of-message delimiter:

- **Send:** For each CoT XML message, send the UTF-8 bytes of the XML **followed by a single space byte** (`\x20`).
- **Receive:** Read until the next space to get one message.

So each packet on the wire is: `cot_xml_utf8_bytes + b' '`. This matches PyTAK’s `readuntil(" ")` behavior and ensures compatibility with TAK Server and other PyTAK clients.

## Update rate and optimizations (1–2s goal)

To keep marker positions updating every 1–2 seconds in TAK, the aggregator reduces several bottlenecks:

- **Connection reuse:** One persistent TCP/TLS socket per CoT push output; reconnect only on send failure. Avoids 100–500ms connect+TLS handshake every cycle.
- **Delta updates:** Only build and send CoT for aircraft whose position/state (lat, lon, alt_baro, track, gs) changed since last send. With thousands of aircraft, only a few hundred typically move between 2s cycles, so each cycle does less work and finishes in time for the next run.
- **Configurable interval:** `COT_PUSH_INTERVAL_SECONDS` (env, default 2) controls how often the cycle runs. Cycle must complete before the next run; with delta + reuse, 2s is usually achievable.
- **Shorter aircraft fetch timeout:** (1, 2) seconds so the cycle does not block long on the merger.

First cycle after startup sends a full set (no prior state); later cycles send only changes. If the cycle takes longer than the interval, the next run is skipped (single-run lock) until the current one finishes.

## TLS (tls://)

For `tls://` URLs:

- Use a **TLS client certificate** and **private key** (and optionally a CA file for server verification).
- PyTAK-style env/config: `PYTAK_TLS_CLIENT_CERT`, `PYTAK_TLS_CLIENT_KEY`, `PYTAK_TLS_CLIENT_CAFILE`, and optionally `PYTAK_TLS_DONT_VERIFY` / `PYTAK_TLS_DONT_CHECK_HOSTNAME` for self-signed servers.

The aggregator stores client cert and key **encrypted** per CoT push output; only the backend that performs the push should decrypt and use them (never expose to API or UI).

## TAK Server setup (admin requirements)

For the feed to work, the **TAK Server administrator** must complete these steps. Without them, the connection may succeed but messages can be **silently dropped** or the cert may show as `__ANON__`.

1. **Assign the feed certificate to the correct group**  
   The client certificate you upload must be assigned to the TAK Server group/channel that this feed should write to. This is done with `UserManager.jar`:
   ```bash
   sudo java -jar /path/to/UserManager.jar certmod -g "<ChannelName>" /path/to/certs/<feed-username>.pem
   ```
   Use the **base channel name** (e.g. `Aircraft Memphis`), not a `_READ`/`_WRITE` suffixed name. The same name must be used in CoreConfig and for client subscriptions.

2. **Add the group to the data feed filtergroup**  
   In TAK Server’s `CoreConfig.xml`, the `<datafeed>` element has a `<filtergroup>` list. The feed’s group **must** be listed there. If it is missing, TAK Server accepts the TLS connection but **drops all CoT messages** with no error.
   ```xml
   <datafeed ...>
     <filtergroup>Existing Channel</filtergroup>
     <filtergroup>Your Feed Channel Name</filtergroup>
   </datafeed>
   ```
   Restart TAK Server after editing CoreConfig.

3. **Group name consistency**  
   The same base channel name must be used for: (a) UserManager cert assignment, (b) CoreConfig `<filtergroup>`, and (c) the group ATAK clients subscribe to (e.g. via identity provider).

## Aggregator integration

- **Config:** CoT push outputs are configured in the dashboard (Outputs, type CoT, mode Push). Each has a `cot_url` (tcp or tls), optional COTProxy transforms, and optional TLS client cert upload.
- **Helpers:** `web/cot_pipeline.py` provides `get_cot_push_outputs()` (list of active push outputs with `cot_url`) and `get_transform_for_aircraft(output_id, hex_code)` for COTProxy-style overrides.
- **Sending:** Any sender (e.g. external adsbcot, or a future in-repo worker) should use the protocol above: connect to `cot_url`, send CoT XML + space for each event, use stored TLS certs for tls://. This keeps the push **compliant** with PyTAK and TAK Server without using a call API.
