"""Health history — in-memory ring buffer of CPU/memory/disk + top processes per sample.

Used to correlate CPU spikes with offending processes. Collector runs every 30s;
history keeps last 120 points (1 hour). No DB; resets on restart.
"""

import time

_history = []
MAX_POINTS = 120  # 1 hour at 30s interval
TOP_N = 5  # top N processes by CPU per sample


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
    """Return history for the last N minutes (oldest first for chart)."""
    cutoff = time.time() - (minutes * 60)
    points = [p for p in _history if p["ts"] >= cutoff]
    return sorted(points, key=lambda x: x["ts"])
