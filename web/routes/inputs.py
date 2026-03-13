"""Inputs routes — feeder list and detail pages."""
from flask import Blueprint, render_template, abort
from models import FeederModel, enrich_feeder_mlat_display
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
    feeder = enrich_feeder_mlat_display(feeder)
    return render_template("inputs/feeder_detail.html", feeder=feeder)
