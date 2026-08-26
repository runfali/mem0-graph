from collections import defaultdict
from datetime import datetime
from typing import Any, Literal, Optional

from auth import require_admin, verify_auth
from errors import upstream_error
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from schemas import MessageResponse
from server_state import get_memory_instance

router = APIRouter(prefix="/entities", tags=["entities"])

SCAN_LIMIT = 10_000

EntityType = Literal["user", "agent", "run"]
TYPE_TO_FIELD: dict[EntityType, str] = {"user": "user_id", "agent": "agent_id", "run": "run_id"}


class Entity(BaseModel):
    id: str
    type: EntityType
    total_memories: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


def _iter_payloads() -> list[dict[str, Any]]:
    results = get_memory_instance().vector_store.list(top_k=SCAN_LIMIT)
    rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []
    return [getattr(row, "payload", None) or {} for row in rows]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("", response_model=list[Entity])
def list_entities(_auth=Depends(verify_auth)):
    buckets: dict[tuple[EntityType, str], dict[str, Any]] = defaultdict(
        lambda: {"total_memories": 0, "created_at": None, "updated_at": None}
    )

    for payload in _iter_payloads():
        created = _parse_timestamp(payload.get("created_at"))
        updated = _parse_timestamp(payload.get("updated_at")) or created

        for entity_type, field in TYPE_TO_FIELD.items():
            value = payload.get(field)
            if not value:
                continue
            bucket = buckets[(entity_type, str(value))]
            bucket["total_memories"] += 1
            if created and (bucket["created_at"] is None or created < bucket["created_at"]):
                bucket["created_at"] = created
            if updated and (bucket["updated_at"] is None or updated > bucket["updated_at"]):
                bucket["updated_at"] = updated

    return [
        Entity(id=entity_id, type=entity_type, **data)
        for (entity_type, entity_id), data in sorted(buckets.items(), key=lambda item: (item[0][0], item[0][1]))
    ]


@router.delete("/{entity_type}/{entity_id}", response_model=MessageResponse)
def delete_entity(entity_type: EntityType, entity_id: str, _auth=Depends(require_admin)):
    try:
        memory = get_memory_instance()
        if entity_type == "user":
            # user 类型：保持原有行为，delete_all(user_id=...) 路径正常
            memory.delete_all(user_id=entity_id)
        else:
            # agent/run 类型：逐条删除记忆，每条记忆的 payload 携带 user_id，
            # graph 清除依赖 user_id 定位用户图，避免 delete_all 因缺少 user_id 触发 KeyError
            field = TYPE_TO_FIELD[entity_type]
            filters = {field: entity_id}
            # 二轮审计：list 默认 top_k=100 无分页，>100 条记忆的实体此前只删
            # 前 100 条且谎报成功——改为按页取满再逐条删
            SCAN_PAGE = 500
            while True:
                batch = memory.vector_store.list(filters=filters, top_k=SCAN_PAGE)[0] or []
                if not batch:
                    break
                for m in batch:
                    memory.delete(m.id)
                if len(batch) < SCAN_PAGE:
                    break
    except Exception:
        raise upstream_error()
    return MessageResponse(message="Entity deleted")
