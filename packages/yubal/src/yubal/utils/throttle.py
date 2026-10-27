"""Process-wide pacing for YouTube Music API requests.

Every YTMusicClient gets its own session, so the spacing lives at module
level to cover all of them (sync jobs, subscription lookups, library scans).
"""

import threading
import time
from typing import Any

import requests

_lock = threading.Lock()
_interval = 0.0
_last_request = 0.0


def set_request_interval(seconds: float) -> None:
    """Set the minimum gap between two YouTube Music API requests."""
    global _interval
    _interval = max(0.0, seconds)


class ThrottledSession(requests.Session):
    """requests.Session that waits out the shared interval before each request."""

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:
        global _last_request
        with _lock:
            wait = _last_request + _interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        return super().request(*args, **kwargs)
