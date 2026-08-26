import asyncio
import logging
import os
import secrets
import string
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import telemetry
from auth import ADMIN_API_KEY, AUTH_DISABLED, JWT_SECRET, hash_password, require_admin, verify_auth
from db import SessionLocal
from dotenv import load_dotenv
from errors import (
    UpstreamError,
    install_request_id_logging,
    new_request_id,
    request_id_var,
    upstream_error,
    upstream_error_handler,
)
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from mem0.exceptions import ValidationError as Mem0ValidationError
from models import EvolveQuery, EvolveSalience, RequestLog, User
from pydantic import BaseModel, Field
from rate_limit import DataPlaneRateLimitMiddleware, limiter
from routers import api_keys as api_keys_router
from routers import auth as auth_router
from routers import entities as entities_router
from routers import evolve as evolve_router
from routers import memories_search as memories_search_router
from routers import refine as refine_router
from routers import requests as requests_router
from routers import search_keywords as search_keywords_router
from schemas import MessageResponse
from server_state import (
    get_current_config,
    get_memory_instance,
    initialize_state,
    set_session_factory,
    update_config,
)
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select

load_dotenv()

install_request_id_logging()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [%(request_id)s] %(message)s")

MIN_KEY_LENGTH = 16
SENSITIVE_CONFIG_KEYS = {
    "admin_api_key",
    "api_key",
    "authorization",
    "jwt_secret",
    "password",
    "password_hash",
    "secret",
    "token",
}
SKIPPED_REQUEST_LOG_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json"}
SKIPPED_REQUEST_LOG_PREFIXES = ("/requests",)

BUNDLED_LLM_PROVIDERS = ("openai", "anthropic", "gemini")
BUNDLED_EMBEDDER_PROVIDERS = ("openai", "gemini")


def _seed_admin_user() -> None:
    """Create a default admin user if no users exist, logging credentials for the operator."""
    try:
        with SessionLocal() as session:
            if session.scalar(select(func.count(User.id))) > 0:
                return

            alphabet = string.ascii_letters + string.digits
            password = "".join(secrets.choice(alphabet) for _ in range(16))
            user = User(name="admin", email="admin@mem0.dev", password_hash=hash_password(password), role="admin")
            session.add(user)
            session.commit()
    except Exception:
        logging.exception("Failed to seed admin user")
        return

    logger = logging.getLogger(__name__)
    logger.info(
        "\n%s\n"
        "  Default admin user created:\n"
        "    Email:    admin@mem0.dev\n"
        "    Password: %s\n"
        "    Dashboard: %s\n"
        "%s",
        "=" * 72,
        password,
        DASHBOARD_URL,
        "=" * 72,
    )


def _warn_if_unconfigured() -> None:
    """Pre-auth deployments upgrading into this build will 401 everywhere until
    an admin key or admin user exists. Surface the fix before the support tickets."""
    try:
        with SessionLocal() as session:
            if session.scalar(select(func.count(User.id))) > 0:
                return
    except Exception:
        return

    logging.warning(
        "\n%s\n"
        "  Auth is enabled by default and this server has no admin configured.\n"
        "  Protected endpoints will return 401 until you either:\n"
        "    1. Set ADMIN_API_KEY=<long-random-value>  (fastest, no client changes)\n"
        "    2. Register an admin at http://<host>:3000/setup\n"
        "    3. Set AUTH_DISABLED=true                 (local development only)\n"
        "  Docs: https://docs.mem0.ai/open-source/features/rest-api#authentication\n"
        "%s",
        "=" * 72,
        "=" * 72,
    )


if not AUTH_DISABLED and not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is required. Set it in .env (generate with `openssl rand -base64 48`) "
        "or set AUTH_DISABLED=true for local development only."
    )

if AUTH_DISABLED:
    logging.warning("AUTH_DISABLED is enabled. Protected endpoints are open for local development only.")
elif ADMIN_API_KEY and len(ADMIN_API_KEY) < MIN_KEY_LENGTH:
    logging.warning(
        "ADMIN_API_KEY is shorter than %d characters - consider using a longer key for production.",
        MIN_KEY_LENGTH,
    )
elif not ADMIN_API_KEY:
    _warn_if_unconfigured()

telemetry.log_status()

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
POSTGRES_COLLECTION_NAME = os.environ.get("POSTGRES_COLLECTION_NAME", "mem0_memories")

FALKORDB_HOST = os.environ.get("FALKORDB_HOST", "falkordb")
FALKORDB_PORT = int(os.environ.get("FALKORDB_PORT", "6379"))
FALKORDB_DATABASE = os.environ.get("FALKORDB_DATABASE", "mem0")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
EMBEDDER_BASE_URL = os.environ.get("EMBEDDER_BASE_URL")
HISTORY_DB_PATH = os.environ.get("HISTORY_DB_PATH", "/app/history/history.db")
DEFAULT_LLM_MODEL = os.environ.get("MEM0_DEFAULT_LLM_MODEL", "gpt-4.1-nano-2025-04-14")
DEFAULT_EMBEDDER_MODEL = os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL", "text-embedding-3-small")


def build_llm_fallbacks_from_env(env: Dict[str, Optional[str]]) -> List[Dict[str, Any]]:
    """Build llm.fallbacks from env vars. Returns [] when no fallback is configured.

    Fallback 1: MEM0_LLM_FALLBACK_MODEL / MEM0_LLM_FALLBACK_BASE_URL / MEM0_LLM_FALLBACK_API_KEY
    Fallback 2: MEM0_LLM_FALLBACK2_MODEL / MEM0_LLM_FALLBACK2_BASE_URL / MEM0_LLM_FALLBACK2_API_KEY / MEM0_LLM_FALLBACK2_REASONING_EFFORT
    Empty api_key is omitted so it never overwrites a stored override with None.
    """
    fallbacks: List[Dict[str, Any]] = []
    for index in (1, 2):
        prefix = "MEM0_LLM_FALLBACK" if index == 1 else "MEM0_LLM_FALLBACK2"
        model = env.get(f"{prefix}_MODEL")
        if not model:
            continue
        fallback_config: Dict[str, Any] = {"model": model}
        base_url = env.get(f"{prefix}_BASE_URL")
        if base_url:
            fallback_config["openai_base_url"] = base_url
        api_key = env.get(f"{prefix}_API_KEY")
        if api_key:
            fallback_config["api_key"] = api_key
        reasoning_effort = env.get(f"{prefix}_REASONING_EFFORT")
        if reasoning_effort:
            fallback_config["reasoning_effort"] = reasoning_effort
        fallbacks.append({"provider": "openai", "config": fallback_config})
    return fallbacks


LLM_CONFIG = {
    "api_key": OPENAI_API_KEY,
    "temperature": float(os.environ.get("MEM0_LLM_TEMPERATURE", "0.2")),
    "max_tokens": int(os.environ.get("MEM0_LLM_MAX_TOKENS", "2000")),
    "model": DEFAULT_LLM_MODEL,
}
if OPENAI_BASE_URL:
    LLM_CONFIG["openai_base_url"] = OPENAI_BASE_URL
_llm_fallbacks = build_llm_fallbacks_from_env(os.environ)

EMBEDDER_API_KEY = os.environ.get("EMBEDDER_API_KEY", OPENAI_API_KEY)
EMBEDDER_CONFIG = {"api_key": EMBEDDER_API_KEY, "model": DEFAULT_EMBEDDER_MODEL}
if EMBEDDER_BASE_URL:
    EMBEDDER_CONFIG["openai_base_url"] = EMBEDDER_BASE_URL
_embedding_dims = os.environ.get("MEM0_EMBEDDING_DIMS")
if _embedding_dims is not None:
    EMBEDDER_CONFIG["embedding_dims"] = int(_embedding_dims)

DEFAULT_CONFIG = {
    "version": "v1.1",
    "vector_store": {
        "provider": "pgvector",
        "config": {
            "host": POSTGRES_HOST,
            "port": int(POSTGRES_PORT),
            "dbname": POSTGRES_DB,
            "user": POSTGRES_USER,
            "password": POSTGRES_PASSWORD,
            "collection_name": POSTGRES_COLLECTION_NAME,
            "embedding_model_dims": int(_embedding_dims) if _embedding_dims is not None else None,
        },
    },
    "llm": {
        "provider": "openai",
        "config": LLM_CONFIG,
        "fallbacks": _llm_fallbacks,
        "layer_timeout": float(os.environ.get("MEM0_LLM_FALLBACK_TIMEOUT", "60")),
    },
    "embedder": {"provider": "openai", "config": EMBEDDER_CONFIG},
    "history_db_path": HISTORY_DB_PATH,
    "graph_store": {
        "provider": "falkordb",
        "config": {
            "host": FALKORDB_HOST,
            "port": FALKORDB_PORT,
            "database": FALKORDB_DATABASE,
        },
    },
}


set_session_factory(SessionLocal)
initialize_state(DEFAULT_CONFIG)


app = FastAPI(
    title="Mem0 REST APIs",
    description=(
        "A REST API for managing and searching memories for your AI Agents and Apps.\n\n"
        "## Authentication\n"
        "Supports Bearer JWT tokens, per-user API keys via `X-API-Key` header, "
        "or the legacy `ADMIN_API_KEY` environment variable. Set `AUTH_DISABLED=true` for local development only."
    ),
    version="1.0.0",
    redirect_slashes=False,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_exception_handler(UpstreamError, upstream_error_handler)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[DASHBOARD_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Data-plane limiter: added after CORS so it runs inside the CORS wrapper
# (preflight OPTIONS is exempt anyway). /auth/* keeps its stricter slowapi
# quotas and is skipped by this middleware.
app.add_middleware(DataPlaneRateLimitMiddleware)


@app.on_event("startup")
def _on_startup() -> None:
    _seed_admin_user()


app.include_router(auth_router.router)
app.include_router(api_keys_router.router)
app.include_router(entities_router.router)
app.include_router(evolve_router.router)
app.include_router(memories_search_router.router)
app.include_router(refine_router.router)
app.include_router(requests_router.router)
app.include_router(search_keywords_router.router)


class Message(BaseModel):
    role: str = Field(..., description="Role of the message (user or assistant).")
    content: str = Field(..., description="Message content.")


class MemoryCreate(BaseModel):
    messages: List[Message] = Field(..., description="List of messages to store.")
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    expiration_date: Optional[str] = Field(None, description="Expiration date in YYYY-MM-DD format.")
    infer: Optional[bool] = Field(None, description="Whether to extract facts from messages. Defaults to True.")
    memory_type: Optional[str] = Field(None, description="Type of memory to store (e.g. 'core').")
    prompt: Optional[str] = Field(None, description="Custom prompt to use for fact extraction.")


class MemoryUpdate(BaseModel):
    text: Optional[str] = Field(None, description="New content to update the memory with.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata to update.")
    expiration_date: Optional[str] = Field(None, description="Expiration date in YYYY-MM-DD format, or null to clear.")


class SearchRequest(BaseModel):
    query: str = Field(..., description="Search query.")
    user_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    run_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    agent_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    filters: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = Field(None, description="Maximum number of results to return.")
    threshold: Optional[float] = Field(None, description="Minimum similarity score for results.")
    explain: Optional[bool] = Field(None, description="Include score details for each search result.")
    show_expired: Optional[bool] = Field(None, description="Include expired memories.")
    rerank: Optional[bool] = Field(None, description="Whether to rerank results with a configured reranker. Defaults to True when reranker is configured.")
    depth: Optional[str] = Field(None, description="Search depth: minimal/standard/full.")


class GenerateInstructionsRequest(BaseModel):
    use_case: str = Field(..., description="Description of what the user will use Mem0 for.")


def _client_error(exc: Exception) -> HTTPException:
    """Map core validation / not-found errors to 4xx so clients can tell a bad
    request from an upstream outage. 'not found' is a 404, everything else a 400."""
    detail = str(exc)
    status_code = 404 if isinstance(exc, ValueError) and "not found" in detail.lower() else 400
    return HTTPException(status_code=status_code, detail=detail)


def _redact_config(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {item_key: _redact_config(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_config(item_value, key) for item_value in value]
    if key is not None and key.lower() in SENSITIVE_CONFIG_KEYS:
        return "[redacted]" if value else value
    return value


def _validate_bundled_providers(config: Dict[str, Any]) -> None:
    llm = config.get("llm")
    if isinstance(llm, dict):
        if (provider := llm.get("provider")) and provider not in BUNDLED_LLM_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"LLM provider '{provider}' is not bundled in this image. "
                    f"Bundled providers: {', '.join(BUNDLED_LLM_PROVIDERS)}. "
                    "To use another provider, install its Python package, rebuild the container, "
                    "and extend BUNDLED_LLM_PROVIDERS in server/main.py."
                ),
            )

        fallbacks = llm.get("fallbacks")
        if fallbacks is not None and not isinstance(fallbacks, list):
            raise HTTPException(
                status_code=400,
                detail=f"LLM 'fallbacks' must be a list, got {type(fallbacks).__name__}.",
            )
        for i, fallback in enumerate(fallbacks or [], start=1):
            if (
                isinstance(fallback, dict)
                and (provider := fallback.get("provider"))
                and provider not in BUNDLED_LLM_PROVIDERS
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"LLM fallbacks[{i}] provider '{provider}' is not bundled in this image. "
                        f"Bundled providers: {', '.join(BUNDLED_LLM_PROVIDERS)}. "
                        "To use another provider, install its Python package, rebuild the container, "
                        "and extend BUNDLED_LLM_PROVIDERS in server/main.py."
                    ),
                )

    embedder = config.get("embedder")
    if (
        isinstance(embedder, dict)
        and (provider := embedder.get("provider"))
        and provider not in BUNDLED_EMBEDDER_PROVIDERS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Embedder provider '{provider}' is not bundled in this image. "
                f"Bundled providers: {', '.join(BUNDLED_EMBEDDER_PROVIDERS)}. "
                "To use another provider, install its Python package, rebuild the container, "
                "and extend BUNDLED_EMBEDDER_PROVIDERS in server/main.py."
            ),
        )


def _should_log_request(request: Request) -> bool:
    if request.method == "OPTIONS":
        return False
    path = request.url.path
    if path in SKIPPED_REQUEST_LOG_PATHS:
        return False
    return not path.startswith(SKIPPED_REQUEST_LOG_PREFIXES)


def _persist_request_log(method: str, path: str, status_code: int, latency_ms: float, auth_type: str) -> None:
    session = SessionLocal()

    try:
        session.add(
            RequestLog(
                method=method,
                path=path,
                status_code=status_code,
                latency_ms=latency_ms,
                auth_type=auth_type,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logging.exception("Failed to persist request log")
    finally:
        session.close()


# Serializes the fire-and-forget evolve writes. Postgres is fine without it,
# but the test suite swaps SessionLocal for a single-connection sqlite engine
# (StaticPool); two concurrent persist transactions on one connection race and
# one gets swallowed. Writes are tiny inserts, so a global lock is harmless.
_EVOLVE_WRITE_LOCK = threading.Lock()


def _persist_evolve_query(row_data: Dict[str, Any]) -> None:
    session = SessionLocal()
    try:
        with _EVOLVE_WRITE_LOCK:
            session.add(EvolveQuery(**row_data))
            session.commit()
    except Exception:
        session.rollback()
        logging.exception("Failed to persist evolve query")
    finally:
        session.close()


def _submit_evolve_query(row_data: Dict[str, Any]) -> None:
    """Fire-and-forget evolve_queries write; never blocks the search request.

    Async handlers have a running loop (run_in_executor); sync handlers run in a
    threadpool with no loop, so fall back to a plain daemon thread. Either way a
    failed write is logged and swallowed.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=_persist_evolve_query, args=(row_data,), daemon=True).start()
        return
    try:
        loop.run_in_executor(None, _persist_evolve_query, row_data)
    except Exception:
        pass


def _persist_evolve_salience_access(memory_ids: List[str]) -> None:
    """Bump access_count + last_access_at for hit memories (fire-and-forget)."""
    if not memory_ids:
        return
    session = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        with _EVOLVE_WRITE_LOCK:
            for mem_id in memory_ids:
                row = session.get(EvolveSalience, mem_id)
                if row is None:
                    session.add(
                        EvolveSalience(memory_id=mem_id, access_count=1, last_access_at=now, updated_at=now)
                    )
                else:
                    row.access_count = (row.access_count or 0) + 1
                    row.last_access_at = now
                    row.updated_at = now
            session.commit()
    except Exception:
        session.rollback()
        logging.exception("Failed to persist evolve salience access")
    finally:
        session.close()


def _submit_evolve_salience_access(memory_ids: List[str]) -> None:
    """Fire-and-forget salience access-count write; never blocks search.

    Mirrors _submit_evolve_query: executor when a loop is running, else a
    daemon thread. Graph-fragment ids are transient uuids and are filtered out
    by the caller before this is reached.
    """
    if not memory_ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=_persist_evolve_salience_access, args=(memory_ids,), daemon=True).start()
        return
    try:
        loop.run_in_executor(None, _persist_evolve_salience_access, memory_ids)
    except Exception:
        pass


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request.state.auth_type = getattr(request.state, "auth_type", "none")
    rid = new_request_id()
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        request_id_var.reset(token)
        if _should_log_request(request):
            asyncio.get_running_loop().run_in_executor(
                None,
                _persist_request_log,
                request.method,
                request.url.path,
                status_code,
                round((time.perf_counter() - start) * 1000, 2),
                getattr(request.state, "auth_type", "none"),
            )


@app.get("/configure", summary="Get current Mem0 configuration")
def get_config(_auth=Depends(verify_auth)):
    return _redact_config(get_current_config())


@app.get("/configure/providers", summary="List bundled LLM and embedder providers")
def list_bundled_providers(_auth=Depends(verify_auth)):
    return {"llm": list(BUNDLED_LLM_PROVIDERS), "embedder": list(BUNDLED_EMBEDDER_PROVIDERS)}


@app.post("/configure", summary="Configure Mem0")
def set_config(config: Dict[str, Any], _auth=Depends(require_admin)):
    """Set memory configuration. Requires admin role.

    Persists atomically to config.json (the authoritative source); the DB
    overrides layer is no longer written. Restart the container to apply.
    """
    _validate_bundled_providers(config)
    update_config(config)
    return {"message": "Configuration saved to config.json. Restart the container to apply."}


@app.post("/generate-instructions", summary="Generate custom instructions from a use case")
def generate_instructions(req: GenerateInstructionsRequest, _auth=Depends(verify_auth)):
    """Generate custom instructions and a contextual test message tailored to a use case."""
    try:
        llm = get_memory_instance().llm
        prompt = (
            "You are configuring a memory system. Given the use case below, produce two things:\n"
            "1. INSTRUCTIONS: A short paragraph of custom instructions telling the memory extraction system "
            "what kinds of facts, preferences, and context to prioritize. Be specific to the use case.\n"
            "2. TEST_MESSAGE: A single realistic sentence a user in this use case would say, suitable for "
            "testing that the memory system works.\n\n"
            "Respond in exactly this format (no markdown, no extra text):\n"
            "INSTRUCTIONS: <your instructions>\n"
            f"TEST_MESSAGE: <your test message>\n\nUse case: {req.use_case}"
        )
        response = llm.generate_response([{"role": "user", "content": prompt}])
        instructions = response
        test_message = "I like to hike on weekends."
        if "INSTRUCTIONS:" in response and "TEST_MESSAGE:" in response:
            parts = response.split("TEST_MESSAGE:")
            instructions = parts[0].replace("INSTRUCTIONS:", "").strip()
            test_message = parts[1].strip()
        return {"custom_instructions": instructions, "test_message": test_message}
    except Exception:
        raise upstream_error()


@app.post("/memories", summary="Create memories")
def add_memory(memory_create: MemoryCreate, _auth=Depends(verify_auth)):
    """Store new memories."""
    if not any([memory_create.user_id, memory_create.agent_id, memory_create.run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier (user_id, agent_id, run_id) is required.")

    params = {k: v for k, v in memory_create.model_dump().items() if v is not None and k != "messages"}
    try:
        response = get_memory_instance().add(messages=[m.model_dump() for m in memory_create.messages], **params)
        if response.get("results"):
            telemetry.log_dashboard_nudge_once(DASHBOARD_URL)
        return JSONResponse(content=response)
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()


ALL_MEMORIES_LIMIT = 1000
_RESERVED_PAYLOAD_KEYS = {"data", "user_id", "agent_id", "run_id", "hash", "created_at", "updated_at", "expiration_date"}


def _serialize_memory(row: Any) -> Dict[str, Any]:
    payload = getattr(row, "payload", None) or {}
    return {
        "id": getattr(row, "id", None),
        "memory": payload.get("data"),
        "user_id": payload.get("user_id"),
        "agent_id": payload.get("agent_id"),
        "run_id": payload.get("run_id"),
        "hash": payload.get("hash"),
        "expiration_date": payload.get("expiration_date"),
        "metadata": {k: v for k, v in payload.items() if k not in _RESERVED_PAYLOAD_KEYS},
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


def _list_all_memories(limit: int = ALL_MEMORIES_LIMIT) -> Dict[str, Any]:
    # 二轮审计 fix-10：SQL 级排序（pgvector.list order_by 白名单模式）——
    # 库超 ALL_MEMORIES_LIMIT 时物理序窗口会回归「最近 N 条」失效；
    # Python 兜底排序保留（对 SQL 已排序结果幂等）
    results = get_memory_instance().vector_store.list(
        top_k=ALL_MEMORIES_LIMIT if limit >= ALL_MEMORIES_LIMIT else limit,
        order_by="updated_at_desc",
    )
    rows = results[0] if results and isinstance(results, list) and isinstance(results[0], list) else results or []
    serialized = [_serialize_memory(row) for row in rows]

    def _recency_key(m: Dict[str, Any]) -> str:
        # 列表端点的语义是「最新在前」，统一按 updated_at（缺则 created_at）
        # 倒序；两个时间戳都缺失的行沉底。
        return m.get("updated_at") or m.get("created_at") or ""

    serialized.sort(key=_recency_key, reverse=True)
    return {"results": serialized[:limit]}


@app.get("/memories", summary="Get memories")
def get_all_memories(
    request: Request,
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    top_k: Optional[int] = Query(None, ge=0, le=ALL_MEMORIES_LIMIT),
    show_expired: bool = Query(False),
    _auth=Depends(verify_auth),
):
    """Retrieve stored memories. Lists all memories when no identifier is provided (admin only)."""
    try:
        if not any([user_id, run_id, agent_id]):
            auth_type = getattr(request.state, "auth_type", "none")
            if _auth is not None and _auth.role != "admin" and auth_type not in {"admin_api_key", "disabled"}:
                raise HTTPException(status_code=403, detail="Admin role required to list all memories.")
            # Admin all-memory listing is intentionally raw; scoped get_all below applies expiry visibility.
            return _list_all_memories(limit=top_k if top_k is not None else ALL_MEMORIES_LIMIT)
        filters = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v
        }
        params = {"filters": filters}
        if top_k is not None:
            params["top_k"] = top_k
        params["show_expired"] = show_expired
        return get_memory_instance().get_all(**params)
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}", summary="Get a memory")
def get_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve a specific memory by ID."""
    try:
        return get_memory_instance().get(memory_id)
    except Exception:
        raise upstream_error()


@app.post("/search", summary="Search memories")
def search_memories(search_req: SearchRequest, _auth=Depends(verify_auth)):
    """Search for memories based on a query."""
    start = time.perf_counter()
    query_log = {"query": search_req.query, "result_count": 0, "avg_score": None, "is_zero_hit": True}
    results = []
    trace_dict = None
    try:
        filters = search_req.filters or {}
        deprecated_keys = []
        for entity_key in ("user_id", "agent_id", "run_id"):
            entity_val = getattr(search_req, entity_key, None)
            if entity_val:
                filters[entity_key] = entity_val
                deprecated_keys.append(entity_key)
        if deprecated_keys:
            logging.warning(
                "Top-level %s in /search is deprecated. Use filters={%s} instead.",
                ", ".join(deprecated_keys),
                ", ".join(f'"{k}": "..."' for k in deprecated_keys),
            )
        query_log["user_id"] = filters.get("user_id")
        query_log["agent_id"] = filters.get("agent_id")
        query_log["run_id"] = filters.get("run_id")
        params = {}
        if search_req.top_k is not None:
            params["top_k"] = search_req.top_k
        if search_req.threshold is not None:
            params["threshold"] = search_req.threshold
        if search_req.explain is not None:
            params["explain"] = search_req.explain
        if search_req.show_expired is not None:
            params["show_expired"] = search_req.show_expired
        # Reranking: explicitly set by caller, or auto-enable when reranker is configured
        memory = get_memory_instance()
        if search_req.rerank is not None:
            params["rerank"] = search_req.rerank
        elif memory.reranker is not None:
            params["rerank"] = True
        if search_req.depth is not None:
            params["depth"] = search_req.depth
        query_log["top_k"] = params.get("top_k")
        query_log["depth"] = params.get("depth")
        query_log["rerank"] = bool(params.get("rerank", False))
        raw = memory.search(query=search_req.query, filters=filters, trace=True, **params)
        if isinstance(raw, dict):
            # trace is internal RECALL observability, never surfaced to clients
            trace_dict = raw.pop("trace", None)
            results = raw.get("results", [])
        else:
            results = raw
        query_log["result_count"] = len(results)
        query_log["is_zero_hit"] = query_log["result_count"] == 0
        scores = [r.get("score") for r in results if isinstance(r, dict) and r.get("score") is not None]
        query_log["avg_score"] = round(sum(scores) / len(scores), 4) if scores else None
        return raw
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()
    finally:
        query_log["latency_ms"] = round((time.perf_counter() - start) * 1000, 2)
        query_log["trace"] = trace_dict
        query_log["temporal_triggered"] = bool(trace_dict and trace_dict.get("temporal_triggered"))
        if isinstance(trace_dict, dict) and trace_dict.get("depth"):
            query_log["depth"] = trace_dict["depth"]
        _submit_evolve_query(query_log)
        # Graph fragments carry transient uuids (source="graph") and must not
        # be counted as accesses; vector results use the real memory id.
        _submit_evolve_salience_access(
            [r["id"] for r in results if isinstance(r, dict) and r.get("id") and r.get("source") != "graph"]
        )


@app.put("/memories/{memory_id}", summary="Update a memory")
def update_memory(memory_id: str, updated_memory: MemoryUpdate, _auth=Depends(verify_auth)):
    """Update an existing memory."""
    try:
        fields_set = getattr(updated_memory, "model_fields_set", None) or set()
        params: dict[str, Any] = {"memory_id": memory_id}
        if "text" in fields_set:
            params["text"] = updated_memory.text
        if "metadata" in fields_set:
            params["metadata"] = updated_memory.metadata
        if "expiration_date" in fields_set:
            params["expiration_date"] = updated_memory.expiration_date
        return get_memory_instance().update(**params)
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}/history", summary="Get memory history")
def memory_history(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve memory history."""
    try:
        return get_memory_instance().history(memory_id=memory_id)
    except Exception:
        raise upstream_error()


@app.delete("/memories/{memory_id}", summary="Delete a memory", response_model=MessageResponse)
def delete_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Delete a specific memory by ID."""
    try:
        get_memory_instance().delete(memory_id=memory_id)
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()

    return MessageResponse(message="Memory deleted successfully")


@app.delete("/memories", summary="Delete all memories", response_model=MessageResponse)
def delete_all_memories(
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    _auth=Depends(require_admin),
):
    """Delete all memories for a given identifier. Requires admin role."""
    if not any([user_id, run_id, agent_id]):
        raise HTTPException(status_code=400, detail="At least one identifier is required.")
    try:
        params = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v
        }
        get_memory_instance().delete_all(**params)
        return MessageResponse(message="All relevant memories deleted")
    except Exception:
        raise upstream_error()


@app.post("/reset", summary="Reset all memories")
def reset_memory(_auth=Depends(require_admin)):
    """Completely reset stored memories. Requires admin role."""
    try:
        get_memory_instance().reset()
        return {"message": "All memories reset"}
    except Exception:
        raise upstream_error()


@app.get("/", summary="Redirect to the OpenAPI documentation", include_in_schema=False)
def home():
    """Redirect to the OpenAPI documentation."""
    return RedirectResponse(url="/docs")


@app.get("/v1/ping", include_in_schema=False)
def v1_ping():
    """MemoryClient 构造期探活兼容端点（二轮审计 fix proxy 契约）。

    MemoryClient.__init__ 先 GET /v1/ping/ 验证 API key 通路；本 server 用
    自有认证体系，该端点只做可达性探活（无敏感信息），有意豁免鉴权。
    /v3/* 等托管形状端点未实现——proxy 的 MemoryClient 分支仅保证不炸，
    完整托管兼容面不在本仓库范围。
    """
    return {"message": "ok"}
