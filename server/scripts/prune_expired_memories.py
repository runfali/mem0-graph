"""Prune expired memories and orphaned FalkorDB nodes.

Run inside the mem0 container. Wire into a cron job.

Environment:
  MEM0_CONFIG_PATH         — path to config.json (default: /app/config.json)
  MEMORY_RETENTION_DAYS    — delete memories past this age (default: 0 = only use expiration_date)
  PRUNE_DRY_RUN            — "true" to only report without deleting

Discovery: scans the vector store for all unique user_ids.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("prune_expired")

SCAN_LIMIT = 10_000


def discover_users(memory) -> list[str]:
    """Scan vector store payloads for unique user_ids."""
    results = memory.vector_store.list(top_k=SCAN_LIMIT)
    rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []
    user_ids: set[str] = set()
    for row in rows:
        payload = getattr(row, "payload", None) or {}
        uid = payload.get("user_id")
        if uid:
            user_ids.add(str(uid))
    return sorted(user_ids)


def delete_expired_memories(memory, user_id: str, retention_days: int, dry_run: bool) -> int:
    """Delete all expired memories for a user. Returns count."""
    try:
        all_mems = memory.get_all(filters={"user_id": user_id}, show_expired=True)
    except Exception as e:
        logger.error("get_all failed for %s: %s", user_id, e)
        return 0

    results = all_mems.get("results", [])
    if not results:
        return 0

    now = datetime.now(timezone.utc)
    deleted = 0
    for mem in results:
        expire = mem.get("expiration_date")
        created = mem.get("created_at")
        should_delete = False
        reason = ""

        if expire:
            try:
                if datetime.fromisoformat(expire) < now:
                    should_delete = True
                    reason = "expired"
            except (ValueError, TypeError):
                pass

        if not should_delete and retention_days > 0 and created:
            try:
                age_days = (now - datetime.fromisoformat(created)).days
                if age_days > retention_days:
                    should_delete = True
                    reason = f"older_than_{retention_days}d"
            except (ValueError, TypeError):
                pass

        if should_delete:
            reason = reason or "unknown"
            if dry_run:
                logger.info("[DRY_RUN] delete %s: %s (%s)", mem["id"][:8], mem.get("memory", "")[:60], reason)
            else:
                try:
                    memory.delete(mem["id"])
                    logger.info("deleted %s: %s (%s)", mem["id"][:8], mem.get("memory", "")[:60], reason)
                except Exception as e:
                    logger.error("delete failed %s: %s", mem["id"][:8], e)
            deleted += 1

    return deleted


def cleanup_orphans(memory, user_id: str, dry_run: bool) -> int:
    """Delete orphaned FalkorDB nodes (zero relationships)."""
    graph = getattr(memory, "graph", None)
    if not graph:
        return 0
    wrapper = getattr(graph, "graph", None)
    if not wrapper or not hasattr(wrapper, "query"):
        return 0
    node_label = graph.node_label or ":`__Entity__`"
    match_clause = f"MATCH (n {node_label}) WHERE NOT (n)--()"

    try:
        if dry_run:
            result = wrapper.query(
                f"{match_clause} RETURN count(n) AS cnt", user_id=user_id
            )
            cnt = result[0]["cnt"] if result else 0
            if cnt:
                logger.info("[DRY_RUN] %s orphan nodes for %s", cnt, user_id)
            return cnt
        else:
            result = wrapper.query(
                f"{match_clause} DETACH DELETE n RETURN count(n) AS cnt",
                user_id=user_id,
            )
            cnt = result[0]["cnt"] if result else 0
            if cnt:
                logger.info("deleted %s orphan nodes for %s", cnt, user_id)
            return cnt
    except Exception as e:
        if "localhost" not in str(e):
            logger.warning("FalkorDB cleanup failed for %s: %s", user_id, e)
        return 0


def main() -> int:
    config_path = os.environ.get("MEM0_CONFIG_PATH", "/app/config.json")
    retention_raw = os.environ.get("MEMORY_RETENTION_DAYS", "0").strip()
    retention = int(retention_raw) if retention_raw else 0
    dry_run = os.environ.get("PRUNE_DRY_RUN", "").lower() == "true"

    if not os.path.exists(config_path):
        logger.error("config not found: %s", config_path)
        return 1

    with open(config_path) as f:
        config = json.load(f)

    from mem0 import Memory

    config["version"] = "v1.1"
    if "graph_store" not in config:
        config["graph_store"] = {"provider": "memory", "config": None}

    memory = Memory.from_config(config)

    # Scripts run from /app/scripts/; server modules (evolve_cleanup) live in /app/.
    _app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _app_dir not in sys.path:
        sys.path.insert(0, _app_dir)
    from evolve_cleanup import build_session_factory, register_delete_cleanup

    register_delete_cleanup(memory, build_session_factory(config))
    user_ids = discover_users(memory)

    if not user_ids:
        logger.info("no users found — nothing to prune")
        return 0

    logger.info("pruning %s users (dry_run=%s, retention=%sd)", len(user_ids), dry_run, retention if retention > 0 else "expiry-only")

    total_expired = 0
    total_orphans = 0
    for uid in user_ids:
        total_expired += delete_expired_memories(memory, uid, retention, dry_run)
        total_orphans += cleanup_orphans(memory, uid, dry_run)

    report = f"deleted {total_expired} expired memories, {total_orphans} orphan nodes"
    if dry_run:
        report = "[DRY_RUN] " + report
    logger.info(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
