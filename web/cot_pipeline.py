"""
CoT (Cursor on Target) output pipeline — transform aircraft to CoT and push to TAK Server.

Outputs with output_type='cot' can send aircraft as CoT to a TAK Server or multicast.
When use_cotproxy is True, transform rules (per ICAO hex) are applied from the cot_transforms
table — same concept as COTProxy known_craft / COTProxyWeb.

Push protocol (no call API): send CoT over TCP or TLS to cot_url. PyTAK-compliant format:
  - URL: tcp://host:port or tls://host:port (see PyTAK protocol_factory).
  - CoT: XML <event> with version, type, uid, how, time, start, stale, <point>, optional <detail>.
  - Framing: each message on the wire must be CoT XML UTF-8 bytes followed by a single space (0x20).
  - TLS: client cert + key (aggregator stores per-output; use for tls:// only).
See COT_PUSH_COMPLIANCE.md in the project root for full details.

The dashboard runs a background job (run_cot_sender_cycle) that fetches aircraft, applies
COTProxy transforms and pass_all filtering, builds CoT XML, and pushes to each configured output.
"""

import json
import logging
import math
import os
import socket
import ssl
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# PyTAK/TAK Server wire format: each CoT message is XML UTF-8 bytes followed by this delimiter.
COT_MESSAGE_DELIMITER = b" "

# Default CoT type for aircraft when no transform specifies one
DEFAULT_COT_TYPE = "a-f-G"
# Stale time seconds — how long until position is considered stale
COT_STALE_SECONDS = 30


def get_cot_push_outputs():
    """
    Return list of active outputs that are CoT push (output_type=cot, mode=push, status=active)
    with a cot_url in config. Each item: { output_id, cot_url, use_cotproxy, name, pass_all, config }.
    pass_all: when False, only send aircraft that have a transform; when True, send all traffic
    (transforms still apply to those that have rules). config is the full output config for filtering.
    """
    from models import get_db
    conn = get_db()
    rows = conn.execute(
        """SELECT id, name, config, use_cotproxy
           FROM outputs
           WHERE output_type = 'cot' AND mode = 'push' AND status = 'active'"""
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        cfg = json.loads(row["config"] or "{}")
        cot_url = (cfg.get("cot_url") or "").strip()
        if not cot_url:
            continue
        result.append({
            "output_id": row["id"],
            "name": row["name"],
            "cot_url": cot_url,
            "use_cotproxy": bool(row["use_cotproxy"]),
            "pass_all": bool(cfg.get("pass_all")),
            "config": cfg,
        })
    return result


def _haversine_nm(lat1, lon1, lat2, lon2):
    """Distance in nautical miles between two (lat, lon) in degrees."""
    R_NM = 3440.065  # Earth radius in nautical miles
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(min(1, a)))
    return R_NM * c


def filter_aircraft_for_output(aircraft_list, config):
    """
    Apply output filters: range limit, elevation min/max, include_network_adsb.
    config: output config dict. Returns subset of aircraft_list that passes all filters.
    """
    if not config:
        return list(aircraft_list)
    out = []
    range_enabled = config.get("range_limit_enabled") and config.get("range_limit_lat") is not None and config.get("range_limit_lon") is not None
    range_lat = _parse_float(config.get("range_limit_lat"))
    range_lon = _parse_float(config.get("range_limit_lon"))
    range_nm = _parse_float(config.get("range_limit_nm"), 250)
    if range_enabled and (range_lat is None or range_lon is None):
        range_enabled = False
    if range_nm is None or range_nm <= 0:
        range_nm = 250
    range_nm = min(250, range_nm)

    elev_enabled = config.get("elevation_filter_enabled")
    elev_min = _parse_float(config.get("elevation_min_ft")) if elev_enabled else None
    elev_max = _parse_float(config.get("elevation_max_ft")) if elev_enabled else None
    no_min = config.get("elevation_no_min")
    no_max = config.get("elevation_no_max")
    if no_min:
        elev_min = None
    if no_max:
        elev_max = None

    include_network = config.get("include_network_adsb", True)

    for ac in aircraft_list:
        if not include_network and (ac.get("source") or "").lower() == "adsbhub":
            continue
        lat = _parse_float(ac.get("lat"))
        lon = _parse_float(ac.get("lon"))
        if lat is None or lon is None:
            continue
        if range_enabled and (range_lat is not None and range_lon is not None):
            if _haversine_nm(range_lat, range_lon, lat, lon) > range_nm:
                continue
        alt = _parse_float(ac.get("alt_baro") or ac.get("altitude"))
        if elev_min is not None and (alt is None or alt < elev_min):
            continue
        if elev_max is not None and (alt is None or alt > elev_max):
            continue
        out.append(ac)
    return out


def _parse_float(v, default=None):
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def get_transform_for_aircraft(output_id, hex_code):
    """
    Return transform override for an aircraft (by ICAO hex) when use_cotproxy is enabled.
    Returns dict with keys: callsign, type, cot, icon, domain, agency, reg, model; or None.
    """
    from models import CotTransformModel
    t = CotTransformModel.get_by_hex(output_id, hex_code)
    if not t:
        return None
    return {
        "callsign": t.get("callsign"),
        "type": t.get("type"),
        "cot": t.get("cot"),
        "icon": t.get("icon"),
        "domain": t.get("domain"),
        "agency": t.get("agency"),
        "reg": t.get("reg"),
        "model": t.get("model"),
    }


def _cot_time():
    """W3C dateTime in UTC for CoT time/start/stale."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _xml_escape(s):
    if s is None:
        return ""
    s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_cot_xml(aircraft, transform=None):
    """
    Build a single CoT <event> XML string for one aircraft.
    aircraft: dict with hex, lat, lon, optional alt_baro/altitude, optional flight (callsign).
    transform: optional dict from get_transform_for_aircraft (callsign, type, cot, etc.).
    """
    hex_code = (aircraft.get("hex") or "").strip().upper()
    if not hex_code:
        return None
    lat = _parse_float(aircraft.get("lat"))
    lon = _parse_float(aircraft.get("lon"))
    if lat is None or lon is None:
        return None

    cot_type = DEFAULT_COT_TYPE
    callsign = (aircraft.get("flight") or "").strip() or hex_code
    if transform:
        if transform.get("cot"):
            cot_type = (transform["cot"] or "").strip() or DEFAULT_COT_TYPE
        if transform.get("callsign"):
            callsign = (transform["callsign"] or "").strip() or callsign

    now = _cot_time()
    # Stale time: now + COT_STALE_SECONDS
    stale_dt = datetime.now(timezone.utc).timestamp() + COT_STALE_SECONDS
    stale = datetime.fromtimestamp(stale_dt, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    alt_ft = _parse_float(aircraft.get("alt_baro") or aircraft.get("altitude"))
    hae_m = (alt_ft * 0.3048) if alt_ft is not None else 0.0
    le = str(hae_m)
    hae = str(hae_m)
    ce = "10"

    root = ET.Element("event", attrib={
        "version": "2.0",
        "type": cot_type,
        "uid": hex_code,
        "how": "m-g",
        "time": now,
        "start": now,
        "stale": stale,
    })
    ET.SubElement(root, "point", attrib={
        "lat": str(lat),
        "lon": str(lon),
        "le": le,
        "hae": hae,
        "ce": ce,
    })
    detail = ET.SubElement(root, "detail")
    ET.SubElement(detail, "contact", attrib={"callsign": _xml_escape(callsign)[:128]})
    return ET.tostring(root, encoding="unicode", default_namespace=None)


def run_cot_sender_cycle():
    """
    Fetch aircraft, for each CoT push output filter and build CoT, then push to cot_url.
    Uses AIRCRAFT_JSON_URL for aircraft data. TLS outputs use stored client cert from OutputCotCertModel.
    """
    import requests
    output_list = get_cot_push_outputs()
    if not output_list:
        log.debug("CoT sender: no active CoT push outputs")
        return
    aircraft_url = os.environ.get("AIRCRAFT_JSON_URL", "http://aircraft-merger:8090/data/aircraft.json")
    try:
        # (connect_timeout, read_timeout) so we don't hang on DNS or slow response
        r = requests.get(aircraft_url, timeout=(3, 5))
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("CoT sender: failed to fetch aircraft from %s: %s", aircraft_url, e)
        return
    aircraft_raw = data.get("aircraft", [])
    with_pos = [a for a in aircraft_raw if _parse_float(a.get("lat")) is not None and _parse_float(a.get("lon")) is not None]
    if not with_pos:
        log.debug("CoT sender: no aircraft with position (total %d)", len(aircraft_raw))
    for out in output_list:
        output_id = out["output_id"]
        name = out.get("name") or ("output-%s" % output_id)
        cot_url = out["cot_url"]
        use_cotproxy = out["use_cotproxy"]
        pass_all = out["pass_all"]
        config = out.get("config") or {}
        aircraft = filter_aircraft_for_output(with_pos, config)
        to_send = []
        for ac in aircraft:
            hex_code = (ac.get("hex") or "").strip().upper()
            if not hex_code:
                continue
            transform = get_transform_for_aircraft(output_id, hex_code) if use_cotproxy else None
            if not pass_all and not transform:
                continue
            xml_str = build_cot_xml(ac, transform)
            if xml_str:
                to_send.append(xml_str)
        if not to_send:
            log.warning(
                "CoT sender: %s — no CoT to send (pass_all=%s, use_cotproxy=%s, aircraft_after_filter=%d). "
                "If pass_all is False, add transforms for ICAO hexes that are currently in the sky.",
                name, pass_all, use_cotproxy, len(aircraft),
            )
            continue
        is_tls = cot_url.lower().startswith("tls://")
        rest = cot_url.split("://", 1)[-1].strip()
        if "/" in rest:
            rest = rest.split("/")[0]
        host_port = rest.rsplit(":", 1)
        host = host_port[0] if host_port else ""
        port = int(host_port[1]) if len(host_port) > 1 and str(host_port[1]).isdigit() else (8089 if is_tls else 8087)
        if not host:
            log.warning("CoT sender: %s — invalid cot_url (no host): %s", name, cot_url)
            continue
        cert_key = None
        if is_tls:
            from models import OutputCotCertModel
            cert_key = OutputCotCertModel.get_decrypted(output_id)
            if not cert_key or not cert_key.get("cert_pem") or not cert_key.get("key_pem"):
                log.warning("CoT sender: %s — TLS required but no client cert/key for output_id %s", name, output_id)
                continue
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            if is_tls and cert_key:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as cf:
                    cf.write(cert_key["cert_pem"])
                    cert_path = cf.name
                with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as kf:
                    kf.write(cert_key["key_pem"])
                    key_path = kf.name
                try:
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    context.load_cert_chain(cert_path, key_path)
                    # TAK Server often uses self-signed server certs; skip server verification so connection succeeds.
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                    sock = context.wrap_socket(sock, server_hostname=host)
                finally:
                    try:
                        os.unlink(cert_path)
                    except Exception:
                        pass
                    try:
                        os.unlink(key_path)
                    except Exception:
                        pass
            for xml_str in to_send:
                msg = (xml_str + " ").encode("utf-8")
                sock.sendall(msg)
            log.info("CoT sender: %s — sent %d CoT message(s) to %s:%s", name, len(to_send), host, port)
        except Exception as e:
            log.warning("CoT sender: %s — connect/send failed to %s:%s: %s", name, host, port, e)
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
