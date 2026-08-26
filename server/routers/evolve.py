from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from auth import require_auth
from db import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from models import (
    EvolveFeedback,
    EvolveQuery,
    EvolveSalience,
    EvolveSalienceAdjustment,
    RequestLog,
    User,
)
from pydantic import BaseModel
from server_state import get_current_config
from sqlalchemy import ColumnElement, String, and_, case, column, func, or_, select, table, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

router = APIRouter(prefix="/evolve", tags=["evolve"])


def _collection_name() -> str:
    config = get_current_config()
    return (
        config.get("vector_store", {}).get("config", {}).get("collection_name")
        or "mem0_memories"
    )


def _memory_owner(db: Session, memory_id: str) -> Optional[str]:
    """Return the payload user_id of a memory, or None if unknown.

    Fast path reads the indexed evolve_salience.user_id cache (stamped at
    add time); falls back to a payload lookup in the vector-store table for
    rows written before the column existed. mem0_memories.id is uuid while
    memory ids are strings, so the fallback casts (CAST(id AS VARCHAR)).
    """
    salience = db.get(EvolveSalience, memory_id)
    if salience is not None and salience.user_id:
        return salience.user_id
    row = db.execute(
        text(f"SELECT payload->>'user_id' FROM {_collection_name()} WHERE CAST(id AS VARCHAR) = :mid LIMIT 1"),
        {"mid": memory_id},
    ).first()
    return row[0] if row else None


def _authorize_memory_write(request: Request, user: User, memory_id: str, db: Session) -> Optional[str]:
    """Scope salience writes to memories owned by the caller.

    Admins, the ADMIN_API_KEY and AUTH_DISABLED callers keep global access;
    everyone else may only touch their own memories. A missing or foreign
    memory yields 404 so existence is not leaked. Returns the resolved owner
    (None for global-access callers or unknown owners) so callers can stamp
    it onto the rows they write.
    """
    auth_type = getattr(request.state, "auth_type", "none")
    if auth_type in {"admin_api_key", "disabled"} or user.role == "admin":
        return None
    owner = _memory_owner(db, memory_id)
    if owner is None or owner != str(user.id):
        raise HTTPException(status_code=404, detail="Memory not found.")
    return owner


def _memory_still_exists() -> ColumnElement[bool]:
    """EXISTS guard: salience row still has a live memory in the vector store.

    mem0_memories.id is uuid while EvolveSalience.memory_id is varchar(255), so
    the comparison must cast (id::text = memory_id), otherwise Postgres raises
    ``operator does not exist: uuid = character varying``.
    """
    memories = table(_collection_name(), column("id"))
    return EvolveSalience.memory_id.in_(select(func.cast(memories.c.id, String)))


FeedbackType = Literal["useful", "useless", "correction"]
DELTAS: dict[str, float] = {"useful": 0.1, "useless": -0.15, "correction": -0.05}
MIN_SCORE = 0.05
MAX_SCORE = 1.0


class FeedbackRequest(BaseModel):
    memory_id: str
    feedback_type: FeedbackType
    source: str = "manual"
    note: Optional[str] = None


class FeedbackResponse(BaseModel):
    memory_id: str
    salience_score: float


class RetainResponse(BaseModel):
    memory_id: str
    last_access_at: str


@router.post("/feedback", response_model=FeedbackResponse)
def submit_feedback(
    body: FeedbackRequest,
    request: Request,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Record feedback for a memory and adjust its salience score.

    Adjusts only the salience score; memory content is never touched, so a
    misreport is reversible.
    """
    owner = _authorize_memory_write(request, user, body.memory_id, db)
    now = datetime.now(timezone.utc)
    feedback = EvolveFeedback(
        memory_id=body.memory_id,
        user_id=owner,
        feedback_type=body.feedback_type,
        source=body.source,
        note=body.note,
        created_at=now,
    )
    db.add(feedback)
    db.flush()

    salience = db.get(EvolveSalience, body.memory_id)
    if salience is None:
        salience = EvolveSalience(
            memory_id=body.memory_id, user_id=owner, salience_score=1.0, updated_at=now
        )
        try:
            # SAVEPOINT 收口 check-then-act 竞态：并发首次 feedback 双方同时
            # INSERT 同 PK 时，后到者 IntegrityError 只回滚保存点（不牵连同
            # 事务内已 flush 的 feedback 写入），重读对方行后走统一分支。
            with db.begin_nested():
                db.add(salience)
                db.flush()
        except IntegrityError:
            salience = db.get(EvolveSalience, body.memory_id)
    elif salience.user_id is None and owner:
        # Backfill the owner cache on first touch of a legacy row.
        salience.user_id = owner
    old_score = salience.salience_score
    new_score = round(min(max(old_score + DELTAS[body.feedback_type], MIN_SCORE), MAX_SCORE), 4)
    salience.salience_score = new_score
    salience.updated_at = now

    applied_delta = round(new_score - old_score, 4)
    db.add(
        EvolveSalienceAdjustment(
            memory_id=body.memory_id,
            delta=applied_delta,
            reason=body.feedback_type,
            feedback_id=feedback.id,
            created_at=now,
        )
    )
    db.commit()

    return FeedbackResponse(memory_id=body.memory_id, salience_score=new_score)


@router.post("/memory/{memory_id}/retain", response_model=RetainResponse)
def retain_memory(
    memory_id: str,
    request: Request,
    user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Manually keep a memory and mark it as reviewed.

    Bumps last_access_at so the stale (>14 days unrecalled) list drops it.
    Memory content and salience score are untouched.
    """
    owner = _authorize_memory_write(request, user, memory_id, db)
    now = datetime.now(timezone.utc)
    salience = db.get(EvolveSalience, memory_id)
    if salience is not None:
        if salience.user_id is None and owner:
            # Backfill the owner cache on first touch of a legacy row.
            salience.user_id = owner
        salience.last_access_at = now
        salience.updated_at = now
    else:
        salience = EvolveSalience(
            memory_id=memory_id, user_id=owner, last_access_at=now, updated_at=now
        )
        try:
            # SAVEPOINT 收口并发首触竞态（同 feedback 端点注释）
            with db.begin_nested():
                db.add(salience)
                db.flush()
        except IntegrityError:
            salience = db.get(EvolveSalience, memory_id)
            if salience.user_id is None and owner:
                salience.user_id = owner
            salience.last_access_at = now
            salience.updated_at = now
    db.commit()

    return RetainResponse(memory_id=memory_id, last_access_at=now.isoformat())


@router.get("/report")
def evolve_report(
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
    days: int = Query(default=30, ge=1, le=90),
):
    """Dashboard stats for the evolve loop. Read-only."""
    now = datetime.now(timezone.utc)

    def cutoff(d: int) -> datetime:
        return now - timedelta(days=d)

    def num(value) -> float:
        return round(float(value), 4) if value is not None else 0

    def rate(numer, denom) -> float:
        return round(numer / denom, 4) if denom else 0

    window_days = [w for w in sorted({7, days}) if w <= days]
    trend_days = min(days, 7)

    def search_window(d: int) -> dict:
        total, zero, avg_score, avg_lat = db.execute(
            select(
                func.count(EvolveQuery.id),
                func.sum(case((EvolveQuery.is_zero_hit.is_(True), 1), else_=0)),
                func.avg(EvolveQuery.avg_score),
                func.avg(EvolveQuery.latency_ms),
            ).where(EvolveQuery.created_at >= cutoff(d))
        ).one()
        total = total or 0
        return {
            "total_queries": total,
            "zero_hit_rate": rate(zero or 0, total),
            "avg_score": num(avg_score),
            "avg_latency_ms": num(avg_lat),
        }

    trend_rows = db.execute(
        select(
            func.date(EvolveQuery.created_at).label("day"),
            func.count(EvolveQuery.id),
            func.avg(EvolveQuery.avg_score),
            func.sum(case((EvolveQuery.is_zero_hit.is_(True), 1), else_=0)),
        )
        .where(EvolveQuery.created_at >= cutoff(trend_days))
        .group_by("day")
        .order_by("day")
    ).all()
    trend_by_day = {
        (row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])): row
        for row in trend_rows
    }
    today = now.date()
    daily_trend = []
    for offset in range(trend_days - 1, -1, -1):
        day = (today - timedelta(days=offset)).isoformat()
        row = trend_by_day.get(day)
        daily_trend.append(
            {
                "date": day,
                "queries": row[1] if row else 0,
                "avg_score": num(row[2]) if row else 0,
                "zero_hits": row[3] if row else 0,
            }
        )

    zero_hit_rows = db.execute(
        select(EvolveQuery.query, func.count(EvolveQuery.id))
        .where(EvolveQuery.is_zero_hit.is_(True), EvolveQuery.created_at >= cutoff(1))
        .group_by(EvolveQuery.query)
        .order_by(func.count(EvolveQuery.id).desc())
        .limit(10)
    ).all()
    top_zero_hits = [{"query": q, "count": c} for q, c in zero_hit_rows]

    type_rows = db.execute(
        select(EvolveFeedback.feedback_type, func.count(EvolveFeedback.id)).group_by(
            EvolveFeedback.feedback_type
        )
    ).all()
    type_distribution = dict.fromkeys(("useful", "useless", "correction"), 0)
    for feedback_type, count in type_rows:
        type_distribution[feedback_type] = count

    corrected_rows = db.execute(
        select(EvolveFeedback.memory_id, func.count(EvolveFeedback.id))
        .where(EvolveFeedback.feedback_type == "correction")
        .group_by(EvolveFeedback.memory_id)
        .order_by(func.count(EvolveFeedback.id).desc())
        .limit(5)
    ).all()
    most_corrected = [{"memory_id": m, "count": c} for m, c in corrected_rows]

    buckets = (
        func.sum(case((EvolveSalience.salience_score < 0.5, 1), else_=0)),
        func.sum(
            case(
                (
                    and_(
                        EvolveSalience.salience_score >= 0.5,
                        EvolveSalience.salience_score < 0.9,
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        func.sum(
            case(
                (
                    and_(
                        EvolveSalience.salience_score >= 0.9,
                        EvolveSalience.salience_score <= 1.1,
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        func.sum(case((EvolveSalience.salience_score > 1.1, 1), else_=0)),
    )
    lt_05, mid, ok, gt_11 = db.execute(select(*buckets)).one()
    score_distribution = {
        "lt_0.5": lt_05 or 0,
        "0.5_0.9": mid or 0,
        "0.9_1.1": ok or 0,
        "gt_1.1": gt_11 or 0,
    }

    hot_rows = db.execute(
        select(
            EvolveSalience.memory_id,
            EvolveSalience.access_count,
            EvolveSalience.salience_score,
        )
        .order_by(EvolveSalience.access_count.desc())
        .limit(10)
    ).all()
    high_frequency = [
        {"memory_id": m, "access_count": a, "salience_score": s}
        for m, a, s in hot_rows
    ]

    stale_rows = db.execute(
        select(
            EvolveSalience.memory_id,
            EvolveSalience.access_count,
            EvolveSalience.last_access_at,
        )
        .where(
            or_(
                EvolveSalience.last_access_at.is_(None),
                EvolveSalience.last_access_at < cutoff(14),
            ),
            _memory_still_exists(),
        )
        .order_by(EvolveSalience.access_count.desc())
    ).all()
    stale = [
        {
            "memory_id": m,
            "access_count": a,
            "last_access_at": last.isoformat() if last else None,
        }
        for m, a, last in stale_rows
    ]

    boost_rows = db.execute(
        select(
            EvolveSalienceAdjustment.memory_id,
            EvolveSalienceAdjustment.delta,
            EvolveSalienceAdjustment.created_at,
        )
        .where(
            EvolveSalienceAdjustment.reason == "evolve_boost",
            EvolveSalienceAdjustment.created_at >= cutoff(30),
        )
        .order_by(EvolveSalienceAdjustment.created_at.desc())
    ).all()
    boost_adjustments = [
        {
            "memory_id": m,
            "delta": num(d),
            "created_at": c.isoformat() if c else None,
        }
        for m, d, c in boost_rows
    ]

    def op_window(d: int) -> dict:
        total, avg_lat, ok_count = db.execute(
            select(
                func.count(RequestLog.id),
                func.avg(RequestLog.latency_ms),
                func.sum(case((RequestLog.status_code < 500, 1), else_=0)),
            ).where(RequestLog.created_at >= cutoff(d))
        ).one()
        total = total or 0
        return {
            "total_requests": total,
            "avg_latency_ms": num(avg_lat),
            "success_rate": rate(ok_count or 0, total),
        }

    stage_order = ["candidates", "threshold", "decay", "graph", "temporal", "rerank", "final"]
    stage_agg: dict[str, list[float]] = {}
    recent: list[dict] = []
    trace_rows = db.execute(
        select(EvolveQuery.query, EvolveQuery.created_at, EvolveQuery.trace)
        .where(
            EvolveQuery.trace.is_not(None),
            EvolveQuery.created_at >= cutoff(7),
        )
        .order_by(EvolveQuery.created_at.desc())
    ).all()
    for query, created, trace in trace_rows:
        if not isinstance(trace, dict) or not isinstance(trace.get("stages"), list):
            continue
        for s in trace["stages"]:
            if s.get("stage") not in stage_order:
                continue
            agg = stage_agg.setdefault(s["stage"], [0.0, 0.0, 0.0])
            agg[0] += 1
            agg[1] += s.get("count") or 0
            agg[2] += s.get("latency_ms") or 0
        if len(recent) < 10:
            recent.append(
                {
                    "query": query,
                    "created_at": created.isoformat() if created else None,
                    "stages": trace["stages"],
                }
            )
    recall = {
        "stages": [
            {
                "stage": stage,
                "avg_count": num(agg[1] / agg[0]),
                "avg_latency_ms": num(agg[2] / agg[0]),
            }
            for stage in stage_order
            if (agg := stage_agg.get(stage))
        ],
        "recent": recent,
    }

    return {
        "search_quality": {
            "windows": {str(w): search_window(w) for w in window_days},
            "daily_trend": daily_trend,
            "top_zero_hits": top_zero_hits,
        },
        "feedback": {
            "type_distribution": type_distribution,
            "most_corrected": most_corrected,
        },
        "heat": {
            "score_distribution": score_distribution,
            "high_frequency": high_frequency,
            "stale": stale,
            "boost_adjustments": boost_adjustments,
        },
        "operations": {"windows": {str(w): op_window(w) for w in window_days}},
        "recall": recall,
    }
