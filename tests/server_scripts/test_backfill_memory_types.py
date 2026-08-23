"""Backfill script unit tests (no real DB — fully mocked).

Exercises discover_users / backfill_memories with a fake memory whose
vector_store.list returns payload-bearing rows and whose vector_store.update
records calls.
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock


_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "server", "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import backfill_memory_types as bf


def _memory(rows, llm_return=json.dumps({"type": "DECISIONS"})):
    memory = MagicMock()
    memory.vector_store.list.return_value = rows
    memory.vector_store.update = MagicMock()
    memory.llm.generate_response = MagicMock(return_value=llm_return)
    return memory


def _row(mem_id, payload):
    return SimpleNamespace(id=mem_id, payload=dict(payload))


class TestBackfillMemoryTypes:
    def test_valid_type_memory_skipped(self):
        rows = [_row("m1", {"data": "服务器在 192.0.2.163", "user_id": "hermes-user", "memory_type": "FACTS"})]
        memory = _memory(rows)

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows)

        assert (scanned, pending, updated) == (1, 0, 0)
        memory.vector_store.update.assert_not_called()

    def test_force_reclassifies_existing_valid_type(self):
        rows = [_row("m1", {"data": "服务器在 192.0.2.163", "user_id": "hermes-user", "memory_type": "EXPERIENCES"})]
        memory = _memory(rows)

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows, force=True)

        assert (pending, updated) == (1, 1)
        assert dist == {"FACTS": 1}
        memory.vector_store.update.assert_called_once()

    def test_force_with_dry_run_only_reports(self):
        rows = [_row("m1", {"data": "服务器在 192.0.2.163", "user_id": "hermes-user", "memory_type": "EXPERIENCES"})]
        memory = _memory(rows)

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows, force=True, dry_run=True)

        assert (pending, updated) == (1, 0)
        memory.vector_store.update.assert_not_called()

    def test_missing_type_backfilled_with_merged_payload(self):
        old = {"data": "服务器部署在 192.0.2.163", "user_id": "hermes-user", "created_at": "2026-01-01"}
        rows = [_row("m1", old)]
        memory = _memory(rows)

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows)

        assert (pending, updated) == (1, 1)
        assert dist == {"FACTS": 1}
        memory.vector_store.update.assert_called_once_with(vector_id="m1", payload={**old, "memory_type": "FACTS"})

    def test_invalid_type_reclassified(self):
        rows = [_row("m1", {"data": "发哥喜欢咖啡", "user_id": "hermes-user", "memory_type": "TEMP"})]
        memory = _memory(rows)

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows)

        assert (pending, updated) == (1, 1)
        assert dist == {"PREFERENCES": 1}
        assert memory.vector_store.update.call_args.kwargs["payload"]["memory_type"] == "PREFERENCES"

    def test_dry_run_reports_without_updating(self):
        rows = [
            _row("m1", {"data": "发哥喜欢咖啡", "user_id": "hermes-user"}),
            _row("m2", {"data": "服务器在 192.0.2.163", "user_id": "hermes-user"}),
        ]
        memory = _memory(rows)

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows, dry_run=True)

        assert (pending, updated) == (2, 0)
        assert dist == {"FACTS": 1, "PREFERENCES": 1}
        assert by_user == {"hermes-user": 2}
        memory.vector_store.update.assert_not_called()

    def test_use_llm_uses_llm_type(self):
        rows = [_row("m1", {"data": "服务器在 192.0.2.163", "user_id": "hermes-user"})]
        memory = _memory(rows, llm_return=json.dumps({"type": "DECISIONS"}))

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows, use_llm=True)

        assert (pending, updated) == (1, 1)
        assert dist == {"DECISIONS": 1}
        assert memory.vector_store.update.call_args.kwargs["payload"]["memory_type"] == "DECISIONS"

    def test_use_llm_exception_falls_back_to_rules(self):
        rows = [_row("m1", {"data": "发哥喜欢咖啡", "user_id": "hermes-user"})]
        memory = _memory(rows)
        memory.llm.generate_response.side_effect = TimeoutError("timeout")

        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows, use_llm=True)

        assert (pending, updated) == (1, 1)
        assert dist == {"PREFERENCES": 1}

    def test_multi_user_discover_covers_all(self):
        rows = [
            _row("m1", {"data": "发哥喜欢咖啡", "user_id": "hermes-user"}),
            _row("m2", {"data": "服务器在 192.0.2.163", "user_id": "another-user"}),
        ]
        memory = _memory(rows)

        assert bf.discover_users(memory) == ["another-user", "hermes-user"]
        scanned, pending, updated, dist, by_user = bf.backfill_memories(memory, rows)

        assert (pending, updated) == (2, 2)
        assert by_user == {"hermes-user": 1, "another-user": 1}
