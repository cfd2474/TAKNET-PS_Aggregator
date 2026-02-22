"""VPN resolver — identifies and resolves hostnames for Tailscale and NetBird peers."""

import http.client
import ipaddress
import json
import os
import socket
import time

import requests

# Configuration from environment
TAILSCALE_ENABLED     = os.environ.get("TAILSCALE_ENABLED", "true").lower() == "true"
TAILSCALE_CIDR        = os.environ.get("TAILSCALE_CIDR", "100.64.0.0/10")
TAILSCALE_API_SOCKET  = os.environ.get("TAILSCALE_API_SOCKET", "/var/run/tailscale/tailscaled.sock")

NETBIRD_ENABLED       = os.environ.get("NETBIRD_ENABLED", "false").lower() == "true"
NETBIRD_API_URL       = os.environ.get("NETBIRD_API_URL", "http://localhost:33073")
NETBIRD_API_TOKEN     = os.environ.get("NETBIRD_API_TOKEN", "")
NETBIRD_CIDR          = os.environ.get("NETBIRD_CIDR", "100.64.0.0/10")

# Pre-parse CIDR networks
_tailscale_net = ipaddress.ip_network(TAILSCALE_CIDR, strict=False) if TAILSCALE_ENABLED else None
_netbird_net   = ipaddress.ip_network(NETBIRD_CIDR,   strict=False) if NETBIRD_ENABLED  else None

# Peer list caches — keyed by IP → {hostname, name}
# Refreshed as a full list every _CACHE_TTL seconds to avoid per-IP API calls
_CACHE_TTL = 60  # seconds

_netbird_peers: dict  = {}   # ip → peer dict
_netbird_cache_ts: float = 0.0

_tailscale_peers: dict = {}  # ip → peer dict
_tailscale_cache_ts: float = 0.0


# ── NetBird ───────────────────────────────────────────────────────────────────

def _refresh_netbird():
    """Refresh full NetBird peer list from management API."""
    global _netbird_peers, _netbird_cache_ts
    if not NETBIRD_API_TOKEN:
        return
    try:
        resp = requests.get(
            f"{NETBIRD_API_URL}/api/peers",
            headers={"Authorization": f"Bearer {NETBIRD_API_TOKEN}",
                     "Content-Type": "application/json"},
            timeout=5,
        )
        if resp.status_code == 200:
            peers = resp.json()
            mapping = {}
            for p in peers:
                ip = p.get("ip", "")
                if ip:
                    mapping[ip] = p
                for extra_ip in p.get("ip_addresses", []):
                    if extra_ip and extra_ip != ip:
                        mapping[extra_ip] = p
            _netbird_peers = mapping
            _netbird_cache_ts = time.monotonic()
    except Exception as e:
        print(f"[vpn] NetBird peer refresh failed: {e}")


def _get_netbird_peer(ip_str):
    """Return NetBird peer dict for an IP, or None."""
    if not NETBIRD_ENABLED or not NETBIRD_API_TOKEN:
        return None
    if time.monotonic() - _netbird_cache_ts > _CACHE_TTL:
        _refresh_netbird()
    return _netbird_peers.get(ip_str)


# ── Tailscale ─────────────────────────────────────────────────────────────────

def _refresh_tailscale():
    """Refresh full Tailscale peer list from local socket API."""
    global _tailscale_peers, _tailscale_cache_ts
    if not os.path.exists(TAILSCALE_API_SOCKET):
        return
    try:
        class _SockConn(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.connect(TAILSCALE_API_SOCKET)

        conn = _SockConn("local-tailscaled.sock")
        conn.request("GET", "/localapi/v0/status", headers={"Sec-Tailscale": "localapi"})
        resp = conn.getresponse()
        if resp.status == 200:
            data = json.loads(resp.read())
            mapping = {}
            for peer in data.get("Peer", {}).values():
                for addr in peer.get("TailscaleIPs", []):
                    mapping[addr] = peer
            _tailscale_peers = mapping
            _tailscale_cache_ts = time.monotonic()
    except Exception as e:
        print(f"[vpn] Tailscale peer refresh failed: {e}")


def _get_tailscale_peer(ip_str):
    """Return Tailscale peer dict for an IP, or None."""
    if not TAILSCALE_ENABLED:
        return None
    if time.monotonic() - _tailscale_cache_ts > _CACHE_TTL:
        _refresh_tailscale()
    return _tailscale_peers.get(ip_str)


# ── Classification ────────────────────────────────────────────────────────────

def classify_connection(ip_str):
    """Classify an IP as 'netbird', 'tailscale', or 'public'.

    NetBird is checked first because it is the primary VPN. An explicit peer
    match always wins over a CIDR-only match. When both CIDRs overlap (both
    default to 100.64.0.0/10) and neither API returns a match, NetBird is
    preferred in the fallback.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return "public"

    in_netbird_range    = NETBIRD_ENABLED    and _netbird_net    and addr in _netbird_net
    in_tailscale_range  = TAILSCALE_ENABLED  and _tailscale_net  and addr in _tailscale_net

    # Explicit peer match — NetBird first (primary VPN)
    if in_netbird_range and _get_netbird_peer(ip_str):
        return "netbird"

    if in_tailscale_range and _get_tailscale_peer(ip_str):
        return "tailscale"

    # CIDR-only fallback — NetBird preferred
    if in_netbird_range:
        return "netbird"
    if in_tailscale_range:
        return "tailscale"

    return "public"


def resolve_hostname(ip_str, conn_type):
    """Resolve hostname for a given IP based on connection type."""
    if conn_type == "netbird":
        peer = _get_netbird_peer(ip_str)
        if peer:
            return peer.get("name") or peer.get("hostname", "")
    elif conn_type == "tailscale":
        peer = _get_tailscale_peer(ip_str)
        if peer:
            return peer.get("HostName", "")
    return None


def refresh_caches():
    """Force refresh of all VPN peer caches."""
    global _netbird_cache_ts, _tailscale_cache_ts
    _netbird_cache_ts = 0.0
    _tailscale_cache_ts = 0.0


def get_vpn_summary():
    """Return summary of VPN configuration for status display."""
    return {
        "tailscale": {
            "enabled": TAILSCALE_ENABLED,
            "cidr": TAILSCALE_CIDR,
        },
        "netbird": {
            "enabled": NETBIRD_ENABLED,
            "cidr": NETBIRD_CIDR,
            "api_url": NETBIRD_API_URL if NETBIRD_ENABLED else None,
        },
    }
