"""VPN status service — reads Tailscale and NetBird status for the dashboard."""

import json
import os
import subprocess

import requests

TAILSCALE_ENABLED = os.environ.get("TAILSCALE_ENABLED", "true").lower() == "true"
NETBIRD_ENABLED = os.environ.get("NETBIRD_ENABLED", "false").lower() == "true"
NETBIRD_API_URL = os.environ.get("NETBIRD_API_URL", "http://localhost:33073")
NETBIRD_API_TOKEN = os.environ.get("NETBIRD_API_TOKEN", "")


def get_tailscale_status():
    """Get Tailscale status and peer list."""
    if not TAILSCALE_ENABLED:
        return {"enabled": False}

    try:
        # Try socket-based API first (works inside Docker if socket is mounted)
        sock_path = "/var/run/tailscale/tailscaled.sock"
        if os.path.exists(sock_path):
            import http.client
            import socket

            class TSConn(http.client.HTTPConnection):
                def connect(self):
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.connect(sock_path)

            conn = TSConn("local-tailscaled.sock")
            conn.request("GET", "/localapi/v0/status",
                         headers={"Sec-Tailscale": "localapi"})
            resp = conn.getresponse()
            if resp.status == 200:
                data = json.loads(resp.read())
                return _parse_tailscale_status(data)
    except Exception as e:
        print(f"[vpn] Tailscale socket error: {e}")

    # Fallback: try CLI
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return _parse_tailscale_status(data)
    except Exception:
        pass

    return {"enabled": True, "state": "unavailable", "peers": []}


def _parse_tailscale_status(data):
    """Parse tailscale status JSON into dashboard-friendly format."""
    self_node = data.get("Self", {})
    peers = []

    for _key, peer in data.get("Peer", {}).items():
        peers.append({
            "hostname": peer.get("HostName", ""),
            "dns_name": peer.get("DNSName", ""),
            "ips": peer.get("TailscaleIPs", []),
            "online": peer.get("Online", False),
            "os": peer.get("OS", ""),
            "last_seen": peer.get("LastSeen", ""),
            "rx_bytes": peer.get("RxBytes", 0),
            "tx_bytes": peer.get("TxBytes", 0),
        })

    return {
        "enabled": True,
        "state": "running",
        "self_hostname": self_node.get("HostName", ""),
        "self_ips": self_node.get("TailscaleIPs", []),
        "tailnet_name": data.get("CurrentTailnet", {}).get("Name", ""),
        "peers_online": sum(1 for p in peers if p["online"]),
        "peers_total": len(peers),
        "peers": sorted(peers, key=lambda p: (not p["online"], p["hostname"])),
    }


def get_netbird_status():
    """Get NetBird status and peer list."""
    if not NETBIRD_ENABLED:
        return {"enabled": False}

    if not NETBIRD_API_TOKEN:
        return {"enabled": True, "state": "no_token", "peers": []}

    try:
        headers = {
            "Authorization": f"Bearer {NETBIRD_API_TOKEN}",
            "Content-Type": "application/json",
        }
        resp = requests.get(f"{NETBIRD_API_URL}/api/peers", headers=headers, timeout=5)
        if resp.status_code == 200:
            raw_peers = resp.json()
            peers = []
            for p in raw_peers:
                peers.append({
                    "hostname": p.get("hostname", p.get("name", "")),
                    "ip": p.get("ip", ""),
                    "connected": p.get("connected", False),
                    "os": p.get("os", ""),
                    "last_seen": p.get("last_seen", ""),
                    "version": p.get("version", ""),
                })
            return {
                "enabled": True,
                "state": "running",
                "peers_online": sum(1 for p in peers if p["connected"]),
                "peers_total": len(peers),
                "peers": sorted(peers, key=lambda p: (not p["connected"], p["hostname"])),
            }
        else:
            return {"enabled": True, "state": "api_error", "error": resp.status_code, "peers": []}
    except requests.RequestException as e:
        return {"enabled": True, "state": "unavailable", "error": str(e), "peers": []}


def get_combined_status():
    """Get combined VPN status for the dashboard."""
    return {
        "tailscale": get_tailscale_status(),
        "netbird": get_netbird_status(),
    }
