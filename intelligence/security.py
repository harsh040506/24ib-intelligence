"""Cross-cutting security primitives.

* :func:`is_safe_redirect_path` — keeps ``?next=`` round-trips local.
* :class:`RateLimiter` — a fixed-window, in-memory throttle used to protect the
  REST API from a runaway or abusive key (``api/routes.py``).

Scope note
----------
:class:`RateLimiter` is intentionally in-process. It protects a single
``python run.py`` / single gunicorn worker, which is the documented default
deployment. Horizontally scaled deployments should front the app with a shared
limiter (e.g. Redis / an API gateway); the interface here is deliberately tiny so
it can be swapped without touching call sites.
"""
from __future__ import annotations

import threading
import time


def is_safe_redirect_path(target: str | None) -> bool:
    """True only for a same-origin relative path like ``/data/?page=3``.

    Several routes accept a ``next=`` parameter so an edit or delete returns
    the user to the exact filtered page they came from. Accepting an arbitrary
    value there would turn every one of those into an open redirect, so only
    root-relative paths are allowed — never an absolute URL, never a
    scheme-relative ``//evil.example`` (which browsers treat as absolute), and
    never a backslash form that some browsers normalise into one.
    """
    if not target:
        return False
    target = target.strip()
    if not target.startswith("/"):
        return False
    if target.startswith("//") or "\\" in target:
        return False
    return True


class RateLimiter:
    """Thread-safe fixed-window rate limiter keyed by an arbitrary string.

    ``hit(key)`` records an attempt and returns ``True`` if the caller is still
    within the allowance for the current window, ``False`` once it is exceeded.
    Memory is bounded by lazily evicting entries whose window has elapsed on each
    call, so an unbounded set of distinct keys cannot leak memory indefinitely.
    """

    def __init__(self, max_hits: int, window_seconds: float) -> None:
        self._max = max_hits
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            timestamps = [t for t in self._hits.get(key, ()) if t > cutoff]
            timestamps.append(now)
            self._hits[key] = timestamps
            # Opportunistic eviction of fully-expired keys to bound memory.
            if len(self._hits) > 2048:
                self._hits = {
                    k: [t for t in v if t > cutoff]
                    for k, v in self._hits.items()
                    if any(t > cutoff for t in v)
                }
            return len(timestamps) <= self._max

    def reset(self, key: str) -> None:
        """Clear a key's counter — e.g. after a successful login."""
        with self._lock:
            self._hits.pop(key, None)
