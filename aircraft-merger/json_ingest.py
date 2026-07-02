"""
Incoming JSON aircraft ingest — push endpoint for remote aggregators.

Accepts tar1090 aircraft.json or ADSBExchange / airplanes.live-style envelopes.
Auth: token in URL path or Authorization: Bearer header.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import threading
import time
from typing import Any, Optional

CONFIG_PATH = os.environ.get(
    "JSON_FEEDS_CONFIG_PATH", "/status/json_feeds_config.json"
)
STATUS_PATH = os.environ.get(
    "JSON_FEEDS_STATUS_PATH", "/status/json_feeds_status.json"
)
SNAPSHOT_DIR = os.environ.get("JSON_FEEDS_SNAPSHOT_DIR", "/status/json_feed_snapshots")
MAX_BODY_BYTES = int(os.environ.get("JSON_INGEST_MAX_BYTES", str(50 * 1024 * 1024)))
CONFIG_RELOAD_SECONDS = float(os.environ.get("JSON_FEEDS_CONFIG_RELOAD_SEC", "5"))
STARTUP_GRACE_SECONDS = float(os.environ.get("JSON_INGEST_STARTUP_GRACE_SEC", "180"))
SNAPSHOT_WRITE_INTERVAL = float(os.environ.get("JSON_FEED_SNAPSHOT_WRITE_SEC", "15"))

_lock = threading.Lock()
_feed_config: list[dict] = []
_config_loaded_at = 0.0
_token_index: dict[str, dict] = {}
_by_feed: dict[int, dict[str, dict]] = {}
_last_seen: dict[int, dict[str, float]] = {}
_feed_status: dict[int, dict] = {}
_boot_time = time.time()
_last_snapshot_write: dict[int, float] = {}


def _now() -> float:
    return time.time()


def _slug_label(name: str, feed_id: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        slug = str(feed_id)
    return f"json:{slug}"


def _snapshot_path(feed_id: int) -> str:
    return os.path.join(SNAPSHOT_DIR, f"feed_{feed_id}.json")


def _effective_stale_seconds(stale_seconds: float) -> float:
    """Extend staleness window briefly after merger boot so remote pushers can reconnect."""
    elapsed = _now() - _boot_time
    if elapsed < STARTUP_GRACE_SECONDS:
        return stale_seconds + (STARTUP_GRACE_SECONDS - elapsed)
    return stale_seconds


def _write_snapshot(feed_id: int, normalized: dict[str, dict], ts: float, force: bool = False) -> None:
    last = _last_snapshot_write.get(feed_id, 0.0)
    if not force and (ts - last) < SNAPSHOT_WRITE_INTERVAL:
        return
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
        payload = {
            "feed_id": feed_id,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "saved_ts": ts,
            "aircraft_count": len(normalized),
            "aircraft": list(normalized.values()),
        }
        path = _snapshot_path(feed_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, separators=(",", ":"))
        os.replace(tmp, path)
        _last_snapshot_write[feed_id] = ts
    except Exception:
        pass


def load_snapshots_from_disk() -> int:
    """Restore last pushed snapshots after merger restart. Returns feeds restored."""
    reload_config(force=True)
    restored = 0
    now = _now()
    try:
        os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    except Exception:
        return 0
    with _lock:
        enabled_ids = {meta["id"] for meta in _token_index.values()}
        for feed_id in enabled_ids:
            path = _snapshot_path(feed_id)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            meta = next((m for m in _token_index.values() if m.get("id") == feed_id), None)
            if not meta:
                continue
            source_label = meta.get("source_label") or _slug_label("", feed_id)
            normalized: dict[str, dict] = {}
            raw_list = data.get("aircraft")
            if isinstance(raw_list, list):
                for ac in raw_list:
                    row = normalize_aircraft(ac, source_label)
                    if row:
                        normalized[row["hex"]] = row
            if not normalized:
                continue
            _by_feed[feed_id] = normalized
            # Fresh clock on restore so partner has full stale window to reconnect.
            _last_seen[feed_id] = {hex_: now for hex_ in normalized}
            _feed_status[feed_id] = {
                "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "aircraft_count": len(normalized),
                "last_error": None,
                "status": "active",
                "restored_from_snapshot": True,
            }
            restored += 1
    if restored:
        _write_status_file()
    return restored


def reload_config(force: bool = False) -> int:
    """Load feed config from shared volume. Returns number of enabled feeds."""
    global _config_loaded_at
    now = _now()
    if not force and _feed_config and (now - _config_loaded_at) < CONFIG_RELOAD_SECONDS:
        return len(_token_index)
    feeds: list[dict] = []
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                raw = data.get("feeds")
                if isinstance(raw, list):
                    feeds = [x for x in raw if isinstance(x, dict)]
    except Exception:
        feeds = []
    new_index: dict[str, dict] = {}
    for row in feeds:
        if not row.get("enabled"):
            continue
        token_hash = (row.get("token_hash") or "").strip()
        feed_id = row.get("id")
        if not token_hash or feed_id is None:
            continue
        try:
            fid = int(feed_id)
        except (TypeError, ValueError):
            continue
        new_index[token_hash] = {
            "id": fid,
            "source_label": (row.get("source_label") or _slug_label("", fid)).strip(),
            "stale_seconds": float(row.get("stale_seconds") or 10),
            "merge_priority": int(row.get("merge_priority") or 100),
            "ip_allowlist": row.get("ip_allowlist") if isinstance(row.get("ip_allowlist"), list) else [],
        }
    with _lock:
        _feed_config.clear()
        _feed_config.extend(feeds)
        _token_index.clear()
        _token_index.update(new_index)
        known_ids = {meta["id"] for meta in _token_index.values()}
        for fid in list(_by_feed.keys()):
            if fid not in known_ids:
                _by_feed.pop(fid, None)
                _last_seen.pop(fid, None)
                _feed_status.pop(fid, None)
        _config_loaded_at = now
    return len(new_index)


def _resolve_feed(token: str, client_ip: str) -> tuple[Optional[dict], Optional[str]]:
    reload_config()
    token = (token or "").strip()
    if not token:
        return None, "Missing token"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _lock:
        meta = _token_index.get(token_hash)
    if not meta:
        return None, "Invalid token"
    allowlist = meta.get("ip_allowlist") or []
    if allowlist and not _ip_allowed(client_ip, allowlist):
        return None, "IP not allowed"
    return meta, None


def _ip_allowed(client_ip: str, allowlist: list) -> bool:
    if not allowlist:
        return True
    try:
        ip = ipaddress.ip_address((client_ip or "").strip())
    except ValueError:
        return False
    for entry in allowlist:
        raw = (entry or "").strip()
        if not raw:
            continue
        try:
            if "/" in raw:
                if ip in ipaddress.ip_network(raw, strict=False):
                    return True
            elif ip == ipaddress.ip_address(raw):
                return True
        except ValueError:
            continue
    return False


def parse_payload(body: bytes) -> tuple[list[dict], float]:
    """Parse tar1090 aircraft.json or ADSBExchange / v2 envelope."""
    if len(body) > MAX_BODY_BYTES:
        raise ValueError(f"Payload too large (max {MAX_BODY_BYTES} bytes)")
    if not body:
        raise ValueError("Empty body")
    try:
        data = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"Invalid JSON: {e}") from e

    if isinstance(data, list):
        return data, _now()

    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object or array")

    aircraft = data.get("aircraft")
    if isinstance(aircraft, list):
        now_val = data.get("now", data.get("ctime", data.get("timestamp")))
        try:
            now_ts = float(now_val) if now_val is not None else _now()
        except (TypeError, ValueError):
            now_ts = _now()
        return aircraft, now_ts

    raise ValueError("Expected {aircraft: [...]} (tar1090 or ADSBExchange envelope)")


def normalize_aircraft(ac: Any, source_label: str) -> Optional[dict]:
    if not isinstance(ac, dict):
        return None
    hex_raw = (
        ac.get("hex")
        or ac.get("icao")
        or ac.get("ICAO")
        or ac.get("addr")
        or ""
    )
    hex_ = str(hex_raw).strip().upper().lstrip("~")
    if len(hex_) != 6:
        return None
    try:
        int(hex_, 16)
    except ValueError:
        return None
    out = dict(ac)
    out["hex"] = hex_
    out["source"] = source_label
    return out


def handle_ingest(token: str, body: bytes, client_ip: str) -> tuple[int, dict]:
    meta, err = _resolve_feed(token, client_ip)
    if not meta:
        return 401, {"error": err or "Unauthorized"}

    feed_id = meta["id"]
    source_label = meta["source_label"]

    try:
        raw_list, _payload_now = parse_payload(body)
    except ValueError as e:
        _set_feed_error(feed_id, str(e))
        return 400, {"error": str(e)}

    normalized: dict[str, dict] = {}
    ts = _now()
    for ac in raw_list:
        row = normalize_aircraft(ac, source_label)
        if row:
            normalized[row["hex"]] = row

    with _lock:
        _by_feed[feed_id] = normalized
        _last_seen[feed_id] = {hex_: ts for hex_ in normalized}
        _feed_status[feed_id] = {
            "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
            "aircraft_count": len(normalized),
            "last_error": None,
            "status": "active",
        }

    _write_snapshot(feed_id, normalized, ts)
    _write_status_file()
    return 200, {
        "success": True,
        "feed_id": feed_id,
        "accepted": len(normalized),
    }


def _set_feed_error(feed_id: int, message: str) -> None:
    with _lock:
        st = _feed_status.get(feed_id) or {}
        st["last_error"] = message
        st["status"] = "error"
        st["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _feed_status[feed_id] = st
    _write_status_file()


def _write_status_file() -> None:
    try:
        with _lock:
            payload = {"feeds": dict(_feed_status), "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        tmp = STATUS_PATH + ".tmp"
        os.makedirs(os.path.dirname(STATUS_PATH) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, STATUS_PATH)
    except Exception:
        pass


def merge_json_feeds(seen_hex: set[str], cutoff_default: float) -> list[dict]:
    """Return aircraft from enabled JSON feeds (after local, before ADSBHub)."""
    reload_config()
    out: list[dict] = []
    with _lock:
        by_id = {meta["id"]: meta for meta in _token_index.values()}
        feed_ids = sorted(_by_feed.keys(), key=lambda fid: by_id.get(fid, {}).get("merge_priority", 100))
        for feed_id in feed_ids:
            meta = by_id.get(feed_id, {})
            stale = _effective_stale_seconds(float(meta.get("stale_seconds") or cutoff_default))
            cutoff = _now() - stale
            ac_map = _by_feed.get(feed_id) or {}
            seen_map = _last_seen.get(feed_id) or {}
            for hex_, ac in ac_map.items():
                if hex_ in seen_hex:
                    continue
                if seen_map.get(hex_, 0) < cutoff:
                    continue
                seen_hex.add(hex_)
                out.append(ac)
    return out


def tick_stale_feeds(default_stale: float) -> None:
    """Mark feeds stale when no recent ingest."""
    now = _now()
    changed = False
    with _lock:
        by_id = {meta["id"]: meta for meta in _token_index.values()}
        for feed_id in list(_feed_status.keys()):
            meta = by_id.get(feed_id, {})
            stale = _effective_stale_seconds(float(meta.get("stale_seconds") or default_stale))
            seen_map = _last_seen.get(feed_id) or {}
            if not seen_map:
                continue
            latest = max(seen_map.values())
            st = _feed_status.get(feed_id) or {}
            if now - latest > stale * 2:
                if st.get("status") != "stale":
                    st["status"] = "stale"
                    _feed_status[feed_id] = st
                    changed = True
            elif st.get("status") == "stale" and now - latest <= stale:
                st["status"] = "active"
                _feed_status[feed_id] = st
                changed = True
    if changed:
        _write_status_file()
