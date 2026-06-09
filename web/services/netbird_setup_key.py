"""Generate ephemeral NetBird setup keys for external MediaMTX video sources."""

import os
import time

import requests

from services.netbird_api import api_request, default_api_base
from services.docker_service import get_netbird_status_json, normalize_netbird_ip

NETBIRD_API_TOKEN = os.environ.get("NETBIRD_API_TOKEN", "")
NETBIRD_VIDEO_SOURCES_GROUP_ID = os.environ.get("NETBIRD_VIDEO_SOURCES_GROUP_ID", "")
MEDIAMTX_VIDEO_GROUP_NAME = "mediamtx_video"
NETBIRD_MANAGEMENT_URL = os.environ.get(
    "NETBIRD_MANAGEMENT_URL", "https://netbird.tak-solutions.com"
).rstrip("/")

# Per-user rate limit: max keys per hour
_SETUP_KEY_LIMIT_PER_HOUR = 10
_setup_key_timestamps: dict[int, list[float]] = {}


def _rate_limit_ok(user_id: int) -> bool:
    now = time.time()
    window = _setup_key_timestamps.get(user_id, [])
    window = [t for t in window if now - t < 3600]
    _setup_key_timestamps[user_id] = window
    return len(window) < _SETUP_KEY_LIMIT_PER_HOUR


def _record_setup_key(user_id: int) -> None:
    _setup_key_timestamps.setdefault(user_id, []).append(time.time())


def get_netbird_self_ip() -> str:
    """Best-effort NetBird overlay IP for this aggregator host."""
    override = (os.environ.get("KLV_NETBIRD_TARGET_IP") or "").strip()
    if override:
        return override
    try:
        data, _err = get_netbird_status_json()
        if data:
            for key in ("netbirdIp", "NetbirdIP", "IP"):
                val = data.get(key)
                if val:
                    return normalize_netbird_ip(str(val))
            self_node = data.get("Self") or data.get("self") or {}
            if isinstance(self_node, dict):
                for key in ("NetbirdIP", "netbirdIp", "IP"):
                    val = self_node.get(key)
                    if val:
                        return normalize_netbird_ip(str(val))
    except Exception:
        pass
    return ""


def create_video_source_setup_key(
    *,
    user_id: int,
    username: str,
    output_id: int,
    transform_id: int | None = None,
    hex_code: str | None = None,
) -> tuple[bool, dict | str]:
    """
    Create a one-off NetBird setup key for an external MediaMTX box.
    Returns (ok, {setup_key, management_url, expires_in} | error_message).
    """
    if not NETBIRD_API_TOKEN:
        return False, "NetBird API token not configured on this server"
    group_id = (NETBIRD_VIDEO_SOURCES_GROUP_ID or "").strip()
    if not group_id:
        return False, (
            "NETBIRD_VIDEO_SOURCES_GROUP_ID is not configured. "
            "Use VPN → Enable secure KLV or set the mediamtx_video group ID in .env."
        )

    if not _rate_limit_ok(user_id):
        return False, "Rate limit exceeded — try again later"

    hx = (hex_code or "aircraft").strip().upper()[:12] or "aircraft"
    tid = transform_id or 0
    name = f"mtx-{hx}-out{output_id}-t{tid}-{int(time.time())}"
    payload = {
        "name": name[:64],
        "type": "one-off",
        "expires_in": 604800,
        "auto_groups": [group_id],
        "usage_limit": 1,
        "ephemeral": False,
        "allow_extra_dns_labels": False,
    }
    try:
        resp, _base = api_request("POST", "/api/setup-keys", NETBIRD_API_TOKEN, json_body=payload, timeout=15)
        if resp is None or resp.status_code not in (200, 201):
            detail = resp.text[:200] if resp is not None else "NetBird API unreachable"
            code = resp.status_code if resp is not None else 0
            return False, f"NetBird API error {code}: {detail}"
        data = resp.json()
        key = (data.get("key") or "").strip()
        if not key:
            return False, "NetBird API did not return a setup key"
        _record_setup_key(user_id)
        return True, {
            "setup_key": key,
            "management_url": NETBIRD_MANAGEMENT_URL,
            "expires_in": payload["expires_in"],
        }
    except requests.RequestException as e:
        return False, f"NetBird API request failed: {e}"
