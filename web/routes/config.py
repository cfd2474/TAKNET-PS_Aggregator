"""Config routes — VPN, services, updates, users pages."""
from flask import Blueprint, render_template, request, jsonify, abort
from flask_login import current_user
from models import UserModel
from routes.auth_utils import admin_required, network_admin_required

bp = Blueprint("config", __name__, url_prefix="/config")

@bp.route("/")
@admin_required
def index():
    return render_template("config/config.html")

@bp.route("/vpn")
@admin_required
def vpn():
    return render_template("config/vpn.html")

@bp.route("/services")
@admin_required
def services():
    return render_template("config/services.html")

@bp.route("/updates")
@admin_required
def updates():
    return render_template("config/updates.html")

@bp.route("/users")
@admin_required
def users():
    all_users = UserModel.get_all()
    return render_template("config/users.html", users=all_users, roles=UserModel.ROLES)


# ── User management API (admin only) ─────────────────────────────────────────

@bp.route("/users/create", methods=["POST"])
@admin_required
def users_create():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    role = data.get("role", "viewer")

    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    ok, msg = UserModel.create(username, password, role)
    if ok:
        return jsonify({"success": True})
    return jsonify({"error": msg}), 400


@bp.route("/users/<int:user_id>/role", methods=["POST"])
@admin_required
def users_update_role(user_id):
    # Prevent admin from removing their own admin role
    if user_id == int(current_user.id):
        return jsonify({"error": "Cannot change your own role"}), 400
    data = request.get_json() or {}
    role = data.get("role", "")
    ok, msg = UserModel.update_role(user_id, role)
    if ok:
        return jsonify({"success": True})
    return jsonify({"error": msg}), 400


@bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def users_reset_password(user_id):
    data = request.get_json() or {}
    password = data.get("password", "").strip()
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    UserModel.update_password(user_id, password)
    return jsonify({"success": True})


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def users_delete(user_id):
    if user_id == int(current_user.id):
        return jsonify({"error": "Cannot delete your own account"}), 400
    UserModel.delete(user_id)
    return jsonify({"success": True})
