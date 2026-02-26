"""Dashboard routes."""
from flask import Blueprint, render_template
from routes.auth_utils import network_admin_required

bp = Blueprint("dashboard", __name__)

@bp.route("/")
@bp.route("/dashboard")
@network_admin_required
def index():
    return render_template("dashboard.html")
