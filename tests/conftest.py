"""Root pytest fixtures: hermetic environment & import-path seeds.

Two ordering hazards this file eliminates:

1. Import-time constant freezing. server/auth.py reads JWT_SECRET /
   AUTH_DISABLED into module constants at first import, and server/main.py
   validates them at ITS import. Whichever test file pytest collects first
   therefore decides those values for the entire session — per-file
   os.environ.setdefault calls in later files have no effect on the frozen
   constants. Seeding defaults here guarantees every session starts from a
   known state regardless of file order or the developer's shell env.

2. Flat-import coupling. server/*.py use bare sibling imports (``import
   telemetry``, ``from db import ...``), mirroring how they run in Docker.
   Tests that ``import server.main`` fail with ModuleNotFoundError unless
   the server/ directory itself is on sys.path. Appending it once here
   covers every import style used by the suites.

The auth-matrix tests (tests/test_server_auth.py) reload server.main under
patch.dict environments; they build fully explicit env baselines and are
unaffected by these seeds.
"""

import os
import sys
import tempfile

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-for-production")
os.environ.setdefault("AUTH_DISABLED", "true")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
# server_state persists config changes to MEM0_CONFIG_PATH (default /app/config.json
# inside Docker); redirect to a throwaway file so tests never touch the host FS.
_TEST_CFG_DIR = tempfile.mkdtemp(prefix="mem0-server-tests-")
os.environ.setdefault("MEM0_CONFIG_PATH", os.path.join(_TEST_CFG_DIR, "config.json"))
# Force mem0's default history DB (~/.mem0/history.db) into a throwaway dir:
# SDK-level suites construct Memory() with default config, which must never
# touch (or depend on the writability of) the developer's real home dir.
os.environ["MEM0_DIR"] = os.path.join(_TEST_CFG_DIR, "mem0-home")

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "server")
if os.path.isdir(_SERVER_DIR) and _SERVER_DIR not in sys.path:
    # append (not insert): repo root stays first so project packages win.
    sys.path.append(_SERVER_DIR)

# Session-scoped SQLite app database so server suites that exercise real
# credential/DB flows (register/login/api-keys) run hermetically instead of
# dialing the docker-compose 'postgres' host. db.py reads DATABASE_URL at its
# first import — which conftest precedes — and models are portable across
# Postgres and sqlite via SQLAlchemy 2.x types.
if "DATABASE_URL" not in os.environ:
    _TEST_DB_DIR = tempfile.mkdtemp(prefix="mem0-server-tests-")
    os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB_DIR}/mem0-app.db"

    def _create_app_tables() -> None:
        server_dir_in_path = _SERVER_DIR in sys.path
        if not server_dir_in_path:
            sys.path.insert(0, _SERVER_DIR)
        try:
            import db as server_db  # noqa: E402
            import models as server_models  # noqa: E402

            server_models.Base.metadata.create_all(server_db.engine)
        finally:
            if not server_dir_in_path:
                sys.path.remove(_SERVER_DIR)

    try:
        import db  # noqa: F401  -- freeze the engine URL before any test imports it

        _create_app_tables()
    except Exception:  # pragma: no cover - keep collection alive without server/
        pass

# ---------------------------------------------------------------------------
# Global-singleton hygiene. Several evolve/server suites stub out
# server_state.initialize_state / get_memory_instance with plain module-level
# assignments (needed at import time so main.py can be imported without a live
# vector store) and never restore them — leaking fakes into every suite that
# runs afterwards. Snapshot the pristine callables NOW (before any test module
# is collected) and restore them after each test.
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

try:
    import server_state as _server_state  # noqa: E402

    _PRISTINE_SERVER_STATE = {
        name: getattr(_server_state, name)
        for name in ("initialize_state", "get_memory_instance", "get_current_config")
        if hasattr(_server_state, name)
    }
except Exception:  # pragma: no cover - server/ optional for pure-SDK runs
    _PRISTINE_SERVER_STATE = None


if _PRISTINE_SERVER_STATE:

    @pytest.fixture(autouse=True)
    def _restore_server_state_singletons():
        yield
        for name, value in _PRISTINE_SERVER_STATE.items():
            setattr(_server_state, name, value)
