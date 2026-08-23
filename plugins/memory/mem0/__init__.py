"""Mem0 memory plugin — MemoryProvider interface.

Server-side LLM fact extraction, semantic search, and automatic deduplication
via the Mem0 Platform API (cloud) or OSS (self-hosted) via Memory.

Original PR #2933 by kartik-mem0, adapted to MemoryProvider ABC.

Configuration
-------------
Secret (lives in $HERMES_HOME/.env or the environment):
  MEM0_API_KEY       — Mem0 Platform API key (required for platform mode)
  MEM0_HOST          — Base URL of a self-hosted Mem0 server. When set, the
                       plugin talks to that server directly over HTTP
                       (X-API-Key auth) instead of the cloud API.

Behavioral settings (live in $HERMES_HOME/mem0.json, set via `hermes memory
setup`):
  mode               — Backend mode: "platform" (default) or "oss"
  host               — Self-hosted Mem0 server URL (alt: MEM0_HOST env var).
                       When set, routes to the self-hosted HTTP backend.
  user_id            — Canonical user identifier. When set, it is applied
                       uniformly across every gateway (CLI, Telegram, Slack,
                       Discord, …) so the same human gets one merged memory
                       store. When unset, the gateway-native id (e.g. Telegram
                       numeric id, Discord snowflake) is used instead.
  agent_id           — Agent identifier (default: hermes)

The matching MEM0_MODE / MEM0_USER_ID / MEM0_AGENT_ID environment variables are
still read as a backward-compatible fallback, but mem0.json is the canonical
home for these non-secret settings.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from agent.secret_scope import get_secret
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Circuit breaker: after this many consecutive failures, pause API calls
# for _BREAKER_COOLDOWN_SECS to avoid hammering a down server.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120
_PREFETCH_WAIT_SECS = 15
# Maximum number of pending sync items in the queue. When the queue is full,
# the oldest item is silently dropped to prevent unbounded memory growth
# during extended mem0 server outages.
_SYNC_QUEUE_MAXLEN = 50

# 潮浪并忆（Tidal Coalescing）：把同一 user+session 的多条短对话合并成一次
# backend.add 批量写入，摊薄服务端 LLM 事实提取的调用次数。全部参数可通过
# MEM0_COALESCE_* 环境变量配置；MEM0_COALESCE_ENABLED=false 关闭合并，
# 回到逐条写入的旧语义。
_COALESCE_ENABLED = os.environ.get("MEM0_COALESCE_ENABLED", "true").lower() in ("1", "true", "yes")
_COALESCE_IDLE_SECS = float(os.environ.get("MEM0_COALESCE_IDLE_SECS", "5"))
_COALESCE_WINDOW_SECS = float(os.environ.get("MEM0_COALESCE_WINDOW_SECS", "15"))
_COALESCE_MAX_TURNS = int(os.environ.get("MEM0_COALESCE_MAX_TURNS", "5"))
_COALESCE_MAX_CHARS = int(os.environ.get("MEM0_COALESCE_MAX_CHARS", "4000"))
_COALESCE_FASTPATH_CHARS = int(os.environ.get("MEM0_COALESCE_FASTPATH_CHARS", "2000"))

_CLIENT_ERROR_TYPES = ("MemoryNotFoundError", "ValidationError")

# Sentinel returned when neither MEM0_USER_ID nor a gateway-native id is
# available. Treated as "no operator-configured user_id" by initialize() so
# that legacy mem0.json files written by the setup wizard (which historically
# wrote this exact placeholder) still allow gateway-native ids to flow
# through instead of silently overriding them with the placeholder.
_DEFAULT_USER_ID = "hermes-user"

# Feedback notes carry a text summary; trim to keep the payload small.
_FEEDBACK_NOTE_MAX_CHARS = 200


def _is_client_error(exc: Exception) -> bool:
    """True for user-caused errors (bad ID, not found) that should NOT trip circuit breaker."""
    etype = type(exc).__name__
    if etype in _CLIENT_ERROR_TYPES:
        return True
    err_str = str(exc).lower()
    return "404" in err_str or "not found" in err_str or "valid uuid" in err_str


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from env vars, with $HERMES_HOME/mem0.json overrides.

    Environment variables provide defaults; mem0.json (if present) overrides
    individual keys.  This avoids a silent failure when the JSON file exists
    but is missing fields like ``api_key`` that the user set in ``.env``.
    """
    from hermes_constants import get_hermes_home

    config = {
        "mode": os.environ.get("MEM0_MODE", "platform"),
        "api_key": get_secret("MEM0_API_KEY", ""),
        "host": os.environ.get("MEM0_HOST", ""),
        "agent_id": os.environ.get("MEM0_AGENT_ID", "hermes"),
        "oss": {},
    }
    # Only carry user_id when the operator explicitly configured one (env or
    # mem0.json). An absent key tells initialize() to fall back to the
    # gateway-native id from kwargs instead of overriding it with a placeholder.
    env_user_id = os.environ.get("MEM0_USER_ID")
    if env_user_id:
        config["user_id"] = env_user_id

    config_path = get_hermes_home() / "mem0.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass

    return config


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "mem0_search",
    "description": (
        "Search the user's memories by meaning; returns facts ranked by "
        "relevance. Use this before answering any question that may depend on "
        "what you know about the user (preferences, facts, history, people, "
        "projects, past decisions). For multi-part or multi-hop questions, "
        "call it several times — vary the wording and run follow-up searches "
        "on what earlier results reveal; one search is rarely enough."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
            "rerank": {"type": "boolean", "description": "Rerank results for relevance (self-hosted supports this when reranker is configured)."},
        },
        "required": ["query"],
    },
}

ADD_SCHEMA = {
    "name": "mem0_add",
    "description": (
        "Store a durable fact about the user, verbatim (no LLM extraction). "
        "Call this the moment the user states a lasting preference, correction, "
        "decision, or personal detail worth recalling on future turns — don't "
        "wait to be asked to remember. Skip transient chit-chat and facts you've "
        "already stored."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to store."},
        },
        "required": ["content"],
    },
}

UPDATE_SCHEMA = {
    "name": "mem0_update",
    "description": (
        "Replace the text of an existing memory by its ID (take the ID from a "
        "mem0_search result). Use when a stored fact has changed "
        "or was wrong — correct it in place instead of adding a duplicate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to update."},
            "text": {"type": "string", "description": "New text content."},
        },
        "required": ["memory_id", "text"],
    },
}

DELETE_SCHEMA = {
    "name": "mem0_delete",
    "description": (
        "Delete a memory by its ID (take the ID from a mem0_search "
        "result). Use when a stored fact is obsolete or the user asks you to "
        "forget it; prefer mem0_update if the fact merely changed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "Memory UUID to delete."},
        },
        "required": ["memory_id"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class Mem0MemoryProvider(MemoryProvider):
    """Mem0 memory with server-side extraction and semantic search.

    Supports Platform API (cloud) and OSS (self-hosted) modes via MEM0_MODE.
    """

    def __init__(self):
        self._config = None
        self._backend = None
        self._mode = "platform"
        self._api_key = ""
        self._host = ""
        self._user_id = _DEFAULT_USER_ID
        self._agent_id = "hermes"
        self._rerank_default = False
        self._channel = "cli"  # gateway channel name (cli/telegram/discord/...)
        self._sync_thread = None
        self._prefetch_thread = None
        self._prefetch_query = ""
        self._prefetch_result = ""
        self._prefetch_done = False
        # Sync queue: consumer thread processes items sequentially so that
        # slow server responses don't cause turns to be skipped.  Bounded to
        # _SYNC_QUEUE_MAXLEN to prevent unbounded growth during outages.
        self._sync_queue = deque(maxlen=_SYNC_QUEUE_MAXLEN)
        self._sync_consumer_thread = None
        self._sync_consumer_started = False
        # 潮浪并忆合并缓冲：key=(user_id, session_id)，value 为含 created/last/
        # chars/messages 的桶。仅在后台消费者线程内读写，无需额外加锁。
        self._coalesce_enabled = _COALESCE_ENABLED
        self._coalesce_buckets = {}
        # 合并统计（可观测性）：batches=已合并批次，direct=fastpath 直接落库数，
        # saved_calls=省下的 backend.add 调用次数，bucket_turns=按 session 分布。
        self._coalesce_stats = {"batches": 0, "direct": 0, "saved_calls": 0, "bucket_turns": {}}
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0
        self._breaker_lock = threading.Lock()
        self._sync_lock = threading.Lock()
        self._prefetch_lock = threading.Lock()
        self._atexit_registered = False

    @property
    def name(self) -> str:
        return "mem0"

    def is_available(self) -> bool:
        cfg = _load_config()
        mode = cfg.get("mode", "platform")
        if mode == "oss":
            return bool(cfg.get("oss", {}).get("vector_store"))
        # Platform needs an api_key; self-hosted needs a host (api_key optional
        # when the server runs with AUTH_DISABLED).
        return bool(cfg.get("api_key") or cfg.get("host"))

    def save_config(self, values, hermes_home):
        """Write config to $HERMES_HOME/mem0.json."""
        import json
        from pathlib import Path
        config_path = Path(hermes_home) / "mem0.json"
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing.update(values)
        from utils import atomic_json_write
        atomic_json_write(config_path, existing, mode=0o600)

    def get_config_schema(self):
        cfg = _load_config()
        mode = cfg.get("mode", "platform")
        api_key_required = mode != "oss"
        return [
            {"key": "api_key", "description": "Mem0 Platform API key", "secret": True, "required": api_key_required, "env_var": "MEM0_API_KEY", "url": "https://app.mem0.ai"},
            {"key": "host", "description": "Self-hosted Mem0 server URL (leave blank for cloud)", "required": False, "env_var": "MEM0_HOST"},
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
            {"key": "rerank", "description": "Enable reranking for recall", "default": "false", "choices": ["true", "false"]},
        ]

    def post_setup(self, hermes_home: str, config: dict) -> None:
        from ._setup import post_setup
        post_setup(hermes_home, config)

    def _create_backend(self):
        # Lazy-install the mem0 SDK on demand before either backend imports
        # it. ensure() honors security.allow_lazy_installs (default true) and,
        # on a sealed Docker venv, redirects the install to the durable
        # target. On failure we fall through so the import inside the backend
        # produces the canonical error, captured below.
        try:
            from tools.lazy_deps import ensure as _lazy_ensure
            _lazy_ensure("memory.mem0", prompt=False)
        except ImportError:
            pass
        except Exception:
            pass
        try:
            if self._mode == "oss":
                from ._backend import OSSBackend
                return OSSBackend(self._config.get("oss", {}))
            if self._host:
                from ._backend import SelfHostedBackend
                return SelfHostedBackend(self._api_key, self._host)
            from ._backend import PlatformBackend
            return PlatformBackend(self._api_key)
        except Exception as e:
            logger.error("Mem0 backend failed to initialize (%s mode): %s", self._mode, e)
            self._init_error = str(e)
            return None

    def _is_breaker_open(self) -> bool:
        """Return True if the circuit breaker is tripped (too many failures)."""
        with self._breaker_lock:
            if self._consecutive_failures < _BREAKER_THRESHOLD:
                return False
            if time.monotonic() >= self._breaker_open_until:
                self._consecutive_failures = 0
                return False
            return True

    def _format_error(self, prefix: str, exc: Exception) -> str:
        msg = f"{prefix}: {exc}"
        if self._mode == "oss":
            err_str = str(exc).lower()
            if "connection" in err_str or "refused" in err_str or "timeout" in err_str:
                vs = self._config.get("oss", {}).get("vector_store", {})
                msg += f" (check that {vs.get('provider', 'vector store')} is running)"
        return msg

    def _record_success(self):
        with self._breaker_lock:
            self._consecutive_failures = 0

    def _record_failure(self):
        with self._breaker_lock:
            self._consecutive_failures += 1
            count = self._consecutive_failures
            if count >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            else:
                count = 0
        if count >= _BREAKER_THRESHOLD:
            hint = ""
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                provider = vs.get("provider", "unknown")
                hint = f" Check that your {provider} vector store is running and reachable."
            logger.warning(
                "Mem0 circuit breaker tripped after %d consecutive failures. "
                "Pausing API calls for %ds.%s",
                count, _BREAKER_COOLDOWN_SECS, hint,
            )

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        self._mode = self._config.get("mode", "platform")
        self._api_key = self._config.get("api_key", "")
        self._host = self._config.get("host", "")
        # Resolution order for user_id:
        #   1. Operator-configured MEM0_USER_ID (env or $HERMES_HOME/mem0.json) —
        #      the canonical principal, applied across every gateway so the same
        #      human gets one merged memory store.
        #   2. Gateway-native id from kwargs (Telegram numeric id, Discord
        #      snowflake, etc.) — preserves per-platform isolation when no
        #      override is configured.
        #   3. Hardcoded fallback _DEFAULT_USER_ID (CLI with no auth).
        # The literal _DEFAULT_USER_ID string is treated as unset so users who
        # ran the setup wizard with the suggested default still get gateway-
        # native ids instead of being silently bucketed together.
        configured = self._config.get("user_id")
        if configured == _DEFAULT_USER_ID:
            configured = None
        self._user_id = configured or kwargs.get("user_id") or _DEFAULT_USER_ID
        self._agent_id = self._config.get("agent_id", "hermes")
        # Persisted rerank preference (setup wizard / mem0.json). Used as the
        # DEFAULT for mem0_search when the model doesn't pass ``rerank``
        # explicitly; per-call args still win. Platform-only feature — other
        # backends accept-and-ignore the flag.
        _rr = self._config.get("rerank", False)
        self._rerank_default = (
            _rr.lower() in ("true", "1", "yes") if isinstance(_rr, str) else bool(_rr)
        )
        self._channel = kwargs.get("platform") or "cli"
        self._backend = self._create_backend()
        if self._backend and not self._atexit_registered:
            atexit.register(self._shutdown_backend)
            self._atexit_registered = True
        # 潮浪并忆启动配置日志：确认合并功能是否启用及各参数实际值
        logger.info(
            "潮浪并忆：合并功能%s，空闲阈值=%ss，窗口阈值=%ss，最大对话数=%d，"
            "最大字符数=%d，快速直写阈值=%d",
            "启用" if self._coalesce_enabled else "关闭（逐条直写）",
            _COALESCE_IDLE_SECS, _COALESCE_WINDOW_SECS,
            _COALESCE_MAX_TURNS, _COALESCE_MAX_CHARS, _COALESCE_FASTPATH_CHARS,
        )

    def _read_filters(self) -> Dict[str, Any]:
        # Scoped to user_id only — by design — so recall surfaces memories
        # written from any gateway/agent under this principal. Writes attach
        # agent_id (and metadata.channel) so per-agent / per-channel views are
        # still possible at query time when needed; reads default to the wider
        # cross-agent recall.
        return {"user_id": self._user_id}

    def _write_metadata(self) -> Dict[str, Any]:
        # Tag every write with the gateway channel so the dashboard can offer
        # per-channel filtered views without coupling identity to the channel.
        return {"channel": self._channel} if self._channel else {}

    def system_prompt_block(self) -> str:
        # Mirror the precedence in _create_backend (oss > host > platform) so
        # the label always names the backend that actually runs. Checking
        # ``host`` first here would mislabel an ``oss``+``host`` config as
        # self-hosted HTTP even though OSS wins the routing.
        if self._mode == "oss":
            mode_label = "OSS (self-hosted)"
        elif self._host:
            mode_label = "self-hosted (HTTP API)"
        else:
            mode_label = "platform (cloud API)"
        # Rerank is available when the backend supports it (self-hosted with reranker config, or platform).
        rerank_note = " Rerank is available on search." if self._rerank_default else ""
        return (
            "# Mem0 Memory\n"
            f"Active. Mode: {mode_label}. User: {self._user_id}.\n"
            "You have persistent memory of this user from past conversations. "
            "You should call mem0_search before answering anything that could depend "
            "on prior context (the user's preferences, facts, history, people, "
            "projects, or earlier decisions) — do not rely on the chat window "
            "alone, and do not assume you have no memory.\n"
            "For multi-part or multi-hop questions, run several searches with "
            "different wording/angles and follow-up searches on what the first "
            "results surface; one search is rarely enough. Keep searching until "
            "you have every fact the question needs before you answer.\n"
            "Tools: mem0_search to find memories, mem0_add to store facts, "
            f"mem0_update and mem0_delete to manage by ID.{rerank_note}"
        )

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._start_prefetch(message)

    def _consume_prefetch_result(self, query: str) -> str | None:
        with self._prefetch_lock:
            if self._prefetch_query != query or not self._prefetch_done:
                return None
            result = self._prefetch_result
            self._prefetch_result = ""
            self._prefetch_done = False
            return result

    def _start_prefetch(self, query: str) -> None:
        if not query or self._backend is None or self._is_breaker_open():
            return
        backend = self._backend
        with self._prefetch_lock:
            if self._prefetch_query == query:
                if self._prefetch_done:
                    return
                if self._prefetch_thread and self._prefetch_thread.is_alive():
                    return
            self._prefetch_query = query
            self._prefetch_result = ""
            self._prefetch_done = False

        def _run():
            body = ""
            try:
                results = backend.search(
                    query, filters=self._read_filters(), top_k=10, rerank=self._rerank_default,
                )
                lines = [r.get("memory", "") for r in (results or []) if r.get("memory")]
                if lines:
                    body = "## Mem0 Memory\n" + "\n".join(f"- {line_text}" for line_text in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("Mem0 prefetch failed: %s", e)
            with self._prefetch_lock:
                if self._prefetch_query == query:
                    self._prefetch_result = body
                    self._prefetch_done = True

        t = threading.Thread(target=_run, daemon=True, name="mem0-prefetch")
        with self._prefetch_lock:
            self._prefetch_thread = t
        t.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall memories for the CURRENT question with a short hot-path wait."""
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        self._start_prefetch(query)
        with self._prefetch_lock:
            thread = self._prefetch_thread if self._prefetch_query == query else None
        if thread:
            thread.join(timeout=_PREFETCH_WAIT_SECS)
        cached = self._consume_prefetch_result(query)
        if cached is not None:
            return cached
        # Slow backend: skip injection; mem0_search tool remains the backstop.
        return ""

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Send the turn to Mem0 for server-side fact extraction (non-blocking).

        Enqueues the turn into a bounded deque; a dedicated consumer thread
        pops items sequentially so that slow server responses don't cause
        subsequent turns to be skipped. session_id 随消息一并入队，供合并缓冲
        按 user+session 维度分桶。
        """
        if self._backend is None or self._is_breaker_open():
            return
        if not self._sync_consumer_started:
            self._start_sync_consumer()
        self._sync_queue.append((session_id, user_content, assistant_content))

    def _start_sync_consumer(self) -> None:
        """Start the background consumer thread (idempotent)."""
        if self._sync_consumer_started:
            return
        self._sync_consumer_started = True
        self._sync_consumer_thread = threading.Thread(
            target=self._sync_consumer_loop,
            daemon=True,
            name="mem0-sync-consumer",
        )
        self._sync_consumer_thread.start()

    def _sync_consumer_loop(self) -> None:
        """Process sync items: coalesce short turns into batch adds.

        队列非空时排空入队项并冲刷已达上限的合并桶；队列为空时按空闲/窗口
        超时唤醒冲刷。每批写入共用后端客户端各自的 HTTP 超时，慢响应不会
        级联影响后续批次——它们只是排队等待。
        """
        while True:
            drained = False
            while True:
                try:
                    item = self._sync_queue.popleft()
                except IndexError:
                    break
                drained = True
                self._route_sync_item(item)
            if drained:
                self._flush_capped_buckets()
                continue

            # 队列已空：冲刷到期的桶，否则睡到最近期限（上限 0.5s 保证响应性）
            now = time.monotonic()
            if self._coalesce_buckets:
                deadline = self._next_flush_deadline(now)
                delay = deadline - now if deadline is not None else 0.5
                if delay <= 0:
                    self._flush_due_buckets(now)
                    continue
                time.sleep(min(delay, 0.5))
            else:
                time.sleep(0.5)

    @staticmethod
    def _looks_like_json(text: str) -> bool:
        """判断单条消息内容是否是可解析的 JSON 结构（工具输出/配置原文）。

        用于在进入 LLM 提取链路前把 JSON 正文替换为占位——提取模型会把对话
        中出现的 JSON 键名/完整对象当"事实"入库（2026-08-12 实证：38 条 JSON
        正文 + 11 条键名碎片）。判断依据：去掉首尾空白后可被 json.loads 解析，
        且以 { [ 开头（排除普通文本）。
        """
        if not text or not text.strip():
            return False
        s = text.strip()
        if s[0] not in "{[":  # JSON 对象/数组开头，排除普通文本
            return False
        try:
            json.loads(s)
            return True
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _sanitize_json_message(content: str) -> str:
        """把纯 JSON 消息替换为占位符，保留自然语言内容原样。

        只剥离"整条消息就是 JSON"的情况（工具输出、配置原文、API 响应）；
        自然语言消息即使内嵌 JSON 片段也放行（由服务端提取指令负向约束拦截）。
        相比整轮跳过，占位替换不丢失同一轮里另一条消息的自然语言事实。
        """
        if Mem0MemoryProvider._looks_like_json(content):
            return "<JSON 结构化数据，已省略>"
        return content

    def _route_sync_item(self, item) -> None:
        """把一条入队项路由到 fastpath 直接落库或合并缓冲。"""
        backend = self._backend
        if backend is None:
            return
        session_id, user_content, assistant_content = item
        # 剥离纯 JSON 消息（工具输出/配置原文）：替换为占位符而非整轮跳过，
        # 避免 LLM 把 JSON 键名/对象当"事实"入库，同时保留同轮自然语言事实。
        user_content = self._sanitize_json_message(user_content)
        assistant_content = self._sanitize_json_message(assistant_content)
        if (user_content != item[1] or assistant_content != item[2]):
            self._coalesce_stats["json_sanitized"] = self._coalesce_stats.get("json_sanitized", 0) + 1
            logger.debug(
                "潮浪并忆：剥离 JSON 结构消息（session=%s）——替换为占位符，保留自然语言",
                session_id or "<empty>",
            )
        msg_chars = len(user_content) + len(assistant_content)
        if not self._coalesce_enabled:
            # 合并功能关闭：沿用旧语义逐条写入
            logger.debug(
                "潮浪并忆：合并已关闭，消息逐条直写（session=%s，chars=%d）",
                session_id or "<empty>", msg_chars,
            )
            self._add_messages(
                backend,
                [{"role": "user", "content": user_content},
                 {"role": "assistant", "content": assistant_content}],
                self._user_id,
            )
            return
        if msg_chars > _COALESCE_FASTPATH_CHARS:
            # fastpath：过长消息直接落库，不进入合并缓冲等待，避免长内容延迟入库
            logger.debug(
                "潮浪并忆：消息超过快速直写阈值，直接落库（session=%s，chars=%d，阈值=%d）",
                session_id or "<empty>", msg_chars, _COALESCE_FASTPATH_CHARS,
            )
            self._add_messages(
                backend,
                [{"role": "user", "content": user_content},
                 {"role": "assistant", "content": assistant_content}],
                self._user_id,
            )
            self._coalesce_stats["direct"] += 1
            return
        # 进入按 user+session 分桶的合并缓冲
        key = (self._user_id, session_id)
        now = time.monotonic()
        bucket = self._coalesce_buckets.get(key)
        if bucket is None:
            bucket = {"created": now, "last": now, "chars": 0, "messages": []}
            self._coalesce_buckets[key] = bucket
            logger.debug(
                "潮浪并忆：创建合并缓冲桶（session=%s，idle=%ss，window=%ss）",
                session_id or "<empty>", _COALESCE_IDLE_SECS, _COALESCE_WINDOW_SECS,
            )
        bucket["messages"].extend([
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ])
        bucket["chars"] += msg_chars
        bucket["last"] = now
        turns = len(bucket["messages"]) // 2
        logger.debug(
            "潮浪并忆：消息进入合并缓冲（session=%s，chars=%d，桶内 turns=%d）",
            session_id or "<empty>", msg_chars, turns,
        )
        if bucket["chars"] >= _COALESCE_MAX_CHARS or turns >= _COALESCE_MAX_TURNS:
            self._flush_bucket(key, trigger="上限")

    def _flush_capped_buckets(self) -> None:
        """冲刷达到条数/字符上限的分桶。"""
        for key in list(self._coalesce_buckets.keys()):
            bucket = self._coalesce_buckets[key]
            turns = len(bucket["messages"]) // 2
            if bucket["chars"] >= _COALESCE_MAX_CHARS or turns >= _COALESCE_MAX_TURNS:
                self._flush_bucket(key, trigger="上限")

    def _next_flush_deadline(self, now: float):
        """返回所有桶中最先到期的空闲/窗口冲刷时刻；无桶时返回 None。"""
        deadline = None
        for bucket in self._coalesce_buckets.values():
            due = min(bucket["last"] + _COALESCE_IDLE_SECS,
                      bucket["created"] + _COALESCE_WINDOW_SECS)
            if deadline is None or due < deadline:
                deadline = due
        return deadline

    def _flush_due_buckets(self, now: float) -> None:
        """冲刷空闲超时或窗口超时已到期的分桶。"""
        for key in list(self._coalesce_buckets.keys()):
            bucket = self._coalesce_buckets[key]
            if now - bucket["last"] >= _COALESCE_IDLE_SECS:
                self._flush_bucket(key, trigger="空闲超时")
            elif now - bucket["created"] >= _COALESCE_WINDOW_SECS:
                self._flush_bucket(key, trigger="窗口超时")

    def _flush_bucket(self, key, trigger: str = "") -> None:
        """把单个分桶合并为一次 backend.add 批量写入。

        trigger 记录冲刷原因（上限/空闲超时/窗口超时/兜底），仅用于日志。
        """
        bucket = self._coalesce_buckets.pop(key, None)
        if not bucket or not bucket["messages"]:
            return
        backend = self._backend
        if backend is None:
            return
        messages = bucket["messages"]
        turns = len(messages) // 2
        user_id, session_id = key
        try:
            backend.add(
                messages,
                user_id=user_id,
                agent_id=self._agent_id,
                infer=True,
                metadata=self._write_metadata(),
            )
            self._record_success()
            saved = max(0, turns - 1)
            self._coalesce_stats["batches"] += 1
            self._coalesce_stats["saved_calls"] += saved
            sid = session_id or "<empty>"
            self._coalesce_stats["bucket_turns"][sid] = (
                self._coalesce_stats["bucket_turns"].get(sid, 0) + turns
            )
            logger.info(
                "潮浪并忆：合并 %d 条对话为 1 次写入（session=%s，省 %d 次调用，chars=%d%s）",
                turns, sid, saved, bucket["chars"],
                f"，原因：{trigger}" if trigger else "",
            )
        except Exception as e:
            self._record_failure()
            logger.warning(
                "潮浪并忆：合并冲刷失败（session=%s，turns=%d，chars=%d，原因：%s）：%s",
                session_id or "<empty>", turns, bucket["chars"], trigger or "未知", e,
            )

    def _add_messages(self, backend, messages, user_id) -> None:
        """单条 backend.add（fastpath / 合并关闭时的逐条写入），统一错误处理。"""
        try:
            backend.add(
                messages,
                user_id=user_id,
                agent_id=self._agent_id,
                infer=True,
                metadata=self._write_metadata(),
            )
            self._record_success()
        except Exception as e:
            self._record_failure()
            logger.warning("Mem0 sync failed: %s", e)

    def _flush_all_buckets(self) -> None:
        """冲刷全部合并缓冲（shutdown / atexit 前兜底，保证记忆不丢失）。"""
        for key in list(self._coalesce_buckets.keys()):
            self._flush_bucket(key, trigger="兜底冲刷")

    def coalesce_stats(self) -> dict:
        """返回潮浪并忆的可观测统计快照（合并批次、省下调用、桶分布）。"""
        return dict(
            self._coalesce_stats,
            bucket_turns=dict(self._coalesce_stats["bucket_turns"]),
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, ADD_SCHEMA, UPDATE_SCHEMA, DELETE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._backend is None:
            err = getattr(self, "_init_error", "unknown error")
            hint = ""
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                provider = vs.get("provider", "vector store")
                hint = f" Check that {provider} is running and reachable."
            return json.dumps({"error": f"Mem0 backend not initialized: {err}.{hint}"})

        if self._is_breaker_open():
            msg = "Mem0 temporarily unavailable (multiple consecutive failures). Will retry automatically."
            if self._mode == "oss":
                vs = self._config.get("oss", {}).get("vector_store", {})
                msg += f" Check that your {vs.get('provider', 'vector store')} is running."
            return json.dumps({"error": msg})

        if tool_name == "mem0_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            try:
                top_k = max(1, min(int(args.get("top_k", 10)), 50))
                rerank_raw = args.get("rerank", getattr(self, "_rerank_default", False))
                if isinstance(rerank_raw, str):
                    rerank = rerank_raw.lower() not in ("false", "0", "no")
                else:
                    rerank = bool(rerank_raw)
                results = self._backend.search(query, filters=self._read_filters(), top_k=top_k, rerank=rerank)
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"id": r.get("id"), "memory": r.get("memory", ""),
                          "score": r.get("score", 0)} for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                if not _is_client_error(e):
                    self._record_failure()
                return tool_error(self._format_error("Search failed", e))

        elif tool_name == "mem0_add":
            content = args.get("content", "")
            if not content:
                return tool_error("Missing required parameter: content")
            try:
                result = self._backend.add(
                    [{"role": "user", "content": content}],
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    infer=False,
                    metadata=self._write_metadata(),
                )
                self._record_success()
                event_id = result.get("event_id") if isinstance(result, dict) else None
                # Cloud add is async (server-side extraction); OSS and self-hosted store synchronously.
                msg = "Fact stored." if (self._mode == "oss" or self._host) else "Fact queued for storage."
                return json.dumps({"result": msg, "event_id": event_id})
            except Exception as e:
                self._record_failure()
                return tool_error(self._format_error("Failed to store", e))

        elif tool_name == "mem0_update":
            memory_id = args.get("memory_id", "")
            text = args.get("text", "")
            if not memory_id:
                return tool_error("Missing required parameter: memory_id")
            if not text:
                return tool_error("Missing required parameter: text")
            try:
                result = self._backend.update(memory_id, text)
                self._record_success()
                # A correction replaces a stored fact — report it so the evolve
                # loop can adjust salience. Best-effort; never affects the result.
                self._report_feedback("correction", memory_id, note=text)
                return json.dumps(result)
            except Exception as e:
                if _is_client_error(e):
                    return tool_error(f"Memory not found: {memory_id}")
                self._record_failure()
                return tool_error(self._format_error("Update failed", e))

        elif tool_name == "mem0_delete":
            memory_id = args.get("memory_id", "")
            if not memory_id:
                return tool_error("Missing required parameter: memory_id")
            try:
                result = self._backend.delete(memory_id)
                self._record_success()
                # Deleting a memory the user no longer wants surfaces useless
                # feedback for the evolve loop. Best-effort; never affects the result.
                self._report_feedback("useless", memory_id)
                return json.dumps(result)
            except Exception as e:
                if _is_client_error(e):
                    return tool_error(f"Memory not found: {memory_id}")
                self._record_failure()
                return tool_error(self._format_error("Delete failed", e))

        return tool_error(f"Unknown tool: {tool_name}")

    def _report_feedback(self, feedback_type: str, memory_id: str, note: str | None = None) -> None:
        """Best-effort report of explicit user feedback on a memory to the server's evolve loop.

        Never raises: a failed report must not break the tool call that just
        succeeded. The backend no-ops when it has no feedback endpoint
        (platform cloud / local OSS Memory).
        """
        if self._backend is None:
            return
        try:
            if note and len(note) > _FEEDBACK_NOTE_MAX_CHARS:
                note = note[:_FEEDBACK_NOTE_MAX_CHARS] + "…"
            self._backend.report_feedback(memory_id, feedback_type, source="auto", note=note)
        except Exception as e:
            logger.debug("Mem0 feedback report skipped (%s, memory=%s): %s", feedback_type, memory_id, e)

    def _shutdown_backend(self):
        # atexit 兜底：先冲刷合并缓冲再关闭后端，保证记忆不丢失
        self._flush_all_buckets()
        try:
            if self._backend:
                self._backend.close()
                self._backend = None
        except Exception:
            pass

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_consumer_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        # 兜底冲刷合并缓冲，避免关闭时丢失尚未写入的记忆
        self._flush_all_buckets()
        self._shutdown_backend()


def register(ctx) -> None:
    """Register Mem0 as a memory provider plugin."""
    ctx.register_memory_provider(Mem0MemoryProvider())
