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


def _file_api_urls(install_dir: str | None) -> list[str]:
    if not install_dir:
        return []
    try:
        from services.netbird_klv_config import read_env_file_values

        file_env = read_env_file_values(install_dir)
        return [
            file_env.get("NETBIRD_API_URL") or "",
            file_env.get("NETBIRD_MANAGEMENT_URL") or "",
        ]
    except Exception:
        return []


def default_api_base(install_dir: str | None = None) -> str:
    for raw in (
        *_file_api_urls(install_dir),
        os.environ.get("NETBIRD_API_URL"),
        os.environ.get("NETBIRD_MANAGEMENT_URL"),
        DEFAULT_NETBIRD_API_URL,
    ):
        base = normalize_api_base(raw or "")
        if base:
            return base
    return DEFAULT_NETBIRD_API_URL


def api_candidates(install_dir: str | None = None) -> list[str]:
    """URLs to try for Management API (deduped). Prefers host .env over stale runtime env."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (
        *_file_api_urls(install_dir),
        os.environ.get("NETBIRD_API_URL"),
        os.environ.get("NETBIRD_MANAGEMENT_URL"),
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
    install_dir: str | None = None,
) -> tuple[requests.Response | None, str | None]:
    """
    Call NetBird API; try each configured base URL.
    Returns (response, base_used) or (None, None) if all fail.
    """
    token = (token or "").strip()
    if not path.startswith("/"):
        path = "/" + path
    last_resp: requests.Response | None = None
    last_base: str | None = None
    for base in api_candidates(install_dir):
        url = f"{base}{path}"
        for scheme in ("Token", "Bearer"):
            headers = {
                "Authorization": f"{scheme} {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            try:
                resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
            except requests.RequestException:
                resp = None
                break
            last_resp = resp
            last_base = base
            if resp.status_code == 404:
                break
            if resp.status_code == 401:
                continue
            return resp, base
    return last_resp, last_base


def parse_json_list(data: Any, *keys: str) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            if isinstance(data.get(k), list):
                return data[k]
    return []
