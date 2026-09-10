from flask import Flask

from .asterisk import AsteriskClient, ari_event_loop
from .config import Config
from .routes import register_routes
from .services import TelephonyService


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    client = AsteriskClient(config_class)
    service = TelephonyService(client, config_class)
    app.extensions["telephony_service"] = service

    register_routes(app, service)

    def on_event(event):
        try:
            service.handle_ari_event(event)
        except Exception:
            app.logger.exception("Failed to process Asterisk ARI event")

    if config_class.ASTERISK_ARI_URL:
        ari_event_loop(on_event, config_class)

    return app
