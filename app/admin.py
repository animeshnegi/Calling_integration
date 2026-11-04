"""Compatibility alias; admin HTTP implementation lives in app.routes.admin."""
import sys
from .routes import admin as _implementation
from .models.settings import (
    extension_digits, extension_scope, extension_key, endpoint_name, browser_username, parse_ice_servers, extension_mailbox, device_registration, device_account_for_extension, extension_registration, same_account, SettingsStore, DB_INTEGRITY_ERRORS, INPUT_DB_ERRORS
)

for _name in ['extension_digits', 'extension_scope', 'extension_key', 'endpoint_name', 'browser_username', 'parse_ice_servers', 'extension_mailbox', 'device_registration', 'device_account_for_extension', 'extension_registration', 'same_account', 'SettingsStore', 'DB_INTEGRITY_ERRORS', 'INPUT_DB_ERRORS']:
    if not hasattr(_implementation, _name):
        setattr(_implementation, _name, globals()[_name])
sys.modules[__name__] = _implementation
