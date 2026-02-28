#!/usr/bin/env python3
"""Collect host-wide CPU, memory, disk and top processes; append to a JSON history file.

Run on the host (e.g. every 30s via systemd timer) so the dashboard can show
real server usage (netbird, readsb, etc.) instead of only container processes.
Writes to var/health_history.json; dashboard container mounts var and reads it.
"""

import json
import os
import sys
import time

MAX_POINTS = 120  # 1 hour at 30s
TOP_N = 15  # top N processes by CPU for server view


def main():
    out_path = os.environ.get("HEALTH_HISTORY_FILE")
    if not out_path and len(sys.argv) > 1:
        out_path = sys.argv[1]
    if not out_path:
        out_path = "/opt/taknet-aggregator/var/health_history.json"
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, mode=0o755, exist_ok=True)

    try:
        import psutil
    except ImportError:
        sys.stderr.write("health_snapshot_host: install psutil on the host (pip3 install psutil or dnf install python3-psutil)\n")
        sys.exit(1)

    try:
        cpu = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()
        mem = vm.percent
        du = psutil.disk_usage("/")
        disk = du.percent
    except Exception:
        sys.exit(2)

    # Process CPU %: psutil returns 0.0 on first call per process; need two samples.
    top_processes = []
    try:
        info_by_pid = {}
        for proc in psutil.process_iter(attrs=["pid", "cmdline", "name", "username"], ad_value=None):
            try:
                info = proc.info
                if info.get("pid") is None:
                    continue
                cmd = info.get("cmdline") or []
                cmd_str = " ".join(cmd)[:80] if cmd else (info.get("name") or "?")
                info_by_pid[info["pid"]] = {
                    "pid": info["pid"],
                    "cmd": (cmd_str or "?")[:80],
                    "username": (info.get("username") or "?"),
                    "cpu_percent": 0.0,
                }
                proc.cpu_percent()  # prime: first call returns 0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(0.2)  # let CPU times accumulate
        for proc in psutil.process_iter(attrs=["pid"], ad_value=None):
            try:
                pid = proc.info.get("pid")
                if pid is not None and pid in info_by_pid:
                    info_by_pid[pid]["cpu_percent"] = round(proc.cpu_percent() or 0, 1)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        procs = sorted(info_by_pid.values(), key=lambda x: x["cpu_percent"], reverse=True)
        top_processes = procs[:TOP_N]
    except Exception:
        pass

    point = {
        "ts": time.time(),
        "cpu": round(cpu, 1),
        "memory": round(mem, 1),
        "disk": round(disk, 1),
        "memory_total_gb": round(vm.total / (1024**3), 1),
        "memory_used_gb": round(vm.used / (1024**3), 1),
        "disk_total_gb": round(du.total / (1024**3), 1),
        "disk_used_gb": round(du.used / (1024**3), 1),
        "top_processes": top_processes,
    }

    history = []
    try:
        with open(out_path, "r") as f:
            history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        history = []
    if not isinstance(history, list):
        history = []

    history.append(point)
    if len(history) > MAX_POINTS:
        history = history[-MAX_POINTS:]

    tmp = out_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f)
    os.replace(tmp, out_path)


if __name__ == "__main__":
    main()
