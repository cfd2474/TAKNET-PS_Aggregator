"""Shared NetBird Management API helpers."""

from __future__ import annotations

import os
from typing import Any

import requests

DEFAULT_NETBIRD_API_URL = "https://netbird.tak-solutions.com"


def normalize_api_base(url: str) -> str:
    """NetBird API base without trailing /api (paths add /api/...)."""
    base = (url or "").strip().rstrip("/")
    if base.endswith("/api"):
        base = base[:-4]
    return base


def default_api_base() -> str:
    raw = (os.environ.get("NETBIRD_API_URL") or DEFAULT_NETBIRD_API_URL).strip()
    return normalize_api_base(raw) or DEFAULT_NETBIRD_API_URL


def api_candidates() -> list[str]:
    """URLs to try for Management API (deduped)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (
        os.environ.get("NETBIRD_API_URL"),
        DEFAULT_NETBIRD_API_URL,
        "http://localhost:33073",
    ):
        if not raw:
            continue
        base = normalize_api_base(raw)
        if base and base not in seen:
            seen.add(base)
            out.append(base)
    return out or [DEFAULT_NETBIRD_API_URL]


def api_request(
    method: str,
    path: str,
    token: str,
    *,
    json_body: dict | None = None,
    timeout: int = 20,
) -> tuple[requests.Response | None, str | None]:
    """
    Call NetBird API; try each configured base URL.
    Returns (response, base_used) or (None, None) if all fail.
    """
    token = (token or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    last_resp: requests.Response | None = None
    for base in api_candidates():
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{base}{path}"
        try:
            resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException:
            continue
        if resp.status_code == 401:
            headers["Authorization"] = f"Token {token}"
            try:
                resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
            except requests.RequestException:
                continue
        last_resp = resp
        if resp.status_code != 404:
            return resp, base
    return last_resp, api_candidates()[0] if api_candidates() else None


def parse_json_list(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []
