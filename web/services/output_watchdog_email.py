"""Watchdog email for CoT outputs paused due to TLS failure (Resend)."""

from __future__ import annotations

import html
import os

from services.mail_client import ResendMailClient, get_resend_from_email


def send_output_paused_notification(output_id: int, output_name: str) -> None:
    """Send notification email to the output creator when a CoT output is paused.

    Safe to call: failures are logged and not re-raised.
    """
    from models import OutputModel, UserModel

    # Use a dummy user_id/role since we need a single output without permission checks
    # OutputModel.get_by_id checks permissions, but we are in a background worker.
    # We can use direct DB query or an "admin" context if we had one.
    # Let's use get_for_user with admin role to find it.
    output = None
    try:
        from models import get_db
        conn = get_db()
        row = conn.execute(
            "SELECT o.*, u.id as user_id, u.email, u.first_name, u.username "
            "FROM outputs o "
            "JOIN users u ON o.created_by = u.id "
            "WHERE o.id = ?",
            (output_id,)
        ).fetchone()
        conn.close()
        if row:
            output = dict(row)
    except Exception as e:
        print(f"[output_watchdog_email] Failed to fetch output/user for id {output_id}: {e}")
        return

    if not output:
        return

    to = (output.get("email") or "").strip()
    if not to:
        return

    mail = ResendMailClient.from_env()
    if not mail.enabled or not mail.api_key:
        return

    site_name = (os.environ.get("SITE_NAME") or "TAKNET-PS Aggregator").strip()
    outputs_url = "https://adsb.tak-solutions.com/outputs"

    raw_first = (output.get("first_name") or "").strip()
    raw_user = (output.get("username") or "").strip()
    greet_name = raw_first or raw_user or "there"
    greet_safe = html.escape(greet_name)
    site_safe = html.escape(site_name)
    output_safe = html.escape(output_name)

    subject = f"{site_name} ALERT — Output '{output_name}' Paused"

    html_body = f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#1f2937;background:#f3f4f6;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:8px;border:1px solid #e5e7eb;overflow:hidden;">
        <tr><td style="padding:24px 28px 8px 28px;">
          <h1 style="margin:0 0 12px 0;font-size:20px;color:#991b1b;">Output Feed Paused</h1>
          <p style="margin:0 0 14px 0;">Hi {greet_safe},</p>
          <p style="margin:0 0 14px 0;">The output feed <strong>{output_safe}</strong> on <strong>{site_safe}</strong> has experienced a pause due to repeated TLS handshake failure.</p>
          <p style="margin:0 0 14px 0;">Feed has been suspended until manual handshake has been restored. Open the output details and select <strong>"Test TLS connection"</strong> to re-establish the handshake. The feed can be resumed upon a successful handshake.</p>
          <p style="margin:20px 0 8px 0;">
            <a href="{html.escape(outputs_url, quote=True)}" style="display:inline-block;padding:12px 22px;background:#991b1b;color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;">Manage Outputs</a>
          </p>
          <p style="margin:16px 0 0 0;font-size:13px;color:#6b7280;">If the button does not work, copy this link into your browser:<br><span style="word-break:break-all;color:#2563eb;">{html.escape(outputs_url, quote=True)}</span></p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
"""

    text_body = (
        f"Hi {greet_name},\n\n"
        f"The output feed '{output_name}' on {site_name} has experienced a pause due to repeated TLS handshake failure.\n\n"
        f"Feed has been suspended until manual handshake has been restored. Open output details and select \"Test TLS connection\" to re-establish handshake. Feed can be resumed on successful handshake.\n\n"
        f"Manage Outputs: {outputs_url}\n"
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
            print(f"[output_watchdog_email] Resend error: {out}")
    except Exception as e:
        print(f"[output_watchdog_email] send failed: {e}")
