"""GeoIP helper — optional geolocation for public IP feeders."""

import os

GEOIP_ENABLED = os.environ.get("GEOIP_ENABLED", "false").lower() == "true"
GEOIP_DB_PATH = os.environ.get("GEOIP_DB_PATH", "/app/GeoLite2-City.mmdb")

_reader = None


def _get_reader():
    global _reader
    if _reader is not None:
        return _reader
    if not GEOIP_ENABLED:
        return None
    if not os.path.exists(GEOIP_DB_PATH):
        print(f"[geoip] Database not found at {GEOIP_DB_PATH}")
        return None
    try:
        import maxminddb
        _reader = maxminddb.open_database(GEOIP_DB_PATH)
        print(f"[geoip] Loaded database from {GEOIP_DB_PATH}")
        return _reader
    except Exception as e:
        print(f"[geoip] Failed to load database: {e}")
        return None


def lookup(ip_str):
    """Lookup geolocation for an IP address.

    Returns dict with 'location' (str), 'latitude', 'longitude' or None.
    """
    reader = _get_reader()
    if reader is None:
        return None

    try:
        result = reader.get(ip_str)
        if result is None:
            return None

        city = result.get("city", {}).get("names", {}).get("en", "")
        subdivisions = result.get("subdivisions", [])
        state = subdivisions[0].get("iso_code", "") if subdivisions else ""
        country = result.get("country", {}).get("iso_code", "")

        parts = [p for p in [city, state] if p]
        location = ", ".join(parts) if parts else country

        loc = result.get("location", {})
        lat = loc.get("latitude")
        lon = loc.get("longitude")

        return {
            "location": location or None,
            "latitude": lat,
            "longitude": lon,
        }
    except Exception as e:
        print(f"[geoip] Lookup failed for {ip_str}: {e}")
        return None
