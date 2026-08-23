"""Server-side salience-on-add wiring (tests/test level).

_attach_memory_added_cleanup registers memory.on_memory_added so every freshly
added memory gets an evolve_salience row (access_count=0, salience_score=1.0)
at write time — the fix for never-searched memories never surfacing in the
stale/idle list. INSERT-if-absent: repeated triggers are no-ops.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from db import Base  # noqa: E402
from models import EvolveSalience  # noqa: E402
import server_state  # noqa: E402


def _make_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class TestAttachMemoryAddedCleanup:
    def test_attach_registers_salience_on_add(self, monkeypatch):
        Session = _make_session_factory()
        monkeypatch.setattr(server_state, "_session_factory", Session)
        memory = MagicMock()

        server_state._attach_memory_added_cleanup(memory)

        assert memory.on_memory_added is not None
        memory.on_memory_added("mem-1")

        with Session() as db:
            row = db.get(EvolveSalience, "mem-1")
            assert row is not None
            assert row.access_count == 0
            assert row.salience_score == 1.0
            assert row.last_access_at is not None

    def test_repeated_trigger_does_not_duplicate(self, monkeypatch):
        Session = _make_session_factory()
        monkeypatch.setattr(server_state, "_session_factory", Session)
        memory = MagicMock()

        server_state._attach_memory_added_cleanup(memory)
        memory.on_memory_added("mem-1")
        memory.on_memory_added("mem-1")
        memory.on_memory_added("mem-1")

        with Session() as db:
            rows = db.query(EvolveSalience).filter(EvolveSalience.memory_id == "mem-1").all()
            assert len(rows) == 1

    def test_register_failure_swallowed(self, monkeypatch):
        class _BoomSession:
            def __init__(self, *a, **k):
                self.boom = True

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, *a, **k):
                return None

            def add(self, *a, **k):
                raise RuntimeError("db down")

            def commit(self):
                raise RuntimeError("db down")

            def rollback(self):
                return None

            def close(self):
                return None

        monkeypatch.setattr(server_state, "_session_factory", _BoomSession)
        memory = MagicMock()

        server_state._attach_memory_added_cleanup(memory)

        # Must not raise even though the registration write fails.
        memory.on_memory_added("mem-1")


class TestOwnerStampingOnAdd:
    def test_owner_stamped_from_vector_payload(self, monkeypatch):
        Session = _make_session_factory()
        monkeypatch.setattr(server_state, "_session_factory", Session)
        fake_memory = MagicMock()
        fake_memory.vector_store.get.return_value = MagicMock(payload={"user_id": "user-9"})
        monkeypatch.setattr(server_state, "get_memory_instance", lambda: fake_memory)
        memory = MagicMock()
        server_state._attach_memory_added_cleanup(memory)

        memory.on_memory_added("mem-owned")

        with Session() as db:
            row = db.get(EvolveSalience, "mem-owned")
            assert row is not None
            assert row.user_id == "user-9"

    def test_owner_left_null_when_unresolvable(self, monkeypatch):
        Session = _make_session_factory()
        monkeypatch.setattr(server_state, "_session_factory", Session)
        fake_memory = MagicMock()
        fake_memory.vector_store.get.side_effect = RuntimeError("store down")
        monkeypatch.setattr(server_state, "get_memory_instance", lambda: fake_memory)
        memory = MagicMock()
        server_state._attach_memory_added_cleanup(memory)

        memory.on_memory_added("mem-orphan")  # must not raise

        with Session() as db:
            row = db.get(EvolveSalience, "mem-orphan")
            assert row is not None
            assert row.user_id is None
