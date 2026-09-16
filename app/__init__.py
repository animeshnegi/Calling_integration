from flask import Flask

from .admin import register_admin
from .ami import AsteriskAMI
from .asterisk_client import AsteriskClient
from .config import Config
from .routes import register_routes
from .services import TelephonyService
from .telephony_config import TelephonyConfigSync


def create_app(config_class=Config, *, start_ari: bool = False, sync_config: bool = False):
    config_class.validate()
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.config["MAX_CONTENT_LENGTH"] = config_class.MAX_CONTENT_LENGTH
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = config_class.FLASK_ENV == "production"
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    asterisk = AsteriskClient(config_class)
    ami = AsteriskAMI(config_class.ASTERISK_AMI_HOST, config_class.ASTERISK_AMI_PORT, config_class.ASTERISK_AMI_USER, config_class.ASTERISK_AMI_PASSWORD)

    def apply_telephony_config():
        store = app.extensions.get("settings_store")
        if store:
            TelephonyConfigSync(store, ami, config_class.ASTERISK_DYNAMIC_CONFIG_PATH).apply()

    register_admin(app, config_class, on_telephony_change=apply_telephony_config)
    settings_store = app.extensions["settings_store"]
    app.extensions["telephony_config_sync"] = TelephonyConfigSync(settings_store, ami, config_class.ASTERISK_DYNAMIC_CONFIG_PATH)
    service = TelephonyService(asterisk, config_class, settings_store=settings_store)
    app.extensions["telephony_service"] = service

    if sync_config:
        try:
            app.extensions["telephony_config_sync"].apply()
        except Exception:
            app.logger.exception("Initial Asterisk database configuration sync failed")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    register_routes(app, service)

    if start_ari:
        from .asterisk_client import ari_event_loop

        def on_event(event):
            try:
                service.handle_ari_event(event)
            except Exception:
                app.logger.exception("Failed to process Asterisk ARI event")

        ari_event_loop(on_event, config_class)

    return app
