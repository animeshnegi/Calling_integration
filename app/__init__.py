from flask import Flask

from .asterisk import AsteriskClient, ari_event_loop
from .config import Config
from .routes import register_routes
from .services import TelephonyService
from .admin import register_admin


def create_app(config_class=Config):
    config_class.validate()
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.config["MAX_CONTENT_LENGTH"] = config_class.MAX_CONTENT_LENGTH
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = config_class.FLASK_ENV == "production"
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    client = AsteriskClient(config_class)
    service = TelephonyService(client, config_class)
    app.extensions["telephony_service"] = service
    register_admin(app, config_class)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    register_routes(app, service)

    def on_event(event):
        try:
            service.handle_ari_event(event)
        except Exception:
            app.logger.exception("Failed to process Asterisk ARI event")

    if config_class.ASTERISK_ARI_URL:
        ari_event_loop(on_event, config_class)

    return app
