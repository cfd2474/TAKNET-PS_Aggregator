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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import urlopen

TAR1090_URL = os.environ.get("TAR1090_URL", "http://tar1090:80/data/aircraft.json")
ADSBHUB_HOST = os.environ.get("ADSBHUB_HOST", "data.adsbhub.org")
ADSBHUB_PORT = int(os.environ.get("ADSBHUB_PORT", "5002"))
POLL_INTERVAL = float(os.environ.get("MERGER_POLL_MS", "1500")) / 1000.0  # local fetch
SBS_BUFFER_SIZE = 65536
STATUS_DIR = os.environ.get("ADSBHUB_STATUS_DIR", "/status")

# Shared state: merged aircraft list, now, messages (updated by merger thread)
_state = {"aircraft": [], "now": 0, "messages": 0}
_lock = threading.Lock()


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
                            adsbhub_by_hex[hex_] = base
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
    """Periodically fetch local, merge with ADSBHub state (prefer local), update _state."""
    while True:
        local_aircraft, now_ts, messages = _fetch_local()
        local_by_hex = {str(a.get("hex", "")).strip().upper().lstrip("~"): a for a in local_aircraft if a.get("hex")}
        with _lock:
            adsbhub = _state.get("_adsbhub") or {}
        merged = []
        seen_hex = set()
        # Prefer local: add all local first
        for ac in local_aircraft:
            hex_ = str(ac.get("hex", "")).strip().upper().lstrip("~")
            if hex_ and hex_ not in seen_hex:
                seen_hex.add(hex_)
                merged.append(ac)
        # Fill in from ADSBHub where we don't have local
        for hex_, ac in adsbhub.items():
            if hex_ not in seen_hex:
                seen_hex.add(hex_)
                merged.append(ac)
        with _lock:
            _state["aircraft"] = merged
            _state["now"] = now_ts
            _state["messages"] = messages
        time.sleep(POLL_INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.rstrip("/") == "/data/aircraft.json" or self.path.rstrip("/") == "/aircraft.json" or self.path == "/":
            with _lock:
                out = {"aircraft": _state["aircraft"], "now": _state["now"], "messages": _state["messages"]}
            body = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    # Initial fetch so first request has data
    local_aircraft, now_ts, messages = _fetch_local()
    with _lock:
        _state["aircraft"] = local_aircraft
        _state["now"] = now_ts
        _state["messages"] = messages
    if os.environ.get("ADSBHUB_RECEIVE_ENABLED", "").lower() not in ("1", "true", "yes"):
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
    server = HTTPServer(("0.0.0.0", port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
