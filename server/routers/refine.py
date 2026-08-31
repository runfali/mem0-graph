"""Recursive memory refinement: candidate discovery + candidate listing.

Batch scope: POST generates candidates (stale-memory discovery -> clustering ->
LLM proposal), GET lists them. Apply/rollback is a later batch — the LLM
output stays a proposal in memory_refine_candidates.suggested_text and is
never written to the vector store here.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from auth import require_auth
from db import get_db
from fastapi import APIRouter, Depends, HTTPException
from models import EvolveSalience, MemoryRefineCandidate
from pydantic import BaseModel
from refine_memory import cluster_candidates, refine_group
from server_state import get_memory_instance
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

router = APIRouter(prefix="/memory/refine", tags=["refine"])

logger = logging.getLogger(__name__)

STATUS_PROPOSED = "proposed"
STATUS_APPLIED = "applied"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_FAILED = "failed"
APPLIED_STATUSES = (STATUS_APPLIED, STATUS_ROLLED_BACK)
STALE_CUTOFF_DAYS = 14
MAX_STALE_SCAN = 500
DEFAULT_USER_ID = "hermes-user"
DEFAULT_AGENT_ID = "hermes"


class RefineCandidateRequest(BaseModel):
    candidate_id: int


class RefineDeleteRequest(BaseModel):
    candidate_id: int
    with_memories: bool = False


def _serialize_candidate(row: MemoryRefineCandidate) -> dict:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "memory_ids": row.memory_ids or [],
        "topic": row.topic,
        "status": row.status,
        "suggested_text": row.suggested_text or [],
        "refined_memory_id": row.refined_memory_id,
        "refined_memory_ids": row.refined_memory_ids or [],
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _collect_stale_items(memory, db: Session, user_id: str) -> list[dict]:
    """Fetch fragmented memory texts for stale (未召回) memories of a user."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_CUTOFF_DAYS)
    stale_rows = db.execute(
        select(EvolveSalience.memory_id)
        .where(
            or_(
                EvolveSalience.last_access_at.is_(None),
                EvolveSalience.last_access_at < cutoff,
            )
        )
        .order_by(EvolveSalience.access_count.desc())
        .limit(MAX_STALE_SCAN)
    ).all()

    items: list[dict] = []
    for (mid,) in stale_rows:
        mid = str(mid)
        try:
            result = memory.vector_store.get(mid)
        except Exception:  # noqa: BLE001
            continue
        if result is None:
            continue
        payload = getattr(result, "payload", None) or {}
        if payload.get("superseded_by"):
            # Already refined (soft-superseded originals must not re-enter the
            # stale pool; otherwise every rerun re-clusters the same memories).
            continue
        if payload.get("user_id") != user_id:
            continue
        data = payload.get("data")
        if not data:
            continue
        items.append({"id": mid, "data": str(data)})
    return items


def _resolve_scope(user_id: Optional[str], _auth) -> Optional[str]:
    """Admin sees all unless user_id is explicit; non-admin scopes to self."""
    if user_id:
        return user_id
    if getattr(_auth, "role", None) == "admin":
        return None
    return str(_auth.id)


def _discover_users(memory) -> list[str]:
    """Scan vector store payloads for unique user_ids (admin full-scope helper)."""
    try:
        results = memory.vector_store.list(top_k=10000)
    except Exception as e:  # noqa: BLE001
        logger.warning("refine discover_users failed: %s", e)
        return []
    rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []
    user_ids: set[str] = set()
    for row in rows:
        payload = getattr(row, "payload", None) or {}
        uid = payload.get("user_id")
        if uid:
            user_ids.add(str(uid))
    return sorted(user_ids)


def discover_refine_candidates(
    memory, db: Session, user_id: str, items: Optional[list[dict]] = None
) -> list[dict]:
    """Shared core: stale discovery -> clustering -> LLM proposal -> candidate rows.

    Candidates are written to the candidate table only — never to the vector
    store (apply is human-triggered). ``items`` may be passed in when the
    caller already collected stale items (avoids a second vector-store scan).
    """
    items = items if items is not None else _collect_stale_items(memory, db, user_id)
    candidates = cluster_candidates(memory, items)

    # Idempotency guard: the daily timer and manual POST share this pipeline;
    # without dedup, reruns re-cluster the same memories into identical
    # candidate rows. Groups already proposed/applied are skipped (failed and
    # rolled_back stay eligible for retry).
    used_groups = {
        frozenset(ids or [])
        for ids in db.execute(
            select(MemoryRefineCandidate.memory_ids).where(
                MemoryRefineCandidate.status.in_((STATUS_PROPOSED, STATUS_APPLIED))
            )
        ).scalars().all()
    }

    created = []
    for cand in candidates:
        if frozenset(cand["memory_ids"]) in used_groups:
            continue
        row = MemoryRefineCandidate(
            user_id=user_id,
            memory_ids=cand["memory_ids"],
            topic=None,
            status=STATUS_PROPOSED,
            suggested_text=[],
        )
        db.add(row)
        db.flush()
        outcome = refine_group(memory, {"memory_ids": cand["memory_ids"]})
        row.status = outcome["status"]
        row.suggested_text = outcome["suggested_text"]
        # Topic comes from the LLM (a proper short title); failed proposals
        # keep topic=None so the UI shows "候选 #id" instead of a raw
        # first-memory truncation.
        if outcome["status"] == STATUS_PROPOSED:
            row.topic = outcome.get("topic")
        created.append(_serialize_candidate(row))
    db.commit()
    return created


@router.post("/candidates")
def generate_refine_candidates(
    user_id: Optional[str] = None,
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Discover fragmented-memory clusters and draft LLM refinement proposals.

    Admin without an explicit user_id generates candidates for ALL users;
    otherwise scoped to the caller (or the requested user).
    """
    memory = get_memory_instance()

    def _run(uid: str) -> tuple[int, list[dict]]:
        items = _collect_stale_items(memory, db, uid)
        created = discover_refine_candidates(memory, db, uid, items=items)
        return len(items), created

    scanned = 0
    created = []
    if user_id:
        scanned, created = _run(user_id)
    elif getattr(_auth, "role", None) == "admin":
        for uid in _discover_users(memory):
            n, rows = _run(uid)
            scanned += n
            created.extend(rows)
    else:
        scanned, created = _run(str(_auth.id))

    membered = sum(len(c["memory_ids"]) for c in created)
    return {
        "candidates": created,
        "stats": {
            "scanned": scanned,
            "groups": len(created),
            "membered": membered,
            "not_clustered": max(scanned - membered, 0),
        },
    }


def _soft_supersede(memory, memory_ids: list[str], superseded_by: str) -> None:
    """Mark original memories as superseded (soft, no physical delete)."""
    now = datetime.now(timezone.utc).isoformat()
    for mid in memory_ids:
        try:
            result = memory.vector_store.get(mid)
        except Exception:  # noqa: BLE001
            continue
        if result is None:
            continue
        payload = dict(getattr(result, "payload", None) or {})
        payload.update({"superseded_by": superseded_by, "superseded_at": now})
        try:
            memory.vector_store.update(mid, payload=payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("refine apply supersede %s failed: %s", mid, e)


@router.post("/apply")
def apply_refine_candidate(
    req: RefineCandidateRequest,
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Apply an approved proposal: write refined memories + soft-supersede originals."""
    row = db.get(MemoryRefineCandidate, req.candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    if row.status != STATUS_PROPOSED:
        raise HTTPException(
            status_code=409,
            detail=f"候选状态为 {row.status}，仅 proposed 可应用",
        )

    memory = get_memory_instance()
    user_id = row.user_id or DEFAULT_USER_ID
    agent_id = DEFAULT_AGENT_ID

    new_ids: list[str] = []
    for text in row.suggested_text or []:
        try:
            result = memory.add(str(text), user_id=user_id, agent_id=agent_id, infer=False)
            for item in (result or {}).get("results", []):
                if item.get("id"):
                    new_ids.append(str(item["id"]))
        except Exception as e:  # noqa: BLE001
            logger.warning("refine apply add failed: %s", e)

    if not new_ids:
        # Nothing written — stay proposed so the candidate can be retried.
        db.commit()
        return {
            "candidate_id": req.candidate_id,
            "status": row.status,
            "refined_memory_ids": [],
        }

    _soft_supersede(memory, row.memory_ids or [], superseded_by=new_ids[0])
    row.status = STATUS_APPLIED
    row.refined_memory_ids = new_ids
    row.refined_memory_id = new_ids[0]
    db.commit()
    return {
        "candidate_id": req.candidate_id,
        "status": row.status,
        "refined_memory_ids": new_ids,
    }


@router.post("/rollback")
def rollback_refine_candidate(
    req: RefineCandidateRequest,
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Roll back an applied proposal: delete new memories + restore originals."""
    row = db.get(MemoryRefineCandidate, req.candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="候选不存在")
    if row.status != STATUS_APPLIED:
        raise HTTPException(
            status_code=409,
            detail=f"候选状态为 {row.status}，仅 applied 可回滚",
        )

    memory = get_memory_instance()
    for mid in row.refined_memory_ids or []:
        try:
            memory.delete(mid)
        except Exception as e:  # noqa: BLE001
            logger.warning("refine rollback delete %s failed: %s", mid, e)

    _restore_originals(memory, row.memory_ids or [])

    row.status = STATUS_ROLLED_BACK
    db.commit()
    return {"candidate_id": req.candidate_id, "status": row.status}


def _restore_originals(memory, memory_ids: list[str]) -> None:
    """Clear superseded marks on original memories (rollback / delete-with-memories)."""
    for mid in memory_ids:
        try:
            result = memory.vector_store.get(mid)
        except Exception:  # noqa: BLE001
            continue
        if result is None:
            continue
        payload = dict(getattr(result, "payload", None) or {})
        payload.pop("superseded_by", None)
        payload.pop("superseded_at", None)
        try:
            memory.vector_store.update(mid, payload=payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("refine restore %s failed: %s", mid, e)


@router.post("/delete")
def delete_refine_candidate(
    req: RefineDeleteRequest,
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Delete a candidate record, optionally removing its refined memories.

    with_memories=false (any status): drop only the record. For applied rows the
    refined memories stay and originals keep their superseded mark.

    with_memories=true (applied only): also delete the refined memories and
    restore the originals' superseded marks (i.e. rollback + remove record).
    """
    row = db.get(MemoryRefineCandidate, req.candidate_id)
    if row is None:
        raise HTTPException(status_code=404, detail="候选不存在")

    if req.with_memories and row.status == STATUS_APPLIED:
        memory = get_memory_instance()
        for mid in row.refined_memory_ids or []:
            try:
                memory.delete(mid)
            except Exception as e:  # noqa: BLE001
                logger.warning("refine delete %s failed: %s", mid, e)
        _restore_originals(memory, row.memory_ids or [])

    db.delete(row)
    db.commit()
    return {
        "candidate_id": req.candidate_id,
        "deleted": True,
        "with_memories": req.with_memories,
    }


@router.get("/history")
def refine_history(
    user_id: Optional[str] = None,
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """List applied / rolled_back candidates (with proposals and new ids).

    Admin without an explicit user_id sees all users' history.
    """
    stmt = (
        select(MemoryRefineCandidate)
        .where(MemoryRefineCandidate.status.in_(APPLIED_STATUSES))
        .order_by(MemoryRefineCandidate.updated_at.desc())
    )
    scope = _resolve_scope(user_id, _auth)
    if scope:
        stmt = stmt.where(MemoryRefineCandidate.user_id == scope)
    rows = db.execute(stmt).scalars().all()
    return {"history": [_serialize_candidate(r) for r in rows]}


@router.get("/candidates")
def list_refine_candidates(
    status: Optional[str] = None,
    user_id: Optional[str] = None,
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """List refinement candidates, optionally filtered by status / user_id.

    Admin without an explicit user_id sees all users' candidates.
    """
    stmt = select(MemoryRefineCandidate).order_by(MemoryRefineCandidate.created_at.desc())
    scope = _resolve_scope(user_id, _auth)
    if scope:
        stmt = stmt.where(MemoryRefineCandidate.user_id == scope)
    if status:
        stmt = stmt.where(MemoryRefineCandidate.status == status)
    rows = db.execute(stmt).scalars().all()
    return {"candidates": [_serialize_candidate(r) for r in rows]}
