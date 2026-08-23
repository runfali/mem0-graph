"""Consolidate related memories by entity group.

DEPRECATED (2026-08-09): 本脚本已弃用，请使用 dedup_memories.py。
原因：fallback 模式把用户全部记忆当一个组做 LLM 压缩合并后删除旧记忆，
曾导致生产记忆从几十条被压至 3 条（信息丢失）。新方案只做语义去重
（合并近重复、不压缩），见 dedup_memories.py。
保留本文件仅为历史参考，cron 已切换至 dedup_memories.py，勿再使用。

Scans users, groups memories by FalkorDB entity, merges groups with 3+
related items into concise summaries. Adds new memories first, then
deletes old ones (crash-safe ordering).

Environment:
  MEM0_CONFIG_PATH         — path to config.json (default: /app/config.json)
  CONSOLIDATION_DRY_RUN    — "true" to only report without changing anything
  CONSOLIDATION_MIN_GROUP  — minimum group size to trigger merge (default: 3)
"""

import json
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

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


def get_entity_groups(memory, user_id: str) -> dict[str, list[dict]]:
    """Group memories by entity name from FalkorDB.

    Falls back to grouping by last N memories when FalkorDB unavailable.
    Returns dict of entity_name -> list of memory dicts.
    """
    groups: dict[str, list[dict]] = {}

    # Try FalkorDB entity grouping first
    graph = getattr(memory, "graph", None)
    if graph and hasattr(graph, "query"):
        try:
            result = graph.query(
                "MATCH (n:Entity {user_id: $uid}) RETURN n.name AS name, n.mentions AS mentions ORDER BY n.mentions DESC",
                {"uid": user_id},
            )
            entity_names = [row[0] for row in result] if result else []
            if entity_names:
                # Get memories for each entity
                for name in entity_names[:10]:  # top 10 entities
                    results = memory.search(name, filters={"user_id": user_id}, top_k=20)
                    mems = results.get("results", [])
                    if len(mems) >= 3:
                        groups[name] = mems
                return groups
        except Exception:
            logger.debug("FalkorDB grouping failed for %s, falling back", user_id)

    # Fallback: use recent memories grouped by search keywords
    all_mems = memory.get_all(filters={"user_id": user_id}, top_k=50)
    mems = all_mems.get("results", [])
    if len(mems) >= 3:
        groups["recent"] = mems
    return groups


def merge_group(memory, entity: str, mems: list[dict], dry_run: bool) -> tuple[int, int]:
    """Merge a group of memories into consolidated facts.

    Returns (old_count, new_count).
    Order: add merged first, then delete old (crash-safe).
    """
    texts = [m.get("memory", "") for m in mems if m.get("memory")]
    if not texts:
        return 0, 0

    # Build LLM merge prompt
    prompt = f"""将以下关于同一主题的 {len(texts)} 条记忆合并为 1-3 条精炼事实。

要求：
- 保留所有关键事实，去除重复和冗余
- 每条事实独立、自包含、上下文丰富
- 保留所有实体名称（人名、地名等）
- 按时间顺序排列（如有时间信息）
- 输出 JSON 格式：{{"merged": ["事实1", "事实2"]}}

原始记忆：
{json.dumps(texts, ensure_ascii=False, indent=2)}
"""

    try:
        response = memory.llm.generate_response([{"role": "user", "content": prompt}])
    except Exception as e:
        logger.warning("LLM merge failed for '%s': %s", entity[:30], e)
        return 0, 0

    try:
        parsed = json.loads(response)
        merged = parsed.get("merged", [])
    except (json.JSONDecodeError, TypeError):
        logger.warning("LLM response parse failed for '%s'", entity[:30])
        return 0, 0

    if not merged or len(merged) >= len(texts):
        logger.info("Skipping '%s': merge not beneficial (%s -> %s)", entity[:30], len(texts), len(merged))
        return 0, 0

    old_ids = [m.get("id") for m in mems if m.get("id")]

    if dry_run:
        logger.info("[DRY_RUN] Would merge '%s': %s memories -> %s", entity[:30], len(texts), len(merged))
        return len(texts), len(merged)

    # Step 1: add merged memories first
    user_id = mems[0].get("user_id") or "hermes-user"
    agent_id = mems[0].get("agent_id") or "hermes"
    new_ids = []
    for t in merged:
        try:
            r = memory.add(t, user_id=user_id, agent_id=agent_id, infer=False)
            for item in r.get("results", []):
                if item.get("id"):
                    new_ids.append(item["id"])
        except Exception as e:
            logger.error("Failed to add merged memory for '%s': %s", entity[:30], e)

    if not new_ids:
        logger.warning("No new memories created for '%s' — skipping delete", entity[:30])
        return 0, 0

    # Step 2: delete old memories (safe — new ones already exist)
    for mid in old_ids:
        try:
            memory.delete(mid)
        except Exception as e:
            logger.warning("Failed to delete old memory %s: %s", mid[:8], e)

    logger.info("Merged '%s': %s memories -> %s", entity[:30], len(texts), len(merged))
    return len(texts), len(merged)


def main() -> int:
    config_path = os.environ.get("MEM0_CONFIG_PATH", "/app/config.json")
    dry_run = os.environ.get("CONSOLIDATION_DRY_RUN", "").lower() == "true"
    min_group = int(os.environ.get("CONSOLIDATION_MIN_GROUP", "3"))

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
        logger.info("no users found — nothing to consolidate")
        return 0

    logger.info("consolidating %s users (dry_run=%s, min_group=%s)", len(user_ids), dry_run, min_group)

    total_old = 0
    total_new = 0
    skipped = 0
    for uid in user_ids:
        groups = get_entity_groups(memory, uid)
        if not groups:
            skipped += 1
            continue
        for entity, mems in groups.items():
            old, new = merge_group(memory, entity, mems, dry_run)
            total_old += old
            total_new += new

    report = f"consolidated {total_old} memories into {total_new} across {len(user_ids)} users"
    if skipped:
        report += f" ({skipped} users skipped — < {min_group} related memories)"
    if dry_run:
        report = "[DRY_RUN] " + report
    logger.info(report)
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
