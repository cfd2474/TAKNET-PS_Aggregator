"""Dashboard routes — landing page with overview stats."""

from flask import Blueprint, render_template

bp = Blueprint("dashboard", __name__)


@bp.route("/")
@bp.route("/dashboard")
def index():
    return render_template("dashboard.html")
