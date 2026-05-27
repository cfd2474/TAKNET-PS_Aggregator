"""Certificate monitor service — checks Caddy cert expiration, handles daily alerts, and logs renewals."""

import os
from datetime import datetime, date, timezone
from services.docker_service import get_caddy_certs
from services.mail_client import ResendMailClient, get_resend_admin_emails, get_resend_from_email
from models import get_setting, set_setting, get_db


def log_activity(event_type, message, feeder_id=None):
    """Log helper to record certificate status in activity_log."""
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO activity_log (event_type, message, feeder_id) VALUES (?, ?, ?)",
            (event_type, message, feeder_id)
        )
        conn.commit()
    except Exception as e:
        print(f"[activity] Failed to log activity: {e}")
    finally:
        conn.close()


def check_caddy_certs_job():
    """Scheduled job to check Caddy certificates, detect renewals, and alert on expiry."""
    # Only run if Resend is enabled
    client = ResendMailClient.from_env()
    if not client.enabled:
        return

    admins = get_resend_admin_emails()
    if not admins:
        return

    from_email = get_resend_from_email()
    certs = get_caddy_certs()
    today_str = date.today().isoformat()

    for cert in certs:
        domain = cert["domain"]
        expiry_str = cert["expiry"]  # ISO format string
        days_remaining = cert["days_remaining"]
        expired = cert["expired"]
        issuer = cert["issuer"]

        if not expiry_str:
            continue

        # Convert expiry_str (ISO format) to datetime for display
        try:
            expiry_dt = datetime.fromisoformat(expiry_str)
            expiry_display = expiry_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except Exception:
            expiry_display = expiry_str

        # Retrieve stored expiry
        stored_expiry_key = f"caddy_cert_expiry_{domain}"
        stored_expiry = get_setting(stored_expiry_key)

        if stored_expiry is None:
            # First time seeing this cert, just store it
            set_setting(stored_expiry_key, expiry_str)
            continue

        # Check for renewal (if the cert on disk expires later than our stored value)
        if expiry_str > stored_expiry:
            # Cert has been renewed! Send confirmation email
            subject = f"[TAKNET] SSL Certificate Renewed: {domain}"
            html_content = f"""
            <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;">
                <h2 style="color: #22c55e; margin-top: 0;">SSL Certificate Successfully Renewed</h2>
                <p>The SSL certificate for your domain <strong>{domain}</strong> has been successfully renewed by Caddy.</p>
                <table style="width: 100%; border-collapse: collapse; margin-top: 20px; margin-bottom: 20px;">
                    <tr>
                        <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;">Domain:</td>
                        <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-weight: bold;">{domain}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;">Issuer:</td>
                        <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-weight: bold;">{issuer}</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;">Expiration Date:</td>
                        <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-weight: bold;">{expiry_display}</td>
                    </tr>
                </table>
                <p style="color: #64748b; font-size: 14px; margin-bottom: 0;">This is an automated notification from your TAKNET-PS Aggregator service.</p>
            </div>
            """
            
            res = client.send_email(
                from_email=from_email,
                to=admins,
                subject=subject,
                html=html_content
            )
            print(f"[caddy-monitor] Sent renewal email for {domain}. Result: {res}")
            
            # Log to activity log
            log_activity(
                event_type="cert_renewed",
                message=f"SSL certificate for {domain} has been successfully renewed (expires {expiry_display})"
            )
            
            # Update stored expiry
            set_setting(stored_expiry_key, expiry_str)
            # Reset any last alert date so we don't block future expiry alerts
            set_setting(f"caddy_cert_alert_date_{domain}", "")
            continue

        # Check for expiry alerts (14 days or less)
        if days_remaining is not None and (days_remaining <= 14 or expired):
            # Check if we already sent an alert today
            alert_date_key = f"caddy_cert_alert_date_{domain}"
            last_alert_date = get_setting(alert_date_key)

            if last_alert_date != today_str:
                # Need to send alert
                if expired:
                    subject = f"[CRITICAL] SSL Certificate EXPIRED: {domain}"
                    title = "SSL Certificate EXPIRED"
                    color = "#ef4444"
                    intro = f"The SSL certificate for your domain <strong>{domain}</strong> has expired! Immediate action is required to restore secure access."
                else:
                    subject = f"[WARNING] SSL Certificate Expiring Soon: {domain}"
                    title = "SSL Certificate Expiring Soon"
                    color = "#f59e0b"
                    intro = f"The SSL certificate for your domain <strong>{domain}</strong> is expiring in {days_remaining} days."

                html_content = f"""
                <div style="font-family: sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px; background-color: #ffffff;">
                    <h2 style="color: {color}; margin-top: 0;">{title}</h2>
                    <p>{intro}</p>
                    <table style="width: 100%; border-collapse: collapse; margin-top: 20px; margin-bottom: 20px;">
                        <tr>
                            <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;">Domain:</td>
                            <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-weight: bold;">{domain}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;">Days Remaining:</td>
                            <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-weight: bold; color: {color};">{days_remaining if not expired else '0 (Expired)'}</td>
                        </tr>
                        <tr>
                            <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; color: #64748b;">Expiration Date:</td>
                            <td style="padding: 8px 0; border-bottom: 1px solid #f1f5f9; font-weight: bold;">{expiry_display}</td>
                        </tr>
                    </table>
                    <p style="color: #64748b; font-size: 14px; margin-bottom: 0;">This is an automated notification from your TAKNET-PS Aggregator service.</p>
                </div>
                """

                res = client.send_email(
                    from_email=from_email,
                    to=admins,
                    subject=subject,
                    html=html_content
                )
                print(f"[caddy-monitor] Sent expiry alert email for {domain}. Result: {res}")

                # Log to activity log
                log_activity(
                    event_type="cert_expiring" if not expired else "cert_expired",
                    message=f"SSL certificate for {domain} is expiring in {days_remaining} days ({expiry_display})" if not expired else f"SSL certificate for {domain} has expired!"
                )

                # Store that we sent the alert today
                set_setting(alert_date_key, today_str)
