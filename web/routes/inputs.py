"""Inputs routes — feeder list and detail pages."""
from flask import Blueprint, render_template, abort
from flask_login import current_user

from models import FeederModel, UserModel, enrich_feeder_mlat_display, user_can_access_feeder
from routes.auth_utils import network_admin_required

bp = Blueprint("inputs", __name__, url_prefix="/inputs")

@bp.route("/")
@bp.route("/feeders")
@network_admin_required
def feeders():
    return render_template("inputs/feeders.html")

@bp.route("/feeder/<int:feeder_id>")
@network_admin_required
def feeder_detail(feeder_id):
    feeder = FeederModel.get_by_id(feeder_id)
    if not feeder:
        abort(404)
    if not user_can_access_feeder(feeder, current_user.username, current_user.role):
        abort(403)
    feeder = enrich_feeder_mlat_display(feeder)
    owner_usernames = []
    if current_user.role == "admin":
        owner_usernames = sorted(
            {
                u["username"]
                for u in UserModel.get_all()
                if (u.get("status") or "active") == "active" and u.get("username")
            }
        )
    return render_template(
        "inputs/feeder_detail.html",
        feeder=feeder,
        is_admin=(current_user.role == "admin"),
        owner_usernames=owner_usernames,
    )
