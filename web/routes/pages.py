"""Misc page routes."""
from flask import Blueprint, render_template
from routes.auth_utils import login_required_any, network_admin_required, admin_required

bp = Blueprint("pages", __name__)

@bp.route("/map")
@login_required_any
def map_page():
    return render_template("map.html")

@bp.route("/stats")
@network_admin_required
def stats():
    return render_template("stats.html")

@bp.route("/outputs")
@network_admin_required
def outputs():
    return render_template("outputs.html")

@bp.route("/about")
@admin_required
def about():
    return render_template("about.html")
