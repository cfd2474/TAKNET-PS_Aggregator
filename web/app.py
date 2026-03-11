"""TAKNET-PS Aggregator Dashboard — Flask Application."""

import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from flask_login import LoginManager, UserMixin, current_user

from models import mark_stale_feeders, ActivityModel, UserModel
from services.health_snapshot import collect_health_snapshot


class AuthUser(UserMixin):
    """Thin wrapper around a user dict for Flask-Login."""
    def __init__(self, user_dict):
        self._user = user_dict

    @property
    def id(self):
        return str(self._user["id"])

    @property
    def username(self):
        return self._user["username"]

    @property
    def role(self):
        return self._user["role"]

    def is_admin(self):
        return self._user["role"] == "admin"

    def can_access(self, section):
        role = self._user["role"]
        if role == "admin":
            return True
        if role == "network_admin":
            return section in ("dashboard", "feeders", "map", "stats", "outputs", "api_general")
        if role == "viewer":
            return section in ("map", "api_map")
        return False


def create_app():
    root = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__, static_folder=os.path.join(root, "static"))
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "taknet-ps-dev-key-change-me")

    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = None

    @login_manager.user_loader
    def load_user(user_id):
        user = UserModel.get_by_id(int(user_id))
        if user:
            return AuthUser(user)
        return None

    with app.app_context():
        try:
            UserModel.seed_default()
        except Exception:
            pass

    # Migrations for existing deployments
    with app.app_context():
        from models import get_db
        conn = get_db()
        for stmt in [
            "ALTER TABLE outputs ADD COLUMN mode TEXT NOT NULL DEFAULT 'api'",
            "ALTER TABLE output_api_keys ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'",
            "ALTER TABLE output_api_keys ADD COLUMN key_display TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE output_api_keys ADD COLUMN key_type TEXT NOT NULL DEFAULT 'single_use'",
        ]:
            try:
                conn.execute(stmt)
                conn.commit()
            except Exception:
                pass
        conn.close()

    @app.context_processor
    def inject_globals():
        """Inject site-wide template variables, including the current version.

        Version is read from the same VERSION file used by the update system so
        the sidebar/footer stay in sync with what taknet-agg and /api/updates
        report after an update.
        """
        version = "unknown"
        try:
            # Prefer project root VERSION (one level above web/ inside the image)
            root_vpath = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
            web_vpath = os.path.join(os.path.dirname(__file__), "VERSION")
            for vpath in (root_vpath, web_vpath):
                if os.path.exists(vpath):
                    with open(vpath) as f:
                        version = f.read().strip()
                    break
        except Exception:
            pass

        return {
            "site_name": os.environ.get("SITE_NAME", "TAKNET-PS Aggregator"),
            "version": version,
            "current_user": current_user,
        }

    from routes.auth import bp as auth_bp
    from routes.dashboard import bp as dashboard_bp
    from routes.inputs import bp as inputs_bp
    from routes.config import bp as config_bp
    from routes.pages import bp as pages_bp
    from routes.api import bp as api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inputs_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    scheduler = BackgroundScheduler()
    scheduler.add_job(mark_stale_feeders, "interval", seconds=30, id="mark_stale")
    scheduler.add_job(lambda: ActivityModel.cleanup(7), "interval", minutes=5, id="cleanup")
    scheduler.add_job(collect_health_snapshot, "interval", seconds=30, id="health_snapshot")
    scheduler.add_job(UserModel.seed_default, "interval", minutes=1, id="seed_user")

    def _run_cot_sender():
        import threading
        def run():
            with app.app_context():
                try:
                    from cot_pipeline import run_cot_sender_cycle
                    run_cot_sender_cycle()
                except Exception:
                    pass
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        # Return immediately so scheduler never blocks; cycle uses short timeouts and single-run lock.
    scheduler.add_job(_run_cot_sender, "interval", seconds=5, id="cot_sender")

    scheduler.start()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
