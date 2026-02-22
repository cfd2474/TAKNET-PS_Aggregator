"""TAKNET-PS Aggregator Dashboard — Flask Application."""

import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from flask_login import LoginManager, UserMixin, current_user

from models import mark_stale_feeders, ActivityModel, UserModel


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
    app = Flask(__name__)
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

    @app.context_processor
    def inject_globals():
        version = "1.0.46"
        try:
            vpath = os.path.join(os.path.dirname(__file__), "VERSION")
            if os.path.exists(vpath):
                version = open(vpath).read().strip()
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
    scheduler.add_job(UserModel.seed_default, "interval", minutes=1, id="seed_user")
    scheduler.start()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
