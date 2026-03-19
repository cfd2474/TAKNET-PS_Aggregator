"""Auth routes — login, logout, register, pending, password change, password reset."""

import os

from flask import Blueprint, render_template, redirect, url_for, request
from flask_login import login_user, logout_user, login_required, current_user

from models import UserModel
from app import AuthUser
from services.mail_client import ResendMailClient, get_resend_from_email

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
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        agency = request.form.get("agency", "").strip()

        if not username or len(username) < 3:
            error = "Username must be at least 3 characters."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif not first_name:
            error = "First name is required."
        elif not last_name:
            error = "Last name is required."
        elif not email:
            error = "Email is required."
        elif not phone:
            error = "Phone is required."
        elif not agency:
            error = "Agency is required."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            ok, result = UserModel.register(
                username,
                password,
                {
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": email,
                    "phone": phone,
                    "agency": agency,
                },
            )
            if ok:
                success = True

                # Admin email notification for new registration (Resend).
                try:
                    mail_client = ResendMailClient.from_env()
                    if mail_client.enabled and mail_client.api_key:
                        # Notify all active admins that have an email.
                        admins = UserModel.get_active_users_by_role(("admin",))
                        admin_emails = [a.get("email") for a in admins if (a.get("email") or "").strip()]
                        if admin_emails:
                            user_id = int((result or {}).get("user_id") or 0)
                            created_user = (
                                UserModel.get_by_id(user_id) if user_id else None
                            ) if user_id else None
                            created_user = created_user or {}

                            site_name = os.environ.get("SITE_NAME", "TAKNET-PS Aggregator")
                            admin_users_url = url_for("config.users", _external=True)
                            admin_user_detail_url = (
                                url_for("config.user_detail", user_id=user_id, _external=True)
                                if user_id else admin_users_url
                            )

                            req_user_first = (created_user.get("first_name") or "").strip() or username
                            req_user_last = (created_user.get("last_name") or "").strip()
                            req_user_email = (created_user.get("email") or "").strip()

                            req_full_name = (req_user_first + (" " + req_user_last if req_user_last else "")).strip()

                            subject = f"{site_name} — New Access Request"
                            html = f"""
<h3 style="margin:0 0 10px 0;">New user registration</h3>
<p style="margin:0 0 12px 0;">An access request was submitted to <strong>{site_name}</strong>.</p>
<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:13px;">
  <tr><td style="padding:6px 10px;border:1px solid #ddd;color:#444;">Username</td><td style="padding:6px 10px;border:1px solid #ddd;color:#111;">{username}</td></tr>
  <tr><td style="padding:6px 10px;border:1px solid #ddd;color:#444;">Name</td><td style="padding:6px 10px;border:1px solid #ddd;color:#111;">{req_full_name}</td></tr>
  <tr><td style="padding:6px 10px;border:1px solid #ddd;color:#444;">Email</td><td style="padding:6px 10px;border:1px solid #ddd;color:#111;">{req_user_email}</td></tr>
  <tr><td style="padding:6px 10px;border:1px solid #ddd;color:#444;">Phone</td><td style="padding:6px 10px;border:1px solid #ddd;color:#111;">{(phone or '').strip()}</td></tr>
  <tr><td style="padding:6px 10px;border:1px solid #ddd;color:#444;">Agency</td><td style="padding:6px 10px;border:1px solid #ddd;color:#111;">{(agency or '').strip()}</td></tr>
</table>
<p style="margin:14px 0 10px 0;">
  Review the request here:
  <a href="{admin_user_detail_url}" style="display:inline-block;padding:10px 14px;background:#3b82f6;color:#fff;text-decoration:none;border-radius:6px;">Open User Details</a>
</p>
<p style="margin:0;color:#666;font-size:12px;">
  Alternatively: <a href="{admin_users_url}">{admin_users_url}</a>
</p>
"""

                            text = (
                                f"New access request submitted to {site_name}.\n\n"
                                f"Username: {username}\n"
                                f"Name: {req_full_name}\n"
                                f"Email: {req_user_email}\n"
                                f"Phone: {phone}\n"
                                f"Agency: {agency}\n\n"
                                f"Review: {admin_user_detail_url}\n"
                            )

                            mail_client.send_email(
                                from_email=get_resend_from_email(),
                                to=admin_emails,
                                subject=subject,
                                html=html,
                                text=text,
                            )
                except Exception:
                    # Don't block registration success if email fails.
                    pass
            else:
                error = "Username already taken." if "UNIQUE" in str(result) else str(result)

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


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Request a password reset email (generic response to prevent enumeration)."""
    success = False
    error = None

    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        # Always show the same success message to avoid user enumeration.
        success = True

        if not email:
            return render_template("auth/forgot_password.html", success=success, error=error)

        user = UserModel.get_by_email(email)
        if not user or not (user.get("email") or "").strip():
            return render_template("auth/forgot_password.html", success=success, error=error)

        # Only create token if mail is enabled (otherwise user won't receive the link).
        mail_client = ResendMailClient.from_env()
        if not (mail_client.enabled and mail_client.api_key):
            return render_template("auth/forgot_password.html", success=success, error=error)

        token = UserModel.create_password_reset_token(int(user["id"]))

        site_name = os.environ.get("SITE_NAME", "TAKNET-PS Aggregator")
        reset_url = url_for("auth.reset_password", token=token, _external=True)

        first_name = (user.get("first_name") or "").strip() or (user.get("username") or "")
        subject = f"{site_name} Password Reset"

        html = f"""
<p>Hi {first_name},</p>
<p>We received a request to reset your password for your {site_name} account.</p>
<p>
  <a href="{reset_url}" style="display:inline-block;padding:10px 14px;background:#3b82f6;color:#fff;text-decoration:none;border-radius:6px;">
    Reset your password
  </a>
</p>
<p>
  If you didn't request this, you can safely ignore this email.
</p>
<p style="font-size:12px;color:#666;">
  This link will expire in about 1 hour.
</p>
"""
        text = (
            f"Hi {first_name},\n\n"
            f"Reset your {site_name} password: {reset_url}\n\n"
            "If you didn't request this, ignore this email.\n"
        )

        mail_client.send_email(
            from_email=get_resend_from_email(),
            to=user["email"],
            subject=subject,
            html=html,
            text=text,
        )

        return render_template("auth/forgot_password.html", success=success, error=error)

    return render_template("auth/forgot_password.html", success=success, error=error)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token: str):
    """Set a new password using a reset token."""
    error = None
    success = False

    token_ok, _ = UserModel.verify_password_reset_token(token)

    if request.method == "POST":
        password = (request.form.get("password") or "").strip()
        confirm = (request.form.get("confirm_password") or "").strip()

        if len(password) < 6:
            error = "Password must be at least 6 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            ok, _msg = UserModel.consume_password_reset_token(token, password)
            if ok:
                success = True
            else:
                error = "This reset link is invalid or expired."

        # Re-render with error/success state.
        return render_template("auth/reset_password.html", error=error, success=success)

    if not token_ok:
        error = "This reset link is invalid or expired."

    return render_template("auth/reset_password.html", error=error, success=success)


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
