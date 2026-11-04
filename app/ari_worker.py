"""Compatibility entrypoint: python -m app.ari_worker remains supported."""
import sys
from .asterisk import worker as _implementation

if __name__ == "__main__":
    _implementation.run()
else:
    sys.modules[__name__] = _implementation
