import json
import logging
import os
import threading
from copy import deepcopy
from typing import Any, Callable, Dict

from mem0 import Memory

_state_lock = threading.RLock()
_current_config: Dict[str, Any] = {}
_memory_instance: Memory | None = None
_session_factory: Callable | None = None


def set_session_factory(factory: Callable) -> None:
    global _session_factory
    _session_factory = factory


def _load_overrides() -> Dict[str, Any]:
    try:
        if _session_factory is None:
            return {}
        from models import Settings

        session = _session_factory()
        try:
            row = session.get(Settings, "config_overrides")
            if row is None:
                return {}
            return json.loads(row.value)
        finally:
            session.close()
    except Exception:
        return {}


def _merge_config(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value

    return merged


def _load_config_file() -> Dict[str, Any]:
    path = os.environ.get("MEM0_CONFIG_PATH")
    if not path:
        return {}
    try:
        with open(path) as f:
            cfg = json.load(f)
        logging.info("Loaded config from MEM0_CONFIG_PATH=%s", path)
        return cfg
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logging.warning("Failed to load MEM0_CONFIG_PATH=%s: %s", path, e)
        return {}


def _attach_salience_provider(memory: Memory) -> Memory:
    """Wire evolve_salience access counts into search ranking.

    Active only when MEM0_EVOLVE_RANK_WEIGHT>0; otherwise _search_vector_store
    never calls the provider. Reads happen inside scoring (opt-in latency).
    """
    from models import EvolveSalience
    from sqlalchemy import select

    def _provider(memory_ids):
        if not memory_ids or _session_factory is None:
            return {}
        session = _session_factory()
        try:
            rows = session.scalars(
                select(EvolveSalience).where(EvolveSalience.memory_id.in_(memory_ids))
            ).all()
            return {
                r.memory_id: {"acc": r.access_count or 0, "sal": r.salience_score or 1.0}
                for r in rows
            }
        finally:
            session.close()

    memory.salience_provider = _provider
    return memory


def _attach_delete_cleanup(memory: Memory) -> Memory:
    """Wire the evolve_* cascade purge onto memory deletion.

    Any delete path (API delete / delete_all, maintenance scripts) goes
    through the core-library _delete_memory hook, so registering here covers
    every entry point that uses this Memory instance.
    """
    from evolve_cleanup import register_delete_cleanup

    if _session_factory is not None:
        register_delete_cleanup(memory, _session_factory)
    return memory


def _resolve_memory_owner(memory_id: str) -> str | None:
    """Best-effort owner lookup from the vector-store payload.

    Used to stamp evolve_salience.user_id at write time so later ownership
    checks can run off the indexed column instead of re-querying payloads.
    Any failure yields None — registration must never break the add flow.
    """
    try:
        row = get_memory_instance().vector_store.get(vector_id=memory_id)
        payload = getattr(row, "payload", None) or {}
        value = payload.get("user_id")
        return str(value) if value is not None else None
    except Exception:
        return None


def _register_salience_on_add(session_factory, memory_id: str) -> None:
    """Insert an evolve_salience row for a freshly added memory (best-effort).

    Salience was previously lazily registered on first search hit, so
    never-searched memories never surfaced in the stale/idle list. Now they are
    registered at write time. INSERT-if-absent (repeated triggers from e.g.
    refine apply are no-ops); failures are swallowed so a broken registration
    never breaks the add flow. The memory's payload user_id is stamped as the
    row-level owner when resolvable.
    """
    if session_factory is None:
        return
    from datetime import datetime, timezone

    from models import EvolveSalience

    session = session_factory()
    try:
        now = datetime.now(timezone.utc)
        if session.get(EvolveSalience, memory_id) is None:
            session.add(
                EvolveSalience(
                    memory_id=memory_id,
                    user_id=_resolve_memory_owner(memory_id),
                    access_count=0,
                    last_access_at=now,
                    salience_score=1.0,
                    updated_at=now,
                )
            )
            session.commit()
    except Exception:
        session.rollback()
        logging.exception("Failed to register evolve salience for memory %s", memory_id)
    finally:
        session.close()


def _attach_memory_added_cleanup(memory: Memory) -> Memory:
    """Register freshly added memories into evolve_salience at write time."""
    if _session_factory is not None:
        memory.on_memory_added = lambda memory_id: _register_salience_on_add(_session_factory, memory_id)
    return memory


def initialize_state(default_config: Dict[str, Any]) -> None:
    global _current_config, _memory_instance
    with _state_lock:
        _current_config = deepcopy(default_config)
        file_overrides = _load_config_file()
        if file_overrides:
            _current_config = _merge_config(_current_config, file_overrides)
        overrides = _load_overrides()
        if overrides:
            _current_config = _merge_config(_current_config, overrides)
        _memory_instance = _attach_memory_added_cleanup(
            _attach_delete_cleanup(
                _attach_salience_provider(Memory.from_config(_current_config))
            )
        )


def _config_file_path() -> str:
    return os.environ.get("MEM0_CONFIG_PATH") or "/app/config.json"


def _save_config_file(path: str, config: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def update_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    global _current_config, _memory_instance
    with _state_lock:
        next_config = _merge_config(_current_config, updates)
        _save_config_file(_config_file_path(), next_config)
        _current_config = next_config
        _memory_instance = _attach_memory_added_cleanup(
            _attach_delete_cleanup(
                _attach_salience_provider(Memory.from_config(next_config))
            )
        )
        return deepcopy(_current_config)


def get_current_config() -> Dict[str, Any]:
    with _state_lock:
        return deepcopy(_current_config)


def get_memory_instance() -> Memory:
    with _state_lock:
        if _memory_instance is None:
            raise RuntimeError("Mem0 runtime has not been initialized.")
        return _memory_instance
