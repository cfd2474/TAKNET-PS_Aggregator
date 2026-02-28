"""Health history — in-memory ring buffer of CPU/memory/disk + top processes per sample.

When the host runs scripts/health_snapshot_host.py and writes to var/health_history.json
( mounted at /app/var ), we use that for full server view (netbird, readsb, etc.).
Otherwise we use in-container collection (only gunicorn visible).
"""

import json
import os
import time

_history = []
MAX_POINTS = 120  # 1 hour at 30s interval
TOP_N = 5  # top N processes by CPU per sample
HOST_HISTORY_PATH = os.environ.get("HEALTH_HISTORY_FILE", "/app/var/health_history.json")


def _read_host_history():
    """Return list of points from host file, or None if not available."""
    if not os.path.isfile(HOST_HISTORY_PATH):
        return None
    try:
        with open(HOST_HISTORY_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else None
    except (OSError, json.JSONDecodeError):
        return None


def collect_health_snapshot():
    """Append one snapshot: cpu %, memory %, disk %, and top N processes by CPU."""
    try:
        import psutil
    except ImportError:
        return
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
    except Exception:
        return
    top_processes = []
    try:
        procs = []
        for proc in psutil.process_iter(attrs=["pid", "cpu_percent", "cmdline", "name"], ad_value=None):
            try:
                info = proc.info
                if info.get("pid") is None:
                    continue
                cmd = info.get("cmdline") or []
                cmd_str = " ".join(cmd)[:50] if cmd else (info.get("name") or "?")
                procs.append({
                    "pid": info["pid"],
                    "cmd": (cmd_str or "?")[:50],
                    "cpu_percent": round(info.get("cpu_percent") or 0, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs.sort(key=lambda x: x["cpu_percent"], reverse=True)
        top_processes = procs[:TOP_N]
    except Exception:
        pass
    ts = time.time()
    global _history
    _history.append({
        "ts": ts,
        "cpu": round(cpu, 1),
        "memory": round(mem, 1),
        "disk": round(disk, 1),
        "top_processes": top_processes,
    })
    if len(_history) > MAX_POINTS:
        _history = _history[-MAX_POINTS:]


def get_health_history(minutes=60):
    """Return history for the last N minutes (oldest first). Prefer host file when present."""
    cutoff = time.time() - (minutes * 60)
    host = _read_host_history()
    source = host if host else _history
    points = [p for p in source if p.get("ts", 0) >= cutoff]
    return sorted(points, key=lambda x: x["ts"])


def get_host_snapshot():
    """Return the latest snapshot from the host file, or None. Used for health detail (CPU, processes)."""
    host = _read_host_history()
    if not host:
        return None
    points = sorted(host, key=lambda x: x.get("ts", 0))
    return points[-1] if points else None
