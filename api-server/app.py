"""
TAKNET-PS ADS-B REST API helper service.

Legacy public /v2 endpoints are retired.
Use keyed output endpoints from dashboard-managed outputs instead.
"""

import math
import os
import time

import requests as http_requests
from flask import Flask, jsonify, request

TAR1090_URL = os.environ.get("TAR1090_URL", "http://tar1090:80/data/aircraft.json")
SITE_NAME   = os.environ.get("SITE_NAME", "TAKNET-PS Aggregator")

app = Flask(__name__)


@app.before_request
def retire_public_v2():
    """Retire legacy public REST API endpoints under /v2."""
    if request.path.startswith("/v2"):
        return _error(
            "Public REST API (/v2/*) has been retired. Use dashboard-managed keyed outputs (for example ADSB Direct).",
            410,
        )


# ── Aircraft data fetch ───────────────────────────────────────────────────────

def _fetch_aircraft():
    """Fetch and return raw aircraft list + metadata from tar1090.
    Returns (aircraft_list, now_ts, messages_total) or raises on error.
    """
    resp = http_requests.get(TAR1090_URL, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    return data.get("aircraft", []), data.get("now", time.time()), data.get("messages", 0)


def _envelope(aircraft, now_ts, ptime_ms):
    """Build an airplanes.live-compatible response envelope."""
    return {
        "msg": "No error",
        "now": now_ts,
        "total": len(aircraft),
        "ctime": now_ts,
        "ptime": round(ptime_ms, 2),
        "aircraft": aircraft,
    }


def _error(msg, code=400):
    return jsonify({"msg": msg, "now": time.time(), "total": 0, "ctime": 0,
                    "ptime": 0, "aircraft": []}), code


# ── Haversine distance (nautical miles) ───────────────────────────────────────

def _haversine_nm(lat1, lon1, lat2, lon2):
    """Return great-circle distance in nautical miles."""
    R_NM = 3440.065  # Earth radius in nautical miles
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R_NM * 2 * math.asin(math.sqrt(a))


# ── dbFlags bit constants ─────────────────────────────────────────────────────

FLAG_MIL         = 1
FLAG_INTERESTING = 2
FLAG_PIA         = 4
FLAG_LADD        = 8


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/v2/health")
def health():
    return jsonify({"status": "ok", "service": SITE_NAME})


@app.route("/v2/all")
def all_aircraft():
    """Return all currently tracked aircraft."""
    t0 = time.time()
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(ac, now_ts, ptime))


@app.route("/v2/hex/<path:hex_str>")
def by_hex(hex_str):
    """Return aircraft matching one or more ICAO hex identifiers (comma-separated)."""
    t0 = time.time()
    hexes = {h.strip().lower().lstrip("~") for h in hex_str.split(",")}
    if not hexes:
        return _error("hex parameter required")
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if a.get("hex", "").lstrip("~").lower() in hexes]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/callsign/<path:callsign_str>")
def by_callsign(callsign_str):
    """Return aircraft matching one or more callsigns (comma-separated)."""
    t0 = time.time()
    callsigns = {c.strip().upper() for c in callsign_str.split(",")}
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if (a.get("flight") or "").strip().upper() in callsigns]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/reg/<path:reg_str>")
def by_reg(reg_str):
    """Return aircraft matching one or more registrations (comma-separated)."""
    t0 = time.time()
    regs = {r.strip().upper() for r in reg_str.split(",")}
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if (a.get("r") or "").strip().upper() in regs]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/type/<path:type_str>")
def by_type(type_str):
    """Return aircraft matching one or more ICAO type codes (comma-separated)."""
    t0 = time.time()
    types = {t.strip().upper() for t in type_str.split(",")}
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if (a.get("t") or "").strip().upper() in types]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/squawk/<path:squawk_str>")
def by_squawk(squawk_str):
    """Return aircraft matching one or more squawk codes (comma-separated)."""
    t0 = time.time()
    squawks = {s.strip() for s in squawk_str.split(",")}
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if (a.get("squawk") or "") in squawks]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/mil")
def military():
    """Return all aircraft with the military dbFlag set."""
    t0 = time.time()
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if (a.get("dbFlags") or 0) & FLAG_MIL]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/ladd")
def ladd():
    """Return all aircraft tagged as LADD (Limiting Aircraft Data Displayed)."""
    t0 = time.time()
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if (a.get("dbFlags") or 0) & FLAG_LADD]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/pia")
def pia():
    """Return all aircraft tagged as PIA (Privacy ICAO Address)."""
    t0 = time.time()
    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)
    result = [a for a in ac if (a.get("dbFlags") or 0) & FLAG_PIA]
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


@app.route("/v2/point/<lat>/<lon>/<radius>")
def by_point(lat, lon, radius):
    """Return all aircraft within <radius> nautical miles of <lat>,<lon>.
    Maximum radius: 250 nm (matches airplanes.live limit).
    Aircraft without position are excluded.
    """
    t0 = time.time()
    try:
        center_lat = float(lat)
        center_lon = float(lon)
        radius_nm  = float(radius)
    except ValueError:
        return _error("lat, lon, and radius must be numeric")

    if not (-90 <= center_lat <= 90):
        return _error("lat must be between -90 and 90")
    if not (-180 <= center_lon <= 180):
        return _error("lon must be between -180 and 180")
    if radius_nm <= 0:
        return _error("radius must be greater than 0")
    if radius_nm > 250:
        radius_nm = 250  # cap at 250 nm, same as airplanes.live

    try:
        ac, now_ts, _ = _fetch_aircraft()
    except Exception as e:
        return _error(f"Upstream error: {e}", 503)

    result = []
    for a in ac:
        a_lat = a.get("lat")
        a_lon = a.get("lon")
        if a_lat is None or a_lon is None:
            continue
        dist = _haversine_nm(center_lat, center_lon, a_lat, a_lon)
        if dist <= radius_nm:
            # Annotate with distance for consumer convenience
            enriched = dict(a)
            enriched["dst"] = round(dist, 2)
            result.append(enriched)

    # Sort nearest first
    result.sort(key=lambda x: x["dst"])
    ptime = (time.time() - t0) * 1000
    return jsonify(_envelope(result, now_ts, ptime))


# ── CORS headers ──────────────────────────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ── 404 handler ───────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return _error(f"Endpoint not found. Available: /v2/all, /v2/hex/<hex>, "
                  f"/v2/callsign/<cs>, /v2/reg/<reg>, /v2/type/<type>, "
                  f"/v2/squawk/<sq>, /v2/mil, /v2/ladd, /v2/pia, "
                  f"/v2/point/<lat>/<lon>/<radius_nm>", 404)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8880, debug=False)
