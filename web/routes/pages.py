"""Page routes — map, stats, outputs, about."""

from flask import Blueprint, render_template

bp = Blueprint("pages", __name__)


@bp.route("/map")
def map_view():
    return render_template("map.html")


@bp.route("/stats")
def stats():
    return render_template("stats.html")


@bp.route("/outputs")
def outputs():
    return render_template("outputs.html")


@bp.route("/about")
def about():
    return render_template("about.html")
