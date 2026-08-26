"""Best-effort cascade cleanup of evolve_* heat rows when a memory is deleted.

Wired onto the core-library delete hook (Memory.on_memory_deleted) from both
the API server (server_state) and the maintenance scripts, so any delete entry
point leaves no orphan evolve_salience / evolve_feedback /
evolve_salience_adjustments rows behind.
"""

import logging
from typing import Any, Callable, Dict

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from models import EvolveFeedback, EvolveSalience, EvolveSalienceAdjustment

logger = logging.getLogger(__name__)


def purge_evolve_memory(session_factory: Callable, memory_id: str) -> None:
    """Delete every evolve_* row for a deleted memory.

    Best-effort: a failure is logged and swallowed so cleanup never breaks
    the memory delete flow (mirrors _persist_evolve_query's fault tolerance).
    """
    session = session_factory()
    try:
        session.execute(delete(EvolveSalience).where(EvolveSalience.memory_id == memory_id))
        session.execute(delete(EvolveFeedback).where(EvolveFeedback.memory_id == memory_id))
        session.execute(delete(EvolveSalienceAdjustment).where(EvolveSalienceAdjustment.memory_id == memory_id))
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("Failed to purge evolve rows for memory %s", memory_id)
    finally:
        session.close()


def register_delete_cleanup(memory, session_factory: Callable) -> None:
    """Attach the cascade purge as the memory instance's delete hook."""
    memory.on_memory_deleted = lambda memory_id: purge_evolve_memory(session_factory, memory_id)


def build_session_factory(config: Dict[str, Any]) -> Callable:
    """SQLAlchemy sessionmaker bound to the postgres connection in the
    vector_store config (mirrors server/db.py's engine construction).

    Used by the standalone maintenance scripts, which run in their own
    process and have no access to the API server's session factory.
    """
    vs_config = (config or {}).get("vector_store", {}).get("config", {})
    # 三轮审计：凭据必须 URL 编码（与 server/db.py 的 quote_plus 对齐）——
    # 密码含 @ : / % 等特殊字符时裸拼接会破坏连接串解析，主服务正常而
    # 维护脚本静默连错库
    from urllib.parse import quote_plus

    url = "postgresql+psycopg://{user}:{password}@{host}:{port}/{dbname}".format(
        user=quote_plus(vs_config.get("user", "postgres")),
        password=quote_plus(vs_config.get("password", "postgres")),
        host=vs_config.get("host", "postgres"),
        port=vs_config.get("port", "5432"),
        dbname=quote_plus(vs_config.get("dbname", "postgres")),
    )
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
