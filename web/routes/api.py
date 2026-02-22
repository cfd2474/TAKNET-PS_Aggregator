"""API routes — JSON endpoints for dashboard data."""

import json
import os
import queue
import threading
import time

import psutil
import requests as http_requests
from flask import Blueprint, jsonify, request, Response, stream_with_context

from models import FeederModel, ConnectionModel, ActivityModel, UpdateModel
from services.docker_service import (get_containers, restart_container, get_logs,
                                      get_netbird_client_status, enroll_netbird,
                                      disconnect_netbird, get_client as _get_docker_client)
from services.vpn_service import get_combined_status
from routes.auth_utils import login_required_any, network_admin_required, admin_required

bp = Blueprint("api", __name__, url_prefix="/api")

READSB_HOST = os.environ.get("READSB_HOST", "readsb")
SITE_NAME = os.environ.get("SITE_NAME", "TAKNET-PS Aggregator")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "cfd2474/TAKNET-PS_Aggregator")
INSTALL_DIR = os.environ.get("INSTALL_DIR", "/opt/taknet-aggregator")

_start_time = time.time()

# ── Update streaming state ────────────────────────────────────────────────────
_update_queue = queue.Queue()
_update_running = False


def _run_update():
    """Background thread: clone from GitHub and deploy via docker:cli container (all writes go to host via bind mount)."""
    global _update_running

    def log(msg, log_type="log"):
        for line in str(msg).splitlines():
            line = line.strip()
            if line:
                _update_queue.put({"type": log_type, "msg": line})

    old_ver = "unknown"
    new_ver = "unknown"

    try:
        # Read current version from .env-mounted path
        try:
            old_ver = open(os.path.join(INSTALL_DIR, "VERSION")).read().strip()
        except Exception:
            pass

        client = _get_docker_client()
        if not client:
            log("Docker not available", "error")
            return

        try:
            client.images.pull("docker:cli")
        except Exception:
            pass

        # Shell script that runs entirely inside docker:cli which has INSTALL_DIR
        # bind-mounted from the host — so all file writes land on the host filesystem.
        script = f"""
set -e
echo "Cloning https://github.com/{GITHUB_REPO}.git ..."
TMPDIR=$(mktemp -d)
git clone --depth 1 https://github.com/{GITHUB_REPO}.git $TMPDIR/repo
echo "Clone complete."
NEW_VER=$(cat $TMPDIR/repo/VERSION 2>/dev/null || echo unknown)
echo "Updating to v$NEW_VER ..."
# Copy all files except .git to INSTALL_DIR on the host
cd $TMPDIR/repo
for item in $(ls -A | grep -v '^[.]git$'); do
    cp -r $item {INSTALL_DIR}/
done
rm -rf $TMPDIR
echo "Files updated on host."
echo "Pulling updated images..."
cd {INSTALL_DIR}
docker compose pull 2>&1
echo "Restarting containers..."
docker compose up -d --build 2>&1
echo "DONE:$NEW_VER"
"""

        log("Starting update via docker:cli ...")
        log("(Dashboard will reconnect automatically after containers restart)", "pre_restart")

        import time
        time.sleep(1)  # give SSE a moment to flush pre_restart to browser

        compose_container = client.containers.create(
            "docker:cli",
            command=["sh", "-c", script],
            volumes={
                "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                INSTALL_DIR: {"bind": INSTALL_DIR, "mode": "rw"},
            },
            working_dir=INSTALL_DIR,
        )
        compose_container.start()

        for chunk in compose_container.logs(stream=True, follow=True):
            line = chunk.decode("utf-8", errors="replace").strip()
            if line.startswith("DONE:"):
                new_ver = line.split(":", 1)[1]
            elif line:
                log(line)

        result = compose_container.wait()
        try:
            compose_container.remove()
        except Exception:
            pass
        exit_code = result.get("StatusCode", -1)

        if exit_code == 0:
            try:
                UpdateModel.log_update(old_ver, new_ver, True, "Updated via web UI")
            except Exception:
                pass
            log(f"✓ Update complete: v{old_ver} → v{new_ver}", "done")
        else:
            log(f"docker:cli exited with code {exit_code}", "error")

    except Exception as e:
        log(f"Update error: {e}", "error")
    finally:
        _update_running = False


# ── Status / Overview ────────────────────────────────────────────────────────

@bp.route("/status")
@login_required_any
def status():
    """Dashboard overview data."""
    feeder_stats = FeederModel.get_stats()
    aircraft = _get_aircraft_data()
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
@network_admin_required
def feeders_list():
    """List all feeders with optional filters."""
    status_filter = request.args.get("status", "all")
    conn_type = request.args.get("conn_type", "all")
    feeders = FeederModel.get_all(status_filter=status_filter, conn_type_filter=conn_type)
    stats = FeederModel.get_stats()
    return jsonify({"feeders": feeders, "stats": stats})


@bp.route("/feeders/<int:feeder_id>")
@network_admin_required
def feeder_detail(feeder_id):
    """Single feeder with full details."""
    feeder = FeederModel.get_by_id(feeder_id)
    if not feeder:
        return jsonify({"error": "Feeder not found"}), 404
    connections = ConnectionModel.get_history(feeder_id, limit=20)
    return jsonify({"feeder": feeder, "connections": connections})


@bp.route("/feeders/<int:feeder_id>", methods=["PUT"])
@network_admin_required
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
@admin_required
def feeder_delete(feeder_id):
    """Delete a feeder."""
    FeederModel.delete(feeder_id)
    return jsonify({"success": True})


@bp.route("/feeders/<int:feeder_id>/connections")
@network_admin_required
def feeder_connections(feeder_id):
    """Connection history for a feeder."""
    limit = request.args.get("limit", 50, type=int)
    connections = ConnectionModel.get_history(feeder_id, limit=limit)
    return jsonify({"connections": connections})


# ── Aircraft ─────────────────────────────────────────────────────────────────

@bp.route("/aircraft")
@login_required_any
def aircraft():
    """Current aircraft data from readsb."""
    data = _get_aircraft_data()
    return jsonify(data)


# ── VPN Status ───────────────────────────────────────────────────────────────

@bp.route("/vpn/status")
@admin_required
def vpn_status():
    """Combined VPN status (Tailscale + NetBird)."""
    return jsonify(get_combined_status())


# ── NetBird Client (server enrollment) ───────────────────────────────────────

@bp.route("/netbird/client")
@admin_required
def netbird_client():
    """Get netbird-client container status."""
    return jsonify(get_netbird_client_status())


@bp.route("/netbird/enroll", methods=["POST"])
@admin_required
def netbird_enroll():
    """Enroll this server as a NetBird peer using a setup key."""
    data = request.get_json()
    setup_key = (data or {}).get("setup_key", "").strip()
    if not setup_key:
        return jsonify({"error": "setup_key is required"}), 400

    management_url = os.environ.get("NETBIRD_API_URL", "https://netbird.tak-solutions.com")
    ok, msg = enroll_netbird(setup_key, management_url)
    if ok:
        # Persist the setup key to .env so it survives updates
        _persist_env_var("NB_SETUP_KEY", setup_key)
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 500


@bp.route("/netbird/disconnect", methods=["POST"])
@admin_required
def netbird_disconnect_route():
    """Stop and remove the netbird-client container."""
    ok, msg = disconnect_netbird()
    if ok:
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": False, "error": msg}), 500


# ── Docker ───────────────────────────────────────────────────────────────────

@bp.route("/docker/containers")
@admin_required
def docker_containers():
    """List all TAKNET containers."""
    return jsonify({"containers": get_containers()})


@bp.route("/docker/containers/<name>/restart", methods=["POST"])
@admin_required
def docker_restart(n):
    """Restart a container."""
    if not n.startswith("taknet-"):
        return jsonify({"error": "Invalid container"}), 400
    ok, msg = restart_container(n)
    return jsonify({"success": ok, "message": msg})


@bp.route("/docker/containers/<n>/logs")
@admin_required
def docker_logs(n):
    """Get container logs."""
    if not n.startswith("taknet-"):
        return jsonify({"error": "Invalid container"}), 400
    tail = request.args.get("tail", 100, type=int)
    logs = get_logs(n, tail=tail)
    return jsonify({"logs": logs})


# ── Activity ─────────────────────────────────────────────────────────────────

@bp.route("/activity")
@network_admin_required
def activity():
    """Recent activity log."""
    limit = request.args.get("limit", 20, type=int)
    events = ActivityModel.get_recent(limit=limit)
    return jsonify({"activity": events})


# ── System ───────────────────────────────────────────────────────────────────

@bp.route("/system")
@network_admin_required
def system_info():
    """System resource information."""
    return jsonify(_get_system_info())


# ── Updates ──────────────────────────────────────────────────────────────────

@bp.route("/updates/check")
@admin_required
def updates_check():
    """Check GitHub for latest version."""
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION"
        resp = http_requests.get(url, timeout=10)
        if resp.status_code == 200:
            latest = resp.text.strip()
            current = _get_current_version()
            return jsonify({
                "current": current,
                "latest": latest,
                "update_available": latest != current,
            })
        return jsonify({"error": f"GitHub returned {resp.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/updates/run", methods=["POST"])
@admin_required
def updates_run():
    """Kick off the update process in a background thread."""
    global _update_running
    if _update_running:
        return jsonify({"error": "Update already in progress"}), 409
    _update_running = True
    while not _update_queue.empty():
        try:
            _update_queue.get_nowait()
        except Exception:
            break
    thread = threading.Thread(target=_run_update, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Update started"})


@bp.route("/updates/stream")
@admin_required
def updates_stream():
    """SSE endpoint — streams update log lines to the browser."""
    def generate():
        while True:
            try:
                item = _update_queue.get(timeout=90)
                yield f"data: {json.dumps(item)}\n\n"
                if item.get("type") in ("done", "error"):
                    break
            except Exception:
                yield "data: {\"type\": \"heartbeat\"}\n\n"
    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers=headers)


@bp.route("/updates/releases")
@login_required_any
def updates_releases():
    """Return release notes from RELEASES.json."""
    limit = request.args.get("limit", 6, type=int)
    try:
        for path in ["/app/RELEASES.json",
                     os.path.join(INSTALL_DIR, "RELEASES.json"),
                     os.path.join(os.path.dirname(os.path.dirname(__file__)), "RELEASES.json")]:
            if os.path.exists(path):
                with open(path) as f:
                    releases = json.load(f)
                return jsonify({"releases": releases[:limit]})
        return jsonify({"releases": []})
    except Exception as e:
        return jsonify({"error": str(e), "releases": []}), 500


@bp.route("/updates/history")
@admin_required
def updates_history():
    """Return past update log."""
    limit = request.args.get("limit", 6, type=int)
    history = UpdateModel.get_history(limit)
    return jsonify({"history": history})


def _get_current_version():
    """Read current VERSION file."""
    try:
        vpath = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        if os.path.exists(vpath):
            return open(vpath).read().strip()
    except Exception:
        pass
    return "unknown"


def _persist_env_var(key, value):
    """Write or update a key=value line in the host .env file."""
    env_path = os.path.join(INSTALL_DIR, ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()
            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                    lines[i] = f"{key}={value}\n"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
        else:
            with open(env_path, "w") as f:
                f.write(f"{key}={value}\n")
    except Exception as e:
        print(f"[api] Failed to persist {key} to .env: {e}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_aircraft_count():
    """Get current aircraft count from readsb."""
    data = _get_aircraft_data()
    return data.get("total", 0)


def _get_aircraft_data():
    """Fetch aircraft.json from tar1090 or readsb."""
    # tar1090 serves aircraft.json over HTTP
    try:
        resp = http_requests.get(
            "http://tar1090:80/data/aircraft.json", timeout=3
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
