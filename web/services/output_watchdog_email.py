"""Watchdog email for CoT outputs paused due to TLS failure (Resend)."""

from __future__ import annotations

import html
import os

from services.mail_client import ResendMailClient, get_resend_from_email


OUTPUTS_URL = "https://adsb.tak-solutions.com/outputs"


def _output_creator(output_id: int) -> dict | None:
    """The output row joined with its creator's email and name, or None."""
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
        return dict(row) if row else None
    except Exception as e:
        print(f"[output_watchdog_email] Failed to fetch output/user for id {output_id}: {e}")
        return None


def _send_to_creator(output_id: int, subject: str, heading: str, color: str, paragraphs: list[str]) -> bool:
    """Email the output's creator. paragraphs are plain text. Returns True if Resend accepted it.

    Safe to call: failures are logged and not re-raised.
    """
    output = _output_creator(output_id)
    if not output:
        return False
    to = (output.get("email") or "").strip()
    if not to:
        return False

    mail = ResendMailClient.from_env()
    if not mail.enabled or not mail.api_key:
        return False

    greet_name = (output.get("first_name") or "").strip() or (output.get("username") or "").strip() or "there"
    url_safe = html.escape(OUTPUTS_URL, quote=True)
    paras_html = "".join(
        f'<p style="margin:0 0 14px 0;">{html.escape(p)}</p>' for p in paragraphs
    )

    html_body = f"""\
<!DOCTYPE html>
<html><body style="margin:0;padding:0;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#1f2937;background:#f3f4f6;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="100%" style="max-width:560px;background:#ffffff;border-radius:8px;border:1px solid #e5e7eb;overflow:hidden;">
        <tr><td style="padding:24px 28px 8px 28px;">
          <h1 style="margin:0 0 12px 0;font-size:20px;color:{color};">{html.escape(heading)}</h1>
          <p style="margin:0 0 14px 0;">Hi {html.escape(greet_name)},</p>
          {paras_html}
          <p style="margin:20px 0 8px 0;">
            <a href="{url_safe}" style="display:inline-block;padding:12px 22px;background:{color};color:#ffffff;text-decoration:none;border-radius:6px;font-weight:600;">Manage Outputs</a>
          </p>
          <p style="margin:16px 0 0 0;font-size:13px;color:#6b7280;">If the button does not work, copy this link into your browser:<br><span style="word-break:break-all;color:#2563eb;">{url_safe}</span></p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>
"""
    text_body = f"Hi {greet_name},\n\n" + "".join(p + "\n\n" for p in paragraphs) + f"Manage Outputs: {OUTPUTS_URL}\n"

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
            return False
        return True
    except Exception as e:
        print(f"[output_watchdog_email] send failed: {e}")
        return False


def _site_name() -> str:
    return (os.environ.get("SITE_NAME") or "TAKNET-PS Aggregator").strip()


def send_output_paused_notification(output_id: int, output_name: str, reason: str) -> bool:
    """Tell the output creator a CoT output was paused. Returns True if the email was sent."""
    site = _site_name()
    return _send_to_creator(
        output_id,
        subject=f"{site} ALERT — Output '{output_name}' Paused",
        heading="Output Feed Paused",
        color="#991b1b",
        paragraphs=[
            f"The output feed '{output_name}' on {site} has been paused: {reason}.",
            "No action is needed if the TAK server is just restarting or briefly unreachable. "
            "The aggregator keeps retrying in the background (every 2 minutes for the first "
            "30 minutes, then every 10 minutes) and resumes the feed on its own; you will get "
            "another email when it does.",
            "If the server address or client certificate has changed, update the output and "
            "select \"Test TLS connection\" to resume immediately.",
        ],
    )


def send_output_resumed_notification(output_id: int, output_name: str) -> bool:
    """Tell the output creator a paused CoT output reconnected on its own."""
    site = _site_name()
    return _send_to_creator(
        output_id,
        subject=f"{site} — Output '{output_name}' Resumed",
        heading="Output Feed Resumed",
        color="#166534",
        paragraphs=[
            f"The output feed '{output_name}' on {site} reconnected to its TAK server and is sending again. No action is needed.",
        ],
    )
