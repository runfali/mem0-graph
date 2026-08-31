"""Candidate discovery + refine engine for recursive memory refinement.

Candidate discovery greedily clusters fragmented memories by embedding cosine
similarity (groups of >= 3 fragments get compressed). The refine engine asks
the LLM to draft 1-3 higher-level abstractions.

IRON RULE: the LLM only produces a *proposal*. It is stored in
memory_refine_candidates.suggested_text and NEVER written to the vector store
until explicitly applied (apply/rollback is a later batch). Original memories
are never deleted here.
"""

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.75
MIN_GROUP_SIZE = 3
MAX_SUMMARY_COUNT = 3

REFINE_SYSTEM_PROMPT = (
    "你是记忆精炼器。把以下 N 条碎记忆合并为 1-3 条高层抽象事实。"
    "要求：保留关键主体、时间、数字、关系；去掉过程细节与重复；每条自包含；"
    '只输出 JSON {"topic": "简短主题，不超过20字", "summary": ["...", "..."]}。'
)


def _cosine(a, b) -> float:
    """Cosine similarity between two equal-length float vectors."""
    if not a or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def cluster_candidates(
    memory,
    items: list[dict],
    threshold: float = SIMILARITY_THRESHOLD,
    min_group_size: int = MIN_GROUP_SIZE,
) -> list[dict]:
    """Greedily cluster fragmented memories by embedding similarity.

    Args:
        memory: mem0 Memory instance exposing embedding_model.embed_batch.
        items: list of {"id": str, "data": str} candidate memories.
        threshold: cosine similarity for joining an existing group.
        min_group_size: groups with fewer members produce no candidate.

    Returns:
        list of {"memory_ids": [...], "topic": str} for qualifying groups.
    """
    if not items:
        return []
    texts = [it.get("data", "") for it in items]
    try:
        embeddings = memory.embedding_model.embed_batch(texts, "refine")
    except Exception as e:  # noqa: BLE001 - degrade, never raise to callers
        logger.warning("refine embed_batch failed: %s", e)
        return []

    groups: list[dict] = []  # {"representative": [...], "members": [item]}
    for item, emb in zip(items, embeddings):
        if emb is None:
            continue
        best_idx, best_sim = -1, -1.0
        for i, group in enumerate(groups):
            sim = _cosine(emb, group["representative"])
            if sim > best_sim:
                best_sim, best_idx = sim, i
        if best_idx >= 0 and best_sim >= threshold:
            group = groups[best_idx]
            group["members"].append(item)
            n = len(group["members"])
            group["representative"] = [
                (prev * (n - 1) + cur) / n
                for prev, cur in zip(group["representative"], emb)
            ]
        else:
            groups.append({"representative": list(emb), "members": [item]})

    candidates = []
    for group in groups:
        members = group["members"]
        if len(members) < min_group_size:
            continue
        candidates.append(
            {
                "memory_ids": [m["id"] for m in members],
                "topic": (members[0].get("data", "") or "")[:20],
            }
        )
    return candidates


def _parse_output(response) -> Optional[tuple[Optional[str], list[str]]]:
    """Parse the LLM response into (topic, summary list).

    Accepts both the current {"topic", "summary"} shape and the legacy
    {"summary"} shape (topic then falls back to the first summary line).
    """
    text = response.strip() if isinstance(response, str) else ""
    if not text:
        return None
    parsed = None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(text[start : end + 1])
            except (ValueError, TypeError):
                parsed = None
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, list):
        return None
    cleaned = [str(s).strip() for s in summary if str(s).strip()]
    if not cleaned:
        return None
    topic = parsed.get("topic")
    topic = str(topic).strip() if topic else ""
    if not topic:
        topic = cleaned[0][:20]
    return topic[:20], cleaned[:MAX_SUMMARY_COUNT]


def refine_group(memory, candidate: dict[str, Any]) -> dict:
    """Produce an LLM-drafted summary for one candidate group (proposal only).

    Never writes to the vector store. Returns {"status", "suggested_text"}:
    status is 'proposed' on success, 'failed' on any LLM/parse/missing-text
    failure (graceful, retryable).
    """
    memory_ids = candidate.get("memory_ids") or []
    texts: list[str] = []
    for mid in memory_ids:
        try:
            result = memory.vector_store.get(mid)
        except Exception as e:  # noqa: BLE001
            logger.warning("refine get %s failed: %s", mid, e)
            result = None
        if result is None:
            continue
        payload = getattr(result, "payload", None) or {}
        data = payload.get("data")
        if data:
            texts.append(str(data))

    if not texts:
        logger.warning("refine: no texts found for candidate %s", memory_ids)
        return {"status": "failed", "suggested_text": []}

    try:
        response = memory.llm.generate_response(
            messages=[
                {"role": "system", "content": REFINE_SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))},
            ]
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("refine LLM failed: %s", e)
        return {"status": "failed", "suggested_text": []}

    summary = _parse_output(response)
    if summary is None:
        logger.warning("refine: unparseable LLM output: %r", response)
        return {"status": "failed", "suggested_text": [], "topic": None}
    topic, cleaned = summary
    return {"status": "proposed", "suggested_text": cleaned, "topic": topic}
