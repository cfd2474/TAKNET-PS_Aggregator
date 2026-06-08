"""Generate ephemeral NetBird setup keys for external MediaMTX video sources."""

import json
import os
import subprocess
import time

import requests

NETBIRD_API_URL = os.environ.get("NETBIRD_API_URL", "http://localhost:33073").rstrip("/")
NETBIRD_API_TOKEN = os.environ.get("NETBIRD_API_TOKEN", "")
NETBIRD_VIDEO_SOURCES_GROUP_ID = os.environ.get("NETBIRD_VIDEO_SOURCES_GROUP_ID", "")
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
        result = subprocess.run(
            ["docker", "exec", "netbird-client", "netbird", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            for key in ("netbirdIp", "NetbirdIP", "IP"):
                val = data.get(key)
                if val:
                    return str(val).strip()
            self_node = data.get("Self") or data.get("self") or {}
            if isinstance(self_node, dict):
                for key in ("NetbirdIP", "netbirdIp", "IP"):
                    val = self_node.get(key)
                    if val:
                        return str(val).strip()
    except Exception:
        pass
    return ""


def create_video_source_setup_key(*, user_id: int, username: str, output_id: int) -> tuple[bool, dict | str]:
    """
    Create a one-off NetBird setup key for an external MediaMTX box.
    Returns (ok, {setup_key, management_url, expires_in} | error_message).
    """
    if not NETBIRD_API_TOKEN:
        return False, "NetBird API token not configured on this server"
    group_id = (NETBIRD_VIDEO_SOURCES_GROUP_ID or "").strip()
    if not group_id:
        return False, "NETBIRD_VIDEO_SOURCES_GROUP_ID is not configured on this server"

    if not _rate_limit_ok(user_id):
        return False, "Rate limit exceeded — try again later"

    name = f"video-src-{username}-out{output_id}-{int(time.time())}"
    payload = {
        "name": name[:64],
        "type": "one-off",
        "expires_in": 604800,
        "auto_groups": [group_id],
        "usage_limit": 1,
        "ephemeral": False,
        "allow_extra_dns_labels": False,
    }
    headers = {
        "Authorization": f"Bearer {NETBIRD_API_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        resp = requests.post(
            f"{NETBIRD_API_URL}/api/setup-keys",
            headers=headers,
            json=payload,
            timeout=15,
        )
        if resp.status_code == 401:
            headers["Authorization"] = f"Token {NETBIRD_API_TOKEN}"
            resp = requests.post(
                f"{NETBIRD_API_URL}/api/setup-keys",
                headers=headers,
                json=payload,
                timeout=15,
            )
        if resp.status_code not in (200, 201):
            return False, f"NetBird API error {resp.status_code}: {resp.text[:200]}"
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
