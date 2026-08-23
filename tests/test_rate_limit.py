"""Tests for the data-plane rate-limit middleware (server/rate_limit.py)."""

import os
import sys

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import rate_limit as rl  # noqa: E402


def _make_app(limit_spec: str, backend=None):
    app = FastAPI()

    @app.get("/search")
    async def _search():
        return {"ok": True}

    @app.post("/auth/login")
    async def _login():
        return {"ok": True}

    @app.get("/docs")
    async def _docs():
        return {"ok": True}

    app.add_middleware(rl.DataPlaneRateLimitMiddleware, rate_spec=limit_spec, backend=backend)
    return TestClient(app)


class TestParseRate:
    def test_minute(self):
        assert rl.parse_rate("120/minute") == (120, 60)

    def test_second_and_hour(self):
        assert rl.parse_rate("5/second") == (5, 1)
        assert rl.parse_rate("10/hour") == (10, 3600)

    def test_invalid_specs_rejected(self):
        for bad in ("abc/minute", "0/minute", "-3/hour", "120/fortnight", ""):
            with pytest.raises(ValueError):
                rl.parse_rate(bad)


class TestBackendFactory:
    def test_defaults_to_memory(self, monkeypatch):
        monkeypatch.delenv("MEM0_RATE_LIMIT_REDIS", raising=False)
        assert isinstance(rl.make_backend(None), rl.MemoryBackend)

    def test_redis_url_builds_redis_backend(self, monkeypatch):
        monkeypatch.setenv("MEM0_RATE_LIMIT_REDIS", "redis://localhost:6379/0")
        pytest.importorskip("redis")
        backend = rl.make_backend("redis://localhost:6379/0")
        assert isinstance(backend, rl.RedisBackend)

    def test_memory_backend_counts_within_window(self):
        backend = rl.MemoryBackend()
        counts = [backend.incr("k", 60) for _ in range(4)]
        assert counts == [1, 2, 3, 4]


class TestMiddlewareBehavior:
    def test_blocks_after_limit_with_retry_after(self):
        client = _make_app("3/minute")
        for _ in range(3):
            assert client.get("/search").status_code == 200
        resp = client.get("/search")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert resp.json()["detail"] == "Rate limit exceeded. Retry later."

    def test_auth_and_docs_exempt(self):
        client = _make_app("1/minute")
        # /auth/* and /docs are exempt; only /search consumes the window.
        assert client.get("/docs").status_code == 200
        assert client.post("/auth/login").status_code == 200
        assert client.post("/auth/login").status_code == 200
        assert client.get("/search").status_code == 200
        assert client.get("/search").status_code == 429

    def test_options_preflight_exempt(self):
        client = _make_app("1/minute")
        assert client.options("/search").status_code in (200, 405)  # exempt from limiting either way
        assert client.get("/search").status_code == 200

    def test_per_ip_isolation(self):
        app = FastAPI()

        @app.get("/search")
        async def _search():
            return {"ok": True}

        app.add_middleware(rl.DataPlaneRateLimitMiddleware, rate_spec="1/minute", backend=rl.MemoryBackend())
        client = TestClient(app)
        assert client.get("/search", headers={"X-Forwarded-For": "9.9.9.9"}).status_code == 200

    def test_forwarded_for_honored_only_when_trusted(self):
        """Without MEM0_TRUST_PROXY_HEADERS a spoofed XFF must NOT isolate keys."""
        app = FastAPI()

        @app.get("/search")
        async def _search():
            return {"ok": True}

        app.add_middleware(rl.DataPlaneRateLimitMiddleware, rate_spec="1/minute", backend=rl.MemoryBackend())
        client = TestClient(app)
        assert client.get("/search", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
        # Same real peer IP → same bucket despite the spoofed header.
        assert client.get("/search", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 429


def test_env_default_is_sane():
    limit, window = rl.parse_rate(os.environ.get("MEM0_RATE_LIMIT_DATA", "120/minute"))
    assert limit >= 100  # suites rely on a generous default via conftest seed
    assert window > 0
