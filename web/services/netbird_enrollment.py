"""NetBird server enrollment status (aggregator host peer)."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from services.docker_service import get_netbird_client_status
from services.netbird_klv_config import read_env_file_values

CONTAINER_NAME = "netbird-client"


def _mask_setup_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return value[:4] + "…" + value[-4:]


def _pick_str(data: dict, *keys: str) -> str:
    for key in keys:
        val = data.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _parse_mesh_status() -> dict[str, Any]:
    """Read netbird status --json from the client container."""
    out: dict[str, Any] = {
        "overlay_ip": "",
        "hostname": "",
        "fqdn": "",
        "management_connected": False,
        "signal_connected": False,
        "peers_total": 0,
        "peers_connected": 0,
        "status_error": "",
    }
    try:
        result = subprocess.run(
            ["docker", "exec", CONTAINER_NAME, "netbird", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "netbird status failed").strip()
            out["status_error"] = err[:300]
            return out
        if not result.stdout.strip():
            out["status_error"] = "Empty status response"
            return out

        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            out["status_error"] = "Unexpected status format"
            return out

        self_node = data.get("Self") or data.get("self") or {}
        if not isinstance(self_node, dict):
            self_node = {}

        out["overlay_ip"] = _pick_str(data, "netbirdIp", "NetbirdIP", "IP") or _pick_str(
            self_node, "NetbirdIP", "netbirdIp", "IP"
        )
        out["hostname"] = _pick_str(self_node, "Hostname", "hostname", "Name", "name") or _pick_str(
            data, "Hostname", "hostname"
        )
        out["fqdn"] = _pick_str(self_node, "FQDN", "fqdn") or _pick_str(data, "FQDN", "fqdn")

        mgmt = data.get("Management") or data.get("management") or {}
        sig = data.get("Signal") or data.get("signal") or {}
        if isinstance(mgmt, dict):
            out["management_connected"] = bool(
                mgmt.get("Connected") or mgmt.get("connected")
            )
        if isinstance(sig, dict):
            out["signal_connected"] = bool(sig.get("Connected") or sig.get("connected"))

        peers = data.get("Peers") or data.get("peers") or []
        if isinstance(peers, list):
            out["peers_total"] = len(peers)
            out["peers_connected"] = sum(
                1
                for p in peers
                if isinstance(p, dict)
                and (p.get("Connected") or p.get("connected"))
            )

        for key in ("ManagementConnected", "managementConnected", "SignalConnected"):
            if key in data:
                if "management" in key.lower():
                    out["management_connected"] = bool(data[key])
                else:
                    out["signal_connected"] = bool(data[key])
    except json.JSONDecodeError as e:
        out["status_error"] = f"Invalid status JSON: {e}"
    except Exception as e:
        out["status_error"] = str(e)[:300]
    return out


def _management_url(install_dir: str) -> str:
    file_env = read_env_file_values(install_dir)
    for raw in (
        file_env.get("NETBIRD_MANAGEMENT_URL"),
        os.environ.get("NETBIRD_MANAGEMENT_URL"),
        file_env.get("NETBIRD_API_URL"),
        os.environ.get("NETBIRD_API_URL"),
        "https://netbird.tak-solutions.com",
    ):
        if raw and str(raw).strip():
            return str(raw).strip().rstrip("/")
    return "https://netbird.tak-solutions.com"


def get_enrollment_status(*, install_dir: str) -> dict[str, Any]:
    """Full server enrollment status for VPN tab."""
    file_env = read_env_file_values(install_dir)
    nb_setup_key = (
        os.environ.get("NB_SETUP_KEY") or file_env.get("NB_SETUP_KEY") or ""
    ).strip()
    container = get_netbird_client_status()
    mesh = _parse_mesh_status() if container.get("running") else {
        "overlay_ip": "",
        "hostname": "",
        "fqdn": "",
        "management_connected": False,
        "signal_connected": False,
        "peers_total": 0,
        "peers_connected": 0,
        "status_error": "",
    }

    enrolled = bool(container.get("enrolled"))
    running = bool(container.get("running"))
    overlay_ip = mesh.get("overlay_ip") or ""
    mesh_up = bool(overlay_ip)

    if enrolled and running and mesh_up:
        state = "connected"
    elif enrolled and running:
        state = "running_not_connected"
    elif enrolled:
        state = "stopped"
    else:
        state = "not_enrolled"

    return {
        "state": state,
        "enrolled": enrolled,
        "running": running,
        "mesh_connected": mesh_up,
        "container_status": container.get("status") or "",
        "started_at": container.get("started_at") or "",
        "container_error": container.get("error") or "",
        "overlay_ip": overlay_ip,
        "hostname": mesh.get("hostname") or "",
        "fqdn": mesh.get("fqdn") or "",
        "management_connected": mesh.get("management_connected"),
        "signal_connected": mesh.get("signal_connected"),
        "peers_total": mesh.get("peers_total") or 0,
        "peers_connected": mesh.get("peers_connected") or 0,
        "mesh_status_error": mesh.get("status_error") or "",
        "management_url": _management_url(install_dir),
        "nb_setup_key_configured": bool(nb_setup_key),
        "nb_setup_key_display": _mask_setup_key(nb_setup_key),
        "klv_target": f"{overlay_ip}:9000" if overlay_ip else "",
    }
