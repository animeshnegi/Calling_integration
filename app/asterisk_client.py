"""Compatibility alias for app.asterisk.client (including module-level state)."""
import sys
from .asterisk import client as _implementation
sys.modules[__name__] = _implementation
