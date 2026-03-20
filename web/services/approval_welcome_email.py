"""Welcome email when a self-registered user moves from pending to approved (Resend)."""

from __future__ import annotations

import html
import os

from flask import url_for

from services.mail_client import ResendMailClient, get_resend_from_email


def send_account_approved_welcome(user: dict | None) -> None:
    """Send welcome email to the user if Resend is configured and profile email is set.

    Safe to call after approve: failures are logged and not re-raised.
    """
    if not user:
        return
    to = (user.get("email") or "").strip()
    if not to:
        return

    mail = ResendMailClient.from_env()
    if not mail.enabled or not mail.api_key:
        return

    site_name = (os.environ.get("SITE_NAME") or "TAKNET-PS Aggregator").strip()
    login_url = url_for("auth.login", _external=True)

    raw_first = (user.get("first_name") or "").strip()
    raw_user = (user.get("username") or "").strip()
    greet_name = raw_first or raw_user or "there"
    greet_safe = html.escape(greet_name)
    site_safe = html.escape(site_name)

    subject = f"{site_name} — Welcome, your account is approved"

    html_body = f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#1f2937;background:#f3f4f6;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:8px;border:1px solid #e5e7eb;overflow:hidden;">
        <tr><td style="padding:24px 28px 8px 28px;">
          <h1 style="margin:0 0 12px 0;font-size:20px;color:#0f172a;">Welcome to TAKNET-PS</h1>
          <p style="margin:0 0 14px 0;">Hi {greet_safe},</p>
          <p style="margin:0 0 14px 0;">Your access request for <strong>{site_safe}</strong> has been <strong>approved</strong>. You can sign in with the username and password you registered.</p>
          <p style="margin:0 0 16px 0;"><strong>TAKNET-PS</strong> (Team Awareness Kit NETwork for Public Safety) — a comprehensive ADS-B aircraft tracking solution designed for distributed deployment with centralized aggregation. Built for public safety, emergency services, and aviation tracking networks.</p>
          <p style="margin:20px 0 8px 0;">
            <a href="{html.escape(login_url, quote=True)}" style="display:inline-block;padding:12px 22px;background:#00596a;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;">Log in</a>
          </p>
          <p style="margin:16px 0 0 0;font-size:13px;color:#6b7280;">If the button does not work, copy this link into your browser:<br><span style="word-break:break-all;color:#2563eb;">{html.escape(login_url, quote=True)}</span></p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
"""

    text_body = (
        f"Hi {greet_name},\n\n"
        f"Your access request for {site_name} has been approved. "
        f"You can sign in with the username and password you registered.\n\n"
        f"TAKNET-PS (Team Awareness Kit NETwork for Public Safety) — a comprehensive ADS-B aircraft "
        f"tracking solution designed for distributed deployment with centralized aggregation. "
        f"Built for public safety, emergency services, and aviation tracking networks.\n\n"
        f"Log in: {login_url}\n"
    )

    try:
        out = mail.send_email(
            from_email=get_resend_from_email(),
            to=to,
            subject=subject,
            html=html_body,
            text=text_body,
        )
        if not out.get("success"):
            print(f"[approval_welcome_email] Resend error: {out}")
    except Exception as e:
        print(f"[approval_welcome_email] send failed: {e}")
