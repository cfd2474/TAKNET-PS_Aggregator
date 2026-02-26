"""Auth routes — login, logout, register, pending, change password."""

from flask import Blueprint, render_template, redirect, url_for, request
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
        user, status = UserModel.verify_password(username, password)
        if user:
            if status == "pending":
                login_user(AuthUser(user), remember=True)
                return redirect(url_for("auth.pending"))
            elif status == "denied":
                error = "Your access request was denied. Contact an administrator."
            else:
                login_user(AuthUser(user), remember=True)
                next_url = request.args.get("next")
                return redirect(next_url or _role_home_url())
        else:
            error = "Invalid username or password."

    return render_template("auth/login.html", error=error)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return _role_home()

    error = None
    success = False
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm_password", "")

        if not username or len(username) < 3:
            error = "Username must be at least 3 characters."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            ok, msg = UserModel.register(username, password)
            if ok:
                success = True
            else:
                error = "Username already taken." if "UNIQUE" in msg else msg

    return render_template("auth/register.html", error=error, success=success)


@bp.route("/pending")
@login_required
def pending():
    user = UserModel.get_by_id(int(current_user.id))
    if not user or user.get("status") != "pending":
        return _role_home()
    return render_template("auth/pending.html")


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
        new_pw     = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        user, _ = UserModel.verify_password(current_user.username, current_pw)
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
    # Pending users go to pending page
    user = UserModel.get_by_id(int(current_user.id))
    if user and user.get("status") == "pending":
        return url_for("auth.pending")
    role = current_user.role
    if role in ("admin", "network_admin"):
        return url_for("dashboard.index")
    return url_for("pages.map_page")


def _role_home():
    return redirect(_role_home_url())
