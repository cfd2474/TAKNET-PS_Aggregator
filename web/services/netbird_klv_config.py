"""NetBird + KLV secure ingest configuration (VPN tab, .env persistence)."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import requests

from services.netbird_setup_key import NETBIRD_API_URL, NETBIRD_MANAGEMENT_URL, get_netbird_self_ip

MEDIAMTX_VIDEO_GROUP_NAME = "mediamtx_video"
AGGREGATOR_GROUP_NAME = "taknet_aggregator"
KLV_POLICY_NAME = "Secure KLV ingest (MediaMTX → aggregator)"

KLV_ENV_KEYS = (
    "NETBIRD_ENABLED",
    "NETBIRD_API_URL",
    "NETBIRD_API_TOKEN",
    "NETBIRD_MANAGEMENT_URL",
    "NETBIRD_VIDEO_SOURCES_GROUP_ID",
    "NETBIRD_CIDR",
    "KLV_INGEST_PORT",
    "KLV_NETBIRD_TARGET_IP",
    "NB_SETUP_KEY",
)


def _default_api_url() -> str:
    return (os.environ.get("NETBIRD_API_URL") or "http://localhost:33073").rstrip("/")


def _api_headers(token: str) -> dict[str, str]:
    token = (token or "").strip()
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _api_request(method: str, path: str, token: str, *, json_body: dict | None = None) -> requests.Response:
    base = _default_api_url()
    headers = _api_headers(token)
    resp = requests.request(method, f"{base}{path}", headers=headers, json=json_body, timeout=20)
    if resp.status_code == 401:
        headers["Authorization"] = f"Token {token}"
        resp = requests.request(method, f"{base}{path}", headers=headers, json=json_body, timeout=20)
    return resp


def read_env_file_values(install_dir: str) -> dict[str, str]:
    """Read raw key=value pairs from host .env (for VPN tab display)."""
    env_path = os.path.join(install_dir, ".env")
    out: dict[str, str] = {}
    try:
        if not os.path.exists(env_path):
            return out
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val.startswith("'") and val.endswith("'"):
                    val = val[1:-1].replace("'\"'\"'", "'")
                elif len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                out[key] = val
    except Exception:
        pass
    return out


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return value[:4] + "…" + value[-4:]


def get_klv_config_status(*, install_dir: str) -> dict[str, Any]:
    """Status for VPN tab: .env keys, NetBird group, enrollment, overlay IP."""
    file_env = read_env_file_values(install_dir)
    runtime_token = (os.environ.get("NETBIRD_API_TOKEN") or file_env.get("NETBIRD_API_TOKEN") or "").strip()

    env_rows = []
    for key in KLV_ENV_KEYS:
        file_val = file_env.get(key, "")
        runtime_val = os.environ.get(key, "")
        effective = runtime_val or file_val
        row: dict[str, Any] = {
            "key": key,
            "configured": bool(str(effective).strip()),
            "in_env_file": bool(str(file_val).strip()),
            "in_runtime": bool(str(runtime_val).strip()),
        }
        if key in ("NETBIRD_API_TOKEN", "NB_SETUP_KEY"):
            row["display"] = _mask_secret(effective) if effective else ""
            row["secret"] = True
        else:
            row["display"] = effective or ""
            row["secret"] = False
        env_rows.append(row)

    group_info = {"name": MEDIAMTX_VIDEO_GROUP_NAME, "exists": False, "id": ""}
    policy_info = {"name": KLV_POLICY_NAME, "exists": False, "id": ""}
    aggregator_group = {"name": AGGREGATOR_GROUP_NAME, "exists": False, "id": ""}
    api_error = ""

    if runtime_token:
        try:
            group_info = _group_status(runtime_token, MEDIAMTX_VIDEO_GROUP_NAME)
            aggregator_group = _group_status(runtime_token, AGGREGATOR_GROUP_NAME)
            policy_info = _policy_status(runtime_token, KLV_POLICY_NAME)
        except Exception as e:
            api_error = str(e)

    nb_ip = get_netbird_self_ip() or file_env.get("KLV_NETBIRD_TARGET_IP", "") or ""

    secure_enabled = (
        (file_env.get("NETBIRD_ENABLED", "").lower() == "true" or os.environ.get("NETBIRD_ENABLED", "").lower() == "true")
        and bool(file_env.get("NETBIRD_VIDEO_SOURCES_GROUP_ID") or os.environ.get("NETBIRD_VIDEO_SOURCES_GROUP_ID"))
    )

    return {
        "env": env_rows,
        "mediamtx_video_group": group_info,
        "aggregator_group": aggregator_group,
        "klv_policy": policy_info,
        "netbird_overlay_ip": nb_ip,
        "klv_port": int(os.environ.get("KLV_INGEST_PORT") or file_env.get("KLV_INGEST_PORT") or "9000"),
        "management_url": file_env.get("NETBIRD_MANAGEMENT_URL") or NETBIRD_MANAGEMENT_URL,
        "api_url": file_env.get("NETBIRD_API_URL") or _default_api_url(),
        "secure_klv_enabled": secure_enabled,
        "api_reachable": bool(runtime_token) and not api_error,
        "api_error": api_error,
        "needs_dashboard_restart": False,
    }


def _list_groups(token: str) -> list[dict]:
    resp = _api_request("GET", "/api/groups", token)
    if resp.status_code != 200:
        raise RuntimeError(f"Groups API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("groups") or data.get("items") or []


def _group_status(token: str, name: str) -> dict:
    for g in _list_groups(token):
        if (g.get("name") or "").strip() == name:
            return {"name": name, "exists": True, "id": g.get("id") or ""}
    return {"name": name, "exists": False, "id": ""}


def ensure_group(token: str, name: str, peer_ids: list[str] | None = None) -> dict:
    """Return {id, name, created: bool}."""
    existing = _group_status(token, name)
    if existing["exists"]:
        gid = existing["id"]
        if peer_ids:
            _update_group_peers(token, gid, peer_ids)
        return {"id": gid, "name": name, "created": False}
    payload: dict[str, Any] = {"name": name}
    if peer_ids:
        payload["peers"] = peer_ids
    resp = _api_request("POST", "/api/groups", token, json_body=payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Create group {name} failed {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return {"id": data.get("id") or "", "name": name, "created": True}


def _update_group_peers(token: str, group_id: str, peer_ids: list[str]) -> None:
    resp = _api_request("GET", f"/api/groups/{group_id}", token)
    if resp.status_code != 200:
        return
    g = resp.json()
    current: set[str] = set()
    for p in g.get("peers") or []:
        if isinstance(p, str) and p:
            current.add(p)
        elif isinstance(p, dict) and p.get("id"):
            current.add(str(p["id"]))
    merged = list(current | set(peer_ids))
    if merged == list(current):
        return
    _api_request(
        "PUT",
        f"/api/groups/{group_id}",
        token,
        json_body={"name": g.get("name") or "", "peers": merged},
    )


def get_aggregator_peer_id() -> str:
    """NetBird peer ID for this host from netbird-client status."""
    try:
        result = subprocess.run(
            ["docker", "exec", "netbird-client", "netbird", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return ""
        data = json.loads(result.stdout)
        for key in ("ID", "id", "peerId", "peer_id"):
            val = data.get(key)
            if val:
                return str(val).strip()
        self_node = data.get("Self") or data.get("self") or {}
        if isinstance(self_node, dict):
            for key in ("ID", "id"):
                val = self_node.get(key)
                if val:
                    return str(val).strip()
    except Exception:
        pass
    return ""


def _list_policies(token: str) -> list[dict]:
    resp = _api_request("GET", "/api/policies", token)
    if resp.status_code != 200:
        raise RuntimeError(f"Policies API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if isinstance(data, list):
        return data
    return data.get("policies") or data.get("items") or []


def _policy_status(token: str, name: str) -> dict:
    for p in _list_policies(token):
        if (p.get("name") or "").strip() == name:
            return {"name": name, "exists": True, "id": p.get("id") or ""}
    return {"name": name, "exists": False, "id": ""}


def ensure_klv_policy(token: str, source_group_id: str, dest_group_id: str, port: int = 9000) -> dict:
    existing = _policy_status(token, KLV_POLICY_NAME)
    if existing["exists"]:
        return {"id": existing["id"], "name": KLV_POLICY_NAME, "created": False}
    payload = {
        "name": KLV_POLICY_NAME,
        "description": "Allow MediaMTX video sources to send KLV NDJSON to the aggregator",
        "enabled": True,
        "source_posture_checks": [],
        "rules": [
            {
                "name": "KLV TCP",
                "description": f"TCP {port} KLV ingest",
                "enabled": True,
                "action": "accept",
                "bidirectional": False,
                "protocol": "tcp",
                "ports": [str(port)],
                "sources": [source_group_id],
                "destinations": [dest_group_id],
            }
        ],
    }
    resp = _api_request("POST", "/api/policies", token, json_body=payload)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Create policy failed {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return {"id": data.get("id") or "", "name": KLV_POLICY_NAME, "created": True}


def enable_secure_klv(
    *,
    install_dir: str,
    persist_env_fn,
    api_token: str | None = None,
) -> tuple[bool, dict | str]:
    """
    Create mediamtx_video group (+ aggregator group/policy when possible), write .env keys.
    persist_env_fn(key, value) writes to host .env.
    """
    file_env = read_env_file_values(install_dir)
    token = (api_token or "").strip() or (os.environ.get("NETBIRD_API_TOKEN") or file_env.get("NETBIRD_API_TOKEN") or "").strip()
    if not token:
        return False, "NETBIRD_API_TOKEN is required. Enter it below and try again."

    steps: list[str] = []
    warnings: list[str] = []

    try:
        mtx_group = ensure_group(token, MEDIAMTX_VIDEO_GROUP_NAME)
        if mtx_group["created"]:
            steps.append(f"Created NetBird group '{MEDIAMTX_VIDEO_GROUP_NAME}'")
        else:
            steps.append(f"NetBird group '{MEDIAMTX_VIDEO_GROUP_NAME}' already exists")

        peer_id = get_aggregator_peer_id()
        agg_group_id = ""
        if peer_id:
            agg = ensure_group(token, AGGREGATOR_GROUP_NAME, peer_ids=[peer_id])
            agg_group_id = agg["id"]
            if agg["created"]:
                steps.append(f"Created group '{AGGREGATOR_GROUP_NAME}' with this server peer")
            else:
                steps.append(f"Group '{AGGREGATOR_GROUP_NAME}' ready")
            try:
                pol = ensure_klv_policy(token, mtx_group["id"], agg_group_id)
                if pol["created"]:
                    steps.append("Created access policy for TCP 9000")
                else:
                    steps.append("Access policy already exists")
            except Exception as e:
                warnings.append(f"Access policy not created: {e}")
        else:
            warnings.append(
                "Aggregator peer ID not found — enroll this server in NetBird first, then re-run Enable secure KLV to create the access policy."
            )

        api_url = file_env.get("NETBIRD_API_URL") or _default_api_url()
        mgmt_url = file_env.get("NETBIRD_MANAGEMENT_URL") or NETBIRD_MANAGEMENT_URL
        nb_ip = get_netbird_self_ip()

        persist_env_fn("NETBIRD_ENABLED", "true")
        persist_env_fn("NETBIRD_API_URL", api_url)
        persist_env_fn("NETBIRD_API_TOKEN", token)
        persist_env_fn("NETBIRD_MANAGEMENT_URL", mgmt_url)
        persist_env_fn("NETBIRD_VIDEO_SOURCES_GROUP_ID", mtx_group["id"])
        persist_env_fn("KLV_INGEST_PORT", "9000")
        if nb_ip:
            persist_env_fn("KLV_NETBIRD_TARGET_IP", nb_ip)
        if not file_env.get("NETBIRD_CIDR"):
            persist_env_fn("NETBIRD_CIDR", "100.64.0.0/10")

        steps.append("Updated /opt/taknet-aggregator/.env")

        return True, {
            "success": True,
            "steps": steps,
            "warnings": warnings,
            "mediamtx_video_group_id": mtx_group["id"],
            "aggregator_group_id": agg_group_id,
            "netbird_overlay_ip": nb_ip,
            "klv_port": 9000,
            "needs_dashboard_restart": True,
            "restart_hint": "docker compose up -d dashboard",
        }
    except Exception as e:
        return False, str(e)
