"""Regression tests: entity cleanup must not be skipped by lazy init.

``Memory._entity_store`` is lazily initialized on first access to the
``entity_store`` property. Processes that only delete memories (dedup /
expired-memory cleanup jobs) never touch the property, so
``_remove_memory_from_entity_store`` used to return early on
``self._entity_store is None`` and orphan entity rows accumulated
(``linked_memory_ids`` pointing at already-deleted memories).

These tests assert that deleting a memory triggers entity-store cleanup even
when ``_entity_store`` is still None — i.e. the cleanup path forces lazy
initialization instead of bailing out — for both the sync and async variants.
"""

from unittest.mock import Mock

import pytest

from mem0.memory.main import AsyncMemory, Memory


def _make_delete_payload():
    return {
        "data": "some old memory",
        "user_id": "user-1",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def _setup_factory_mocks(mocker, entity_store_create_exc=None):
    """Patch factories so Memory() builds, and the 2nd VectorStoreFactory
    create call (the lazily-initialized entity store) returns our mock (or
    raises ``entity_store_create_exc`` when given)."""
    entity_store_mock = mocker.MagicMock()
    entity_store_mock.list.return_value = []
    vector_store_mock = mocker.MagicMock()
    second_create_result = (
        entity_store_create_exc if entity_store_create_exc is not None else entity_store_mock
    )
    mocker.patch(
        "mem0.utils.factory.VectorStoreFactory.create",
        side_effect=[vector_store_mock, second_create_result],
    )
    mocker.patch("mem0.utils.factory.EmbedderFactory.create", mocker.MagicMock())
    mocker.patch("mem0.utils.factory.LlmFactory.create", mocker.MagicMock())
    # Patch the name bound in mem0.memory.main (from ... import SQLiteManager),
    # not the defining module — patching the latter has no effect.
    mocker.patch("mem0.memory.main.SQLiteManager", mocker.MagicMock())
    # Disable telemetry so __init__ doesn't consume a second
    # VectorStoreFactory.create call for the migration/telemetry store.
    mocker.patch("mem0.memory.main.MEM0_TELEMETRY", False)
    mocker.patch("mem0.memory.main.capture_event", mocker.MagicMock())
    return entity_store_mock, vector_store_mock


class TestEntityCleanupNotSkippedByLazyInit:
    def test_sync_delete_forces_entity_store_initialization(self, mocker):
        entity_store_mock, _ = _setup_factory_mocks(mocker)

        memory = Memory()
        assert memory._entity_store is None  # lazy: never touched so far

        existing = Mock(payload=_make_delete_payload())
        memory._delete_memory("mem-1", existing_memory=existing)

        # The cleanup path must have forced lazy initialization of the entity
        # store via the property instead of silently returning.
        assert memory._entity_store is entity_store_mock
        entity_store_mock.list.assert_called_once_with(
            filters={"user_id": "user-1"}, top_k=10000
        )

    @pytest.mark.asyncio
    async def test_async_delete_forces_entity_store_initialization(self, mocker):
        entity_store_mock, _ = _setup_factory_mocks(mocker)

        memory = AsyncMemory()
        assert memory._entity_store is None  # lazy: never touched so far

        existing = Mock(payload=_make_delete_payload())
        await memory._delete_memory("mem-1", existing_memory=existing)

        # Same guarantee for the async cleanup path.
        assert memory._entity_store is entity_store_mock
        entity_store_mock.list.assert_called_once_with(
            filters={"user_id": "user-1"}, top_k=10000
        )

    def test_sync_entity_cleanup_unavailable_is_non_fatal(self, mocker):
        """If the entity store cannot be initialized, cleanup is skipped but
        the primary delete path must still complete."""
        entity_store_mock, vector_store_mock = _setup_factory_mocks(
            mocker, entity_store_create_exc=RuntimeError("provider unavailable")
        )

        memory = Memory()
        assert memory._entity_store is None

        existing = Mock(payload=_make_delete_payload())
        result = memory._delete_memory("mem-1", existing_memory=existing)

        assert result == "mem-1"  # delete completed despite entity store failure
        assert memory._entity_store is None  # init failed, stayed None
        assert memory._entity_store is not entity_store_mock
        vector_store_mock.delete.assert_called_once_with(vector_id="mem-1")

    @pytest.mark.asyncio
    async def test_async_entity_cleanup_unavailable_is_non_fatal(self, mocker):
        entity_store_mock, vector_store_mock = _setup_factory_mocks(
            mocker, entity_store_create_exc=RuntimeError("provider unavailable")
        )

        memory = AsyncMemory()
        assert memory._entity_store is None

        existing = Mock(payload=_make_delete_payload())
        result = await memory._delete_memory("mem-1", existing_memory=existing)

        assert result == "mem-1"  # delete completed despite entity store failure
        assert memory._entity_store is None  # init failed, stayed None
        assert memory._entity_store is not entity_store_mock
        vector_store_mock.delete.assert_called_once_with(vector_id="mem-1")
