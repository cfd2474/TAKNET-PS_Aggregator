"""
TAKNET-PS Aggregator — REST API v2
Provides an airplanes.live-compatible REST API backed by the local aggregator.

Auth: pass your API key via:
  - URL param:  ?key=tak-xxxx
  - Header:     X-API-Key: tak-xxxx
  - Header:     Authorization: Bearer tak-xxxx
"""

import json
import os
import math
import time
import hashlib
import sqlite3
import threading

import requests as http_requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
DB_PATH     = os.environ.get("DB_PATH",     "/data/aggregator.db")
TAR1090_URL = os.environ.get("TAR1090_URL", "http://tar1090/data/aircraft.json")

# ── DB (read-only key validation) ─────────────────────────────────────────────
_local = threading.local()

def _get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True,
                                      timeout=5, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def _validate_key(raw_key: str):
    """Validate API key. Returns output dict (incl. config) or None.
    Works for both single_use (status ignored for REST) and durable keys."""
    if not raw_key:
        return None
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    try:
        conn = _get_conn()
        row = conn.execute(
            """SELECT o.id, o.name, o.output_type, o.status as output_status,
                      o.config, k.key_type, k.status as key_status
               FROM output_api_keys k
               JOIN outputs o ON k.output_id = o.id
               WHERE k.key_hash = ? AND o.status = 'active' AND o.output_type = 'json'""",
            (key_hash,)
        ).fetchone()
        if not row:
            return None
        return dict(row)
    except Exception:
        return None


def _extract_key():
    """Pull API key from ?key=, X-API-Key:, or Authorization: Bearer."""
    k = request.args.get("key", "").strip()
    if k:
        return k
    k = request.headers.get("X-API-Key", "").strip()
    if k:
        return k
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _require_auth():
    """Return (output_dict, None) on success or (None, Response) on failure."""
    raw_key = _extract_key()
    output = _validate_key(raw_key)
    if not output:
        return None, (jsonify({"msg": "Invalid or missing API key",
                               "now": time.time(), "total": 0, "aircraft": []}), 401)
    return output, None


# ── Aircraft data ──────────────────────────────────────────────────────────────

def _fetch_aircraft():
    """Fetch aircraft list from tar1090/merger. Returns (aircraft_list, now_ts)."""
    resp = http_requests.get(TAR1090_URL, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    return data.get("aircraft", []), data.get("now", time.time())


def _filter_aircraft_by_output(aircraft, output):
    """When output has include_network_adsb False, return only direct feeder (exclude source=adsbhub)."""
    if not output:
        return aircraft
    raw = output.get("config")
    if isinstance(raw, str):
        try:
            config = json.loads(raw or "{}")
        except Exception:
            return aircraft
    elif isinstance(raw, dict):
        config = raw
    else:
        return aircraft
    if config.get("include_network_adsb", True):
        return aircraft
    return [a for a in aircraft if (a.get("source") or "").lower() != "adsbhub"]


def _envelope(aircraft, now_ts, t0):
    """Wrap aircraft list in airplanes.live-compatible envelope."""
    return {
        "msg":   "No error",
        "now":   now_ts,
        "total": len(aircraft),
        "ctime": now_ts,
        "ptime": round((time.time() - t0) * 1000, 2),
        "aircraft": aircraft,
    }


def _error_envelope(msg, status=503):
    return jsonify({
        "msg":      msg,
        "now":      time.time(),
        "total":    0,
        "ctime":    time.time(),
        "ptime":    0,
        "aircraft": [],
    }), status


# ── Haversine distance ─────────────────────────────────────────────────────────

def _haversine_nm(lat1, lon1, lat2, lon2):
    """Great-circle distance in nautical miles."""
    R = 3440.065  # Earth radius in nm
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi  = math.radians(lat2 - lat1)
    dlam  = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.route("/v2/all")
def v2_all():
    """Return all currently tracked aircraft."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    return jsonify(_envelope(aircraft, now_ts, t0))


@app.route("/v2/hex/<path:hex_ids>")
def v2_hex(hex_ids):
    """Return aircraft matching one or more comma-separated ICAO hex IDs."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    ids = {h.strip().lower() for h in hex_ids.split(",") if h.strip()}
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("hex") or "").lower() in ids]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/callsign/<path:callsigns>")
def v2_callsign(callsigns):
    """Return aircraft matching one or more comma-separated callsigns (flight field)."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    targets = {c.strip().upper() for c in callsigns.split(",") if c.strip()}
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("flight") or "").strip().upper() in targets]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/reg/<path:regs>")
def v2_reg(regs):
    """Return aircraft matching one or more comma-separated registrations."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    targets = {r.strip().upper() for r in regs.split(",") if r.strip()}
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("r") or "").strip().upper() in targets]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/type/<path:types>")
def v2_type(types):
    """Return aircraft matching one or more comma-separated ICAO type codes (B738, A321, etc.)."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    targets = {t.strip().upper() for t in types.split(",") if t.strip()}
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("t") or "").strip().upper() in targets]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/squawk/<path:squawks>")
def v2_squawk(squawks):
    """Return aircraft squawking the specified code(s), comma-separated."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    targets = {s.strip() for s in squawks.split(",") if s.strip()}
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("squawk") or "") in targets]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/mil")
def v2_mil():
    """Return all aircraft tagged as military (dbFlags & 1)."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("dbFlags") or 0) & 1]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/ladd")
def v2_ladd():
    """Return all aircraft tagged as LADD (dbFlags & 8)."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("dbFlags") or 0) & 8]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/pia")
def v2_pia():
    """Return all aircraft tagged as PIA (dbFlags & 4)."""
    output, err = _require_auth()
    if err:
        return err
    t0 = time.time()
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = [a for a in aircraft if (a.get("dbFlags") or 0) & 4]
    return jsonify(_envelope(matched, now_ts, t0))


@app.route("/v2/point/<float:lat>/<float:lon>/<float:radius>")
def v2_point(lat, lon, radius):
    """Return all aircraft within <radius> nautical miles of <lat>,<lon>. Max 250 nm."""
    output, err = _require_auth()
    if err:
        return err
    radius = min(radius, 250.0)
    t0 = time.time()
    try:
        aircraft, now_ts = _fetch_aircraft()
    except Exception as e:
        return _error_envelope(f"Upstream error: {e}")
    aircraft = _filter_aircraft_by_output(aircraft, output)
    matched = []
    for a in aircraft:
        a_lat = a.get("lat")
        a_lon = a.get("lon")
        if a_lat is None or a_lon is None:
            continue
        dist = _haversine_nm(lat, lon, a_lat, a_lon)
        if dist <= radius:
            matched.append({**a, "_distance_nm": round(dist, 2)})
    # Sort nearest first
    matched.sort(key=lambda a: a.get("_distance_nm", 9999))
    return jsonify(_envelope(matched, now_ts, t0))


# ── Health + info ──────────────────────────────────────────────────────────────

@app.route("/v2/")
@app.route("/v2")
def v2_index():
    return jsonify({
        "msg":      "TAKNET-PS Aggregator REST API v2",
        "auth":     "Pass your API key via ?key=, X-API-Key: header, or Authorization: Bearer",
        "endpoints": [
            "GET /v2/all",
            "GET /v2/hex/<hex>[,<hex>...]",
            "GET /v2/callsign/<callsign>[,<callsign>...]",
            "GET /v2/reg/<reg>[,<reg>...]",
            "GET /v2/type/<type>[,<type>...]",
            "GET /v2/squawk/<squawk>[,<squawk>...]",
            "GET /v2/mil",
            "GET /v2/ladd",
            "GET /v2/pia",
            "GET /v2/point/<lat>/<lon>/<radius_nm>",
        ],
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
