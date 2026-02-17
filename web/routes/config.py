"""Config routes — VPN, services, updates pages."""

from flask import Blueprint, render_template

bp = Blueprint("config", __name__, url_prefix="/config")


@bp.route("/")
def index():
    return render_template("config/config.html")


@bp.route("/vpn")
def vpn():
    return render_template("config/vpn.html")


@bp.route("/services")
def services():
    return render_template("config/services.html")


@bp.route("/updates")
def updates():
    return render_template("config/updates.html")
