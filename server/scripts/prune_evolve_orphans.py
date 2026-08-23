"""Prune orphaned evolve_* rows whose memory no longer exists in mem0_memories.

Cron-driven (docker exec mem0-dev-mem0-1 sh -c "cd /app && python3 scripts/prune_evolve_orphans.py").
Safety net for any delete path that bypasses the core-library cleanup hook
(manual SQL, future scripts, etc.). Runs AFTER the cascade hook, so rows it
finds are leftovers the hook could not catch.

Watchdog-friendly stdout: lines are emitted only when something was deleted.
No orphans at all -> empty output, exit 0.

Environment:
  PRUNE_DRY_RUN            — "true" to report orphans without deleting
"""

import logging
import os

from sqlalchemy import text

from db import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("prune_evolve_orphans")

# memory_id column -> evolve table. mem0_memories.id is uuid, evolve memory_id is varchar.
TABLES = ["evolve_salience", "evolve_feedback", "evolve_salience_adjustments"]


def find_orphans(session, table: str) -> list[str]:
    """Return memory_ids present in the evolve table but missing from mem0_memories."""
    sql = text(
        f"""
        SELECT {table}.memory_id
        FROM {table}
        LEFT JOIN mem0_memories m ON m.id::text = {table}.memory_id
        WHERE m.id IS NULL
        """
    )
    rows = session.execute(sql).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def delete_orphans(session, table: str, ids: list[str], dry_run: bool) -> int:
    if not ids:
        return 0
    if dry_run:
        logger.info("[DRY_RUN] %s orphan row(s) in %s", len(ids), table)
        return len(ids)
    sql = text(
        f"DELETE FROM {table} WHERE memory_id = ANY(:ids)"
    )
    result = session.execute(sql, {"ids": ids})
    return result.rowcount or 0


def main() -> int:
    dry_run = os.environ.get("PRUNE_DRY_RUN", "").lower() == "true"
    try:
        session = SessionLocal()
    except Exception as e:
        logger.error("cannot open DB session: %s", e)
        return 1

    total = 0
    try:
        for table in TABLES:
            ids = find_orphans(session, table)
            if not ids:
                continue
            removed = delete_orphans(session, table, ids, dry_run)
            total += removed
            logger.info("pruned %s: %s row(s) (%s ids)", table, removed, len(ids))
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error("prune failed: %s", e)
        return 1
    finally:
        session.close()

    if total:
        prefix = "[DRY_RUN] " if dry_run else ""
        print(f"{prefix}pruned {total} orphaned evolve row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
