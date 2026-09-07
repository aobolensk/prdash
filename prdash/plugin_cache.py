"""A simple in-process TTL cache available to plugin worker code.

Plugins run in an isolated worker process and cannot use Django's shared
cache, so this gives them a per-process equivalent instead of each plugin
hand-rolling its own.
"""

import time

_CACHE = {}


def cache_get(key):
    entry = _CACHE.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.time() >= expires_at:
        _CACHE.pop(key, None)
        return None
    return value


def cache_set(key, value, ttl):
    _CACHE[key] = (value, time.time() + ttl)


def cache_add(key, value, ttl):
    """Set key to value only if not already cached. Returns whether it was set."""
    if cache_get(key) is not None:
        return False
    cache_set(key, value, ttl)
    return True
