"""
CoT (Cursor on Target) output pipeline — transform aircraft to CoT and push to TAK Server.

Outputs with output_type='cot' can send aircraft as CoT to a TAK Server or multicast.
When use_cotproxy is True, transform rules (per ICAO hex) are applied from the cot_transforms
table — same concept as COTProxy known_craft / COTProxyWeb.

Push protocol (no call API): send CoT over TLS to cot_url. PyTAK-compliant format:
  - URL: tls://host:port only (see PyTAK protocol_factory). Plain TCP is not supported.
  - CoT: XML <event> with version, type, uid, how, time, start, stale, <point>, optional <detail>.
  - Framing: each message on the wire must be CoT XML UTF-8 bytes followed by a single space (0x20).
  - TLS: client cert + key (aggregator stores per-output).
See COT_PUSH_COMPLIANCE.md in the project root for full details.

The dashboard runs a background job (run_cot_sender_cycle) that fetches aircraft, applies
COTProxy transforms and pass_all filtering, builds CoT XML, and pushes to each configured output.
Optional fast path: COT_XML_USE_TEMPLATE (1/true/yes/on) uses a string template with the same
payload logic as ElementTree; toggle from Config → Services (COT push).

Optional phase timing: set COT_PHASE_TIMING in the environment for gunicorn logs, or enable
"Record timings on this page" on Config → System Health (persists in settings); lines buffer
on the Health page (see get_cot_phase_timing_lines_snapshot / API /health/cot-timing).
"""

import json
import logging
import math
import os
import socket
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional
import ssl
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

# Use gunicorn's error logger so CoT sender messages appear in `taknet-agg logs dashboard`
_logger = logging.getLogger("gunicorn.error")
if not _logger.handlers:
    _logger = logging.getLogger(__name__)
log = _logger
log.setLevel(logging.INFO)


def _cot_phase_timing_env():
    """True when COT_PHASE_TIMING is set — also logs to gunicorn.error (in addition to optional UI buffer)."""
    v = (os.environ.get("COT_PHASE_TIMING") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# Ring buffer for System Health page (same process as CoT worker; gunicorn -w 1).
_COT_TIMING_LINES = deque(maxlen=400)
_cot_timing_lines_lock = threading.Lock()


def _cot_phase_timing_ui_from_db():
    """True when admin enabled CoT phase timing from System Health (persisted in settings)."""
    try:
        from models import SETTINGS_KEY_COT_PHASE_TIMING_UI, get_setting

        v = (get_setting(SETTINGS_KEY_COT_PHASE_TIMING_UI) or "").strip().lower()
        return v in ("1", "true", "yes", "on")
    except Exception:
        return False


def clear_cot_phase_timing_lines():
    """Clear captured phase timing lines (e.g. when turning the Health UI toggle off)."""
    with _cot_timing_lines_lock:
        _COT_TIMING_LINES.clear()


def get_cot_phase_timing_lines_snapshot():
    """Return a list of recent phase timing lines (newest last) for the Health API."""
    with _cot_timing_lines_lock:
        return list(_COT_TIMING_LINES)


def _cot_phase_timing_emit(message: str, log_to_gunicorn: bool):
    """Append line to Health page buffer; optionally log to gunicorn (env-based)."""
    with _cot_timing_lines_lock:
        _COT_TIMING_LINES.append(message)
    if log_to_gunicorn:
        log.info(message)


def _phase_ms(t0, t1):
    """Elapsed milliseconds between two perf_counter() samples."""
    return round((t1 - t0) * 1000.0, 1)


# Only one cycle at a time (avoid overlapping fetch/connect from scheduler).
_cot_sender_lock = threading.Lock()

# Persistent sockets per output_id so we don't connect+TLS every cycle (saves 100–500ms+ per run).
# Cleared on send failure; next cycle reconnects. Single-threaded per cycle so no lock needed.
_persistent_sockets = {}

# Last-sent state per (output_id, hex) for delta updates: only send when position/state changed.
# Format: output_id -> { hex -> (lat, lon, alt_baro, track, gs) }. Pruned to hexes seen recently.
_last_sent_state = {}
_MAX_LAST_SENT_HEXES = 15000

# PyTAK/TAK Server wire format: each CoT message is XML UTF-8 bytes followed by this delimiter.
COT_MESSAGE_DELIMITER = b" "


def _cot_send_chunk_message_count():
    """Max CoT XML strings per sendall (complete messages only). Env COT_SEND_CHUNK_MESSAGES, default 200."""
    try:
        n = int(os.environ.get("COT_SEND_CHUNK_MESSAGES", "200"))
    except (TypeError, ValueError):
        n = 200
    return max(1, min(5000, n))


def _cot_xml_use_template():
    """When true, build CoT XML via string template (faster bulk); false uses ElementTree (default). Env COT_XML_USE_TEMPLATE."""
    v = (os.environ.get("COT_XML_USE_TEMPLATE") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# Default CoT type for aircraft when no transform specifies one.
# MIL-STD-2525 / FTS CoT table: a = atoms, f = friend, A = Air. See:
# https://freetakteam.github.io/FreeTAKServer-User-Docs/About/architecture/cot_table/
DEFAULT_COT_TYPE = "a-f-A"
# FTS CoT table air types: civil (a-f-A-C-*) and military (a-f-A-M-*) by category
COT_TYPE_CIVIL_FIXED = "a-f-A-C-F"
COT_TYPE_CIVIL_ROTOR = "a-f-A-C-H"
COT_TYPE_CIVIL_LTA = "a-f-A-C-L"
COT_TYPE_CIVIL_UAV = "a-f-A-C-F-q"
COT_TYPE_CIVIL = "a-f-A-C"
COT_TYPE_MIL_FIXED = "a-f-A-M-F"
COT_TYPE_MIL_ROTOR = "a-f-A-M-H"
COT_TYPE_MIL_LTA = "a-f-A-M-L"
COT_TYPE_MIL_UAV = "a-f-A-M-F-Q"
COT_TYPE_MIL = "a-f-A-M"
# Unknown air track (e.g. TIS-B); MIL-STD-2525 affiliation unknown (u), battle dimension air (A), function unknown (U)
COT_TYPE_UNKNOWN_AIR = "a-u-A-U"


def _cot_type_hostile_variant(cot_type: str) -> str:
    """
    Convert a-f-A... (friendly) CoT type into the matching hostile variant (a-h-A...).
    Keeps the same class (civil/military/fixed/rotor/LTA/UAV/unknown) by only switching f->h.
    """
    if not isinstance(cot_type, str):
        return cot_type
    parts = cot_type.split("-")
    # Expected shape: a-f-A-...
    if len(parts) >= 2 and parts[0] == "a" and parts[1] in ("f", "h"):
        parts[1] = "h"
        return "-".join(parts)
    return cot_type.replace("-f-", "-h-")


_DISTRESS_SQUAWK_LABELS = {
    "7700": "General Emergency.",
    "7600": "Radio Failure/Lost Communication.",
    "7500": "Unlawful Interference/Hijacking.",
}

_EMERGENCY_CODE_LABELS = {
    "none": None,
    "general": "Transponder code 7700 or general emergency",
    "medical": "Medical emergency",
    "nordo": "Radio communication failure",
    "downed": "Aircraft downed (hard-coded TCAS/ADS-B code)",
    "emergency": "Generic emergency status (older decoders)",
    "reserved": "Code value not assigned",
}


def _emergency_code_descriptor(aircraft: dict) -> tuple[str, str] | None:
    """
    Decode aircraft emergency field into (code, meaning) for remarks.
    Returns None when emergency indicates no distress.
    """
    if not isinstance(aircraft, dict):
        return None

    # Primary decoded field: 'emergency' (string). Also accept common variants.
    val = aircraft.get("emergency")
    if val is None:
        val = aircraft.get("emergency_status")
    if val is None:
        val = aircraft.get("emergencyCode")

    # Some feeds may provide a dict (e.g. {code: 'general'}).
    if isinstance(val, dict):
        for k in ("code", "type", "emergency", "status"):
            if val.get(k) is not None:
                val = val.get(k)
                break

    # If only a boolean is provided, treat True as generic emergency.
    if isinstance(val, bool):
        if val is True:
            meaning = _EMERGENCY_CODE_LABELS.get("emergency")
            return ("emergency", meaning) if meaning else None
        return None

    if val is None:
        return None

    try:
        s = str(val).strip().lower()
    except Exception:
        return None

    if not s:
        return None

    # Exact code match.
    if s in _EMERGENCY_CODE_LABELS:
        meaning = _EMERGENCY_CODE_LABELS.get(s)
        if meaning is None:
            return None
        return (s, meaning)

    # Legacy decoders may emit values containing 'emerg'.
    if "emerg" in s:
        meaning = _EMERGENCY_CODE_LABELS.get("emergency")
        return ("emergency", meaning) if meaning else None

    return None


def _distress_descriptor(aircraft: dict) -> str | None:
    """
    Return a human-readable distress descriptor string (for remarks),
    or None when aircraft is not in distress.
    """
    if not isinstance(aircraft, dict):
        return None

    squawk_desc = None
    # Squawk codes (often string or int; sometimes partially provided).
    squawk = aircraft.get("squawk")
    if squawk is None:
        squawk = aircraft.get("squawk_code")
    if squawk is not None:
        try:
            s = str(squawk).strip()
            if s:
                code = s.zfill(4)[:4]
                label = _DISTRESS_SQUAWK_LABELS.get(code)
                if label:
                    squawk_desc = f"Distress: {label} (squawk {code})"
        except Exception:
            pass

    # Emergency status via decoded emergency codes.
    emergency_desc = None
    emergency_info = _emergency_code_descriptor(aircraft)
    if emergency_info:
        code, meaning = emergency_info
        emergency_desc = f"Emergency: {code} — {meaning}."

    if squawk_desc and emergency_desc:
        return squawk_desc + " | " + emergency_desc
    return squawk_desc or emergency_desc


def _aircraft_is_distress(aircraft: dict) -> bool:
    """True if aircraft indicates emergency/distress (emergency status or squawk list)."""
    return _distress_descriptor(aircraft) is not None
# Stale time seconds — how long until position is considered stale (default; use cot_stale_seconds in output config to override)
COT_STALE_SECONDS = 30
# ft/min per knot (for track slope from baro_rate and gs)
FT_PER_MIN_PER_KNOT = 101.268


def _parse_tls_cot_endpoint(cot_url: str) -> tuple[str, int] | None:
    """
    Parse cot_url for outbound CoT push. Only tls:// is accepted.
    Strips accidental http(s):// or nested scheme prefixes in the host section
    (e.g. tls://https://host:port from UI mistakes).
    Returns (host, port) or None if invalid or tcp:// (unsupported).
    """
    u = (cot_url or "").strip()
    if not u:
        return None
    low = u.lower()
    if low.startswith("tcp://"):
        return None
    if not low.startswith("tls://"):
        return None
    rest = u[6:].strip()
    for _ in range(8):
        lr = rest.lower()
        if lr.startswith("https://"):
            rest = rest[8:].strip()
        elif lr.startswith("http://"):
            rest = rest[7:].strip()
        elif lr.startswith("tls://"):
            rest = rest[6:].strip()
        elif lr.startswith("tcp://"):
            rest = rest[6:].strip()
        else:
            break
    if "/" in rest:
        rest = rest.split("/")[0]
    rest = rest.strip()
    if not rest:
        return None
    host_port = rest.rsplit(":", 1)
    host = host_port[0].strip()
    if not host:
        return None
    default_port = 8089
    if len(host_port) > 1 and str(host_port[1]).strip().isdigit():
        port = int(host_port[1])
    else:
        port = default_port
    if port < 1 or port > 65535:
        return None
    return (host, port)


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
        if cfg.get("cot_tls_paused") is True:
            continue
        result.append({
            "output_id": row["id"],
            "name": row["name"],
            "cot_url": cot_url,
            # CoT outputs always use COTProxy transforms (forced for consistency with UI).
            "use_cotproxy": True,
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


def _parse_category_int(category):
    """Return ADS-B emitter category as int, or None.

    Handles:
    - ints (already parsed)
    - decimal strings (e.g. "10")
    - hex strings with/without prefix (e.g. "0x0A" or "0A")
    - float-like strings that represent integers (e.g. "10.0")
    """
    if category is None:
        return None
    if isinstance(category, int):
        return category if 0 <= category <= 255 else None
    s = str(category).strip()
    if not s:
        return None
    try:
        if s.startswith("0x") or s.startswith("0X"):
            v = int(s, 16)
            return v if 0 <= v <= 255 else None

        # Some aircraft/feeds emit small categories as hex without 0x (e.g. "0A").
        # Only attempt this when the value is short and contains hex letters.
        if len(s) <= 2 and any(c in "abcdefABCDEF" for c in s):
            if all(c in "0123456789abcdefABCDEF" for c in s):
                v = int(s, 16)
                return v if 0 <= v <= 255 else None

        # Float-like strings occasionally show up (e.g. "10.0") — treat as integer.
        if "." in s:
            f = float(s)
            v = int(f)
            if f == float(v) and 0 <= v <= 255:
                return v
            return v if 0 <= v <= 255 else None

        v = int(s, 10)
        return v if 0 <= v <= 255 else None
    except (TypeError, ValueError):
        return None


# ICAO ADS-B emitter category (decimal): 1=no info, 2–6=fixed wing, 7=rotor, 9=glider, 10=LTA, 11/14=UAV
EMITTER_ROTOR = 7
EMITTER_LTA = 10
EMITTER_UAV = 14
EMITTER_UAV_ALT = 11  # Parachutist/UAV in some specs
EMITTER_GLIDER = 9

# Type description (tar1090/ADS-B Exchange style): 3–4 char code, 1st = class.
# L=landplane, S=seaplane, A=amphibian, H=helicopter, G=gyroplane, T=tilt-wing/tiltrotor (see ADS-B Exchange map help / Reddit filter values).
TYPE_DESC_ROTOR_FIRST = frozenset("HG")   # Helicopter, Gyroplane
TYPE_DESC_FIXED_FIRST = frozenset("LSAT")  # Landplane, Seaplane, Amphibian, Tilt-wing

# ICAO type (t) -> first letter of type description (L/H/G/S/A/T). Loaded from web/data/icao_type_to_desc_first.json (from tar1090-db).
_ICAO_TYPE_FIRST = None


def _load_icao_type_first():
    """Load ICAO type -> first letter mapping from data file. Once loaded, cached in _ICAO_TYPE_FIRST."""
    global _ICAO_TYPE_FIRST
    if _ICAO_TYPE_FIRST is not None:
        return _ICAO_TYPE_FIRST
    path = os.path.join(os.path.dirname(__file__), "data", "icao_type_to_desc_first.json")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                _ICAO_TYPE_FIRST = json.load(f)
        else:
            _ICAO_TYPE_FIRST = {}
    except Exception:
        _ICAO_TYPE_FIRST = {}
    return _ICAO_TYPE_FIRST


def _get_type_desc_from_aircraft(aircraft):
    """
    Return the 3–4 char type description if present (tar1090 / readsb DB / ADS-B Exchange style).
    Checks t_adsb, type_desc, desc. Value must be 3–4 chars with first char in L,H,G,S,A,T.
    If none of those, derives from ICAO type (t) using web/data/icao_type_to_desc_first.json
    (from tar1090-db icao_aircraft_types.json) so aggregator can show fixed/rotor/etc. without
    needing the 3-char field in the feed.
    Refs: https://www.adsbexchange.com/map-help/ https://www.reddit.com/r/ADSB/comments/1161thj/
    Never raises — returns None on any error so CoT flow is not interrupted.
    """
    try:
        if not isinstance(aircraft, dict):
            return None
        for key in ("t_adsb", "type_desc", "desc"):
            val = aircraft.get(key)
            if not val or not isinstance(val, str):
                continue
            s = val.strip().upper()
            if 3 <= len(s) <= 4 and s[0:1] in "LHSATG":
                return s
        # Derive from ICAO type (t) when readsb/tar1090 provide it (e.g. B738, A109)
        t = aircraft.get("t")
        if t and isinstance(t, str) and t.strip():
            icao = t.strip().upper()
            mapping = _load_icao_type_first()
            first = mapping.get(icao) if mapping else None
            if first and first in "LHSATG":
                return first + "xx"
    except Exception:
        pass
    return None


def _is_tisb(aircraft):
    """True if aircraft appears to be TIS-B.

    readsb/tar1090 typically marks this via aircraft["type"] prefixes like tisb_icao/tisb_trackfile/tisb_other.
    Some feeds also expose a text squawk marker (e.g. "TIS-B") instead of numeric transponder codes.
    """
    if not isinstance(aircraft, dict):
        return False
    t = (aircraft.get("type") or "").strip().lower()
    if t.startswith("tisb_"):
        return True

    # Fallback: some integrations provide a text squawk marker.
    squawk = aircraft.get("squawk")
    if squawk is None:
        squawk = aircraft.get("squawk_code")
    if isinstance(squawk, str):
        s = squawk.strip().lower()
        # Normalize common punctuation variants.
        s_norm = s.replace("_", "-").replace(" ", "-")
        if s_norm in ("tis-b", "tisb", "tis-b-b") or s_norm.startswith("tisb-") or s_norm.startswith("tisb"):
            return True
    return False


def _cot_type_from_aircraft(aircraft):
    """
    Derive MIL-STD-2525 CoT type for untransformed aircraft from type description (tar1090),
    ADS-B emitter category, and dbFlags. FTS CoT table:
    https://freetakteam.github.io/FreeTAKServer-User-Docs/About/architecture/cot_table/
    dbFlags & 1 => military; else civil. Type desc (L2J, H..) overrides fixed/rotor when present;
    LTA/UAV still from emitter category only. Never raises — returns DEFAULT_COT_TYPE on error.
    """
    try:
        if not isinstance(aircraft, dict):
            return DEFAULT_COT_TYPE
        raw = aircraft.get("dbFlags") or 0
        try:
            military = bool(int(raw) & 1)
        except (TypeError, ValueError):
            military = False
        cat = _parse_category_int(aircraft.get("category") or aircraft.get("category_adsb"))
        type_desc = _get_type_desc_from_aircraft(aircraft)
        first_char = type_desc[0:1] if type_desc else None

        # LTA and UAV only from emitter category (type desc has no LTA/UAV class)
        if cat == EMITTER_LTA:
            return COT_TYPE_MIL_LTA if military else COT_TYPE_CIVIL_LTA
        if cat in (EMITTER_UAV, EMITTER_UAV_ALT):
            return COT_TYPE_MIL_UAV if military else COT_TYPE_CIVIL_UAV

        # Rotor from type desc (H, G) or from emitter category 7
        if first_char in TYPE_DESC_ROTOR_FIRST:
            return COT_TYPE_MIL_ROTOR if military else COT_TYPE_CIVIL_ROTOR
        if cat in (EMITTER_ROTOR,):
            return COT_TYPE_MIL_ROTOR if military else COT_TYPE_CIVIL_ROTOR

        # Fixed from type desc (L, S, A, T) or from category (fixed/glider/ultralight)
        if first_char in TYPE_DESC_FIXED_FIRST:
            return COT_TYPE_MIL_FIXED if military else COT_TYPE_CIVIL_FIXED
        if cat is not None and (cat in (EMITTER_GLIDER,) or (1 <= cat <= 6) or (cat in (12,))):
            return COT_TYPE_MIL_FIXED if military else COT_TYPE_CIVIL_FIXED

        return COT_TYPE_MIL if military else COT_TYPE_CIVIL
    except Exception:
        return DEFAULT_COT_TYPE


def get_transform_for_aircraft(output_id, hex_code):
    """
    Return transform override for an aircraft (by ICAO hex) when use_cotproxy is enabled.
    Returns dict with keys: callsign, type, cot, icon, domain, agency, reg, model, remarks, video; or None.
    """
    from models import CotTransformModel
    t = CotTransformModel.get_by_hex(output_id, hex_code)
    if not t:
        return None
    return _transform_row_to_dict(t)


def _transform_row_to_dict(t):
    """Normalize a DB row to the transform dict used by build_cot_xml."""
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
        "remarks": t.get("remarks"),
        "video": t.get("video"),
    }


def get_transforms_by_hex(output_id):
    """
    Return dict of hex (uppercase) -> transform dict for all transforms of this output.
    Prefer get_transforms_for_aircraft for CoT push (loads only rows for current aircraft hexes).
    """
    from models import CotTransformModel
    rows = CotTransformModel.get_all(output_id)
    out = {}
    for r in rows:
        hex_val = (r.get("hex") or "").strip().upper()
        if hex_val:
            out[hex_val] = _transform_row_to_dict(r)
    return out


def get_transforms_for_aircraft(output_id, aircraft_list):
    """
    Load COTProxy transforms only for ICAO hexes present in aircraft_list (batched IN queries).
    Same dict shape as get_transforms_by_hex; duplicate DB hex rows: last row wins (matches full scan).
    """
    if not aircraft_list:
        return {}
    seen = set()
    hex_codes = []
    for ac in aircraft_list:
        if not isinstance(ac, dict):
            continue
        h = (ac.get("hex") or "").strip().upper()
        if h and h not in seen:
            seen.add(h)
            hex_codes.append(h)
    if not hex_codes:
        return {}
    from models import CotTransformModel

    rows = CotTransformModel.get_for_hexes(output_id, hex_codes)
    out = {}
    for r in rows:
        hex_val = (r.get("hex") or "").strip().upper()
        if hex_val:
            out[hex_val] = _transform_row_to_dict(r)
    return out


def _cot_time():
    """W3C dateTime in UTC for CoT time/start/stale."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _xml_escape(s):
    if s is None:
        return ""
    s = str(s)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@dataclass
class _CotXmlParts:
    """Intermediate CoT event fields; shared by ElementTree and template serializers."""

    cot_type: str
    hex_code: str
    now: str
    stale: str
    lat: str
    lon: str
    le: str
    hae: str
    ce: str
    callsign_raw: str
    reg_name_raw: Optional[str]
    include_icon_in_cot: bool
    icon_path_raw: Optional[str]
    rem_text_raw: str
    track_attrib: Optional[dict]
    video_url_raw: Optional[str]


def _compute_cot_xml_parts(
    aircraft,
    transform=None,
    include_icon_in_cot=True,
    now=None,
    stale=None,
    *,
    distress_hostile: bool = False,
):
    """
    Compute fields for one CoT event from aircraft + optional COTProxy transform.
    Returns None when the aircraft cannot be encoded (missing hex or position).
    """
    hex_code = (aircraft.get("hex") or "").strip().upper()
    if not hex_code:
        return None
    lat = _parse_float(aircraft.get("lat"))
    lon = _parse_float(aircraft.get("lon"))
    if lat is None or lon is None:
        return None

    cot_type = _cot_type_from_aircraft(aircraft)
    callsign = (aircraft.get("flight") or "").strip() or hex_code
    distress_desc = _distress_descriptor(aircraft) if bool(distress_hostile) else None
    distress = distress_desc is not None
    is_tisb = _is_tisb(aircraft)
    if transform:
        if transform.get("cot"):
            cot_type = (transform["cot"] or "").strip() or _cot_type_from_aircraft(aircraft)
        if transform.get("callsign"):
            callsign = (transform["callsign"] or "").strip() or callsign
    if is_tisb:
        cot_type = COT_TYPE_UNKNOWN_AIR

    if distress:
        base_type = _cot_type_from_aircraft(aircraft)
        if base_type != COT_TYPE_UNKNOWN_AIR:
            cot_type = _cot_type_hostile_variant(base_type)
        else:
            cot_type = base_type
        callsign = "*ALERT* - " + (callsign or "")

    if now is None:
        now = _cot_time()
    if stale is None:
        stale_dt = datetime.now(timezone.utc).timestamp() + COT_STALE_SECONDS
        stale = datetime.fromtimestamp(stale_dt, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    alt_ft = _parse_float(aircraft.get("alt_baro") or aircraft.get("altitude"))
    hae_m = (alt_ft * 0.3048) if alt_ft is not None else 0.0
    hae = str(hae_m)
    le = "50" if alt_ft is not None else "9999999.0"
    ce = "10"

    reg = (transform or {}).get("reg") if transform else None
    reg_name_raw = reg.strip() if reg and isinstance(reg, str) and reg.strip() else None

    icon_path_raw = None
    if include_icon_in_cot:
        icon_path = (transform or {}).get("icon") if transform else None
        if icon_path and isinstance(icon_path, str) and icon_path.strip():
            icon_path_raw = icon_path.strip()

    feed_type = "ADSBHub" if (aircraft.get("source") or "").strip().lower() == "adsbhub" else "direct feed"
    rem_parts = ["taknet-ps", feed_type, f"Hex: {hex_code}"]
    if distress_desc:
        rem_parts.append(distress_desc)
    if transform:
        rem_parts.append("CoT-Proxy")
    if is_tisb:
        rem_parts.append("Squawk: TISB_B")
    transform_remarks = (transform or {}).get("remarks") if transform else None
    if transform_remarks is not None and isinstance(transform_remarks, str) and transform_remarks.strip():
        rem_parts.append(transform_remarks.strip()[:1024])
    elif not transform or not (transform.get("remarks") and str(transform.get("remarks", "")).strip()):
        adsb_parts = []
        raw_squawk = aircraft.get("squawk")
        if not is_tisb and raw_squawk is not None and (raw_squawk != "" or raw_squawk == 0) and isinstance(raw_squawk, (str, int)):
            adsb_parts.append("Squawk: %s" % str(raw_squawk).zfill(4)[:4])
        category = aircraft.get("category") or aircraft.get("category_adsb")
        if category is not None and str(category).strip():
            adsb_parts.append("Category: %s" % str(category).strip()[:32])
        if adsb_parts:
            rem_parts.append(" | ".join(adsb_parts))
    rem_text_raw = " | ".join(rem_parts)

    track_attrib = None
    track_deg = _parse_float(aircraft.get("track"))
    gs_kts = _parse_float(aircraft.get("gs"))
    baro_rate = _parse_float(aircraft.get("baro_rate"))
    if track_deg is not None or gs_kts is not None or baro_rate is not None:
        ta = {}
        if track_deg is not None:
            ta["course"] = str(track_deg)
        if gs_kts is not None:
            ta["speed"] = str(gs_kts)
        if baro_rate is not None and gs_kts is not None and gs_kts > 0:
            try:
                gs_ft_min = gs_kts * FT_PER_MIN_PER_KNOT
                slope_deg = math.degrees(math.atan2(baro_rate, gs_ft_min))
                ta["slope"] = "%.2f" % slope_deg
            except (ValueError, TypeError, ZeroDivisionError):
                pass
        if ta:
            track_attrib = ta

    video_url_raw = None
    video_url = (transform or {}).get("video") if transform else None
    if video_url is not None and isinstance(video_url, str) and video_url.strip():
        video_url_raw = video_url.strip()

    return _CotXmlParts(
        cot_type=cot_type,
        hex_code=hex_code,
        now=now,
        stale=stale,
        lat=str(lat),
        lon=str(lon),
        le=le,
        hae=hae,
        ce=ce,
        callsign_raw=callsign,
        reg_name_raw=reg_name_raw,
        include_icon_in_cot=include_icon_in_cot,
        icon_path_raw=icon_path_raw,
        rem_text_raw=rem_text_raw,
        track_attrib=track_attrib,
        video_url_raw=video_url_raw,
    )


def _serialize_cot_xml_et(parts: _CotXmlParts) -> str:
    """Build CoT XML using ElementTree (must match _serialize_cot_xml_template byte-for-byte)."""
    root = ET.Element(
        "event",
        attrib={
            "version": "2.0",
            "type": parts.cot_type,
            "uid": parts.hex_code,
            "how": "m-g",
            "time": parts.now,
            "start": parts.now,
            "stale": parts.stale,
        },
    )
    ET.SubElement(
        root,
        "point",
        attrib={
            "lat": parts.lat,
            "lon": parts.lon,
            "le": parts.le,
            "hae": parts.hae,
            "ce": parts.ce,
        },
    )
    detail = ET.SubElement(root, "detail")
    cs = parts.callsign_raw[:128]
    detail.set("callsign", cs)
    contact_attrib = {"callsign": cs}
    if parts.reg_name_raw:
        contact_attrib["name"] = parts.reg_name_raw[:128]
    ET.SubElement(detail, "contact", attrib=contact_attrib)
    if parts.include_icon_in_cot and parts.icon_path_raw:
        ET.SubElement(detail, "usericon", attrib={"iconsetpath": parts.icon_path_raw})
    if parts.rem_text_raw:
        rem_el = ET.SubElement(detail, "remarks")
        rem_el.text = parts.rem_text_raw[:2048]
    if parts.track_attrib:
        ET.SubElement(detail, "track", attrib=parts.track_attrib)
    if parts.video_url_raw:
        video_el = ET.Element("__video")
        video_el.set("url", parts.video_url_raw[:2048])
        root.append(video_el)
    return ET.tostring(root, encoding="unicode", default_namespace=None)


def _serialize_cot_xml_template(parts: _CotXmlParts) -> str:
    """Build the same CoT XML as _serialize_cot_xml_et without ElementTree (faster bulk builds)."""
    aq = _xml_escape
    cs = aq(parts.callsign_raw[:128])
    chunks = [
        "<event ",
        f'version="{aq("2.0")}" ',
        f'type="{aq(parts.cot_type)}" ',
        f'uid="{aq(parts.hex_code)}" ',
        f'how="{aq("m-g")}" ',
        f'time="{aq(parts.now)}" ',
        f'start="{aq(parts.now)}" ',
        f'stale="{aq(parts.stale)}">',
        "<point ",
        f'lat="{aq(parts.lat)}" ',
        f'lon="{aq(parts.lon)}" ',
        f'le="{aq(parts.le)}" ',
        f'hae="{aq(parts.hae)}" ',
        f'ce="{aq(parts.ce)}" />',
        f'<detail callsign="{cs}">',
        "<contact ",
        f'callsign="{cs}"',
    ]
    if parts.reg_name_raw:
        chunks.append(f' name="{aq(parts.reg_name_raw[:128])}"')
    chunks.append(" />")
    if parts.include_icon_in_cot and parts.icon_path_raw:
        chunks.append(f'<usericon iconsetpath="{aq(parts.icon_path_raw)}" />')
    if parts.rem_text_raw:
        chunks.append("<remarks>")
        chunks.append(aq(parts.rem_text_raw[:2048]))
        chunks.append("</remarks>")
    if parts.track_attrib:
        chunks.append("<track")
        for k, v in parts.track_attrib.items():
            chunks.append(f' {k}="{aq(v)}"')
        chunks.append(" />")
    chunks.append("</detail>")
    if parts.video_url_raw:
        chunks.append(f'<__video url="{aq(parts.video_url_raw[:2048])}" />')
    chunks.append("</event>")
    return "".join(chunks)


def build_cot_xml(
    aircraft,
    transform=None,
    include_icon_in_cot=True,
    now=None,
    stale=None,
    *,
    distress_hostile: bool = False,
):
    """
    Build a single CoT <event> XML string for one aircraft.
    aircraft: dict with hex, lat, lon, optional alt_baro/altitude, optional flight (callsign).
    transform: optional dict from get_transform_for_aircraft (callsign, type, cot, etc.).
    include_icon_in_cot: when False, do not add <usericon> (avoids ATAK label sitting too high above icon).
    now, stale: optional precomputed time strings (W3C dateTime UTC); if None, computed per call (slower for bulk).
    When COT_XML_USE_TEMPLATE is set (1/true/yes/on), uses a string template instead of ElementTree.
    """
    parts = _compute_cot_xml_parts(
        aircraft,
        transform=transform,
        include_icon_in_cot=include_icon_in_cot,
        now=now,
        stale=stale,
        distress_hostile=distress_hostile,
    )
    if parts is None:
        return None
    if _cot_xml_use_template():
        return _serialize_cot_xml_template(parts)
    return _serialize_cot_xml_et(parts)


def _state_key(ac):
    """Return a comparable state tuple for delta updates: (lat, lon, alt_baro, track, gs). Rounded to avoid float noise."""
    lat = _parse_float(ac.get("lat"))
    lon = _parse_float(ac.get("lon"))
    alt = _parse_float(ac.get("alt_baro") or ac.get("altitude"))
    track = _parse_float(ac.get("track"))
    gs = _parse_float(ac.get("gs"))
    return (
        round(lat, 5) if lat is not None else None,
        round(lon, 5) if lon is not None else None,
        round(alt, 0) if alt is not None else None,
        round(track, 1) if track is not None else None,
        round(gs, 1) if gs is not None else None,
    )


def _connect_cot_socket(name, output_id, host, port, is_tls, cert_key, *, connect_timeout_sec=3):
    """Create and return a connected socket (plain or TLS), or None on failure."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(float(connect_timeout_sec))
        log.debug("CoT sender: %s — TCP connect to %s:%s", name, host, port)
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
        return sock
    except Exception as e:
        log.warning("CoT sender: %s — connect failed to %s:%s: %s", name, host, port, e)
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        return None


def drop_cot_persistent_socket(output_id):
    """Close and remove cached TLS socket for an output (e.g. after cert or URL change)."""
    sock = _persistent_sockets.pop(output_id, None)
    if sock:
        try:
            sock.close()
        except Exception:
            pass


def _cot_pause_tls_push(output_id, name, reason: str):
    """Persist cot_tls_paused and drop cached socket so we stop reconnecting until Test TLS clears the flag."""
    import json as _json

    from models import OutputModel, get_db

    conn = get_db()
    row = conn.execute("SELECT config FROM outputs WHERE id = ?", (output_id,)).fetchone()
    conn.close()
    if not row:
        return
    try:
        cfg = _json.loads(row["config"] or "{}")
    except (TypeError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    if cfg.get("cot_tls_paused") is True:
        drop_cot_persistent_socket(output_id)
        return
    OutputModel.merge_config(output_id, {"cot_tls_paused": True})
    drop_cot_persistent_socket(output_id)
    log.warning(
        "CoT sender: %s — TLS push paused (%s). Fix server, port, or certificates, then use Outputs → Test TLS connection.",
        name,
        reason,
    )


def test_cot_tls_handshake(output_id: int, cot_url_override: str | None = None, *, connect_timeout_sec=8) -> tuple[bool, str]:
    """
    Verify TCP + TLS client handshake to the CoT endpoint using stored cert/key.
    cot_url_override: optional tls:// URL to test (e.g. unsaved form values); otherwise uses DB config.
    Returns (success, message for UI).
    """
    import json as _json

    from models import OutputCotCertModel, get_db

    conn = get_db()
    row = conn.execute("SELECT config FROM outputs WHERE id = ?", (output_id,)).fetchone()
    conn.close()
    if not row:
        return False, "Output not found"
    try:
        cfg = _json.loads(row["config"] or "{}")
    except (TypeError, ValueError):
        cfg = {}
    cot_url = (cot_url_override or cfg.get("cot_url") or "").strip()
    parsed = _parse_tls_cot_endpoint(cot_url)
    if not parsed:
        return False, "Set a valid tls:// host:port (TLS only)."
    host, port = parsed
    cert_key = OutputCotCertModel.get_decrypted(output_id)
    if not cert_key or not cert_key.get("cert_pem") or not cert_key.get("key_pem"):
        return False, "Upload a client certificate and private key before testing."
    sock = _connect_cot_socket(
        "cot-tls-test",
        output_id,
        host,
        port,
        True,
        cert_key,
        connect_timeout_sec=connect_timeout_sec,
    )
    if sock is None:
        return False, "Could not connect or complete TLS handshake (host, port, firewall, or certificate)."
    try:
        sock.close()
    except Exception:
        pass
    return True, "TLS handshake succeeded."


def run_cot_sender_cycle():
    """
    Fetch aircraft, for each CoT push output filter and build CoT, then push to cot_url.
    Uses AIRCRAFT_JSON_URL for aircraft data. TLS outputs use stored client cert from OutputCotCertModel.
    """
    import requests
    if not _cot_sender_lock.acquire(blocking=False):
        log.debug("CoT sender: skip (previous run still active)")
        return
    try:
        _run_cot_sender_cycle_impl(requests)
    finally:
        _cot_sender_lock.release()


def _run_cot_sender_cycle_impl(requests):
    """Inner implementation; hold _cot_sender_lock before calling."""
    timing_gunicorn = _cot_phase_timing_env()
    timing_emit = timing_gunicorn or _cot_phase_timing_ui_from_db()
    t_cycle = time.perf_counter()
    log.debug("CoT sender: cycle start")
    t0 = time.perf_counter()
    output_list = get_cot_push_outputs()
    db_outputs_ms = _phase_ms(t0, time.perf_counter())
    log.debug("CoT sender: got %d push output(s)", len(output_list))
    if not output_list:
        if timing_emit:
            _cot_phase_timing_emit(
                "CoT phase timing: outputs_db=%.1fms (no active CoT push outputs) cycle=%.1fms"
                % (db_outputs_ms, _phase_ms(t_cycle, time.perf_counter())),
                timing_gunicorn,
            )
        return
    aircraft_url = os.environ.get("AIRCRAFT_JSON_URL", "http://aircraft-merger:8090/data/aircraft.json")
    try:
        t0 = time.perf_counter()
        r = requests.get(aircraft_url, timeout=(1, 2))
        r.raise_for_status()
        data = r.json()
        http_ms = _phase_ms(t0, time.perf_counter())
        log.debug("CoT sender: aircraft fetch OK (%d aircraft)", len(data.get("aircraft", [])))
    except Exception as e:
        log.warning("CoT sender: failed to fetch aircraft from %s: %s", aircraft_url, e)
        if timing_emit:
            _cot_phase_timing_emit(
                "CoT phase timing: outputs_db=%.1fms http=failed cycle=%.1fms"
                % (db_outputs_ms, _phase_ms(t_cycle, time.perf_counter())),
                timing_gunicorn,
            )
        return
    aircraft_raw = data.get("aircraft", [])
    with_pos = [a for a in aircraft_raw if _parse_float(a.get("lat")) is not None and _parse_float(a.get("lon")) is not None]
    if not with_pos:
        log.debug("CoT sender: no aircraft with position (total %d)", len(aircraft_raw))
    if timing_emit:
        _cot_phase_timing_emit(
            "CoT phase timing: shared outputs_db=%.1fms http_fetch+json=%.1fms n_raw=%d n_with_pos=%d"
            % (db_outputs_ms, http_ms, len(aircraft_raw), len(with_pos)),
            timing_gunicorn,
        )
    from models import OutputCotCertModel
    for out in output_list:
        output_id = out["output_id"]
        name = out.get("name") or ("output-%s" % output_id)
        cot_url = out["cot_url"]
        use_cotproxy = out["use_cotproxy"]
        pass_all = out["pass_all"]
        config = out.get("config") or {}
        pass_only_tisb = bool(config.get("pass_only_tisb"))
        include_icon_in_cot = config.get("include_icon_in_cot", True)
        # Per-output stale seconds so TAK refreshes/expires markers sooner (e.g. 10–15 when pushing every 2s)
        try:
            ss = config.get("cot_stale_seconds")
            stale_seconds = int(ss) if ss is not None else COT_STALE_SECONDS
            if stale_seconds < 5:
                stale_seconds = 5
            elif stale_seconds > 300:
                stale_seconds = 300
        except (TypeError, ValueError):
            stale_seconds = COT_STALE_SECONDS
        t0 = time.perf_counter()
        aircraft = filter_aircraft_for_output(with_pos, config)
        filter_ms = _phase_ms(t0, time.perf_counter())
        t0 = time.perf_counter()
        # Transforms only for hexes in this cycle's filtered aircraft (batched IN); avoids full-table scans.
        transforms_by_hex = get_transforms_for_aircraft(output_id, aircraft) if use_cotproxy else {}
        transforms_ms = _phase_ms(t0, time.perf_counter())
        now = _cot_time()
        stale_dt = datetime.now(timezone.utc).timestamp() + stale_seconds
        stale = datetime.fromtimestamp(stale_dt, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        # Delta updates: only build/send CoT for aircraft whose position/state changed (or new)
        last_sent = dict(_last_sent_state.get(output_id, {}))
        seen_hexes = set()
        to_send = []
        t0 = time.perf_counter()
        for ac in aircraft:
            hex_code = (ac.get("hex") or "").strip().upper() if isinstance(ac, dict) else ""
            if not hex_code:
                continue
            transform = transforms_by_hex.get(hex_code) if use_cotproxy else None
            is_tisb = _is_tisb(ac)
            if not pass_all and not transform and not (pass_only_tisb and is_tisb):
                continue
            seen_hexes.add(hex_code)
            state = _state_key(ac)
            if last_sent.get(hex_code) == state:
                continue
            try:
                distress_hostile = bool(config.get("distress_hostile"))
                xml_str = build_cot_xml(
                    ac,
                    transform,
                    include_icon_in_cot=include_icon_in_cot,
                    now=now,
                    stale=stale,
                    distress_hostile=distress_hostile,
                )
            except Exception as e:
                log.warning("CoT sender: %s — skip aircraft %s (build_cot_xml failed): %s", name, hex_code, e)
                continue
            if xml_str:
                to_send.append(xml_str)
                last_sent[hex_code] = state
        build_loop_ms = _phase_ms(t0, time.perf_counter())
        # Prune cache to hexes seen this cycle; cap size so we don't grow forever
        _last_sent_state[output_id] = {h: last_sent[h] for h in seen_hexes if h in last_sent}
        if len(_last_sent_state[output_id]) > _MAX_LAST_SENT_HEXES:
            keys = list(_last_sent_state[output_id].keys())[:_MAX_LAST_SENT_HEXES]
            _last_sent_state[output_id] = {k: _last_sent_state[output_id][k] for k in keys}
        if not to_send:
            log.debug(
                "CoT sender: %s — no CoT to send this cycle (pass_all=%s, use_cotproxy=%s, aircraft_after_filter=%d, delta may have skipped all).",
                name, pass_all, use_cotproxy, len(aircraft),
            )
            if timing_emit:
                _cot_phase_timing_emit(
                    "CoT phase timing:   %s id=%s n_filtered=%d n_to_send=0 filter=%.1fms transforms=%.1fms build_loop=%.1fms (delta: nothing to send)"
                    % (name, output_id, len(aircraft), filter_ms, transforms_ms, build_loop_ms),
                    timing_gunicorn,
                )
            continue
        parsed = _parse_tls_cot_endpoint(cot_url)
        if parsed is None:
            cul = (cot_url or "").strip().lower()
            if cul.startswith("tcp://"):
                log.warning(
                    "CoT sender: %s — plain TCP is no longer supported; set cot_url to tls://host:port and upload a client certificate.",
                    name,
                )
            else:
                log.warning("CoT sender: %s — invalid or non-TLS cot_url (expected tls://host:port): %s", name, cot_url)
            if timing_emit:
                _cot_phase_timing_emit(
                    "CoT phase timing:   %s id=%s n_filtered=%d n_to_send=%d filter=%.1fms transforms=%.1fms build_loop=%.1fms (invalid cot_url, no send)"
                    % (name, output_id, len(aircraft), len(to_send), filter_ms, transforms_ms, build_loop_ms),
                    timing_gunicorn,
                )
            continue
        host, port = parsed
        is_tls = True
        t0 = time.perf_counter()
        cert_key = OutputCotCertModel.get_decrypted(output_id)
        cert_ms = _phase_ms(t0, time.perf_counter())
        if not cert_key or not cert_key.get("cert_pem") or not cert_key.get("key_pem"):
            log.warning("CoT sender: %s — TLS required but no client cert/key for output_id %s", name, output_id)
            if timing_emit:
                _cot_phase_timing_emit(
                    "CoT phase timing:   %s id=%s n_filtered=%d n_to_send=%d filter=%.1fms transforms=%.1fms build_loop=%.1fms cert=%.1fms (no cert, no send)"
                    % (
                        name,
                        output_id,
                        len(aircraft),
                        len(to_send),
                        filter_ms,
                        transforms_ms,
                        build_loop_ms,
                        cert_ms,
                    ),
                    timing_gunicorn,
                )
            continue
        # Reuse persistent socket to avoid connect+TLS every cycle (saves 100–500ms+)
        sock = _persistent_sockets.get(output_id)
        connect_ms = 0.0
        if sock is None:
            t0 = time.perf_counter()
            sock = _connect_cot_socket(name, output_id, host, port, is_tls, cert_key)
            connect_ms = _phase_ms(t0, time.perf_counter())
            if sock is not None:
                _persistent_sockets[output_id] = sock
        if sock is None:
            if timing_emit:
                _cot_phase_timing_emit(
                    "CoT phase timing:   %s id=%s n_filtered=%d n_to_send=%d filter=%.1fms transforms=%.1fms build_loop=%.1fms cert=%.1fms connect=%.1fms (connect failed)"
                    % (
                        name,
                        output_id,
                        len(aircraft),
                        len(to_send),
                        filter_ms,
                        transforms_ms,
                        build_loop_ms,
                        cert_ms,
                        connect_ms,
                    ),
                    timing_gunicorn,
                )
            _cot_pause_tls_push(output_id, name, "connect failed")
            continue
        try:
            # Chunked send: complete messages only (space between XML events, trailing space per chunk).
            # Reduces single giant sendall blocking and peak memory vs one ~MB buffer.
            chunk_n = _cot_send_chunk_message_count()
            send_timeout = max(60, 15 + len(to_send) // 60)
            sock.settimeout(send_timeout)
            t0 = time.perf_counter()
            total_buf_bytes = 0
            join_encode_ms = 0.0
            sendall_ms = 0.0
            for c0 in range(0, len(to_send), chunk_n):
                chunk = to_send[c0 : c0 + chunk_n]
                t_je = time.perf_counter()
                buf = (" ".join(chunk) + " ").encode("utf-8")
                total_buf_bytes += len(buf)
                join_encode_ms += _phase_ms(t_je, time.perf_counter())
                t_s = time.perf_counter()
                sock.sendall(buf)
                sendall_ms += _phase_ms(t_s, time.perf_counter())
            encode_send_ms = _phase_ms(t0, time.perf_counter())
            log.info(
                "CoT sender: %s — sent %d CoT message(s) to %s:%s (%d chunk(s), connection reused)",
                name,
                len(to_send),
                host,
                port,
                (len(to_send) + chunk_n - 1) // chunk_n,
            )
            if timing_emit:
                _cot_phase_timing_emit(
                    "CoT phase timing:   %s id=%s n_filtered=%d n_to_send=%d buf_bytes=%d filter=%.1fms transforms=%.1fms build_loop=%.1fms cert=%.1fms connect=%.1fms join_encode=%.1fms sendall=%.1fms encode_send=%.1fms"
                    % (
                        name,
                        output_id,
                        len(aircraft),
                        len(to_send),
                        total_buf_bytes,
                        filter_ms,
                        transforms_ms,
                        build_loop_ms,
                        cert_ms,
                        connect_ms,
                        join_encode_ms,
                        sendall_ms,
                        encode_send_ms,
                    ),
                    timing_gunicorn,
                )
        except Exception as e:
            log.warning("CoT sender: %s — send failed to %s:%s: %s (will reconnect next cycle)", name, host, port, e)
            if timing_emit:
                _cot_phase_timing_emit(
                    "CoT phase timing:   %s id=%s n_filtered=%d n_to_send=%d filter=%.1fms transforms=%.1fms build_loop=%.1fms cert=%.1fms connect=%.1fms encode_send=failed"
                    % (
                        name,
                        output_id,
                        len(aircraft),
                        len(to_send),
                        filter_ms,
                        transforms_ms,
                        build_loop_ms,
                        cert_ms,
                        connect_ms,
                    ),
                    timing_gunicorn,
                )
            try:
                sock.close()
            except Exception:
                pass
            _persistent_sockets.pop(output_id, None)
    if timing_emit:
        _cot_phase_timing_emit(
            "CoT phase timing: cycle_total=%.1fms" % (_phase_ms(t_cycle, time.perf_counter()),),
            timing_gunicorn,
        )
    log.debug("CoT sender: cycle done")
