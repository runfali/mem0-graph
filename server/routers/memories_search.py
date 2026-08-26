from typing import Optional

from auth import require_auth
from db import get_db
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from server_state import get_current_config
from sqlalchemy import text
from sqlalchemy.orm import Session

router = APIRouter(prefix="/memories", tags=["memories-search"])


def _get_collection() -> str:
    config = get_current_config()
    return (
        config.get("vector_store", {}).get("config", {}).get("collection_name")
        or "mem0_memories"
    )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/search")
def search_memories(
    request: Request,
    q: str = Query(...),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user_id: Optional[str] = None,
    memory_type: Optional[str] = None,
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Global substring search over all memories via ILIKE on payload->>'data'.

    Non-admin callers without an explicit user_id are scoped to their own
    memories; admins (and admin_api_key / AUTH_DISABLED callers) search globally.
    """
    q = q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="Search query 'q' is required.")

    if user_id is None:
        auth_type = getattr(request.state, "auth_type", "none")
        if _auth.role != "admin" and auth_type not in {"admin_api_key", "disabled"}:
            user_id = str(_auth.id)

    where = ["payload->>'data' ILIKE '%' || :q || '%' ESCAPE '\\'"]
    params = {"q": _escape_like(q)}
    if user_id is not None:
        where.append("payload->>'user_id' = :uid")
        params["uid"] = user_id
    if memory_type:
        where.append("payload->>'memory_type' = :mt")
        params["mt"] = memory_type
    where_sql = " AND ".join(where)
    collection = _get_collection()

    data_sql = (
        f"SELECT id, payload FROM {collection} WHERE {where_sql} "
        # 二轮审计：NULLS LAST + COALESCE 回退 created_at——与 _list_all_memories
        # 的排序语义对齐（旧 DESC 默认 NULLS FIRST 会把缺 updated_at 的行浮顶）
        "ORDER BY COALESCE(payload->>'updated_at', payload->>'created_at') DESC NULLS LAST "
        "LIMIT :limit OFFSET :offset"
    )
    count_sql = f"SELECT count(*) FROM {collection} WHERE {where_sql}"

    rows = db.execute(text(data_sql), {**params, "limit": limit, "offset": offset}).all()
    total = db.execute(text(count_sql), params).scalar()

    results = []
    for row in rows:
        payload = row[1] or {}
        results.append(
            {
                "id": str(row[0]),
                "memory": payload.get("data"),
                "user_id": payload.get("user_id"),
                "agent_id": payload.get("agent_id"),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
                "memory_type": payload.get("memory_type"),
            }
        )
    return {"results": results, "total": total}


@router.get("/types-distribution")
def memories_types_distribution(
    _auth=Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Count memories grouped by memory_type, unclassified included."""
    collection = _get_collection()
    sql = (
        f"SELECT COALESCE(payload->>'memory_type', 'unclassified') AS mt, count(*) "
        f"FROM {collection} GROUP BY 1 ORDER BY 2 DESC"
    )
    rows = db.execute(text(sql)).all()
    distribution = [{"type": str(row[0]), "count": row[1]} for row in rows]
    return {"distribution": distribution}
