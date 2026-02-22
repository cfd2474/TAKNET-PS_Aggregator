"""Auth routes — login, logout, change password."""

from flask import Blueprint, render_template, redirect, url_for, request, flash, abort
from flask_login import login_user, logout_user, login_required, current_user

from models import UserModel
from app import AuthUser

bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return _role_home()

    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = UserModel.verify_password(username, password)
        if user:
            login_user(AuthUser(user), remember=True)
            next_url = request.args.get("next")
            return redirect(next_url or _role_home_url())
        error = "Invalid username or password."

    return render_template("auth/login.html", error=error)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


@bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    error = None
    success = None
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        user = UserModel.verify_password(current_user.username, current_pw)
        if not user:
            error = "Current password is incorrect."
        elif len(new_pw) < 6:
            error = "New password must be at least 6 characters."
        elif new_pw != confirm_pw:
            error = "Passwords do not match."
        else:
            UserModel.update_password(int(current_user.id), new_pw)
            success = "Password updated successfully."

    return render_template("auth/profile.html", error=error, success=success)


def _role_home_url():
    role = current_user.role
    if role in ("admin", "network_admin"):
        return url_for("dashboard.index")
    return url_for("pages.map_page")


def _role_home():
    return redirect(_role_home_url())
