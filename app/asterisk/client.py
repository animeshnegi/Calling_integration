"""ARI client facade - the implementation is ``app.asterisk_client``.

Kept as a real module so tests can patch its module-level socket helpers.
"""
from ..asterisk_client import (
    AsteriskClient, AsteriskError, endpoint_name, ari_event_loop, _set_ready, _clear_ready,
)

__all__ = ["AsteriskClient", "AsteriskError", "endpoint_name", "ari_event_loop",
           "_set_ready", "_clear_ready"]
