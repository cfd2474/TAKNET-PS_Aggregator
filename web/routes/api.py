"""API routes — JSON endpoints for dashboard data."""

import json
import os
import queue
import threading
import time

import psutil
import requests as http_requests
from flask import Blueprint, jsonify, request, Response, stream_with_context

from models import FeederModel, ConnectionModel, ActivityModel, UpdateModel, UserModel, OutputModel, CotTransformModel, OutputCotCertModel
from services.docker_service import (get_containers, restart_container, get_logs,
                                      get_netbird_client_status, enroll_netbird,
                                      disconnect_netbird, get_client as _get_docker_client)
from services.vpn_service import get_combined_status
from services.health_snapshot import get_health_history, get_host_snapshot
from routes.auth_utils import login_required_any, network_admin_required, admin_required

bp = Blueprint("api", __name__, url_prefix="/api")

READSB_HOST = os.environ.get("READSB_HOST", "readsb")
SITE_NAME = os.environ.get("SITE_NAME", "TAKNET-PS Aggregator")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "cfd2474/TAKNET-PS_Aggregator")
INSTALL_DIR = os.environ.get("INSTALL_DIR", "/opt/taknet-aggregator")
# Merged aircraft (local + optional ADSBHub) when aircraft-merger is used
AIRCRAFT_JSON_URL = os.environ.get("AIRCRAFT_JSON_URL", "http://tar1090:80/data/aircraft.json")
# Shared volume mount where feeder/merger write connection status (read-only in dashboard)
ADSBHUB_STATUS_PATH = os.environ.get("ADSBHUB_STATUS_PATH", "/app/var/adsbhub-status")

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
echo "Building local images..."
docker compose build dashboard beast-proxy mlat-server api 2>&1
echo "PRE_RESTART"
sleep 3
echo "Restarting containers..."
docker compose up -d 2>&1
echo "DONE:$NEW_VER"
"""

        log("Starting update via docker:cli ...")

        volumes = {
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
            INSTALL_DIR:            {"bind": INSTALL_DIR,            "mode": "rw"},
        }
        compose_container = client.containers.create(
            "docker:cli",
            command=["sh", "-c", script],
            volumes=volumes,
            working_dir=INSTALL_DIR,
        )
        compose_container.start()

        for chunk in compose_container.logs(stream=True, follow=True):
            line = chunk.decode("utf-8", errors="replace").strip()
            if line.startswith("DONE:"):
                new_ver = line.split(":", 1)[1]
            elif line == "PRE_RESTART":
                log("Dashboard will reconnect automatically after containers restart", "pre_restart")
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


def _get_adsbhub_connection_status():
    """Read live feed/receive connection status from shared volume (written by feeder and merger)."""
    feed_enabled = _read_env_bool("ADSBHUB_FEED_ENABLED", False)
    receive_enabled = _read_env_bool("ADSBHUB_RECEIVE_ENABLED", False)
    out = {
        "feed_enabled": feed_enabled,
        "feed_connected": None,
        "feed_updated": None,
        "receive_enabled": receive_enabled,
        "receive_connected": None,
        "receive_updated": None,
    }
    try:
        feed_path = os.path.join(ADSBHUB_STATUS_PATH, "feed.json")
        if os.path.exists(feed_path):
            with open(feed_path, "r") as f:
                data = json.load(f)
            out["feed_connected"] = data.get("connected", False)
            out["feed_updated"] = data.get("updated")
    except Exception:
        pass
    try:
        recv_path = os.path.join(ADSBHUB_STATUS_PATH, "receive.json")
        if os.path.exists(recv_path):
            with open(recv_path, "r") as f:
                data = json.load(f)
            out["receive_connected"] = data.get("connected", False)
            out["receive_updated"] = data.get("updated")
    except Exception:
        pass
    return out


# ── Status / Overview ────────────────────────────────────────────────────────

@bp.route("/status")
@login_required_any
def status():
    """Dashboard overview data."""
    feeder_stats = FeederModel.get_stats()
    aircraft = _get_aircraft_data()
    system = _get_system_info()
    activity = ActivityModel.get_recent(10)
    adsbhub = _get_adsbhub_connection_status()

    return jsonify({
        "site_name": SITE_NAME,
        "feeders": feeder_stats,
        "aircraft": aircraft,
        "system": system,
        "activity": activity,
        "pending_users": UserModel.pending_count(),
        "adsbhub": adsbhub,
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


@bp.route("/feeders/<int:feeder_id>/suggest-name")
@network_admin_required
def feeder_suggest_name(feeder_id):
    """Return a suggested name in CC-ST-City format.
    Uses MLAT GPS coordinates if available, falls back to IP geolocation.
    """
    feeder = FeederModel.get_by_id(feeder_id)
    if not feeder:
        return jsonify({"error": "Feeder not found"}), 404

    lat  = feeder.get("latitude")
    lon  = feeder.get("longitude")
    ip   = feeder.get("ip_address", "")

    def _alpha3(alpha2):
        try:
            import pycountry
            c = pycountry.countries.get(alpha_2=(alpha2 or "").upper())
            return c.alpha_3 if c else (alpha2 or "").upper()
        except Exception:
            return (alpha2 or "").upper()

    def _format_name(country_a2, state, city):
        """Build AAA-FullState-FullCity slug."""
        import re
        def slugify(s):
            return re.sub(r"[^a-z0-9\-]", "", (s or "").lower().replace(" ", "-"))
        parts = [p for p in [slugify(_alpha3(country_a2)), slugify(state), slugify(city)] if p]
        return "-".join(parts)

    # ── Method 1: GPS coordinates via Nominatim reverse geocode ──────────────
    if lat is not None and lon is not None:
        try:
            resp = http_requests.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
                headers={"User-Agent": "TAKNET-PS-Aggregator/1.0"},
                timeout=8,
            )
            if resp.status_code == 200:
                addr = resp.json().get("address", {})
                country_a2 = addr.get("country_code", "")
                state      = (addr.get("state") or addr.get("province") or
                              addr.get("region") or "")
                city       = (addr.get("city") or addr.get("town") or
                              addr.get("village") or addr.get("county") or "")
                name = _format_name(country_a2, state, city)
                if name:
                    return jsonify({"name": name, "source": "gps"})
        except Exception:
            pass  # fall through to IP lookup

    # ── Method 2: IP geolocation via ip-api.com (free, no key) ──────────────
    # Skip private/RFC1918 and NetBird ranges
    import ipaddress as _ipaddress
    try:
        addr_obj = _ipaddress.ip_address(ip)
        if addr_obj.is_private or addr_obj.is_loopback:
            return jsonify({"error": "No GPS coordinates and IP is private — cannot geolocate"}), 422
    except ValueError:
        return jsonify({"error": "Invalid IP address"}), 422

    try:
        resp = http_requests.get(
            f"http://ip-api.com/json/{ip}",
            params={"fields": "countryCode,region,city,status,message"},
            timeout=8,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                name = _format_name(
                    data.get("countryCode", ""),
                    data.get("region", ""),   # full state/province name
                    data.get("city", ""),
                )
                if name:
                    return jsonify({"name": name, "source": "ip"})
            return jsonify({"error": data.get("message", "IP geolocation failed")}), 422
    except Exception as e:
        return jsonify({"error": f"IP geolocation failed: {e}"}), 500

    return jsonify({"error": "Could not determine location"}), 422


@bp.route("/feeders/<int:feeder_id>", methods=["DELETE"])
@admin_required
def feeder_delete(feeder_id):
    """Delete a feeder."""
    FeederModel.delete(feeder_id)
    return jsonify({"success": True})


@bp.route("/feeders/<int:feeder_id>/merge", methods=["POST"])
@admin_required
def feeder_merge(feeder_id):
    """Merge a duplicate feeder record into a target feeder, then delete it.
    Body: { "into": <target_feeder_id> }
    Moves connection history to the target, then deletes this record.
    """
    data = request.get_json(silent=True) or {}
    into_id = data.get("into")
    if not into_id:
        return jsonify({"error": "Missing 'into' feeder id"}), 400
    if int(into_id) == feeder_id:
        return jsonify({"error": "Cannot merge a feeder into itself"}), 400

    from models import get_db
    conn = get_db()
    try:
        # Re-parent connection history
        conn.execute(
            "UPDATE connections SET feeder_id = ? WHERE feeder_id = ?",
            (into_id, feeder_id)
        )
        # Re-parent activity log
        conn.execute(
            "UPDATE activity_log SET feeder_id = ? WHERE feeder_id = ?",
            (into_id, feeder_id)
        )
        # Delete the duplicate
        conn.execute("DELETE FROM feeders WHERE id = ?", (feeder_id,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@bp.route("/feeders/purge-inactive", methods=["POST"])
@admin_required
def feeders_purge_inactive():
    """Delete all non-active feeders (stale, offline)."""
    count = FeederModel.purge_inactive()
    return jsonify({"success": True, "deleted": count})


@bp.route("/feeders/purge-old", methods=["POST"])
@admin_required
def feeders_purge_old():
    """Delete feeders not seen in the last 24 hours."""
    count = FeederModel.purge_old(hours=24)
    return jsonify({"success": True, "deleted": count})


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


@bp.route("/aircraft.json")
@login_required_any
def aircraft_json():
    """Full aircraft.json from merger (for Merged map); avoids nginx /data/ routing."""
    try:
        resp = http_requests.get(AIRCRAFT_JSON_URL, timeout=5)
        if resp.status_code == 200:
            return Response(resp.content, mimetype="application/json")
    except Exception:
        pass
    return jsonify({"aircraft": [], "now": 0, "messages": 0})


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


@bp.route("/docker/restart-all", methods=["POST"])
@admin_required
def docker_restart_all():
    """Restart all TAKNET containers."""
    containers = get_containers()
    output_lines = []
    errors = []
    for c in containers:
        ok, msg = restart_container(c["name"])
        output_lines.append(f"{'✓' if ok else '✗'} {c['short_name']}: {msg}")
        if not ok:
            errors.append(c["short_name"])
    output = "\n".join(output_lines)
    if errors:
        return jsonify({"success": False, "error": f"Failed: {', '.join(errors)}", "output": output})
    return jsonify({"success": True, "output": output})


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
    """Check GitHub for latest version and return release notes for new versions."""
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION"
        # Avoid GitHub CDN cache so we see the real latest
        resp = http_requests.get(url, timeout=10, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        if resp.status_code != 200:
            return jsonify({"error": f"GitHub returned {resp.status_code}"}), 502

        latest = resp.text.strip()
        current = _get_current_version()
        update_available = latest != current

        # Fetch RELEASES.json from GitHub to get notes for unreleased versions
        new_releases = []
        if update_available:
            try:
                rurl = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/RELEASES.json"
                rresp = http_requests.get(rurl, timeout=10, headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
                if rresp.status_code == 200:
                    all_releases = rresp.json()
                    # Return entries that are newer than current (appear before current in list)
                    for rel in all_releases:
                        if rel.get("version") == current:
                            break
                        new_releases.append(rel)
            except Exception:
                pass

        response = jsonify({
            "current": current,
            "latest": latest,
            "update_available": update_available,
            "new_releases": new_releases,
        })
        # Prevent browser from caching so "Check Now" and page load always get fresh comparison
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response
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
                # After pre_restart, pad with comments to force gunicorn to flush
                # the chunk before the container restarts and kills the connection.
                if item.get("type") == "pre_restart":
                    for _ in range(8):
                        yield ": keepalive\n\n"
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


def _env_value_escape(value):
    """Escape a value for .env: wrap in single quotes if needed so $ and newlines don't break parsing."""
    if not value:
        return value
    if "\n" in value or "\r" in value or "=" in value or "$" in value or " " in value or "#" in value or "'" in value:
        # Single-quoted: only ' must be escaped as '"'"' for shell-style .env
        escaped = value.replace("'", "'\"'\"'")
        return f"'{escaped}'"
    return value


def _persist_env_var(key, value):
    """Write or update a key=value line in the host .env file. Value is escaped for .env safety."""
    env_path = os.path.join(INSTALL_DIR, ".env")
    safe_value = _env_value_escape(value)
    try:
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                lines = f.readlines()
            found = False
            for i, line in enumerate(lines):
                if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                    lines[i] = f"{key}={safe_value}\n"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={safe_value}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
        else:
            with open(env_path, "w") as f:
                f.write(f"{key}={safe_value}\n")
    except Exception as e:
        print(f"[api] Failed to persist {key} to .env: {e}")


def _read_env_bool(key, default=False):
    """Read a key from the host .env and return True only if value is 'true' (case-insensitive)."""
    env_path = os.path.join(INSTALL_DIR, ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'").lower()
                        return val == "true"
    except Exception:
        pass
    return default


def _read_env_value(key, default=""):
    """Read a key from the host .env and return the raw value (stripping surrounding quotes)."""
    env_path = os.path.join(INSTALL_DIR, ".env")
    try:
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
                        val = line.split("=", 1)[1].strip()
                        if len(val) >= 2 and val.startswith("'") and val.endswith("'"):
                            val = val[1:-1].replace("'\"'\"'", "'")
                        elif len(val) >= 2 and val.startswith('"') and val.endswith('"'):
                            val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                        return val
    except Exception:
        pass
    return default


# ── ADSBHub settings (Config → Services) ──────────────────────────────────────

@bp.route("/settings/adsbhub", methods=["GET"])
@admin_required
def get_adsbhub_settings():
    """Return current ADSBHub feed/receive flags and client key from .env."""
    return jsonify({
        "feed_enabled": _read_env_bool("ADSBHUB_FEED_ENABLED", False),
        "receive_enabled": _read_env_bool("ADSBHUB_RECEIVE_ENABLED", False),
        "client_key": _read_env_value("ADSBHUB_CLIENT_KEY", ""),
    })


def _restart_adsbhub_containers_background():
    """Run container restarts in background so the HTTP request can return immediately."""
    for name in ("taknet-adsbhub-feeder", "taknet-aircraft-merger"):
        try:
            restart_container(name)
        except Exception as e:
            print(f"[api] Background restart {name}: {e}")


def _write_receive_enabled_to_volume(enabled):
    """Write receive_enabled to shared volume so aircraft-merger drops ADSBHub data immediately when disabled."""
    try:
        path = os.path.join(ADSBHUB_STATUS_PATH, "receive_enabled")
        with open(path, "w") as f:
            f.write("true" if enabled else "false")
    except Exception as e:
        print(f"[api] Write receive_enabled: {e}")


@bp.route("/settings/adsbhub", methods=["POST"])
@admin_required
def set_adsbhub_settings():
    """Update ADSBHub flags and client key in .env; write receive_enabled to shared volume; restart adsbhub-feeder and aircraft-merger in background."""
    data = request.get_json() or {}
    feed = data.get("feed_enabled", False)
    receive = data.get("receive_enabled", False)
    client_key = (data.get("client_key") or "").strip()
    _persist_env_var("ADSBHUB_FEED_ENABLED", "true" if feed else "false")
    _persist_env_var("ADSBHUB_RECEIVE_ENABLED", "true" if receive else "false")
    _persist_env_var("ADSBHUB_CLIENT_KEY", client_key)
    _write_receive_enabled_to_volume(receive)
    # Restart containers in background so we don't timeout (restarts can take 30+ s each)
    thread = threading.Thread(target=_restart_adsbhub_containers_background, daemon=True)
    thread.start()
    return jsonify({
        "success": True,
        "message": "ADSBHub settings saved. Services are restarting in the background (refresh containers in a moment).",
    })


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_aircraft_count():
    """Get current aircraft count from readsb."""
    data = _get_aircraft_data()
    return data.get("total", 0)


def _get_aircraft_data():
    """Fetch aircraft.json (merged local + ADSBHub when merger is used)."""
    try:
        resp = http_requests.get(AIRCRAFT_JSON_URL, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            aircraft = data.get("aircraft", [])
            with_pos = [a for a in aircraft if "lat" in a and "lon" in a]
            direct = sum(1 for a in aircraft if (a.get("source") or "").lower() != "adsbhub")
            network = sum(1 for a in aircraft if (a.get("source") or "").lower() == "adsbhub")
            return {
                "total": len(aircraft),
                "with_position": len(with_pos),
                "direct": direct,
                "network": network,
                "messages": data.get("messages", 0),
            }
    except Exception:
        pass

    return {"total": 0, "with_position": 0, "direct": 0, "network": 0, "messages": 0}


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


def _get_health_detail():
    """Detailed health for System Health page. Uses host snapshot when available (full server view)."""
    feeder_stats = FeederModel.get_stats()
    host = get_host_snapshot()
    if host:
        base = _get_system_info()
        base["cpu_percent"] = host.get("cpu", 0)
        base["memory"] = {
            "total_gb": host.get("memory_total_gb", 0),
            "used_gb": host.get("memory_used_gb", 0),
            "percent": host.get("memory", 0),
        }
        base["disk"] = {
            "total_gb": host.get("disk_total_gb", 0),
            "used_gb": host.get("disk_used_gb", 0),
            "percent": host.get("disk", 0),
        }
        base["cpu_per_core"] = []
        base["memory_breakdown"] = {}
        base["disk_partitions"] = []
        procs = host.get("top_processes") or []
        base["processes"] = [
            {
                "pid": p.get("pid"),
                "username": p.get("username", "—"),
                "cpu_percent": p.get("cpu_percent", 0),
                "memory_percent": p.get("memory_percent", 0),
                "rss_mb": p.get("rss_mb", 0),
                "status": (p.get("status") or "—")[:12],
                "cmdline": (p.get("cmd") or "?")[:80],
            }
            for p in procs[:50]
        ]
        base["from_host"] = True
        base["feeder_total"] = feeder_stats.get("total", 0)
        base["feeder_active"] = feeder_stats.get("active", 0)
        base["feeder_breakdown"] = feeder_stats.get("breakdown") or []
        return base

    vm = psutil.virtual_memory()
    try:
        cpu_per_core = psutil.cpu_percent(interval=0.15, percpu=True)
    except Exception:
        cpu_per_core = []
    memory_breakdown = {
        "total_gb": round(vm.total / (1024**3), 2),
        "used_gb": round(vm.used / (1024**3), 2),
        "available_gb": round(vm.available / (1024**3), 2),
        "percent": vm.percent,
        "cached_gb": round((getattr(vm, "cached", 0) or 0) / (1024**3), 2),
        "buffers_gb": round((getattr(vm, "buffers", 0) or 0) / (1024**3), 2),
    }
    disk_partitions = []
    for p in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(p.mountpoint)
            disk_partitions.append({
                "mount": p.mountpoint,
                "device": p.device,
                "total_gb": round(usage.total / (1024**3), 1),
                "used_gb": round(usage.used / (1024**3), 1),
                "free_gb": round(usage.free / (1024**3), 1),
                "percent": usage.percent,
            })
        except (PermissionError, OSError):
            continue
    processes = []
    try:
        for proc in psutil.process_iter(attrs=["pid", "username", "cpu_percent", "memory_percent", "memory_info", "status", "cmdline"], ad_value=None):
            try:
                info = proc.info
                if info.get("pid") is None:
                    continue
                cmd = info.get("cmdline") or []
                cmd_str = " ".join(cmd)[:80] if cmd else (proc.name() if hasattr(proc, "name") else "")
                rss_mb = 0
                if info.get("memory_info"):
                    rss_mb = round(info["memory_info"].rss / (1024 * 1024), 1)
                processes.append({
                    "pid": info["pid"],
                    "username": info.get("username") or "?",
                    "cpu_percent": round(info.get("cpu_percent") or 0, 1),
                    "memory_percent": round(info.get("memory_percent") or 0, 1),
                    "rss_mb": rss_mb,
                    "status": (info.get("status") or "?")[:12],
                    "cmdline": (cmd_str or "?")[:80],
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    processes.sort(key=lambda x: (x["cpu_percent"], x["memory_percent"]), reverse=True)
    processes = processes[:50]
    base = _get_system_info()
    base["cpu_per_core"] = cpu_per_core
    base["memory_breakdown"] = memory_breakdown
    base["disk_partitions"] = disk_partitions
    base["processes"] = processes
    base["from_host"] = False
    base["feeder_total"] = feeder_stats.get("total", 0)
    base["feeder_active"] = feeder_stats.get("active", 0)
    base["feeder_breakdown"] = feeder_stats.get("breakdown") or []
    return base


@bp.route("/health/detail")
@admin_required
def health_detail():
    """Detailed system health for the System Health page (admin only)."""
    return jsonify(_get_health_detail())


@bp.route("/health/history")
@admin_required
def health_history():
    """CPU/memory/disk history with top processes per sample (admin only). For correlating spikes with processes."""
    minutes = request.args.get("minutes", 60, type=int)
    minutes = min(max(minutes, 5), 120)
    points = get_health_history(minutes=minutes)
    return jsonify({"points": points})


# ── Outputs ───────────────────────────────────────────────────────────────────

@bp.route("/outputs")
@login_required_any
def outputs_list():
    from flask_login import current_user
    from models import OutputKeyModel
    items = OutputModel.get_for_user(current_user.id, current_user.role)
    for item in items:
        if item.get("mode") == "api":
            item["key_meta"] = OutputKeyModel.get_for_output(item["id"])
    return jsonify({"outputs": items})


@bp.route("/outputs/<int:output_id>")
@network_admin_required
def output_get(output_id):
    from flask_login import current_user
    item = OutputModel.get_by_id(output_id, current_user.id, current_user.role)
    if item is None:
        return jsonify({"error": "Not found or access denied"}), 404
    from models import OutputKeyModel
    item["key_meta"] = OutputKeyModel.get_for_output(output_id)
    return jsonify({"output": item})


@bp.route("/outputs", methods=["POST"])
@network_admin_required
def output_create():
    from flask_login import current_user
    from models import OutputKeyModel
    data = request.get_json(silent=True) or {}
    name        = (data.get("name") or "").strip()
    output_type = (data.get("output_type") or "").strip()
    mode        = data.get("mode", "api")
    key_type    = data.get("key_type", "single_use")
    if not name or not output_type:
        return jsonify({"error": "name and output_type are required"}), 400
    if output_type not in ("json", "beast_raw", "cot"):
        return jsonify({"error": "output_type must be 'json', 'beast_raw', or 'cot'"}), 400
    if mode not in ("api", "push"):
        return jsonify({"error": "mode must be 'api' or 'push'"}), 400
    if key_type not in ("single_use", "durable"):
        return jsonify({"error": "key_type must be 'single_use' or 'durable'"}), 400
    output_format = (data.get("output_format") or ("cot" if output_type == "cot" else "as_is")).strip()
    if output_format not in ("as_is", "cot"):
        output_format = "cot" if output_type == "cot" else "as_is"
    use_cotproxy = bool(data.get("use_cotproxy")) if output_type == "cot" else False
    import json as _json
    config = _json.dumps(data.get("config") or {})
    output_id = OutputModel.create(
        name=name,
        output_type=output_type,
        mode=mode,
        config=config,
        created_by=current_user.id,
        notes=data.get("notes"),
        output_format=output_format,
        use_cotproxy=use_cotproxy,
    )
    raw_key = None
    if mode == "api":
        raw_key = OutputKeyModel.generate(output_id, key_type=key_type)
    return jsonify({"success": True, "id": output_id, "api_key": raw_key}), 201


@bp.route("/outputs/<int:output_id>", methods=["PUT"])
@network_admin_required
def output_update(output_id):
    from flask_login import current_user
    import json as _json
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    data = request.get_json(silent=True) or {}
    if "use_cotproxy" in data:
        data["use_cotproxy"] = 1 if data["use_cotproxy"] else 0
    if "config" in data and isinstance(data["config"], dict):
        data["config"] = _json.dumps(data["config"])
    OutputModel.update(output_id, data)
    return jsonify({"success": True})


@bp.route("/outputs/<int:output_id>", methods=["DELETE"])
@network_admin_required
def output_delete(output_id):
    from flask_login import current_user
    from models import signal_drop_output
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    signal_drop_output(output_id)  # drop any active beast connection first
    OutputModel.delete(output_id)
    return jsonify({"success": True})


@bp.route("/outputs/<int:output_id>/regenerate-key", methods=["POST"])
@network_admin_required
def output_regenerate_key(output_id):
    from flask_login import current_user
    from models import OutputKeyModel, signal_drop_output
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    item = OutputModel.get_by_id(output_id, current_user.id, current_user.role)
    if not item:
        return jsonify({"error": "Not found"}), 404
    if item.get("mode") != "api":
        return jsonify({"error": "Output is not in API mode"}), 400
    # Signal beast-proxy to drop any active connection using the old key
    signal_drop_output(output_id)
    # Preserve existing key_type when regenerating
    existing_key = OutputKeyModel.get_for_output(output_id)
    key_type = (existing_key or {}).get("key_type", "single_use")
    raw_key = OutputKeyModel.generate(output_id, key_type=key_type)
    return jsonify({"success": True, "api_key": raw_key})


# ── COTProxy-style transforms (per output) ───────────────────────────────────

@bp.route("/ps-air-icons")
@network_admin_required
def ps_air_icons_list():
    """Return list of PS Air Icons (Public Safety Air v8) for COTProxy icon picker/reference."""
    try:
        from ps_air_icons import get_ps_air_icons_list
        icons = get_ps_air_icons_list()
        return jsonify({"icons": icons, "iconset_uid": "66f14976-4b62-4023-8edb-d8d2ebeaa336"})
    except Exception as e:
        return jsonify({"icons": [], "error": str(e)})


@bp.route("/outputs/<int:output_id>/cot-transforms")
@network_admin_required
def cot_transforms_list(output_id):
    from flask_login import current_user
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    items = CotTransformModel.get_all(output_id)
    return jsonify({"transforms": items})


@bp.route("/outputs/cot-transforms/template")
@network_admin_required
def cot_transforms_template():
    """Download blank CSV template for bulk import (DOMAIN,AGENCY,REG,CALLSIGN,TYPE,MODEL,HEX,COT,ICON)."""
    import io
    from flask import send_file
    buf = io.BytesIO()
    buf.write(b"\xef\xbb\xbf")  # UTF-8 BOM for Excel
    buf.write(",".join(CotTransformModel.CSV_HEADERS).encode("utf-8") + b"\n")
    buf.seek(0)
    return send_file(
        buf,
        mimetype="text/csv",
        as_attachment=True,
        download_name="cot_transforms_template.csv",
    )


@bp.route("/outputs/<int:output_id>/cot-transforms", methods=["POST"])
@network_admin_required
def cot_transform_create(output_id):
    from flask_login import current_user
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    data = request.get_json(silent=True) or {}
    try:
        tid = CotTransformModel.create(output_id, data)
        return jsonify({"success": True, "id": tid}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/outputs/<int:output_id>/cot-transforms/<int:transform_id>", methods=["PUT"])
@network_admin_required
def cot_transform_update(output_id, transform_id):
    from flask_login import current_user
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    data = request.get_json(silent=True) or {}
    if CotTransformModel.get_by_id(transform_id, output_id) is None:
        return jsonify({"error": "Not found"}), 404
    ok = CotTransformModel.update(transform_id, output_id, data)
    return jsonify({"success": ok})


@bp.route("/outputs/<int:output_id>/cot-transforms/<int:transform_id>", methods=["DELETE"])
@network_admin_required
def cot_transform_delete(output_id, transform_id):
    from flask_login import current_user
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    CotTransformModel.delete(transform_id, output_id)
    return jsonify({"success": True})


@bp.route("/outputs/<int:output_id>/cot-transforms/bulk-delete", methods=["POST"])
@network_admin_required
def cot_transforms_bulk_delete(output_id):
    from flask_login import current_user
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "ids array required"}), 400
    for tid in ids:
        try:
            CotTransformModel.delete(int(tid), output_id)
        except (TypeError, ValueError):
            pass
    return jsonify({"success": True, "deleted": len(ids)})


# ── CoT push TLS certificates (owner-only; never return cert content) ─────────

def _cot_cert_owner_only(output_id):
    """Ensure current user is the output creator. Certs are never visible to admins or other users."""
    from flask_login import current_user
    output = OutputModel.get_by_id(output_id, int(current_user.id), current_user.role)
    if not output:
        return None, 404
    if output.get("output_type") != "cot":
        return None, 404
    if int(output["created_by"]) != int(current_user.id):
        return None, 403
    return output, None


@bp.route("/outputs/<int:output_id>/cot-certs/status")
@network_admin_required
def cot_certs_status(output_id):
    """Return only { has_cert: bool }. Only the output creator can call this."""
    from flask_login import current_user
    output, err = _cot_cert_owner_only(output_id)
    if err:
        return jsonify({"error": "Not found or access denied"}), err
    return jsonify({"has_cert": OutputCotCertModel.has_cert(output_id)})


@bp.route("/outputs/<int:output_id>/cot-certs", methods=["POST"])
@network_admin_required
def cot_certs_upload(output_id):
    """Upload client cert + key (and optional CA) for CoT push TLS. Owner only. Stored encrypted; never returned."""
    from flask_login import current_user
    output, err = _cot_cert_owner_only(output_id)
    if err:
        return jsonify({"error": "Not found or access denied"}), err
    cert_pem = key_pem = ca_pem = None
    if request.content_type and "multipart/form-data" in request.content_type:
        cert_file = request.files.get("cert")
        key_file = request.files.get("key")
        ca_file = request.files.get("ca")
        if cert_file and cert_file.filename:
            cert_pem = cert_file.read().decode("utf-8", errors="replace")
        if key_file and key_file.filename:
            key_pem = key_file.read().decode("utf-8", errors="replace")
        if ca_file and ca_file.filename:
            ca_pem = ca_file.read().decode("utf-8", errors="replace")
    if not cert_pem or not cert_pem.strip():
        return jsonify({"error": "Client certificate (cert) file is required"}), 400
    if not key_pem or not key_pem.strip():
        return jsonify({"error": "Private key (key) file is required"}), 400
    try:
        OutputCotCertModel.set(output_id, cert_pem.strip(), key_pem.strip(), (ca_pem or "").strip() or None)
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/outputs/<int:output_id>/cot-transforms/import", methods=["POST"])
@network_admin_required
def cot_transforms_import(output_id):
    from flask_login import current_user
    if not OutputModel.can_modify(output_id, current_user.id, current_user.role):
        return jsonify({"error": "Access denied"}), 403
    csv_text = None
    if request.content_type and "multipart/form-data" in request.content_type and "file" in request.files:
        f = request.files.get("file")
        if f and f.filename:
            csv_text = f.read().decode("utf-8", errors="replace")
    if csv_text is None:
        data = request.get_json(silent=True) or {}
        csv_text = data.get("csv") or request.get_data(as_text=True)
    if not csv_text or not csv_text.strip():
        return jsonify({"error": "No CSV data (send file or JSON { csv: \"...\" } or raw body)"}), 400
    # Single transaction in import_from_csv to avoid 504 on large CSVs; if behind nginx, consider proxy_read_timeout.
    inserted, errors = CotTransformModel.import_from_csv(output_id, csv_text)
    return jsonify({"success": True, "inserted": inserted, "errors": errors})


@bp.route("/outputs/stream/<string:raw_key>")
def output_json_stream(raw_key):
    """Public JSON aircraft stream — authenticated by API key in URL."""
    from models import OutputKeyModel
    output = OutputKeyModel.validate(raw_key)
    if not output:
        return jsonify({"error": "Invalid or inactive API key"}), 401
    if output.get("output_type") != "json":
        return jsonify({"error": "This key is for a beast_raw output, not JSON"}), 400

    # Output config: include_network_adsb False => only direct feeder traffic (exclude source=adsbhub)
    config = {}
    try:
        config = json.loads(output.get("config") or "{}")
    except (TypeError, ValueError):
        pass
    include_network = config.get("include_network_adsb", True)

    try:
        resp = http_requests.get(AIRCRAFT_JSON_URL, timeout=5)
        if resp.status_code != 200:
            return jsonify({"error": "Upstream data unavailable"}), 503
        if include_network:
            return Response(resp.content, content_type="application/json",
                            headers={"Access-Control-Allow-Origin": "*"})
        data = resp.json()
        aircraft = data.get("aircraft", [])
        direct_only = [a for a in aircraft if (a.get("source") or "").lower() != "adsbhub"]
        out = {"aircraft": direct_only, "now": data.get("now"), "messages": data.get("messages", 0)}
        body = json.dumps(out).encode()
        return Response(body, content_type="application/json",
                        headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        return jsonify({"error": str(e)}), 503


# ── Feeder Proxy (nginx handles actual proxying, Flask handles auth) ───────────

import re as _re
import urllib.parse as _urlparse

def _feeder_proxy_url(feeder, view_type):
    """Build the /feeder-view/<ip>:<port>/... URL for a given feeder and view type."""
    if view_type == "map":
        raw = (feeder.get("tar1090_url") or "").rstrip("/")
    elif view_type == "graphs":
        raw = (feeder.get("graphs1090_url") or "").rstrip("/")
    else:
        return None
    if not raw:
        return None
    parsed = _urlparse.urlparse(raw)
    host = parsed.hostname or feeder.get("ip_address", "")
    port = parsed.port or 8080
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return f"/feeder-view/{host}:{port}{path}"


@bp.route("/feeder-view-auth")
def feeder_view_auth():
    """nginx auth_request sub-endpoint. Returns 200 if user has a valid session,
    401 otherwise. NetBird IPs are only routable within the mesh so no further
    IP validation is required."""
    from flask_login import current_user
    if not current_user.is_authenticated:
        return Response("Unauthorized", status=401)
    return Response("OK", status=200)


@bp.route("/feeder-proxy/<int:feeder_id>/<view_type>")
@login_required_any
def feeder_proxy(feeder_id, view_type):
    """Return a redirect to the nginx-proxied feeder URL."""
    feeder = FeederModel.get_by_id(feeder_id)
    if not feeder:
        return "Feeder not found", 404

    proxy_url = _feeder_proxy_url(feeder, view_type)
    if not proxy_url:
        return f"No {'map' if view_type == 'map' else 'stats'} URL configured for this feeder", 404

    from flask import redirect
    return redirect(proxy_url)
