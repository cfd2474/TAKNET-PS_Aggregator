#!/usr/bin/env python3
"""
Aircraft merger: combine local (tar1090) and ADSBHub SBS feed.
Dedupe by ICAO hex — prefer local (direct feeders) for accuracy; use ADSBHub when we don't have the aircraft.
Serves aircraft.json in tar1090 format so map and REST API work unchanged.
"""

import json
import os
import socket
import threading
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen

from aircraft_db import enrich_adsbhub_aircraft, init as init_aircraft_db, refresh_if_due
from json_ingest import (
    handle_ingest,
    load_snapshots_from_disk,
    merge_json_feeds,
    reload_config,
    tick_stale_feeds,
)

TAR1090_URL = os.environ.get("TAR1090_URL", "http://tar1090:80/data/aircraft.json")
ADSBHUB_HOST = os.environ.get("ADSBHUB_HOST", "data.adsbhub.org")
ADSBHUB_PORT = int(os.environ.get("ADSBHUB_PORT", "5002"))
POLL_INTERVAL = float(os.environ.get("MERGER_POLL_MS", "1500")) / 1000.0  # local fetch
SBS_BUFFER_SIZE = 65536
STATUS_DIR = os.environ.get("ADSBHUB_STATUS_DIR", "/status")
STALE_SECONDS = float(os.environ.get("MERGER_STALE_SECONDS", "10"))

# Shared state: merged aircraft list, now, messages (updated by merger thread)
_state = {"aircraft": [], "now": 0, "messages": 0}
_lock = threading.Lock()
# Pre-serialized aircraft.json body, rebuilt once per merge cycle so GETs don't
# re-serialize ~14k aircraft per request (was pegging a core and blocking outputs).
_cached_body = b'{"aircraft":[],"now":0,"messages":0}'
# Patched tar1090 receiver.json (binCraft/zstd off) so the tar1090 web UI keeps
# polling aircraft.json — which nginx routes here — instead of switching to
# aircraft.binCraft.zst, which only contains local (direct feeder) aircraft.
_receiver_cache = {"body": None, "ts": 0.0}
RECEIVER_JSON_TTL = 60.0
# ADSBHub: last time we got an update per hex (for 10s staleness purge)
_adsbhub_last_seen = {}


def _parse_sbs_line(line):
    """Parse one SBS (30003) line; return dict with hex, lat, lon, etc. or None."""
    line = line.strip()
    if not line or not line.startswith("MSG,"):
        return None
    parts = line.split(",")
    if len(parts) < 17:
        return None
    try:
        hex_ = (parts[4] or "").strip().upper()
        if not hex_ or len(hex_) != 6:
            return None
        out = {"hex": hex_, "source": "adsbhub"}
        # parts: 0=MSG, 1=type, 2=session, 3=aircraft_id, 4=hex, 5=flightid, 6=date, 7=time, 8=date_log, 9=time_log
        # 10=callsign, 11=altitude, 12=groundspeed, 13=track, 14=lat, 15=lon, 16=vert_rate, 17=squawk
        if len(parts) > 10 and (parts[10] or "").strip():
            out["flight"] = (parts[10] or "").strip()[:8]
        if len(parts) > 11 and parts[11]:
            try:
                out["alt_baro"] = int(float(parts[11]))
            except (ValueError, TypeError):
                pass
        if len(parts) > 12 and parts[12]:
            try:
                out["gs"] = int(float(parts[12]))
            except (ValueError, TypeError):
                pass
        if len(parts) > 13 and parts[13]:
            try:
                out["track"] = int(float(parts[13]))
            except (ValueError, TypeError):
                pass
        if len(parts) > 14 and parts[14]:
            try:
                out["lat"] = float(parts[14])
            except (ValueError, TypeError):
                pass
        if len(parts) > 15 and parts[15]:
            try:
                out["lon"] = float(parts[15])
            except (ValueError, TypeError):
                pass
        if len(parts) > 16 and parts[16]:
            try:
                out["baro_rate"] = int(float(parts[16])) * 64  # 64 ft resolution
            except (ValueError, TypeError):
                pass
        if len(parts) > 17 and parts[17]:
            try:
                out["squawk"] = str(int(float(parts[17]))).zfill(4)
            except (ValueError, TypeError):
                pass
        if len(parts) > 18 and parts[18]:
            try:
                out["on_ground"] = bool(int(float(parts[18])))
            except (ValueError, TypeError):
                pass
        return out
    except (IndexError, ValueError, TypeError):
        return None


RECEIVER_URL = TAR1090_URL.rsplit("/", 1)[0] + "/receiver.json"


def _patched_receiver_json() -> bytes:
    """tar1090 receiver.json with binCraft/zstd disabled so the web UI polls aircraft.json (merged)."""
    now = time.time()
    with _lock:
        if _receiver_cache["body"] and (now - _receiver_cache["ts"]) < RECEIVER_JSON_TTL:
            return _receiver_cache["body"]
    try:
        with urlopen(RECEIVER_URL, timeout=5) as r:
            data = json.loads(r.read().decode())
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data.setdefault("readsb", True)
    data["binCraft"] = False
    data["aircraft_binCraft"] = False
    data["zstd"] = False
    # ~10k merged aircraft = ~5MB JSON per poll; don't let clients poll every second
    try:
        data["refresh"] = max(int(data.get("refresh") or 1000), 2000)
    except (TypeError, ValueError):
        data["refresh"] = 2000
    body = json.dumps(data).encode()
    with _lock:
        _receiver_cache["body"] = body
        _receiver_cache["ts"] = now
    return body


def _fetch_local():
    """Fetch aircraft.json from tar1090. Return (aircraft_list, now, messages) or ([], 0, 0)."""
    try:
        with urlopen(TAR1090_URL, timeout=5) as r:
            data = json.loads(r.read().decode())
        return (
            data.get("aircraft", []),
            data.get("now", time.time()),
            data.get("messages", 0),
        )
    except Exception:
        return [], time.time(), 0


def _write_receive_status(connected):
    """Write receive connection status to shared volume for dashboard."""
    try:
        path = os.path.join(STATUS_DIR, "receive.json")
        data = {"connected": connected, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _is_receive_enabled():
    """True if Receive from ADSBHub is enabled (dashboard writes this file when user toggles)."""
    try:
        path = os.path.join(STATUS_DIR, "receive_enabled")
        if os.path.isfile(path):
            with open(path, "r") as f:
                return (f.read().strip().lower() in ("1", "true", "yes"))
    except Exception:
        pass
    # Fallback: env at startup (used until dashboard writes the file)
    return os.environ.get("ADSBHUB_RECEIVE_ENABLED", "").lower() in ("1", "true", "yes")


def _run_sbs_client():
    """Connect to ADSBHub:5002, parse SBS, update shared adsbhub_by_hex."""
    adsbhub_by_hex = {}
    reconnect_delay = 5
    while True:
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect((ADSBHUB_HOST, ADSBHUB_PORT))
            _write_receive_status(True)
            sock.settimeout(300)
            buf = b""
            while True:
                chunk = sock.recv(SBS_BUFFER_SIZE)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf or b"\r" in buf:
                    line, _, buf = buf.partition(b"\n")
                    if b"\r" in line:
                        line = line.split(b"\r")[0]
                    try:
                        s = line.decode("utf-8", errors="ignore")
                    except Exception:
                        continue
                    rec = _parse_sbs_line(s)
                    if rec:
                        hex_ = rec.get("hex")
                        if hex_:
                            # Merge into existing record (ADSBHub sends MSG,1 / MSG,3 / MSG,4 separately)
                            base = adsbhub_by_hex.get(hex_, {"hex": hex_, "source": "adsbhub"})
                            for k, v in rec.items():
                                if k != "hex" and v is not None:
                                    base[k] = v
                            enrich_adsbhub_aircraft(base)
                            adsbhub_by_hex[hex_] = base
                            _adsbhub_last_seen[hex_] = time.time()
                with _lock:
                    _state["_adsbhub"] = dict(adsbhub_by_hex)
        except (socket.error, OSError, Exception):
            pass
        _write_receive_status(False)
        try:
            if sock:
                sock.close()
        except Exception:
            pass
        time.sleep(reconnect_delay)


def _merge_loop():
    """Periodically fetch local, merge with ADSBHub state (prefer local), purge stale > STALE_SECONDS, update _state."""
    now_ts = time.time()
    db_refresh_counter = 0
    while True:
        db_refresh_counter += 1
        if db_refresh_counter >= 400:
            db_refresh_counter = 0
            refresh_if_due()
        local_aircraft, now_ts, messages = _fetch_local()
        receive_on = _is_receive_enabled()
        with _lock:
            adsbhub = (_state.get("_adsbhub") or {}).copy() if receive_on else {}
            if not receive_on:
                _state["_adsbhub"] = {}
                _adsbhub_last_seen.clear()
        # Staleness: drop local aircraft with seen > STALE_SECONDS (seen = seconds ago)
        fresh_local = []
        for ac in local_aircraft:
            seen = ac.get("seen")
            if seen is not None:
                try:
                    if float(seen) > STALE_SECONDS:
                        continue
                except (TypeError, ValueError):
                    pass
            fresh_local.append(ac)
        merged = []
        seen_hex = set()
        # Prefer local: add all local first (already filtered stale)
        for ac in fresh_local:
            hex_ = str(ac.get("hex", "")).strip().upper().lstrip("~")
            if hex_ and hex_ not in seen_hex:
                seen_hex.add(hex_)
                merged.append(ac)
        # Fill in from JSON push feeds (after local, before ADSBHub)
        json_aircraft = merge_json_feeds(seen_hex, STALE_SECONDS)
        merged.extend(json_aircraft)
        # Fill in from ADSBHub where we don't have local; drop if stale > STALE_SECONDS
        cutoff = time.time() - STALE_SECONDS
        for hex_, ac in list(adsbhub.items()):
            if hex_ in seen_hex:
                continue
            if _adsbhub_last_seen.get(hex_, 0) < cutoff:
                continue
            seen_hex.add(hex_)
            enrich_adsbhub_aircraft(ac)
            merged.append(ac)
        # Purge old entries from _adsbhub_last_seen to avoid unbounded growth
        for hex_ in list(_adsbhub_last_seen.keys()):
            if _adsbhub_last_seen[hex_] < cutoff:
                del _adsbhub_last_seen[hex_]
        body = json.dumps(
            {"aircraft": merged, "now": now_ts, "messages": messages},
            separators=(",", ":"),
        ).encode()
        global _cached_body
        with _lock:
            _state["aircraft"] = merged
            _state["now"] = now_ts
            _state["messages"] = messages
            _cached_body = body
        tick_stale_feeds(STALE_SECONDS)
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def _client_ip(self) -> str:
        fwd = self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or ""
        if fwd:
            return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/data/aircraft.json" or path == "/aircraft.json" or path == "":
            with _lock:
                body = _cached_body
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client disconnected mid-response; not an error worth logging
        elif path == "/data/receiver.json" or path == "/receiver.json":
            body = _patched_receiver_json()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store, no-cache")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        prefix = "/ingest/json/"
        if not path.startswith(prefix):
            self.send_response(404)
            self.end_headers()
            return
        token = path[len(prefix):].strip("/")
        if not token:
            auth = (self.headers.get("Authorization") or "").strip()
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not token:
            self._send_json(401, {"error": "Missing token in URL or Authorization header"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": "Missing Content-Length"})
            return
        body = self.rfile.read(length)
        status, payload = handle_ingest(
            token, body, self._client_ip(),
            content_encoding=self.headers.get("Content-Encoding") or "",
        )
        try:
            self._send_json(status, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        pass


def _write_receive_enabled_file(enabled):
    """Write receive_enabled flag only if file does not exist (dashboard is source of truth after first save)."""
    try:
        path = os.path.join(STATUS_DIR, "receive_enabled")
        if os.path.isfile(path):
            return
        with open(path, "w") as f:
            f.write("true" if enabled else "false")
    except Exception:
        pass


def main():
    init_aircraft_db()
    # Initial fetch so first request has data
    local_aircraft, now_ts, messages = _fetch_local()
    with _lock:
        _state["aircraft"] = local_aircraft
        _state["now"] = now_ts
        _state["messages"] = messages
    # Use enable file if present (dashboard is source of truth); else env at startup
    receive_enabled = _is_receive_enabled()
    _write_receive_enabled_file(receive_enabled)
    reload_config(force=True)
    restored = load_snapshots_from_disk()
    if restored:
        print(f"json_ingest: restored {restored} feed snapshot(s) from disk", flush=True)
    if not receive_enabled:
        # Pass-through only: keep fetching local
        t = threading.Thread(target=_merge_loop, daemon=True)
        t.start()
    else:
        _state["_adsbhub"] = {}
        t1 = threading.Thread(target=_run_sbs_client, daemon=True)
        t1.start()
        t2 = threading.Thread(target=_merge_loop, daemon=True)
        t2.start()
    port = int(os.environ.get("MERGER_PORT", "8090"))
    # Threading server: a slow client or large ingest POST must not block
    # dashboard/map/CoT requests for aircraft.json.
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
