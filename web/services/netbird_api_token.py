"""NetBird Management API personal access token lifecycle."""

from __future__ import annotations

import os
import socket
import time
from datetime import datetime, timezone
from typing import Any, Callable

from services.netbird_api import api_request
from services.netbird_klv_config import read_env_file_values

TOKEN_NAME_PREFIX = "taknet-aggregator-api"
ENV_TOKEN_ID = "NETBIRD_API_TOKEN_ID"
ENV_TOKEN_EXPIRES = "NETBIRD_API_TOKEN_EXPIRES"
DEFAULT_EXPIRES_DAYS = 90


def _api_token_from_env(install_dir: str) -> str:
    file_env = read_env_file_values(install_dir)
    return (os.environ.get("NETBIRD_API_TOKEN") or file_env.get("NETBIRD_API_TOKEN") or "").strip()


def _token_meta_from_env(install_dir: str) -> dict[str, str]:
    file_env = read_env_file_values(install_dir)
    return {
        "token_id": (
            os.environ.get(ENV_TOKEN_ID) or file_env.get(ENV_TOKEN_ID) or ""
        ).strip(),
        "expiration": (
            os.environ.get(ENV_TOKEN_EXPIRES) or file_env.get(ENV_TOKEN_EXPIRES) or ""
        ).strip(),
    }


def _expiration_info(expiration_date: str | None) -> dict[str, Any]:
    if not expiration_date:
        return {
            "expiration_date": "",
            "expired": False,
            "seconds_remaining": None,
            "days_remaining": None,
            "hours_remaining": None,
            "expiration_unknown": True,
        }
    try:
        exp = datetime.fromisoformat(expiration_date.replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = exp - now
        seconds = int(delta.total_seconds())
        expired = seconds <= 0
        remaining = max(0, seconds)
        return {
            "expiration_date": expiration_date,
            "expired": expired,
            "seconds_remaining": remaining if not expired else 0,
            "days_remaining": remaining // 86400 if not expired else 0,
            "hours_remaining": (remaining % 86400) // 3600 if not expired else 0,
            "expiration_unknown": False,
        }
    except Exception:
        return {
            "expiration_date": expiration_date,
            "expired": False,
            "seconds_remaining": None,
            "days_remaining": None,
            "hours_remaining": None,
            "expiration_unknown": True,
        }


def _get_current_user(token: str) -> dict[str, Any]:
    resp, _ = api_request("GET", "/api/users/current", token)
    if resp is None:
        raise RuntimeError("NetBird API unreachable")
    if resp.status_code == 401:
        raise RuntimeError("Current API token is invalid or expired")
    if resp.status_code != 200:
        raise RuntimeError(f"Users API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("id"):
        raise RuntimeError("NetBird API did not return current user id")
    return data


def _list_user_tokens(token: str, user_id: str) -> list[dict]:
    resp, _ = api_request("GET", f"/api/users/{user_id}/tokens", token)
    if resp is None:
        raise RuntimeError("NetBird API unreachable")
    if resp.status_code != 200:
        raise RuntimeError(f"Tokens API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    if isinstance(data, list):
        return [t for t in data if isinstance(t, dict)]
    return []


def _get_token_record(token: str, user_id: str, token_id: str) -> dict | None:
    resp, _ = api_request("GET", f"/api/users/{user_id}/tokens/{token_id}", token)
    if resp is None or resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise RuntimeError(f"Token API {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    return data if isinstance(data, dict) else None


def _guess_managed_token(tokens: list[dict]) -> dict | None:
    managed = [
        t for t in tokens
        if str(t.get("name") or "").startswith(TOKEN_NAME_PREFIX)
    ]
    if not managed:
        return None
    managed.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return managed[0]


def get_api_token_status(*, install_dir: str) -> dict[str, Any]:
    """Status for VPN tab: validity, expiration, remaining lifetime."""
    configured = bool(_api_token_from_env(install_dir))
    meta = _token_meta_from_env(install_dir)
    out: dict[str, Any] = {
        "configured": configured,
        "valid": False,
        "token_id": meta["token_id"],
        "token_name": "",
        "created_at": "",
        "last_used": "",
        "user_email": "",
        "user_name": "",
        "revoke_previous_available": bool(meta["token_id"]),
        "expiration_unknown": True,
        "expired": False,
        "expiration_date": meta["expiration"] or "",
        "seconds_remaining": None,
        "days_remaining": None,
        "hours_remaining": None,
        "error": "",
    }

    if not configured:
        out["error"] = "NETBIRD_API_TOKEN is not set"
        return out

    token = _api_token_from_env(install_dir)
    try:
        user = _get_current_user(token)
        out["valid"] = True
        out["user_email"] = user.get("email") or ""
        out["user_name"] = user.get("name") or ""
        user_id = str(user["id"])

        record: dict | None = None
        if meta["token_id"]:
            record = _get_token_record(token, user_id, meta["token_id"])
        if record is None:
            record = _guess_managed_token(_list_user_tokens(token, user_id))

        if record:
            out["token_id"] = record.get("id") or meta["token_id"]
            out["token_name"] = record.get("name") or ""
            out["created_at"] = record.get("created_at") or ""
            out["last_used"] = record.get("last_used") or ""
            exp = record.get("expiration_date") or meta["expiration"]
            out.update(_expiration_info(exp))
            out["expiration_date"] = out.get("expiration_date") or exp or ""
        elif meta["expiration"]:
            out.update(_expiration_info(meta["expiration"]))
        else:
            out["expiration_unknown"] = True
    except Exception as e:
        out["error"] = str(e)
        if meta["expiration"]:
            exp_info = _expiration_info(meta["expiration"])
            out.update(exp_info)
            if exp_info.get("expired"):
                out["valid"] = False

    return out


def _token_name() -> str:
    host = socket.gethostname().split(".")[0][:24] or "aggregator"
    return f"{TOKEN_NAME_PREFIX}-{host}-{int(time.time())}"[:64]


def _delete_token(token: str, user_id: str, token_id: str) -> None:
    api_request("DELETE", f"/api/users/{user_id}/tokens/{token_id}", token)


def regenerate_api_token(
    *,
    install_dir: str,
    persist_env_fn: Callable[[str, str], None],
    expires_in_days: int = DEFAULT_EXPIRES_DAYS,
    bootstrap_token: str | None = None,
    revoke_previous: bool = False,
) -> tuple[bool, dict | str]:
    """
    Create a new NetBird PAT and persist NETBIRD_API_TOKEN (+ metadata) to .env.
    Uses bootstrap_token or the configured token to authenticate creation.
    """
    auth = (bootstrap_token or "").strip() or _api_token_from_env(install_dir)
    if not auth:
        return False, "No API token available. Paste a valid token first."

    expires_in_days = max(1, min(365, int(expires_in_days)))

    old_meta = _token_meta_from_env(install_dir)
    try:
        user = _get_current_user(auth)
        user_id = str(user["id"])
        payload = {"name": _token_name(), "expires_in": expires_in_days}
        resp, _ = api_request(
            "POST",
            f"/api/users/{user_id}/tokens",
            auth,
            json_body=payload,
        )
        if resp is None:
            return False, "NetBird API unreachable"
        if resp.status_code == 401:
            return False, (
                "Authentication failed — paste a valid Management API token "
                "with permission to create tokens for this user."
            )
        if resp.status_code not in (200, 201):
            return False, f"Create token failed {resp.status_code}: {resp.text[:200]}"

        data = resp.json()
        plain = data.get("plain_token")
        if isinstance(plain, dict):
            plain = ""
        plain = str(plain or "").strip()
        pat = data.get("personal_access_token") or {}
        if not plain:
            return False, "NetBird API did not return a plain text token"

        token_id = str(pat.get("id") or "").strip()
        expiration = str(pat.get("expiration_date") or "").strip()
        token_name = str(pat.get("name") or payload["name"]).strip()

        persist_env_fn("NETBIRD_API_TOKEN", plain)
        if token_id:
            persist_env_fn(ENV_TOKEN_ID, token_id)
        if expiration:
            persist_env_fn(ENV_TOKEN_EXPIRES, expiration)

        revoked = False
        if revoke_previous and old_meta["token_id"] and old_meta["token_id"] != token_id:
            try:
                _delete_token(auth, user_id, old_meta["token_id"])
                revoked = True
            except Exception:
                pass

        exp_info = _expiration_info(expiration)
        return True, {
            "success": True,
            "token_name": token_name,
            "token_id": token_id,
            "expiration_date": expiration,
            "expires_in_days": expires_in_days,
            "revoked_previous": revoked,
            "needs_dashboard_restart": True,
            "restart_hint": "docker compose up -d dashboard",
            **{k: exp_info[k] for k in (
                "days_remaining", "hours_remaining", "seconds_remaining", "expired"
            )},
        }
    except Exception as e:
        return False, str(e)
