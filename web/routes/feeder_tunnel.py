"""Feeder tunnel reverse proxy: /feeder/<feeder_id>/... proxies HTTP to feeder over its WebSocket tunnel.

Path rewriting ensures /api/... and /static/... in HTML/JS become /feeder/<id>/api/... and /feeder/<id>/static/...
so the browser sends requests through the proxy. Forward path to feeder is path minus the /feeder/<id> prefix.
See docs/FEEDER_WEB_API_REFERENCE.md for the full feeder route list and proxy requirements.
"""

import base64
import gzip
import os
import re
import zlib

import requests
from flask import Blueprint, Response, request

from routes.auth_utils import login_required_any

bp = Blueprint("feeder_tunnel", __name__, url_prefix="/feeder")

# Path prefix for this feeder so browser requests stay on the proxy (e.g. /feeder/92882-test_test_test)
def _feeder_prefix(feeder_id: str) -> str:
    return f"/feeder/{feeder_id}"

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


def _decompress_body(body: bytes, content_encoding: str) -> bytes:
    """Return decompressed body when Content-Encoding is gzip, deflate, or br; else return body unchanged."""
    if not body or not content_encoding:
        return body
    enc = content_encoding.lower().strip()
    try:
        if "gzip" in enc:
            return gzip.decompress(body)
        if "deflate" in enc:
            try:
                return zlib.decompress(body, 15)  # zlib format
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate
        if "br" in enc:
            try:
                import brotli
                return brotli.decompress(body)
            except Exception:
                return body
    except Exception:
        return body
    return body


def _rewrite_location_header(value: str, feeder_id: str) -> str:
    """Rewrite Location/Content-Location so they stay under /feeder/<feeder_id>/."""
    if not value or not value.strip():
        return value
    value = value.strip()
    prefix = _feeder_prefix(feeder_id)
    # Relative path: /foo or foo -> /feeder/<id>/foo
    if value.startswith("/") and not value.startswith(prefix):
        return prefix + value
    if not value.startswith("/") and not value.startswith("http"):
        return f"{prefix}/{value}"
    return value


def _rewrite_html_body(body: bytes, feeder_id: str, base_url: str) -> bytes:
    """Inject <base> and rewrite absolute paths in HTML so assets and API calls hit the proxy."""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return body
    prefix = _feeder_prefix(feeder_id)
    # Avoid double-rewriting
    if prefix + "/" in text and text.count(prefix) > 2:
        return body
    # Absolute path references that must go through the proxy
    text = text.replace('href="/', f'href="{prefix}/')
    text = text.replace("href='/", f"href='{prefix}/")
    text = text.replace('src="/', f'src="{prefix}/')
    text = text.replace("src='/", f"src='{prefix}/")
    text = text.replace('url("/', f'url("{prefix}/')
    text = text.replace("url('/", f"url('{prefix}/")
    # Inject <base> so relative URLs (e.g. style.css, api/...) resolve under the feeder path
    base_tag = f'<base href="{base_url}">'
    if "<head>" in text:
        text = text.replace("<head>", "<head>" + base_tag, 1)
    elif "<HEAD>" in text:
        text = text.replace("<HEAD>", "<HEAD>" + base_tag, 1)
    else:
        text = base_tag + text
    # Rewrite inline <script>...</script> so fetch("/api/...") etc. go through the proxy
    def _rewrite_inline_script(match):
        open_tag, content, close = match.group(1), match.group(2), match.group(3)
        if "src=" in open_tag.lower():
            return match.group(0)
        return open_tag + _rewrite_js_text(content, feeder_id) + close
    text = re.sub(
        r'(<script(?:\s[^>]*)?>)([\s\S]*?)(</script>)',
        _rewrite_inline_script,
        text,
        flags=re.IGNORECASE,
    )
    return text.encode("utf-8", errors="replace")


def _rewrite_js_text(text: str, feeder_id: str) -> str:
    """Rewrite path strings in JS (in quoted strings only) so API/static calls hit the proxy.
    We only replace after \" or ' or ` to avoid corrupting regex literals (e.g. /api/version/g).
    See docs/FEEDER_WEB_API_REFERENCE.md.
    """
    prefix = _feeder_prefix(feeder_id)
    if prefix in text:
        return text
    text = text.replace('"/', '"' + prefix + "/")
    text = text.replace("'/", "'" + prefix + "/")
    text = text.replace('`/', '`' + prefix + "/")
    # Catch concatenation like base + "/api/..." or url + '/static/...' (quote after +)
    text = text.replace('+ "/', '+ "' + prefix + "/")
    text = text.replace("+ '/", "+ '" + prefix + "/")
    text = text.replace(prefix + prefix, prefix)
    return text


def _rewrite_js_body(body: bytes, feeder_id: str) -> bytes:
    """Rewrite absolute path strings in JS (fetch, etc.) so API calls hit the proxy."""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return body
    return _rewrite_js_text(text, feeder_id).encode("utf-8", errors="replace")


def _rewrite_css_body(body: bytes, feeder_id: str) -> bytes:
    """Rewrite url() in CSS so assets load from the proxy."""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return body
    prefix = _feeder_prefix(feeder_id)
    if prefix in text:
        return body
    text = re.sub(r'url\s*\(\s*["\']/', rf'url("{prefix}/', text)
    text = re.sub(r"url\s*\(\s*'/", rf"url('{prefix}/", text)
    return text.encode("utf-8", errors="replace")


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

    # Collect content-type and content-encoding; rewrite Location; build headers (we'll drop encoding/length when we rewrite)
    headers_out = []
    content_type = ""
    content_encoding = ""
    for name, value in resp_headers.items():
        if name.lower() == "content-type":
            content_type = (value or "").lower()
        if name.lower() == "content-encoding":
            content_encoding = (value or "").strip()
        if name.lower() in ("location", "content-location") and value:
            value = _rewrite_location_header(value, feeder_id)
        headers_out.append((name, value))

    # Rewrite response body so assets and API calls go through the proxy (fix 404s and broken page).
    # For HTML/JS/CSS: if feeder sent gzip etc., decompress first so we have plain text; then rewrite; then drop Content-Encoding.
    we_rewrote = False
    if body_bytes and content_type:
        if "text/html" in content_type:
            if content_encoding:
                body_bytes = _decompress_body(body_bytes, content_encoding)
            base_url = request.url_root.rstrip("/") + _feeder_prefix(feeder_id) + "/"
            body_bytes = _rewrite_html_body(body_bytes, feeder_id, base_url)
            we_rewrote = True
        elif "javascript" in content_type:
            if content_encoding:
                body_bytes = _decompress_body(body_bytes, content_encoding)
            body_bytes = _rewrite_js_body(body_bytes, feeder_id)
            we_rewrote = True
        elif "text/css" in content_type:
            if content_encoding:
                body_bytes = _decompress_body(body_bytes, content_encoding)
            body_bytes = _rewrite_css_body(body_bytes, feeder_id)
            we_rewrote = True

    # When we rewrote, body is uncompressed — drop content-encoding and content-length so browser decodes correctly
    resp = Response(body_bytes, status=status)
    for name, value in headers_out:
        if we_rewrote and name.lower() in ("content-length", "content-encoding"):
            continue
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
