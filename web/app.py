"""TAKNET-PS Aggregator Dashboard — Flask Application."""

import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

from models import mark_stale_feeders, ActivityModel


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "taknet-ps-dev-key")

    # Inject site name into all templates
    @app.context_processor
    def inject_globals():
        version = "1.0.16"
        try:
            vpath = os.path.join(os.path.dirname(__file__), "VERSION")
            if os.path.exists(vpath):
                version = open(vpath).read().strip()
        except Exception:
            pass
        return {
            "site_name": os.environ.get("SITE_NAME", "TAKNET-PS Aggregator"),
            "version": version,
        }

    # Register blueprints
    from routes.dashboard import bp as dashboard_bp
    from routes.inputs import bp as inputs_bp
    from routes.config import bp as config_bp
    from routes.pages import bp as pages_bp
    from routes.api import bp as api_bp

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(inputs_bp)
    app.register_blueprint(config_bp)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    # Start background scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(mark_stale_feeders, "interval", seconds=30, id="mark_stale")
    scheduler.add_job(lambda: ActivityModel.cleanup(7), "interval", minutes=5, id="cleanup")
    scheduler.start()

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
