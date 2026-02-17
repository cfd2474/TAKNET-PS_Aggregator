"""API routes — JSON endpoints for dashboard data."""

import os
import time

import psutil
import requests as http_requests
from flask import Blueprint, jsonify, request

from models import FeederModel, ConnectionModel, ActivityModel
from services.docker_service import get_containers, restart_container, get_logs
from services.vpn_service import get_combined_status

bp = Blueprint("api", __name__, url_prefix="/api")

READSB_HOST = os.environ.get("READSB_HOST", "readsb")
SITE_NAME = os.environ.get("SITE_NAME", "TAKNET-PS Aggregator")

_start_time = time.time()


# ── Status / Overview ────────────────────────────────────────────────────────

@bp.route("/status")
def status():
    """Dashboard overview data."""
    feeder_stats = FeederModel.get_stats()
    aircraft = _get_aircraft_count()
    system = _get_system_info()
    activity = ActivityModel.get_recent(10)

    return jsonify({
        "site_name": SITE_NAME,
        "feeders": feeder_stats,
        "aircraft": aircraft,
        "system": system,
        "activity": activity,
    })


# ── Feeders ──────────────────────────────────────────────────────────────────

@bp.route("/feeders")
def feeders_list():
    """List all feeders with optional filters."""
    status_filter = request.args.get("status", "all")
    conn_type = request.args.get("conn_type", "all")
    feeders = FeederModel.get_all(status_filter=status_filter, conn_type_filter=conn_type)
    stats = FeederModel.get_stats()
    return jsonify({"feeders": feeders, "stats": stats})


@bp.route("/feeders/<int:feeder_id>")
def feeder_detail(feeder_id):
    """Single feeder with full details."""
    feeder = FeederModel.get_by_id(feeder_id)
    if not feeder:
        return jsonify({"error": "Feeder not found"}), 404
    connections = ConnectionModel.get_history(feeder_id, limit=20)
    return jsonify({"feeder": feeder, "connections": connections})


@bp.route("/feeders/<int:feeder_id>", methods=["PUT"])
def feeder_update(feeder_id):
    """Update feeder metadata."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    ok = FeederModel.update(feeder_id, data)
    if ok:
        return jsonify({"success": True})
    return jsonify({"error": "Update failed"}), 400


@bp.route("/feeders/<int:feeder_id>", methods=["DELETE"])
def feeder_delete(feeder_id):
    """Delete a feeder."""
    FeederModel.delete(feeder_id)
    return jsonify({"success": True})


@bp.route("/feeders/<int:feeder_id>/connections")
def feeder_connections(feeder_id):
    """Connection history for a feeder."""
    limit = request.args.get("limit", 50, type=int)
    connections = ConnectionModel.get_history(feeder_id, limit=limit)
    return jsonify({"connections": connections})


# ── Aircraft ─────────────────────────────────────────────────────────────────

@bp.route("/aircraft")
def aircraft():
    """Current aircraft data from readsb."""
    data = _get_aircraft_data()
    return jsonify(data)


# ── VPN Status ───────────────────────────────────────────────────────────────

@bp.route("/vpn/status")
def vpn_status():
    """Combined VPN status (Tailscale + NetBird)."""
    return jsonify(get_combined_status())


# ── Docker ───────────────────────────────────────────────────────────────────

@bp.route("/docker/containers")
def docker_containers():
    """List all TAKNET containers."""
    return jsonify({"containers": get_containers()})


@bp.route("/docker/containers/<name>/restart", methods=["POST"])
def docker_restart(n):
    """Restart a container."""
    if not n.startswith("taknet-"):
        return jsonify({"error": "Invalid container"}), 400
    ok, msg = restart_container(n)
    return jsonify({"success": ok, "message": msg})


@bp.route("/docker/containers/<name>/logs")
def docker_logs(n):
    """Get container logs."""
    if not n.startswith("taknet-"):
        return jsonify({"error": "Invalid container"}), 400
    tail = request.args.get("tail", 100, type=int)
    logs = get_logs(n, tail=tail)
    return jsonify({"logs": logs})


# ── Activity ─────────────────────────────────────────────────────────────────

@bp.route("/activity")
def activity():
    """Recent activity log."""
    limit = request.args.get("limit", 20, type=int)
    events = ActivityModel.get_recent(limit=limit)
    return jsonify({"activity": events})


# ── System ───────────────────────────────────────────────────────────────────

@bp.route("/system")
def system_info():
    """System resource information."""
    return jsonify(_get_system_info())


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_aircraft_count():
    """Get current aircraft count from readsb."""
    data = _get_aircraft_data()
    return data.get("total", 0)


def _get_aircraft_data():
    """Fetch aircraft.json from readsb."""
    try:
        resp = http_requests.get(
            f"http://{READSB_HOST}:30005/data/aircraft.json", timeout=3
        )
        if resp.status_code == 200:
            data = resp.json()
            aircraft = data.get("aircraft", [])
            with_pos = [a for a in aircraft if "lat" in a and "lon" in a]
            return {
                "total": len(aircraft),
                "with_position": len(with_pos),
                "messages": data.get("messages", 0),
            }
    except Exception:
        pass

    # Fallback: try tar1090 path
    try:
        resp = http_requests.get(
            f"http://tar1090:80/data/aircraft.json", timeout=3
        )
        if resp.status_code == 200:
            data = resp.json()
            aircraft = data.get("aircraft", [])
            with_pos = [a for a in aircraft if "lat" in a and "lon" in a]
            return {
                "total": len(aircraft),
                "with_position": len(with_pos),
                "messages": data.get("messages", 0),
            }
    except Exception:
        pass

    return {"total": 0, "with_position": 0, "messages": 0}


def _get_system_info():
    """Get system resource usage."""
    uptime_seconds = int(time.time() - psutil.boot_time())
    app_uptime = int(time.time() - _start_time)

    return {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory": {
            "total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
            "used_gb": round(psutil.virtual_memory().used / (1024**3), 1),
            "percent": psutil.virtual_memory().percent,
        },
        "disk": {
            "total_gb": round(psutil.disk_usage("/").total / (1024**3), 1),
            "used_gb": round(psutil.disk_usage("/").used / (1024**3), 1),
            "percent": psutil.disk_usage("/").percent,
        },
        "uptime_seconds": uptime_seconds,
        "app_uptime_seconds": app_uptime,
    }
