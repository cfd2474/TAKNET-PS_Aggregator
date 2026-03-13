"""Misc page routes."""
from flask import Blueprint, render_template, abort
from flask_login import current_user
from routes.auth_utils import login_required_any, network_admin_required, admin_required
from models import OutputModel

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

@bp.route("/outputs/<int:output_id>/cotproxy")
@network_admin_required
def output_cotproxy(output_id):
    """COTProxy-style transform config page for a CoT output (manual entries + CSV import)."""
    import json
    from nato_icons import get_nato_icons_list
    from ps_air_icons import get_ps_air_icons_list
    output = OutputModel.get_by_id(output_id, int(current_user.id), current_user.role)
    if not output:
        abort(404)
    if output.get("output_type") != "cot":
        abort(404)
    output_config = json.loads(output.get("config") or "{}")
    nato_icons = get_nato_icons_list()
    ps_air_icons = get_ps_air_icons_list()
    return render_template(
        "output_cotproxy.html",
        output=output,
        output_config=output_config,
        nato_icons=nato_icons,
        ps_air_icons=ps_air_icons,
    )

@bp.route("/about")
@login_required_any
def about():
    return render_template("about.html")
