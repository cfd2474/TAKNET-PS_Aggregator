"""Feeder tunnel reverse proxy: /feeder/<feeder_id>/... proxies HTTP to feeder over its WebSocket tunnel."""

import base64
import os

import requests
from flask import Blueprint, Response, request

from routes.auth_utils import login_required_any

bp = Blueprint("feeder_tunnel", __name__, url_prefix="/feeder")

# Hop-by-hop and other headers not to forward to the feeder
SKIP_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
    "host",
}

TUNNEL_SERVICE_URL = os.environ.get("TUNNEL_SERVICE_URL", "http://tunnel:5001")
PROXY_TIMEOUT = 30


def _request_headers_for_proxy():
    """Build dict of request headers to send to tunnel (and thus to feeder)."""
    out = {}
    for key, value in request.headers:
        if key.lower() in SKIP_HEADERS or value is None:
            continue
        out[key] = value
    return out


def _rewrite_location_header(value: str, feeder_id: str) -> str:
    """Rewrite Location/Content-Location so they stay under /feeder/<feeder_id>/."""
    if not value or not value.strip():
        return value
    value = value.strip()
    prefix = f"/feeder/{feeder_id}"
    # Relative path: /foo or foo -> /feeder/<id>/foo
    if value.startswith("/") and not value.startswith(prefix):
        return prefix + value
    if not value.startswith("/") and not value.startswith("http"):
        return f"{prefix}/{value}"
    # Absolute URL with same path style: leave path as-is or rewrite path segment (optional)
    return value


def _proxy_to_feeder(feeder_id: str, path: str, method: str, headers: dict, body_b64: str):
    """POST to tunnel service /proxy; returns (status_code, response_headers_dict, body_bytes) or (503, {}, None)."""
    url = f"{TUNNEL_SERVICE_URL.rstrip('/')}/proxy"
    payload = {
        "feeder_id": feeder_id,
        "method": method,
        "path": path,
        "headers": headers,
        "body": body_b64,
    }
    try:
        r = requests.post(url, json=payload, timeout=PROXY_TIMEOUT)
    except requests.RequestException:
        return 503, {}, None
    if r.status_code == 503:
        return 503, {}, None
    if r.status_code == 504:
        return 504, {}, None
    if r.status_code != 200:
        return 502, {}, None
    try:
        data = r.json()
    except Exception:
        return 502, {}, None
    status = data.get("status", 502)
    resp_headers = data.get("headers") or {}
    body_b64_resp = data.get("body") or ""
    try:
        body_bytes = base64.b64decode(body_b64_resp) if body_b64_resp else b""
    except Exception:
        body_bytes = b""
    return status, resp_headers, body_bytes


@bp.route("/<feeder_id>/", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@bp.route("/<feeder_id>/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@login_required_any
def feeder_tunnel_proxy(feeder_id: str, subpath: str = ""):
    """Proxy request to feeder over its tunnel WebSocket; return response with path rewriting."""
    # Build path including query string (per wire protocol: path includes query)
    local_path = "/" + subpath if subpath else "/"
    if request.query_string:
        local_path = f"{local_path}?{request.query_string.decode('utf-8', errors='replace')}"

    # Request body (base64)
    body_bytes = request.get_data()
    body_b64 = base64.b64encode(body_bytes).decode("ascii") if body_bytes else ""

    status, resp_headers, body_bytes = _proxy_to_feeder(
        feeder_id,
        local_path,
        request.method,
        _request_headers_for_proxy(),
        body_b64,
    )

    if status == 503:
        return (
            _feeder_offline_html(feeder_id),
            503,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    if status == 504:
        return "Feeder response timeout", 504

    # Rewrite Location and Content-Location so links stay under /feeder/<feeder_id>/
    headers_out = []
    for name, value in resp_headers.items():
        if name.lower() in ("location", "content-location") and value:
            value = _rewrite_location_header(value, feeder_id)
        headers_out.append((name, value))

    # Build Flask Response
    resp = Response(body_bytes, status=status)
    for name, value in headers_out:
        resp.headers[name] = value
    return resp


def _feeder_offline_html(feeder_id: str) -> str:
    """Simple HTML page when feeder is offline."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Feeder offline</title></head>
<body>
  <h1>Feeder offline</h1>
  <p>Feeder <strong>{_escape_html(feeder_id)}</strong> is not connected via tunnel. Connect the feeder and try again.</p>
</body>
</html>"""


def _escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
