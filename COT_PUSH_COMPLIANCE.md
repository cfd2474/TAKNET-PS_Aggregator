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
- **Child:** `<point lat="..." lon="..." le="..." hae="..." ce="..."/>`
- **Optional:** `<detail>` (e.g. `<contact callsign="..."/>`, `_flow-tags_`)

Time fields use **W3C XML Schema dateTime** in UTC, e.g. `2025-03-09T12:00:00.000000Z` (see PyTAK `cot_time()`).

Example (minimal):

```xml
<event version="2.0" type="a-f-G" uid="ICAO_HEX" how="m-g" time="..." start="..." stale="...">
  <point lat="40.78" lon="-73.96" le="9999999.0" hae="0" ce="10"/>
  <detail><contact callsign="N12345"/></detail>
</event>
```

## Message framing (wire format)

TAK Server / PyTAK expect **one CoT message per connection** or a stream where messages are delimited. PyTAK’s receiver uses **space (0x20)** as the end-of-message delimiter:

- **Send:** For each CoT XML message, send the UTF-8 bytes of the XML **followed by a single space byte** (`\x20`).
- **Receive:** Read until the next space to get one message.

So each packet on the wire is: `cot_xml_utf8_bytes + b' '`. This matches PyTAK’s `readuntil(" ")` behavior and ensures compatibility with TAK Server and other PyTAK clients.

## TLS (tls://)

For `tls://` URLs:

- Use a **TLS client certificate** and **private key** (and optionally a CA file for server verification).
- PyTAK-style env/config: `PYTAK_TLS_CLIENT_CERT`, `PYTAK_TLS_CLIENT_KEY`, `PYTAK_TLS_CLIENT_CAFILE`, and optionally `PYTAK_TLS_DONT_VERIFY` / `PYTAK_TLS_DONT_CHECK_HOSTNAME` for self-signed servers.

The aggregator stores client cert and key **encrypted** per CoT push output; only the backend that performs the push should decrypt and use them (never expose to API or UI).

## Aggregator integration

- **Config:** CoT push outputs are configured in the dashboard (Outputs, type CoT, mode Push). Each has a `cot_url` (tcp or tls), optional COTProxy transforms, and optional TLS client cert upload.
- **Helpers:** `web/cot_pipeline.py` provides `get_cot_push_outputs()` (list of active push outputs with `cot_url`) and `get_transform_for_aircraft(output_id, hex_code)` for COTProxy-style overrides.
- **Sending:** Any sender (e.g. external adsbcot, or a future in-repo worker) should use the protocol above: connect to `cot_url`, send CoT XML + space for each event, use stored TLS certs for tls://. This keeps the push **compliant** with PyTAK and TAK Server without using a call API.
