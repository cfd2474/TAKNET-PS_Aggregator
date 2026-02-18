"""VPN resolver — identifies and resolves hostnames for Tailscale and NetBird peers."""

import ipaddress
import json
import os
import subprocess

import requests

# Configuration from environment
TAILSCALE_ENABLED = os.environ.get("TAILSCALE_ENABLED", "true").lower() == "true"
TAILSCALE_CIDR = os.environ.get("TAILSCALE_CIDR", "100.64.0.0/10")

NETBIRD_ENABLED = os.environ.get("NETBIRD_ENABLED", "false").lower() == "true"
NETBIRD_API_URL = os.environ.get("NETBIRD_API_URL", "http://localhost:33073")
NETBIRD_API_TOKEN = os.environ.get("NETBIRD_API_TOKEN", "")
NETBIRD_CIDR = os.environ.get("NETBIRD_CIDR", "100.64.0.0/10")

# Pre-parse CIDR networks
_tailscale_net = ipaddress.ip_network(TAILSCALE_CIDR, strict=False) if TAILSCALE_ENABLED else None
_netbird_net = ipaddress.ip_network(NETBIRD_CIDR, strict=False) if NETBIRD_ENABLED else None

# Cache for peer lookups
_tailscale_cache = {}
_netbird_cache = {}


def classify_connection(ip_str):
    """Classify an IP as 'tailscale', 'netbird', or 'public'.

    When both VPNs use overlapping CIDR ranges, we check Tailscale first
    (since it's the current feeder VPN), then NetBird.
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return "public"

    # Check private/link-local ranges that aren't VPN
    if addr.is_private and not addr.is_loopback:
        # Could be VPN — check specifically
        if TAILSCALE_ENABLED and _tailscale_net and addr in _tailscale_net:
            # Verify it's actually a Tailscale peer
            hostname = _resolve_tailscale(ip_str)
            if hostname:
                return "tailscale"
        if NETBIRD_ENABLED and _netbird_net and addr in _netbird_net:
            hostname = _resolve_netbird(ip_str)
            if hostname:
                return "netbird"
        # In the overlap CIDR but not found in either — still classify by range
        if TAILSCALE_ENABLED and _tailscale_net and addr in _tailscale_net:
            return "tailscale"
        if NETBIRD_ENABLED and _netbird_net and addr in _netbird_net:
            return "netbird"

    return "public"


def resolve_hostname(ip_str, conn_type):
    """Resolve hostname for a given IP based on connection type."""
    if conn_type == "tailscale":
        return _resolve_tailscale(ip_str)
    elif conn_type == "netbird":
        return _resolve_netbird(ip_str)
    return None


def _resolve_tailscale(ip_str):
    """Resolve Tailscale hostname via CLI status."""
    if ip_str in _tailscale_cache:
        return _tailscale_cache[ip_str]

    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            peers = data.get("Peer", {})
            for _key, peer in peers.items():
                for addr in peer.get("TailscaleIPs", []):
                    if addr == ip_str:
                        hostname = peer.get("HostName", "")
                        _tailscale_cache[ip_str] = hostname
                        return hostname
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[vpn] Tailscale lookup failed: {e}")

    # Fallback: try socket-based lookup via Tailscale API
    try:
        sock_path = os.environ.get("TAILSCALE_API_SOCKET", "/var/run/tailscale/tailscaled.sock")
        if os.path.exists(sock_path):
            import http.client
            import socket

            class TailscaleConnection(http.client.HTTPConnection):
                def connect(self):
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(sock_path)

            conn = TailscaleConnection("local-tailscaled.sock")
            conn.request("GET", "/localapi/v0/status",
                         headers={"Sec-Tailscale": "localapi"})
            resp = conn.getresponse()
            if resp.status == 200:
                data = json.loads(resp.read())
                peers = data.get("Peer", {})
                for _key, peer in peers.items():
                    for addr in peer.get("TailscaleIPs", []):
                        if addr == ip_str:
                            hostname = peer.get("HostName", "")
                            _tailscale_cache[ip_str] = hostname
                            return hostname
    except Exception as e:
        print(f"[vpn] Tailscale socket lookup failed: {e}")

    return None


def _resolve_netbird(ip_str):
    """Resolve NetBird hostname via management API."""
    if ip_str in _netbird_cache:
        return _netbird_cache[ip_str]

    if not NETBIRD_API_TOKEN:
        return None

    try:
        headers = {
            "Authorization": f"Bearer {NETBIRD_API_TOKEN}",
            "Content-Type": "application/json",
        }
        resp = requests.get(
            f"{NETBIRD_API_URL}/api/peers",
            headers=headers,
            timeout=5,
        )
        if resp.status_code == 200:
            peers = resp.json()
            for peer in peers:
                peer_ip = peer.get("ip", "")
                if peer_ip == ip_str or ip_str in peer.get("ip_addresses", []):
                    hostname = peer.get("hostname", peer.get("name", ""))
                    _netbird_cache[ip_str] = hostname
                    return hostname
    except (requests.RequestException, ValueError) as e:
        print(f"[vpn] NetBird lookup failed: {e}")

    return None


def refresh_caches():
    """Clear VPN caches to force fresh lookups."""
    global _tailscale_cache, _netbird_cache
    _tailscale_cache = {}
    _netbird_cache = {}


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
