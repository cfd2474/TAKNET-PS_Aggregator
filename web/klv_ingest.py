"""
KLV metadata ingest — TCP NDJSON listener for MediaMTX klv-to-cot sidecar feeds.

Receives newline-delimited JSON with per-transform ingest tokens, maintains in-memory
stream state, and exposes status for the CoT pipeline and COTProxy UI.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_logger = logging.getLogger("gunicorn.error")
if not _logger.handlers:
    _logger = logging.getLogger(__name__)
log = _logger

KLV_PORT = int(os.environ.get("KLV_INGEST_PORT", "9000"))
KLV_STALE_SECONDS = 5.0
KLV_NO_DECODE_GRACE_SECONDS = 10.0

_lock = threading.Lock()
_token_index: dict[str, dict] = {}
_stream_state: dict[str, "StreamState"] = {}
_listener_started = False


@dataclass
class StreamState:
    stream_up: bool = False
    last_event_ts: float = 0.0
    last_sample_ts: float = 0.0
    first_up_ts: float = 0.0
    has_klv: bool = False
    platform: Optional[dict] = None
    spi: Optional[dict] = None


def _now() -> float:
    return time.time()


def reload_token_index() -> int:
    """Reload token -> (output_id, hex, transform_id) from DB. Returns count loaded."""
    from models import CotTransformModel

    rows = CotTransformModel.get_all_with_tokens()
    new_index: dict[str, dict] = {}
    for r in rows:
        token = (r.get("klv_ingest_token") or "").strip()
        if not token:
            continue
        new_index[token] = {
            "output_id": r["output_id"],
            "hex": (r.get("hex") or "").strip().upper(),
            "transform_id": r["id"],
        }
    with _lock:
        _token_index.clear()
        _token_index.update(new_index)
        known = set(_token_index.keys())
        for tok in list(_stream_state.keys()):
            if tok not in known:
                del _stream_state[tok]
    return len(new_index)


def register_token(token: str, output_id: int, hex_code: str, transform_id: int) -> None:
    with _lock:
        _token_index[token] = {
            "output_id": output_id,
            "hex": hex_code.strip().upper(),
            "transform_id": transform_id,
        }


def unregister_token(token: str) -> None:
    with _lock:
        _token_index.pop(token, None)
        _stream_state.pop(token, None)


def _get_state(token: str) -> StreamState:
    with _lock:
        st = _stream_state.get(token)
        if st is None:
            st = StreamState()
            _stream_state[token] = st
        return st


def _has_platform(data: dict) -> bool:
    plat = data.get("platform")
    if not isinstance(plat, dict):
        return False
    lat = plat.get("lat")
    lon = plat.get("lon")
    return lat is not None and lon is not None


def _has_spi(data: dict) -> bool:
    spi = data.get("spi")
    if not isinstance(spi, dict):
        return False
    lat = spi.get("lat")
    lon = spi.get("lon")
    return lat is not None and lon is not None


def handle_message(msg: dict) -> None:
    """Process one decoded NDJSON object from a MediaMTX sidecar."""
    token = (msg.get("token") or "").strip()
    if not token:
        return
    with _lock:
        if token not in _token_index:
            return
    event = (msg.get("event") or "").strip().lower()
    ts = _now()
    st = _get_state(token)

    if event == "up":
        st.stream_up = True
        st.first_up_ts = ts
        st.last_event_ts = ts
        st.has_klv = False
        return

    if event == "down":
        st.stream_up = False
        st.last_event_ts = ts
        st.has_klv = False
        st.platform = None
        st.spi = None
        return

    if event != "sample":
        return

    st.last_sample_ts = ts
    st.last_event_ts = ts
    if _has_platform(msg) or _has_spi(msg):
        st.has_klv = True
    if isinstance(msg.get("platform"), dict):
        st.platform = dict(msg["platform"])
    if isinstance(msg.get("spi"), dict):
        st.spi = dict(msg["spi"])


def get_stream_state(token: str) -> Optional[StreamState]:
    if not token:
        return None
    with _lock:
        st = _stream_state.get(token)
        if st is None:
            return None
        return StreamState(
            stream_up=st.stream_up,
            last_event_ts=st.last_event_ts,
            last_sample_ts=st.last_sample_ts,
            first_up_ts=st.first_up_ts,
            has_klv=st.has_klv,
            platform=dict(st.platform) if st.platform else None,
            spi=dict(st.spi) if st.spi else None,
        )


def klv_status_for_token(token: str) -> str:
    """
    Return UI status: idle | live | no_klv | stale.
    """
    if not token:
        return "idle"
    st = get_stream_state(token)
    if st is None:
        return "idle"
    now = _now()
    if st.last_sample_ts and (now - st.last_sample_ts) <= KLV_STALE_SECONDS:
        if st.has_klv:
            return "live"
        if st.stream_up and st.first_up_ts and (now - st.first_up_ts) > KLV_NO_DECODE_GRACE_SECONDS:
            return "no_klv"
        if st.stream_up or st.last_event_ts:
            return "no_klv"
        return "idle"
    if st.last_sample_ts and (now - st.last_sample_ts) > KLV_STALE_SECONDS:
        if st.stream_up:
            return "stale"
        return "idle"
    if st.stream_up and st.first_up_ts and (now - st.first_up_ts) > KLV_NO_DECODE_GRACE_SECONDS:
        return "no_klv"
    if st.stream_up:
        return "no_klv"
    return "idle"


def is_klv_live(token: str) -> bool:
    """True when KLV samples are fresh and carry usable platform position."""
    if not token:
        return False
    st = get_stream_state(token)
    if st is None:
        return False
    now = _now()
    if not st.last_sample_ts or (now - st.last_sample_ts) > KLV_STALE_SECONDS:
        return False
    if not st.has_klv or not st.platform:
        return False
    lat = st.platform.get("lat")
    lon = st.platform.get("lon")
    return lat is not None and lon is not None


def is_spi_live(token: str) -> bool:
    if not is_klv_live(token):
        return False
    st = get_stream_state(token)
    if not st or not st.spi:
        return False
    lat = st.spi.get("lat")
    lon = st.spi.get("lon")
    return lat is not None and lon is not None


def apply_klv_platform(aircraft: dict, token: str) -> dict:
    """Return aircraft dict with KLV platform fields overlaid when live."""
    if not is_klv_live(token):
        return aircraft
    st = get_stream_state(token)
    if not st or not st.platform:
        return aircraft
    out = dict(aircraft)
    plat = st.platform
    if plat.get("lat") is not None:
        out["lat"] = float(plat["lat"])
    if plat.get("lon") is not None:
        out["lon"] = float(plat["lon"])
    if plat.get("alt_m") is not None:
        out["alt_baro"] = float(plat["alt_m"]) / 0.3048
    if plat.get("heading_deg") is not None:
        out["track"] = float(plat["heading_deg"])
    return out


def _handle_client(conn: socket.socket, addr) -> None:
    conn.settimeout(120.0)
    buf = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict):
                    handle_message(msg)
    except (ConnectionResetError, BrokenPipeError, TimeoutError, OSError):
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _accept_loop(sock: socket.socket) -> None:
    while True:
        try:
            client, addr = sock.accept()
        except OSError:
            break
        t = threading.Thread(target=_handle_client, args=(client, addr), daemon=True)
        t.start()


def get_klv_hexes_for_output(output_id: int) -> set[str]:
    """Hexes with recent ingest activity for an output (for CoT transform prefetch)."""
    now = _now()
    out: set[str] = set()
    with _lock:
        for token, meta in _token_index.items():
            if meta.get("output_id") != output_id:
                continue
            st = _stream_state.get(token)
            if not st:
                continue
            if st.last_sample_ts and (now - st.last_sample_ts) <= KLV_STALE_SECONDS:
                hx = (meta.get("hex") or "").strip().upper()
                if hx:
                    out.add(hx)
            elif st.stream_up and st.first_up_ts and (now - st.first_up_ts) <= KLV_NO_DECODE_GRACE_SECONDS + KLV_STALE_SECONDS:
                hx = (meta.get("hex") or "").strip().upper()
                if hx:
                    out.add(hx)
    return out


def start_klv_listener(port: Optional[int] = None) -> None:
    """Start TCP NDJSON listener (idempotent)."""
    global _listener_started
    if _listener_started:
        return
    p = port if port is not None else KLV_PORT
    try:
        reload_token_index()
    except Exception as e:
        log.warning("KLV ingest: token index reload failed at startup: %s", e)

    def _run():
        global _listener_started
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("0.0.0.0", p))
            sock.listen(64)
            _listener_started = True
            log.info("KLV ingest: listening on TCP 0.0.0.0:%d", p)
            _accept_loop(sock)
        except Exception as e:
            log.error("KLV ingest: failed to bind port %d: %s", p, e)
        finally:
            try:
                sock.close()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()

    def _periodic_reload():
        while True:
            time.sleep(60)
            try:
                reload_token_index()
            except Exception:
                pass

    threading.Thread(target=_periodic_reload, daemon=True).start()
