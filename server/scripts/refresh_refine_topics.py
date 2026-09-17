"""Regenerate refine-candidate titles that predate the LLM title fix.

Candidates created before 2026-08-31 carried a topic cut from the first 20
characters of a raw memory ("mem0_falkordb 审计修复与未"), because the title
was never generated. This one-shot backfill re-asks the LLM for the title of
each candidate group and writes ONLY the topic column back.

Guarantees:
  - the vector store is never written (no insert / update / delete);
  - status / suggested_text / memory_ids are left untouched, so the
    apply/rollback semantics of every candidate are unaffected;
  - a group whose source memories are gone, or whose LLM call fails, keeps
    its existing topic and the run continues (partial progress is committed).

Environment:
  MEM0_CONFIG_PATH        — path to config.json (default: /app/config.json)
  REFRESH_TOPIC_DRY_RUN   — "true" to report without writing
  REFRESH_TOPIC_STATUSES  — comma-separated candidate statuses to refresh
                            (default: proposed)
  REFRESH_TOPIC_LIMIT     — max candidates per run (default: 0 = no limit)

Usage (inside the container):
  python3 scripts/refresh_refine_topics.py
"""

import json
import logging
import os
import sys

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _APP_DIR not in sys.path:
    sys.path.insert(0, _APP_DIR)

from sqlalchemy import select  # noqa: E402

from db import SessionLocal  # noqa: E402
from models import MemoryRefineCandidate  # noqa: E402
from refine_memory import refresh_topic  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("refresh_refine_topics")

DEFAULT_STATUS = "proposed"


def _statuses() -> tuple[str, ...]:
    raw = os.environ.get("REFRESH_TOPIC_STATUSES", DEFAULT_STATUS)
    values = tuple(s.strip() for s in raw.split(",") if s.strip())
    return values or (DEFAULT_STATUS,)


def _select_candidates(db, statuses: tuple[str, ...]) -> list[MemoryRefineCandidate]:
    return list(
        db.execute(
            select(MemoryRefineCandidate)
            .where(MemoryRefineCandidate.status.in_(statuses))
            .order_by(MemoryRefineCandidate.id)
        )
        .scalars()
        .all()
    )


def main() -> int:
    config_path = os.environ.get("MEM0_CONFIG_PATH", "/app/config.json")
    dry_run = os.environ.get("REFRESH_TOPIC_DRY_RUN", "").lower() == "true"
    limit = int(os.environ.get("REFRESH_TOPIC_LIMIT", "0") or 0)

    if not dry_run and not os.path.exists(config_path):
        logger.error("config not found: %s", config_path)
        return 1

    statuses = _statuses()
    memory = None
    if not dry_run:
        with open(config_path) as f:
            config = json.load(f)
        from mem0 import Memory

        config["version"] = "v1.1"
        if "graph_store" not in config:
            config["graph_store"] = {"provider": "memory", "config": None}
        memory = Memory.from_config(config)

    updated = failed = 0
    with SessionLocal() as db:
        rows = _select_candidates(db, statuses)
        if limit > 0:
            rows = rows[:limit]
        logger.info("refreshing %d candidate(s) (dry_run=%s)", len(rows), dry_run)
        for row in rows:
            before = row.topic
            if dry_run:
                logger.info("[DRY_RUN] #%s %r", row.id, before)
                continue
            outcome = refresh_topic(
                memory, {"memory_ids": row.memory_ids, "topic": before}
            )
            if outcome["status"] != "updated":
                failed += 1
                logger.warning("#%s: 保留原标题（生成失败） %r", row.id, before)
                continue
            row.topic = outcome["topic"]
            updated += 1
            logger.info("#%s: %r -> %r", row.id, before, row.topic)
        if dry_run:
            db.rollback()
        if updated:
            db.commit()

    report = f"updated {updated}, kept {failed} of {updated + failed} candidate(s)"
    if dry_run:
        report = "[DRY_RUN] " + report
    logger.info(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
