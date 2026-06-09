"""NetBird + KLV secure ingest configuration (VPN tab, .env persistence)."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable

from services.netbird_api import api_request, default_api_base, normalize_api_base, parse_json_list
from services.netbird_setup_key import NETBIRD_MANAGEMENT_URL, get_netbird_self_ip

MEDIAMTX_VIDEO_GROUP_NAME = "mediamtx_video"
AGGREGATOR_GROUP_NAME = "taknet_aggregator"
KLV_POLICY_NAME = "Secure KLV ingest (MediaMTX → aggregator)"

# Server-wide .env keys only (per-aircraft values live on COTProxy transform rows).
KLV_ENV_KEYS = (
    "NETBIRD_ENABLED",
    "NETBIRD_API_URL",
    "NETBIRD_API_TOKEN",
    "NETBIRD_MANAGEMENT_URL",
    "NETBIRD_VIDEO_SOURCES_GROUP_ID",
    "NETBIRD_CIDR",
    "KLV_INGEST_PORT",
)


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


def _api_token_from_env(install_dir: str) -> str:
    file_env = read_env_file_values(install_dir)
    return (os.environ.get("NETBIRD_API_TOKEN") or file_env.get("NETBIRD_API_TOKEN") or "").strip()


def _list_groups(token: str, install_dir: str | None = None) -> list[dict]:
    resp, _base = api_request("GET", "/api/groups", token, install_dir=install_dir)
    if resp is None:
        raise RuntimeError("NetBird API unreachable")
    if resp.status_code != 200:
        raise RuntimeError(f"Groups API {resp.status_code}: {resp.text[:200]}")
    return parse_json_list(resp.json(), "groups", "items")


def _group_status(token: str, name: str, install_dir: str | None = None) -> dict:
    for g in _list_groups(token, install_dir):
        if (g.get("name") or "").strip() == name:
            return {"name": name, "exists": True, "id": g.get("id") or ""}
    return {"name": name, "exists": False, "id": ""}


def ensure_group(
    token: str,
    name: str,
    peer_ids: list[str] | None = None,
    install_dir: str | None = None,
) -> dict:
    """Return {id, name, created: bool}."""
    existing = _group_status(token, name, install_dir)
    if existing["exists"]:
        gid = existing["id"]
        if peer_ids:
            _update_group_peers(token, gid, peer_ids, install_dir)
        return {"id": gid, "name": name, "created": False}
    payload: dict[str, Any] = {"name": name}
    if peer_ids:
        payload["peers"] = peer_ids
    resp, _base = api_request(
        "POST", "/api/groups", token, json_body=payload, install_dir=install_dir
    )
    if resp is None or resp.status_code not in (200, 201):
        detail = resp.text[:200] if resp is not None else "no response"
        code = resp.status_code if resp is not None else 0
        raise RuntimeError(f"Create group {name} failed {code}: {detail}")
    data = resp.json()
    return {"id": data.get("id") or "", "name": name, "created": True}


def _update_group_peers(
    token: str, group_id: str, peer_ids: list[str], install_dir: str | None = None
) -> None:
    resp, _ = api_request(
        "GET", f"/api/groups/{group_id}", token, install_dir=install_dir
    )
    if resp is None or resp.status_code != 200:
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
    api_request(
        "PUT",
        f"/api/groups/{group_id}",
        token,
        json_body={"name": g.get("name") or "", "peers": merged},
        install_dir=install_dir,
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


def _list_policies(token: str, install_dir: str | None = None) -> list[dict]:
    resp, _ = api_request("GET", "/api/policies", token, install_dir=install_dir)
    if resp is None:
        raise RuntimeError("NetBird API unreachable")
    if resp.status_code != 200:
        raise RuntimeError(f"Policies API {resp.status_code}: {resp.text[:200]}")
    return parse_json_list(resp.json(), "policies", "items")


def _policy_status(token: str, name: str, install_dir: str | None = None) -> dict:
    for p in _list_policies(token, install_dir):
        if (p.get("name") or "").strip() == name:
            return {"name": name, "exists": True, "id": p.get("id") or ""}
    return {"name": name, "exists": False, "id": ""}


def ensure_klv_policy(
    token: str,
    source_group_id: str,
    dest_group_id: str,
    port: int = 9000,
    install_dir: str | None = None,
) -> dict:
    existing = _policy_status(token, KLV_POLICY_NAME, install_dir)
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
    resp, _ = api_request(
        "POST", "/api/policies", token, json_body=payload, install_dir=install_dir
    )
    if resp is None or resp.status_code not in (200, 201):
        detail = resp.text[:200] if resp is not None else "no response"
        code = resp.status_code if resp is not None else 0
        raise RuntimeError(f"Create policy failed {code}: {detail}")
    data = resp.json()
    return {"id": data.get("id") or "", "name": KLV_POLICY_NAME, "created": True}


def ensure_mediamtx_video_group(
    *,
    install_dir: str,
    persist_env_fn: Callable[[str, str], None] | None = None,
    api_token: str | None = None,
) -> tuple[bool, dict | str]:
    """Create mediamtx_video if needed and persist NETBIRD_VIDEO_SOURCES_GROUP_ID."""
    token = (api_token or "").strip() or _api_token_from_env(install_dir)
    if not token:
        return False, "NETBIRD_API_TOKEN is required"
    try:
        mtx = ensure_group(token, MEDIAMTX_VIDEO_GROUP_NAME, install_dir=install_dir)
        if persist_env_fn and mtx.get("id"):
            persist_env_fn("NETBIRD_VIDEO_SOURCES_GROUP_ID", mtx["id"])
        return True, {
            "success": True,
            "group_id": mtx["id"],
            "group_name": MEDIAMTX_VIDEO_GROUP_NAME,
            "created": mtx["created"],
        }
    except Exception as e:
        return False, str(e)


def get_klv_config_status(*, install_dir: str) -> dict[str, Any]:
    """Status for VPN tab: .env keys, NetBird group, enrollment, overlay IP."""
    file_env = read_env_file_values(install_dir)
    runtime_token = _api_token_from_env(install_dir)
    group_id_env = (
        os.environ.get("NETBIRD_VIDEO_SOURCES_GROUP_ID")
        or file_env.get("NETBIRD_VIDEO_SOURCES_GROUP_ID")
        or ""
    ).strip()

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
        if key == "NETBIRD_API_TOKEN":
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
    api_base_used = default_api_base()

    if runtime_token:
        try:
            group_info = _group_status(
                runtime_token, MEDIAMTX_VIDEO_GROUP_NAME, install_dir
            )
            aggregator_group = _group_status(
                runtime_token, AGGREGATOR_GROUP_NAME, install_dir
            )
            policy_info = _policy_status(runtime_token, KLV_POLICY_NAME, install_dir)
            if group_info["exists"] and not group_id_env:
                group_id_env = group_info["id"]
        except Exception as e:
            api_error = str(e)
            if "401" in api_error:
                api_error += (
                    " — verify NETBIRD_API_TOKEN is a valid Management API token "
                    "(NetBird dashboard → Settings → API tokens) with permission to manage groups."
                )

    nb_ip = get_netbird_self_ip() or ""

    secure_enabled = (
        (file_env.get("NETBIRD_ENABLED", "").lower() == "true" or os.environ.get("NETBIRD_ENABLED", "").lower() == "true")
        and bool(group_id_env or group_info.get("id"))
    )

    return {
        "env": env_rows,
        "mediamtx_video_group": group_info,
        "mediamtx_video_group_id": group_id_env or group_info.get("id") or "",
        "aggregator_group": aggregator_group,
        "klv_policy": policy_info,
        "netbird_overlay_ip": nb_ip,
        "klv_port": int(os.environ.get("KLV_INGEST_PORT") or file_env.get("KLV_INGEST_PORT") or "9000"),
        "management_url": file_env.get("NETBIRD_MANAGEMENT_URL") or NETBIRD_MANAGEMENT_URL,
        "api_url": normalize_api_base(file_env.get("NETBIRD_API_URL") or default_api_base()),
        "secure_klv_enabled": secure_enabled,
        "api_reachable": bool(runtime_token) and not api_error,
        "api_error": api_error,
        "architecture_note": (
            "One aggregator listener (TCP 9000). Each MediaMTX server enrolls via its own setup key "
            "on the matching COTProxy aircraft row (hex + ingest token). Routing is per-token, not per-port."
        ),
    }


def enable_secure_klv(
    *,
    install_dir: str,
    persist_env_fn,
    api_token: str | None = None,
) -> tuple[bool, dict | str]:
    """Create mediamtx_video group (+ policy when possible), write server-wide .env keys."""
    file_env = read_env_file_values(install_dir)
    token = (api_token or "").strip() or _api_token_from_env(install_dir)
    if not token:
        return False, "NETBIRD_API_TOKEN is required. Enter it below and try again."

    steps: list[str] = []
    warnings: list[str] = []

    try:
        mtx_group = ensure_group(token, MEDIAMTX_VIDEO_GROUP_NAME, install_dir=install_dir)
        if mtx_group["created"]:
            steps.append(f"Created NetBird group '{MEDIAMTX_VIDEO_GROUP_NAME}'")
        else:
            steps.append(f"NetBird group '{MEDIAMTX_VIDEO_GROUP_NAME}' already exists")

        peer_id = get_aggregator_peer_id()
        agg_group_id = ""
        if peer_id:
            agg = ensure_group(
                token, AGGREGATOR_GROUP_NAME, peer_ids=[peer_id], install_dir=install_dir
            )
            agg_group_id = agg["id"]
            if agg["created"]:
                steps.append(f"Created group '{AGGREGATOR_GROUP_NAME}' with this server peer")
            else:
                steps.append(f"Group '{AGGREGATOR_GROUP_NAME}' ready")
            try:
                pol = ensure_klv_policy(
                    token, mtx_group["id"], agg_group_id, install_dir=install_dir
                )
                if pol["created"]:
                    steps.append("Created access policy for TCP 9000")
                else:
                    steps.append("Access policy already exists")
            except Exception as e:
                warnings.append(f"Access policy not created: {e}")
        else:
            warnings.append(
                "Aggregator not enrolled in NetBird yet — use Server Enrollment above, then re-run Enable secure KLV for the access policy."
            )

        api_url = normalize_api_base(file_env.get("NETBIRD_API_URL") or default_api_base())
        mgmt_url = file_env.get("NETBIRD_MANAGEMENT_URL") or NETBIRD_MANAGEMENT_URL
        nb_ip = get_netbird_self_ip()

        persist_env_fn("NETBIRD_ENABLED", "true")
        persist_env_fn("NETBIRD_API_URL", api_url)
        persist_env_fn("NETBIRD_API_TOKEN", token)
        persist_env_fn("NETBIRD_MANAGEMENT_URL", mgmt_url)
        persist_env_fn("NETBIRD_VIDEO_SOURCES_GROUP_ID", mtx_group["id"])
        persist_env_fn("KLV_INGEST_PORT", "9000")
        if not file_env.get("NETBIRD_CIDR"):
            persist_env_fn("NETBIRD_CIDR", "100.64.0.0/10")

        steps.append("Updated /opt/taknet-aggregator/.env (server-wide keys only)")

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
