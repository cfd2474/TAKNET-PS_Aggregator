"""
Mail client abstractions for sending transactional emails.

Currently implements a minimal Resend client, using settings stored in the
mounted host .env file (written by the dashboard UI).
"""

from __future__ import annotations

import os
from typing import Any

import requests


INSTALL_DIR = os.environ.get("INSTALL_DIR", "/opt/taknet-aggregator")
ENV_PATH = os.path.join(INSTALL_DIR, ".env")

RESEND_API_URL = "https://api.resend.com/emails"
RESEND_API_KEY_ENV = "RESEND_API_KEY"
RESEND_ENABLED_ENV = "RESEND_ENABLED"
RESEND_FROM_EMAIL_ENV = "RESEND_FROM_EMAIL"
RESEND_ADMIN_EMAILS_ENV = "RESEND_ADMIN_EMAILS"
DEFAULT_FROM_EMAIL = "noreply@notify.tak-solutions.com"


def _read_env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean-ish key from the host .env file."""
    try:
        if not os.path.exists(ENV_PATH):
            return default
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                    return val == "true"
    except Exception:
        pass
    return default


def _read_env_value(key: str, default: str = "") -> str:
    """Read a string key from the host .env file."""
    try:
        if not os.path.exists(ENV_PATH):
            return default
        with open(ENV_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                    val = line.split("=", 1)[1].strip()
                    # Handle single-quoted and double-quoted values.
                    if len(val) >= 2 and val.startswith("'") and val.endswith("'"):
                        return val[1:-1].replace("'\"'\"'", "'")
                    if len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                        return val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                    return val
    except Exception:
        pass
    return default


def get_resend_settings() -> dict[str, Any]:
    """Return current Resend settings from the mounted .env file."""
    enabled = _read_env_bool(RESEND_ENABLED_ENV, False)
    api_key = _read_env_value(RESEND_API_KEY_ENV, "").strip()
    return {
        "enabled": enabled,
        # Do not return the raw key in case callers log/serialize it.
        "api_key_present": bool(api_key),
    }


def get_resend_from_email() -> str:
    """Sender email address used for transactional emails."""
    # If RESEND_FROM_EMAIL is unset, fall back to the project's default.
    return (_read_env_value(RESEND_FROM_EMAIL_ENV, DEFAULT_FROM_EMAIL) or DEFAULT_FROM_EMAIL).strip()


def get_resend_admin_emails() -> list[str]:
    """Return admin recipient email list from env.

    Stored as comma-separated addresses in `RESEND_ADMIN_EMAILS`.
    """
    raw = _read_env_value(RESEND_ADMIN_EMAILS_ENV, "")
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


class ResendMailClient:
    """Tiny wrapper around Resend's transactional email endpoint."""

    def __init__(self, *, enabled: bool, api_key: str):
        self.enabled = bool(enabled)
        self.api_key = (api_key or "").strip()

    @classmethod
    def from_env(cls) -> "ResendMailClient":
        enabled = _read_env_bool(RESEND_ENABLED_ENV, False)
        api_key = _read_env_value(RESEND_API_KEY_ENV, "").strip()
        return cls(enabled=enabled, api_key=api_key)

    def send_email(
        self,
        *,
        from_email: str,
        to: str | list[str],
        subject: str,
        html: str,
        text: str | None = None,
        cc: str | list[str] | None = None,
        bcc: str | list[str] | None = None,
        reply_to: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send one transactional email via Resend.

        Returns a dict that caller can inspect for success/errors.
        """
        if not self.enabled:
            return {"success": False, "error": "mail_disabled"}
        if not self.api_key:
            return {"success": False, "error": "missing_api_key"}

        payload: dict[str, Any] = {
            "from": from_email,
            "to": to,
            "subject": subject,
            "html": html,
        }
        if text:
            payload["text"] = text
        if cc:
            payload["cc"] = cc
        if bcc:
            payload["bcc"] = bcc
        if reply_to:
            payload["reply_to"] = reply_to
        if headers:
            payload["headers"] = headers

        # Resend expects Authorization: Bearer <api_key>
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "TAKNET-PS-Aggregator",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )

        if 200 <= resp.status_code < 300:
            try:
                body = resp.json() or {}
            except Exception:
                body = {}
            return {"success": True, "id": body.get("id")}

        # Don't leak secrets; only include HTTP status and response snippet.
        snippet = (resp.text or "")[:500]
        return {
            "success": False,
            "error": "resend_http_error",
            "status_code": resp.status_code,
            "details": snippet,
        }

