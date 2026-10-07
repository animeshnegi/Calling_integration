import logging
import time

from app import create_app

try:
    app = create_app(sync_config=False)
except Exception:
    # Docker restarts the container the moment gunicorn exits, and its restart
    # backoff resets after a short successful run. Without this pause a boot
    # failure (for example an unreachable or misconfigured database) becomes a
    # hot crash-restart loop that saturates every CPU core with imports.
    logging.getLogger(__name__).exception("Fatal boot failure; pausing before exit to avoid a hot restart loop")
    time.sleep(10)
    raise
