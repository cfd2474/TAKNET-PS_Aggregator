"""Feeder tunnel reverse proxy: /feeder/<feeder_id>/... proxies HTTP to feeder over its WebSocket tunnel.

Path rewriting ensures /api/... and /static/... in HTML/JS become /feeder/<id>/api/... and /feeder/<id>/static/...
so the browser sends requests through the proxy. Forward path to feeder is path minus the /feeder/<id> prefix.
See docs/FEEDER_WEB_API_REFERENCE.md for the full feeder route list and proxy requirements.
"""

import base64
import gzip
import json
import os
import re
import threading
import time
import zlib

import requests
from requests.adapters import HTTPAdapter
from flask import Blueprint, Response, request
from flask_login import current_user

from models import FeederModel, user_can_access_feeder
from routes.auth_utils import login_required_any

# Bare HTML pages at tunnel root can come from FR24/PiAware root-absolute links.
_RE_BARE_HTML_PATH = re.compile(r"^/[^/]+\.html$", re.IGNORECASE)
# Root-level CSS/JS (e.g. /fr24.css, /jquery.js) may 404 until retried under /fr24/ or /piaware/.
_RE_BARE_ROOT_ASSET = re.compile(r"^/[^/]+\.(css|js)$", re.IGNORECASE)


def _is_fr24_service_path(p: str) -> bool:
    """True for /fr24/... or exactly /fr24 — not /fr24.css (filename collision)."""
    return p.startswith("/fr24/") or p == "/fr24"


def _is_piaware_service_path(p: str) -> bool:
    """True for /piaware/... or exactly /piaware — not filenames starting with piaware."""
    return p.startswith("/piaware/") or p == "/piaware"


# Last URL path segment looks like a file (not a directory); strip for <base href> directory.
_RE_BASE_LAST_SEGMENT_FILE = re.compile(
    r"\.(html|htm|xhtml|css|js|mjs|json|map|xml|txt|svg|ico|png|jpg|jpeg|gif|webp|woff2?|ttf|eot)$",
    re.IGNORECASE,
)


def _base_href_path_suffix(path_only: str) -> str:
    """Directory part of path for <base href> — strip filename so relative assets resolve correctly.

    If we set base to .../settings.html/, relative fr24.css resolves to .../settings.html/fr24.css (404).
    Correct base is .../fr24/ or .../ when the document is settings.html.
    """
    p = (path_only or "/").split("?", 1)[0].rstrip("/")
    if not p or p == "/":
        return "/"
    parts = [x for x in p.split("/") if x]
    if not parts:
        return "/"
    last = parts[-1]
    if _RE_BASE_LAST_SEGMENT_FILE.search(last):
        parts = parts[:-1]
    if not parts:
        return "/"
    return "/" + "/".join(parts) + "/"

# Cache tunnel URL segment -> enriched feeder row (avoids full-table scan every proxied asset)
_tunnel_feeder_row_cache: dict[str, tuple[dict | None, float]] = {}
_TUNNEL_FEEDER_CACHE_TTL = 30.0


def _cached_feeder_for_tunnel(tunnel_id: str) -> dict | None:
    k = (tunnel_id or "").strip().lower()
    now = time.monotonic()
    ent = _tunnel_feeder_row_cache.get(k)
    if ent and now - ent[1] < _TUNNEL_FEEDER_CACHE_TTL:
        return ent[0]
    f = FeederModel.get_by_tunnel_feeder_id(tunnel_id)
    _tunnel_feeder_row_cache[k] = (f, now)
    return f

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

# Reuse HTTP connections to tunnel service (significant win under many asset/API requests).
_TUNNEL_HTTP = requests.Session()
_TUNNEL_HTTP.mount("http://", HTTPAdapter(pool_connections=64, pool_maxsize=128))
_TUNNEL_HTTP.mount("https://", HTTPAdapter(pool_connections=64, pool_maxsize=128))

# Cache feeder host lookups to avoid GET /feeder/<id>/host on every proxied request.
_HOST_CACHE_TTL_SEC = 60.0
_host_cache_lock = threading.Lock()
_host_cache: dict[str, tuple[str, float]] = {}


def _normalize_feeder_host(host: str) -> str:
    """Return host:port suitable for HTTP Host header. Strip scheme; ensure port 8080 if missing."""
    if not host or not isinstance(host, str):
        return ""
    s = host.strip()
    if "://" in s:
        s = s.split("://", 1)[1].split("/")[0]
    if ":" not in s:
        s = f"{s}:8080"
    return s


def _is_static_asset_path(path_only: str) -> bool:
    """Return True when request path is a static asset suitable for passthrough/cache."""
    p = (path_only or "/").split("?", 1)[0]
    if p.startswith(("/libs/", "/images/", "/graphs1090/graphs/", "/data/", "/db2/", "/tracks/", "/tar1090/")):
        return True
    return bool(re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|ico|map|json|woff2?|ttf|eot|wasm)$", p, re.IGNORECASE))


def _cache_control_for_path(path_only: str) -> str:
    """Choose cache-control policy for static assets under tunnel paths."""
    p = (path_only or "/").split("?", 1)[0]
    # hashed tar1090 assets like script_<hash>.js / style_<hash>.css can be cached longer
    if re.search(r"_(?:[a-f0-9]{16,})\.(css|js|png|jpg|jpeg|gif|svg|map)$", p, re.IGNORECASE):
        return "public, max-age=604800, immutable"
    return "public, max-age=86400"


def _tunnel_feeder_ids_to_try(feeder_id: str) -> list[str]:
    """Tunnel WebSocket keys feeder_id exactly; try dash/underscore variants so URL id matches register id."""
    ids = [feeder_id]
    alt = feeder_id.replace("_", "-")
    if alt != feeder_id and alt not in ids:
        ids.append(alt)
    alt2 = feeder_id.replace("-", "_")
    if alt2 != feeder_id and alt2 not in ids:
        ids.append(alt2)
    return ids


def _get_feeder_host_for_proxy(feeder_id: str) -> str:
    """Resolve Host value for proxying to this feeder (host:8080). Prefer host from tunnel register, then DB IP."""
    now = time.monotonic()
    with _host_cache_lock:
        cached = _host_cache.get(feeder_id)
    if cached and cached[1] > now:
        return cached[0]

    base = TUNNEL_SERVICE_URL.rstrip("/")
    for fid in _tunnel_feeder_ids_to_try(feeder_id):
        try:
            r = _TUNNEL_HTTP.get(f"{base}/feeder/{fid}/host", timeout=2)
            if r.status_code == 200:
                data = r.json()
                raw = data.get("host")
                if raw:
                    normalized = _normalize_feeder_host(raw)
                    if normalized:
                        with _host_cache_lock:
                            _host_cache[feeder_id] = (normalized, now + _HOST_CACHE_TTL_SEC)
                        return normalized
        except Exception:
            continue
    feeder = FeederModel.get_by_tunnel_feeder_id(feeder_id)
    if feeder and feeder.get("ip_address"):
        fallback = f"{feeder['ip_address']}:8080"
    else:
        fallback = "localhost:8080"
    with _host_cache_lock:
        _host_cache[feeder_id] = (fallback, now + _HOST_CACHE_TTL_SEC)
    return fallback


def _infer_tunnel_target(path: str) -> str:
    """Infer feeder-side backend target from request path.

    - tar1090/graphs1090 traffic should be handled by feeder's web stack on :8080
    - dashboard/api/static traffic should be handled by feeder app backend
    """
    p = (path or "/").split("?", 1)[0]
    referer = (request.headers.get("Referer") or "").lower()
    tar_prefixes = (
        "/graphs1090/",
        "/data/",
        "/db2/",
        "/tar1090/",
        "/tracks/",
        "/libs/",
        "/images/",
    )
    if _is_fr24_service_path(p) or _is_piaware_service_path(p) or p.startswith("/flightaware/") or p == "/flightaware":
        return "dashboard"
    # FR24 UI can reference origin-root assets outside /fr24.
    if p in ("/logo.png", "/monitor.json"):
        return "dashboard"
    # Third-party FR24/PiAware pages may request root-level assets such as
    # /jquery.min.js, /bootstrap.min.js, etc. Keep those on dashboard side when
    # the request originates from an FR24/PiAware tunnel page.
    if "/fr24/" in referer or "/piaware/" in referer or "/flightaware/" in referer:
        return "dashboard"
    if p == "/" or p.startswith("/graphs1090"):
        return "tar1090"
    if any(p.startswith(pref) for pref in tar_prefixes):
        return "tar1090"
    # Root-level tar1090/graphs assets (hashed css/js, jquery, bootstrap, etc.)
    # commonly appear as /style_xxx.css, /script_xxx.js, /jquery-*.js, /portal.css...
    # Route these to tar1090 unless they clearly belong to feeder dashboard/app paths.
    if re.search(r"\.(css|js|png|jpg|jpeg|gif|svg|ico|map|json|woff2?|ttf|eot)$", p, re.IGNORECASE):
        # /fr24.css and similar are upstream root files, not the /fr24/ service prefix.
        if re.match(r"^/fr24\.[^/]+$", p) or re.match(r"^/piaware\.[^/]+$", p):
            return "dashboard"
        dashboard_roots = (
            "/api/",
            "/static/",
            "/dashboard",
            "/settings",
            "/feeds",
            "/map",
            "/logs",
            "/about",
            "/taknet-ps-status",
            "/setup",
            "/loading",
            "/logo.png",
            "/monitor.json",
        )
        if not any(p.startswith(r) for r in dashboard_roots):
            return "tar1090"
    # If referer is map/stats path, prefer tar1090 for ambiguous relative assets.
    if "/graphs1090/" in referer:
        return "tar1090"
    if re.search(r"/feeder/[^/]+/$", referer):
        return "tar1090"
    return "dashboard"


def _request_headers_for_proxy(feeder_id: str = "", path: str = "/"):
    """Build dict of request headers to send to tunnel (and thus to feeder).
    Set Host to the feeder's host:8080 (from tunnel register, else DB ip, else localhost) so the
    feeder serves tar1090/graphs1090 the same as when browsed directly.
    """
    out = {}
    for key, value in request.headers:
        if key.lower() in SKIP_HEADERS or value is None:
            continue
        out[key] = value
    # Hint feeder tunnel client which local backend should receive this request.
    # Feeder can use this to route map/stats paths to :8080 tar1090 web stack.
    out["X-Tunnel-Target"] = _infer_tunnel_target(path)
    # Avoid compressed upstream payloads so HTML/JS rewriting never receives encoded bytes
    # with missing/mismatched Content-Encoding metadata.
    out["Accept-Encoding"] = "identity"
    if feeder_id:
        out["Host"] = _get_feeder_host_for_proxy(feeder_id)
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


def _decompress_body_best_effort(body: bytes, content_encoding: str) -> bytes:
    """Decompress by declared encoding, then fall back to gzip magic sniffing.

    Some feeder upstreams may return compressed bytes while dropping the
    Content-Encoding header. This keeps the proxy from treating compressed
    payloads as plain text and rendering gibberish.
    """
    out = _decompress_body(body, content_encoding)
    if out is not body:
        return out
    if not body:
        return body
    # gzip magic: 1f 8b
    if body.startswith(b"\x1f\x8b"):
        try:
            return gzip.decompress(body)
        except Exception:
            return body
    return body


# Feeder's local origins (map/stats links); rewrite to proxy path so they open through tunnel
_FEEDER_LOCAL_ORIGINS = (
    "http://127.0.0.1:8080",
    "https://127.0.0.1:8080",
    "http://localhost:8080",
    "https://localhost:8080",
)
# Regex: any http(s)://host:8080 (feeder's tar1090 port — 127.0.0.1, NetBird IP, etc.)
_RE_FEEDER_ORIGIN_8080 = re.compile(r"https?://[^/]+:8080")
# Full absolute URLs to FR24 (:8754) and PiAware/FlightAware (:8082) — rewrite to tunnel /fr24/ and /piaware/ paths.
# The feeder's nginx on port 80 is expected to proxy /fr24/ -> :8754 and /piaware/ -> :8082 internally.
_RE_FEEDER_ORIGIN_FR24 = re.compile(r"https?://[^/]+:8754(/[^\"'`\s<]*)?")
_RE_FEEDER_ORIGIN_FA = re.compile(r"https?://[^/]+:8082(/[^\"'`\s<]*)?")

def _rewrite_feeder_local_urls(text: str, prefix: str) -> str:
    """Rewrite feeder local URLs to proxy path so Map/Statistics open in tunnel.
    Covers 127.0.0.1:8080, localhost:8080, and any host:8080 (e.g. feeder's NetBird IP).
    Also rewrites full absolute URLs to :8754 (FR24) and :8082 (PiAware/FlightAware) so they route
    through the tunnel via the feeder's dashboard nginx which proxies /fr24/ and /piaware/.
    """
    for origin in _FEEDER_LOCAL_ORIGINS:
        text = text.replace(origin, prefix)
    text = _RE_FEEDER_ORIGIN_8080.sub(prefix, text)
    def _fr24_repl(m):
        path = m.group(1) or "/"
        return f"{prefix}/fr24{path}"
    def _fa_repl(m):
        path = m.group(1) or "/"
        return f"{prefix}/piaware{path}"
    text = _RE_FEEDER_ORIGIN_FR24.sub(_fr24_repl, text)
    text = _RE_FEEDER_ORIGIN_FA.sub(_fa_repl, text)
    return text


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


def _normalize_tar1090_path_for_proxy(path_only: str) -> str:
    """Fix graphs1090 root-relative asset requests to feeder path expected by graphs stack.

    Some graphs1090 pages request assets like /portal.css or /graphs.js while rendered from
    /graphs1090/. When referer indicates /graphs1090/, map those root-level file requests to
    /graphs1090/<file> before proxying to feeder.
    """
    p = path_only or "/"
    referer = (request.headers.get("Referer") or "").lower()
    if "/graphs1090/" not in referer:
        # tar1090 root often requests this JSON from /upintheair.json, while feeder serves it under /data/.
        if p == "/upintheair.json":
            return "/data/upintheair.json"
        return p
    # Graphs pages request images under /graphs/*.png; feeder serves them under /graphs1090/graphs/*.
    if p.startswith("/graphs/"):
        return "/graphs1090" + p
    # Keep explicit/known paths unchanged
    if p.startswith(("/graphs1090/", "/data/", "/db2/", "/tracks/", "/libs/", "/images/", "/tar1090/")):
        return p
    # Root-level single-file assets -> graphs1090 asset path
    if re.match(r"^/[^/]+\.(css|js|png|jpg|jpeg|gif|svg|ico|map|json|woff2?|ttf|eot)$", p, re.IGNORECASE):
        return "/graphs1090" + p
    return p


def _normalize_aux_path_for_proxy(path_only: str) -> str:
    """For FR24/PiAware pages, map root-relative requests back under service prefix.

    Third-party UIs can emit links like /settings.html or /jquery.min.js while the page is
    served under /fr24/ or /piaware/. In tunnel mode these must be forwarded to feeder as
    /fr24/... or /piaware/... to match nginx locations on port 80.
    """
    p = path_only or "/"
    if not p.startswith("/"):
        p = "/" + p
    # Filename collision: /fr24.css is NOT under /fr24/; nginx expects /fr24/fr24.css -> upstream /fr24.css
    if re.match(r"^/fr24\.[^/]+$", p):
        return "/fr24" + p
    if re.match(r"^/piaware\.[^/]+$", p):
        return "/piaware" + p
    if _is_fr24_service_path(p) or _is_piaware_service_path(p):
        return p
    referer = (request.headers.get("Referer") or "").lower()
    if "/fr24/" in referer:
        return "/fr24" + p
    if "/piaware/" in referer or "/flightaware/" in referer:
        return "/piaware" + p
    return p


def _rewrite_html_body(
    body: bytes,
    feeder_id: str,
    base_url: str,
    origin_no_slash: str = "",
    aux_service_prefix: str = "",
) -> bytes:
    """Inject <base> and rewrite absolute paths in HTML so assets and API calls hit the proxy.
    origin_no_slash is used for window.location.origin in inline JS (no trailing slash to avoid .../id//path 404s).
    """
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return body
    prefix = _feeder_prefix(feeder_id)
    # Avoid double-rewriting on dashboard HTML. Do NOT use this for FR24/PiAware: those
    # pages often embed the feeder id or /feeder/... many times in JSON/scripts, which
    # would skip <base> injection and break relative assets (fr24.css, jquery.js).
    if not aux_service_prefix:
        if prefix + "/" in text and text.count(prefix) > 2:
            return body
    # Absolute path references: replace only path start so attribute stays valid.
    # For FR24/PiAware documents, scope root-absolute links under service prefix:
    # e.g. href="/jquery.js" -> href="/feeder/<id>/fr24/jquery.js"
    html_prefix = prefix + (aux_service_prefix or "")
    text = text.replace('href="/', 'href="' + html_prefix + '/')
    text = text.replace("href='/", "href='" + html_prefix + "/")
    text = text.replace('src="/', 'src="' + html_prefix + '/')
    text = text.replace("src='/", "src='" + html_prefix + "/")
    text = text.replace('url("/', 'url("' + html_prefix + '/')
    text = text.replace("url('/", "url='" + html_prefix + "/")
    text = text.replace('action="/', 'action="' + html_prefix + '/')
    text = text.replace("action='/", "action='" + html_prefix + "/")
    # Map/Statistics links: rewrite feeder local URLs so they open through the proxy
    text = _rewrite_feeder_local_urls(text, prefix)
    # Inject <base> so relative URLs (e.g. style.css, api/...) resolve under the feeder path
    base_tag = f'<base href="{base_url}">'
    if "<head>" in text:
        text = text.replace("<head>", "<head>" + base_tag, 1)
    elif "<HEAD>" in text:
        text = text.replace("<HEAD>", "<HEAD>" + base_tag, 1)
    else:
        text = base_tag + text
    # Rewrite inline <script>...</script> so fetch("/api/...") etc. go through the proxy
    js_origin = origin_no_slash or base_url.rstrip("/")
    def _rewrite_inline_script(match):
        open_tag, content, close = match.group(1), match.group(2), match.group(3)
        if "src=" in open_tag.lower():
            return match.group(0)
        return open_tag + _rewrite_js_text(content, feeder_id, js_origin) + close
    text = re.sub(
        r'(<script(?:\s[^>]*)?>)([\s\S]*?)(</script>)',
        _rewrite_inline_script,
        text,
        flags=re.IGNORECASE,
    )
    return text.encode("utf-8", errors="replace")


def _inject_base_only_html(body: bytes, base_url: str) -> bytes:
    """Inject only <base href> into HTML (no JS/path rewrites)."""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return body
    base_tag = f'<base href="{base_url}">'
    if "<head>" in text:
        text = text.replace("<head>", "<head>" + base_tag, 1)
    elif "<HEAD>" in text:
        text = text.replace("<HEAD>", "<HEAD>" + base_tag, 1)
    else:
        text = base_tag + text
    return text.encode("utf-8", errors="replace")


def _rewrite_js_text(text: str, feeder_id: str, origin_for_js: str = "") -> str:
    """Rewrite path strings in JS (in quoted strings only) so API/static calls hit the proxy.
    We only replace after \" or ' or ` to avoid corrupting regex literals. Skip '/ when it's
    part of a regex like /'/g (e.g. feeder Settings SSID escaping). See docs/FEEDER_WEB_API_REFERENCE.md.
    origin_for_js (no trailing slash) replaces window.location.origin so map/stats URLs don't get double slash.
    """
    prefix = _feeder_prefix(feeder_id)
    # Don't short-circuit just because prefix appears somewhere in file:
    # mixed JS bundles can contain both already-prefixed and absolute root paths.
    # We still need to rewrite remaining "/api/..." etc. occurrences.
    if origin_for_js:
        text = text.replace("window.location.origin", json.dumps(origin_for_js.rstrip("/")))
    # Map/Statistics: rewrite feeder local URLs (e.g. window.open('http://127.0.0.1:8080/'))
    text = _rewrite_feeder_local_urls(text, prefix)
    # Handle common dynamic link patterns like "... + ':8754/'" and "... + ':8082/'" safely.
    # Restrict to quoted literals only to avoid broad content corruption.
    text = text.replace("':8754/'", f"'{prefix}/fr24/'")
    text = text.replace('":8754/"', f'"{prefix}/fr24/"')
    text = text.replace("':8082/'", f"'{prefix}/piaware/'")
    text = text.replace('":8082/"', f'"{prefix}/piaware/"')
    text = text.replace('"/', '"' + prefix + "/")
    # Don't replace '/ when followed by regex flags (e.g. .replace(/'/g, ...) for SSID escaping)
    text = re.sub(r"'/(?![gimsuy][,\)\s\"'])", "'" + prefix + "/", text)
    text = text.replace('`/', '`' + prefix + "/")
    # Catch concatenation like base + "/api/..." or url + '/static/...' (quote after +)
    text = text.replace('+ "/', '+ "' + prefix + "/")
    text = text.replace("+ '/", "+ '" + prefix + "/")
    text = text.replace(prefix + prefix, prefix)
    return text


def _rewrite_js_body(body: bytes, feeder_id: str, origin_for_js: str = "") -> bytes:
    """Rewrite absolute path strings in JS (fetch, etc.) so API calls hit the proxy."""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return body
    return _rewrite_js_text(text, feeder_id, origin_for_js).encode("utf-8", errors="replace")


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
        r = _TUNNEL_HTTP.post(url, json=payload, timeout=PROXY_TIMEOUT)
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
    fdb = _cached_feeder_for_tunnel(feeder_id)
    if current_user.role != "admin":
        if fdb is None:
            return Response(
                "<!DOCTYPE html><html><head><meta charset=utf-8><title>Forbidden</title></head>"
                "<body style=\"font-family:sans-serif;padding:2rem;\"><h1>Not authorized</h1>"
                "<p>This tunnel URL does not match a feeder in the dashboard, or you are not listed as an <strong>Owner</strong>. "
                "Ask an admin to assign owners and ensure the feeder name matches the tunnel register id.</p></body></html>",
                status=403,
                mimetype="text/html; charset=utf-8",
            )
        if not user_can_access_feeder(fdb, current_user.username, current_user.role):
            return Response(
                "<!DOCTYPE html><html><head><meta charset=utf-8><title>Forbidden</title></head>"
                "<body style=\"font-family:sans-serif;padding:2rem;\"><h1>Not authorized</h1>"
                "<p>You do not have access to this feeder tunnel. Ask an admin to add your account under <strong>Owners</strong> for this feeder.</p></body></html>",
                status=403,
                mimetype="text/html; charset=utf-8",
            )
    # Build path including query string (per wire protocol: path includes query)
    browser_path_only = "/" + subpath if subpath else "/"
    # Backward-compat alias: old rewrites may still point to /flightaware.
    if browser_path_only == "/flightaware" or browser_path_only.startswith("/flightaware/"):
        browser_path_only = "/piaware" + browser_path_only[len("/flightaware"):]
    aux_ui_path = (
        _is_fr24_service_path(browser_path_only)
        or _is_piaware_service_path(browser_path_only)
        or browser_path_only.startswith("/flightaware/") or browser_path_only == "/flightaware"
    )
    target_hint = _infer_tunnel_target(browser_path_only)
    path_only = _normalize_tar1090_path_for_proxy(browser_path_only)
    path_only = _normalize_aux_path_for_proxy(path_only)
    is_static_asset = _is_static_asset_path(browser_path_only)
    local_path = path_only
    if request.query_string:
        local_path = f"{path_only}?{request.query_string.decode('utf-8', errors='replace')}"

    # Request body (base64)
    body_bytes = request.get_data()
    body_b64 = base64.b64encode(body_bytes).decode("ascii") if body_bytes else ""

    headers = _request_headers_for_proxy(feeder_id, local_path)
    status, resp_headers, body_bytes = 503, {}, None
    for tunnel_fid in _tunnel_feeder_ids_to_try(feeder_id):
        status, resp_headers, body_bytes = _proxy_to_feeder(
            tunnel_fid,
            local_path,
            request.method,
            headers,
            body_b64,
        )
        if status != 503:
            break

    if status == 503:
        return (
            _feeder_offline_html(feeder_id),
            503,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    if status == 504:
        return "Feeder response timeout", 504
    # FR24/PiAware can emit root-absolute links like /settings.html, /fr24.css, /jquery.js.
    # Retry under /fr24/ and /piaware/ if bare-path request returns 404.
    if status == 404 and (
        _RE_BARE_HTML_PATH.match(path_only)
        or _RE_BARE_ROOT_ASSET.match(path_only)
    ):
        retry_headers = dict(headers)
        retry_headers["X-Tunnel-Target"] = "dashboard"
        for pref in ("/fr24", "/piaware"):
            retry_path = pref + path_only
            if request.query_string:
                retry_path = f"{retry_path}?{request.query_string.decode('utf-8', errors='replace')}"
            for tunnel_fid in _tunnel_feeder_ids_to_try(feeder_id):
                r_status, r_headers, r_body = _proxy_to_feeder(
                    tunnel_fid,
                    retry_path,
                    request.method,
                    retry_headers,
                    body_b64,
                )
                if r_status == 503:
                    continue
                if r_status != 404:
                    status, resp_headers, body_bytes = r_status, r_headers, r_body
                    path_only = retry_path.split("?", 1)[0]
                    break
            if status != 404:
                break
    # tar1090 optional endpoint: some builds request /upintheair.json but feeder may not provide it.
    # drawUpintheair() expects object with .rings; provide empty rings to avoid runtime errors.
    if status == 404 and path_only in ("/upintheair.json", "/data/upintheair.json"):
        return Response(b'{"rings":[]}', status=200, headers={"Content-Type": "application/json; charset=utf-8"})

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

    # Rewrite response body so assets and API calls go through the proxy.
    # For static assets, always passthrough (no decompress/rewrite) for speed and to avoid JS corruption.
    # For tar1090/graphs HTML, inject base only; for dashboard HTML/JS/CSS, apply existing rewrites.
    we_rewrote = False
    target = target_hint
    # Only force static passthrough for tar1090 target. Dashboard static JS/CSS still needs
    # path-prefix rewriting (/api -> /feeder/<id>/api) when served through tunnel.
    skip_body_rewrite = is_static_asset and target == "tar1090"
    if body_bytes and content_type and not skip_body_rewrite:
        if "text/html" in content_type:
            body_bytes = _decompress_body_best_effort(body_bytes, content_encoding)
            # Base URL must be the document directory (path_only), not path + "/" for filenames.
            # Use path_only so 404 retry to /fr24/settings.html yields base .../fr24/ not .../settings.html/
            dir_suffix = _base_href_path_suffix(path_only)
            if dir_suffix == "/":
                base_suffix = _feeder_prefix(feeder_id) + "/"
            else:
                base_suffix = _feeder_prefix(feeder_id) + dir_suffix.lstrip("/")
            base_url = request.url_root.rstrip("/") + base_suffix
            origin_no_slash = base_url.rstrip("/")
            if target == "tar1090":
                body_bytes = _inject_base_only_html(body_bytes, base_url)
            else:
                aux_service_prefix = ""
                p_no_qs = (path_only or "/").split("?", 1)[0]
                if p_no_qs.startswith("/fr24/") or p_no_qs == "/fr24":
                    aux_service_prefix = "/fr24"
                elif p_no_qs.startswith("/piaware/") or p_no_qs == "/piaware":
                    aux_service_prefix = "/piaware"
                body_bytes = _rewrite_html_body(
                    body_bytes,
                    feeder_id,
                    base_url,
                    origin_no_slash,
                    aux_service_prefix,
                )
            we_rewrote = True
        elif "javascript" in content_type:
            # Do not rewrite third-party FR24/PiAware JS bundles; generic string rewrites can
            # corrupt minified regex literals (e.g., jquery/bootstrap) and break the page.
            if target != "tar1090" and not aux_ui_path:
                body_bytes = _decompress_body_best_effort(body_bytes, content_encoding)
                origin_no_slash = request.url_root.rstrip("/") + _feeder_prefix(feeder_id)
                body_bytes = _rewrite_js_body(body_bytes, feeder_id, origin_no_slash)
                we_rewrote = True
        elif "text/css" in content_type:
            # Keep FR24/PiAware CSS byte-identical for compatibility with bundled assets.
            if target != "tar1090" and not aux_ui_path:
                body_bytes = _decompress_body_best_effort(body_bytes, content_encoding)
                body_bytes = _rewrite_css_body(body_bytes, feeder_id)
                we_rewrote = True

    # When we rewrote, body is uncompressed — drop content-encoding and content-length so browser decodes correctly
    resp = Response(body_bytes, status=status)
    for name, value in headers_out:
        if we_rewrote and name.lower() in ("content-length", "content-encoding"):
            continue
        resp.headers[name] = value
    # Cache static assets under feeder tunnel paths to reduce repeated fetch latency.
    if is_static_asset and status == 200 and not resp.headers.get("Cache-Control"):
        resp.headers["Cache-Control"] = _cache_control_for_path(browser_path_only)
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
