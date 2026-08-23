"""Rate limiting for the mem0 server.

Two layers:

1. slowapi decorators (``limiter`` below) stay on the credential endpoints in
   routers/auth.py — strict per-route quotas (login 5/min etc.) with
   slowapi's Retry-After handling.

2. ``DataPlaneRateLimitMiddleware`` covers everything else (search, memory
   CRUD, evolve, refine, ...) with a single fixed-window limiter, so data
   endpoints are no longer unlimited. Configuration:

   - ``MEM0_RATE_LIMIT_DATA``   — window quota for the data plane,
                                  default ``120/minute``. Accepts
                                  ``N/second|minute|hour``.
   - ``MEM0_RATE_LIMIT_REDIS``  — optional redis:// URL; when set the window
                                  counters live in Redis so limits hold
                                  across workers and replicas. Without it a
                                  per-process in-memory counter is used
                                  (fine for the single-container default).
   - ``MEM0_TRUST_PROXY_HEADERS`` — when ``true``, the client IP is taken
                                  from X-Forwarded-For / X-Real-IP. Only
                                  enable behind a proxy that overwrites
                                  these headers, otherwise clients can
                                  spoof the key.

   ``/auth/*``, docs endpoints and OPTIONS requests are exempt here (auth
   has its own stricter layer; preflight must never be limited).
"""

import logging
import os
import time
from typing import Any, Optional

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = {"second": 1, "minute": 60, "hour": 3600}
_EXEMPT_PREFIXES = ("/auth", "/docs", "/redoc", "/openapi.json", "/health")


def parse_rate(spec: str) -> tuple[int, int]:
    """Parse ``N/second|minute|hour`` into (limit, window_seconds)."""
    try:
        count_s, _, unit = spec.strip().partition("/")
        count = int(count_s)
        window = _WINDOW_SECONDS[unit.rstrip("s").lower() if unit.rstrip("s").lower() != "min" else "minute"]
        if count <= 0:
            raise ValueError
        return count, window
    except (ValueError, KeyError) as e:
        raise ValueError(f"Invalid rate limit spec {spec!r} (expected e.g. '120/minute')") from e


class MemoryBackend:
    """Per-process fixed-window counters."""

    def __init__(self) -> None:
        self._counters: dict[str, tuple[int, float]] = {}
        self._last_prune = 0.0

    def incr(self, key: str, window_seconds: int) -> int:
        now = time.time()
        # Opportunistic pruning keeps the dict bounded without a background task.
        if now - self._last_prune > 300:
            self._counters = {k: v for k, v in self._counters.items() if v[1] > now}
            self._last_prune = now
        hit = self._counters.get(key)
        if hit is None or hit[1] <= now:
            self._counters[key] = (1, now + window_seconds)
            return 1
        count, expiry = hit
        self._counters[key] = (count + 1, expiry)
        return count + 1

    def ttl(self, key: str, window_seconds: int) -> int:
        hit = self._counters.get(key)
        if hit is None:
            return window_seconds
        return max(1, int(hit[1] - time.time()) + 1)


class RedisBackend:
    """Fixed-window counters in Redis (INCR + EXPIRE), shared across processes."""

    def __init__(self, url: str) -> None:
        import redis  # imported lazily; optional dependency at runtime

        self._redis = redis.Redis.from_url(url, decode_responses=True)

    def incr(self, key: str, window_seconds: int) -> int:
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count, ttl = pipe.execute()
        if ttl < 0:  # first hit in this window — set the expiry
            self._redis.expire(key, window_seconds)
            ttl = window_seconds
        self._last_ttl = int(ttl)
        return int(count)

    def ttl(self, key: str, window_seconds: int) -> int:
        return getattr(self, "_last_ttl", window_seconds) or window_seconds


def make_backend(redis_url: Optional[str]) -> Any:
    if redis_url:
        try:
            return RedisBackend(redis_url)
        except Exception:
            logger.warning("rate_limit: Redis unavailable (%s); falling back to in-memory", redis_url)
    return MemoryBackend()


def _client_ip(scope: dict, trust_proxy: bool) -> str:
    """Client IP for the rate-limit key; optionally honors X-Forwarded-For.

    Only trust proxy headers when MEM0_TRUST_PROXY_HEADERS is set AND the
    front proxy overwrites them — otherwise clients can spoof the key.
    """
    if trust_proxy:
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                first = value.decode("latin-1").split(",")[0].strip()
                if first:
                    return first
    client = scope.get("client")
    return client[0] if client else "unknown"


class DataPlaneRateLimitMiddleware:
    """Pure-ASGI fixed-window limiter for all non-auth HTTP routes."""

    def __init__(self, app, rate_spec: Optional[str] = None, backend: Any = None):
        self.app = app
        spec = rate_spec if rate_spec is not None else os.environ.get("MEM0_RATE_LIMIT_DATA", "120/minute")
        self.limit, self.window_seconds = parse_rate(spec)
        self.backend = backend if backend is not None else make_backend(os.environ.get("MEM0_RATE_LIMIT_REDIS"))
        self.trust_proxy = os.environ.get("MEM0_TRUST_PROXY_HEADERS", "").lower() in {"1", "true", "yes", "on"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("method") == "OPTIONS" or any(path.startswith(p) for p in _EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        ip = _client_ip(scope, self.trust_proxy)
        key = f"rl:data:{ip}:{int(time.time() // self.window_seconds)}"
        count = self.backend.incr(key, self.window_seconds)

        if count > self.limit:
            retry_after = str(self.backend.ttl(key, self.window_seconds))
            body = b'{"detail":"Rate limit exceeded. Retry later."}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 429,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"retry-after", retry_after.encode("latin-1")),
                        (b"content-length", str(len(body)).encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
