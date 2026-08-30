"""FalkorDB graph memory implementation for Mem0."""

import json
import logging
import os
import time as _time
from collections import OrderedDict

from mem0.memory.utils import (
    format_entities,
    sanitize_label_for_cypher,
    sanitize_relationship_for_cypher,
)

try:
    from falkordb import FalkorDB
except ImportError:
    raise ImportError(
        "falkordb is not installed. Please install it using pip install falkordb"
    )

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    raise ImportError(
        "rank_bm25 is not installed. Please install it using pip install rank-bm25"
    )

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    _JIEBA_AVAILABLE = True
except ImportError:
    jieba = None
    _JIEBA_AVAILABLE = False

from mem0.graphs.tools import (
    DELETE_MEMORY_STRUCT_TOOL_GRAPH,
    DELETE_MEMORY_TOOL_GRAPH,
    EXTRACT_ENTITIES_STRUCT_TOOL,
    EXTRACT_ENTITIES_TOOL,
    RELATIONS_STRUCT_TOOL,
    RELATIONS_TOOL,
)
from mem0.graphs.utils import EXTRACT_RELATIONS_PROMPT, get_invalidate_messages
from mem0.llms.fallback import FallbackLLM, inherit_primary_config
from mem0.utils.factory import EmbedderFactory, LlmFactory

logger = logging.getLogger(__name__)


_MAX_GRAPH_CACHE = 256

# 图搜索 token 上限（环境变量 MEM0_GRAPH_SEARCH_TOKENS，不设置或设为 0 表示不限制）
# 之前设 25 是为控制 voyageai embed 输入规模；图数据用独立 bge-m3 后通常不需要此限制
_MAX_GRAPH_SEARCH_TOKENS = int(os.environ.get("MEM0_GRAPH_SEARCH_TOKENS", "0"))

_CHINESE_STOP_WORDS = frozenset({
    "的", "了", "是", "在", "和", "也", "都", "就", "有", "不",
    "与", "对", "从", "到", "而", "及", "以", "但", "或", "所",
    "被", "把", "给", "让", "向", "将", "着", "要", "会", "能",
    "可以", "什么", "怎么", "哪", "那", "这", "很", "吗", "呢",
    "啊", "吧", "哦", "嗯", "哈", "呀", "嘛", "么",
})

_RELATION_CN_SYNONYM_GROUPS = [
    frozenset({"部署", "安装", "上线", "发布", "挂在", "跑在"}),
    frozenset({"偏好", "喜欢", "喜爱", "钟爱"}),
    frozenset({"修复", "解决", "修补", "改正"}),
    frozenset({"使用", "采用", "运用", "利用"}),
    frozenset({"属于", "隶属", "归属"}),
    frozenset({"负责", "管理", "主管"}),
    frozenset({"创建", "建立", "新建", "搭建"}),
    frozenset({"配置", "设置", "设定"}),
    frozenset({"依赖", "依存"}),
    frozenset({"包含", "包括"}),
    frozenset({"支持", "兼容", "适配"}),
    frozenset({"监控", "监视", "巡检"}),
    frozenset({"连接", "接入", "对接"}),
    frozenset({"删除", "移除", "清除"}),
    frozenset({"测试", "验证", "校验"}),
]
_RELATION_CN_SYNONYM_LOOKUP = {}
for _g in _RELATION_CN_SYNONYM_GROUPS:
    for _w in _g:
        _RELATION_CN_SYNONYM_LOOKUP[_w] = _g

_threshold_cache = {"mtime": 0, "value": 0.7}

# 图关系失效时间字段：冲突消解时被替换的旧关系不再物理删除，
# 而是写入该字段标记失效（时间戳）；检索时默认排除已失效关系。
# 未设置该字段的存量关系视为有效（向后兼容）。
_RELATION_INVALIDATED_AT = "invalidated_at"


def _read_dynamic_threshold():
    env_val = os.environ.get("MEM0_GRAPH_THRESHOLD")
    if env_val is not None:
        return float(env_val)
    config_path = os.environ.get("MEM0_CONFIG_PATH")
    if config_path:
        try:
            cur_mtime = os.path.getmtime(config_path)
            if cur_mtime != _threshold_cache["mtime"]:
                with open(config_path) as f:
                    cfg = json.load(f)
                gs = cfg.get("graph_store", {})
                if isinstance(gs, dict):
                    t = gs.get("threshold")
                    if t is None and isinstance(gs.get("config"), dict):
                        t = gs["config"].get("threshold")
                    if t is not None:
                        _threshold_cache["value"] = float(t)
                _threshold_cache["mtime"] = cur_mtime
        except Exception:
            pass
    return _threshold_cache["value"]


def _tokenize_cjk(text):
    """Tokenize text for BM25 — uses jieba for CJK, whitespace-split otherwise."""
    if _JIEBA_AVAILABLE and text and any('\u4e00' <= c <= '\u9fff' for c in text):
        return list(jieba.cut(text))
    return text.split()


def _tokenize_query_for_search(query):
    """Tokenize query for graph search — jieba for CJK, filter noise tokens."""
    tokens = _tokenize_cjk(query)
    seen = set()
    result = []
    for t in tokens:
        t_stripped = t.strip()
        if not t_stripped or len(t_stripped) < 2 or t_stripped in _CHINESE_STOP_WORDS:
            continue
        if t_stripped not in seen:
            seen.add(t_stripped)
            result.append(t_stripped)
    return result


class _FalkorDBGraphWrapper:
    """Thin wrapper around the FalkorDB client to provide a .query() interface
    consistent with what the MemoryGraph methods expect (list-of-dict results).

    Each user_id gets a separate FalkorDB graph for natural data isolation.
    """

    def __init__(self, host, port, database, username=None, password=None):
        connect_kwargs = {"host": host, "port": port}
        if username and password:
            connect_kwargs["username"] = username
            connect_kwargs["password"] = password
        self._db = FalkorDB(**connect_kwargs)
        self._database = database
        self._graph_cache = OrderedDict()

    def _get_graph(self, user_id):
        """Get the FalkorDB graph object for the given user_id."""
        if user_id in self._graph_cache:
            self._graph_cache.move_to_end(user_id)
            return self._graph_cache[user_id]
        graph_name = f"{self._database}_{user_id}"
        graph = self._db.select_graph(graph_name)
        self._graph_cache[user_id] = graph
        if len(self._graph_cache) > _MAX_GRAPH_CACHE:
            self._graph_cache.popitem(last=False)
        return graph

    def query(self, cypher, params=None, user_id=None):
        """Execute a Cypher query and return results as a list of dicts."""
        graph = self._get_graph(user_id)
        result = graph.query(cypher, params=params)
        if not result.result_set:
            return []
        # FalkorDB headers are [column_type, column_name] pairs
        header = [h[1] if isinstance(h, (list, tuple)) else h for h in result.header]
        return [dict(zip(header, row)) for row in result.result_set]

    def delete_graph(self, user_id):
        """Delete an entire user graph."""
        graph_name = f"{self._database}_{user_id}"
        try:
            graph = self._db.select_graph(graph_name)
            graph.delete()
        except Exception:
            logger.debug("Graph %s not found or already deleted", graph_name)
        self._graph_cache.pop(user_id, None)

    def reset_all_graphs(self):
        """Delete all graphs matching the database prefix."""
        prefix = f"{self._database}_"
        try:
            all_graphs = self._db.list_graphs()
        except Exception:
            logger.warning("Failed to list graphs for reset")
            return
        for graph_name in all_graphs:
            if graph_name.startswith(prefix):
                try:
                    self._db.select_graph(graph_name).delete()
                except Exception:
                    logger.debug("Failed to delete graph %s during reset", graph_name)
        self._graph_cache.clear()


def _build_llm(provider, llm_config):
    """Build the graph entity-extraction LLM with optional fallback layers.

    语义与 mem0/memory/main.py._build_llm 保持一致（图链路无法直接 import main，避免循环依赖）：
    fallbacks 数量自适应——N 个 fallback 即为 N 层兜底，0 个时仅返回主层 LLM。
    """
    primary = LlmFactory.create(provider, llm_config.config)
    if not llm_config.fallbacks:
        return primary
    # 同 memory/main.py.__build_llm：兜底层跟随主层(L0)采样/输出参数（含 reasoning_effort）
    fallbacks = [
        LlmFactory.create(fb.provider, inherit_primary_config(llm_config.config, fb.config))
        for fb in llm_config.fallbacks
    ]
    return FallbackLLM(primary, fallbacks, layer_timeout=llm_config.layer_timeout)


class MemoryGraph:
    def __init__(self, config):
        self.config = config
        self.graph = _FalkorDBGraphWrapper(
            host=self.config.graph_store.config.host,
            port=self.config.graph_store.config.port,
            database=self.config.graph_store.config.database,
            username=self.config.graph_store.config.username,
            password=self.config.graph_store.config.password,
        )
        # 图数据优先用自己的 embedder，未配置时 fallback 到全局 embedder
        if (
            self.config.graph_store
            and self.config.graph_store.embedder
            and self.config.graph_store.embedder.provider
        ):
            _embedder_provider = self.config.graph_store.embedder.provider
            _embedder_config = self.config.graph_store.embedder.config
        else:
            _embedder_provider = self.config.embedder.provider
            _embedder_config = self.config.embedder.config
        self.embedding_model = EmbedderFactory.create(
            _embedder_provider, _embedder_config, self.config.vector_store.config,
        )

        self.use_base_label = getattr(
            self.config.graph_store.config, "base_label", True
        )
        self.node_label = ":`__Entity__`" if self.use_base_label else ""

        self._indexed_user_graphs = set()

        # Default to openai if no specific provider is configured
        self.llm_provider = "openai"
        if self.config.llm and self.config.llm.provider:
            self.llm_provider = self.config.llm.provider
        if (
            self.config.graph_store
            and self.config.graph_store.llm
            and self.config.graph_store.llm.provider
        ):
            self.llm_provider = self.config.graph_store.llm.provider

        # Get LLM config with proper null checks; use the full LlmConfig object
        # (not the inner .config dict) so fallbacks/layer_timeout survive.
        llm_config = None
        if self.config.graph_store and self.config.graph_store.llm:
            llm_config = self.config.graph_store.llm
        elif self.config.llm:
            llm_config = self.config.llm
        self.llm = _build_llm(self.llm_provider, llm_config)

    @property
    def threshold(self):
        return _read_dynamic_threshold()

    # ------------------------------------------------------------------
    # Index management
    # ------------------------------------------------------------------

    def _ensure_indexes(self, user_id):
        """Create property indexes in FalkorDB. Silently ignores if they already exist."""
        label = "__Entity__"
        try:
            self.graph.query(
                f"CREATE INDEX FOR (n:{label}) ON (n.name)",
                user_id=user_id,
            )
        except Exception:
            logger.debug(
                "Index on %s.name may already exist for user %s", label, user_id
            )

    def _ensure_vector_index(self, dim, user_id):
        """Create vector index if not already created."""
        label = "__Entity__" if self.use_base_label else "Node"
        try:
            self.graph.query(
                f"CREATE VECTOR INDEX FOR (n:{label}) ON (n.embedding) "
                f"OPTIONS {{dimension: {dim}, similarityFunction: 'cosine'}}",
                user_id=user_id,
            )
        except Exception:
            logger.debug("Vector index may already exist for user %s", user_id)

    def _ensure_user_graph_indexes(self, user_id):
        """Ensure indexes exist for a user's graph (skips if already done)."""
        if user_id in self._indexed_user_graphs:
            return
        if self.use_base_label:
            self._ensure_indexes(user_id=user_id)
        self._indexed_user_graphs.add(user_id)

    def _build_node_props(self, filters, include_name=False, name_param="name"):
        """Build node property filter string and params dict.

        user_id is implicit (separate graph per user), so it's excluded
        from property filters. Only agent_id/run_id are included when present.
        """
        props = []
        params = {}

        if include_name:
            props.append(f"name: ${name_param}")

        if filters.get("agent_id"):
            props.append("agent_id: $agent_id")
            params["agent_id"] = filters["agent_id"]
        if filters.get("run_id"):
            props.append("run_id: $run_id")
            params["run_id"] = filters["run_id"]

        return ", ".join(props), params

    @staticmethod
    def _user_id(filters):
        """Return user_id for graph selection."""
        return filters["user_id"]

    # ------------------------------------------------------------------
    # Public API (matches Mem0 graph store interface)
    # ------------------------------------------------------------------

    def add(self, data, filters):
        """Add data to the graph."""
        _t0 = _time.perf_counter()
        _data_len = len(data) if isinstance(data, str) else 0
        logger.info(
            "graph add start: data_len=%d, user_id=%s",
            _data_len, filters.get("user_id", "?"),
        )

        self._ensure_user_graph_indexes(filters["user_id"])
        entity_type_map = self._retrieve_nodes_from_data(data, filters)
        logger.info("graph add: extracted %d entity types", len(entity_type_map))

        to_be_added = self._establish_nodes_relations_from_data(
            data, filters, entity_type_map
        )
        logger.info("graph add: extracted %d relations (pre-filter)", len(to_be_added))

        # Filter low-quality relations before writing (mirrors search-path quality gates)
        _before = len(to_be_added)
        to_be_added = [
            item for item in to_be_added
            if item["source"] != item["destination"]
            and item["relationship"] != "related_to"
            and len(item["source"]) >= 2
            and len(item["destination"]) >= 2
        ]
        _filtered = _before - len(to_be_added)
        if _filtered:
            logger.info("graph add: filtered %d low-quality relations (self-ref/related_to/fragment)", _filtered)

        search_output = self._search_graph_db(
            node_list=list(entity_type_map.keys()), filters=filters
        )
        to_be_invalidated = self._get_entities_to_invalidate(
            search_output, data, filters
        )

        invalidated_entities = self._invalidate_entities(to_be_invalidated, filters)
        added_entities = self._add_entities(to_be_added, filters, entity_type_map)

        _elapsed = _time.perf_counter() - _t0
        logger.info(
            "graph add done: added=%d, invalidated=%d, elapsed=%.2fs",
            len(added_entities), len(invalidated_entities), _elapsed,
        )
        # 兼容对外返回结构：冲突消解不再物理删除，仅标记失效（旧关系保留在图中）
        return {
            "deleted_entities": invalidated_entities,
            "added_entities": added_entities,
        }

    def search(self, query, filters, limit=100):
        """Search for memories and related graph data."""
        _t0 = _time.perf_counter()
        logger.info("graph search start: query=%.80s, user_id=%s", query, filters.get("user_id", "?"))

        node_list = _tokenize_query_for_search(query)
        # A: 限制参与图搜索的 token 数量（0 表示不限制，图数据用独立 embedder 后通常不需要此限制）
        if _MAX_GRAPH_SEARCH_TOKENS > 0 and len(node_list) > _MAX_GRAPH_SEARCH_TOKENS:
            logger.info(
                "graph search tokens capped: %d -> %d",
                len(node_list), _MAX_GRAPH_SEARCH_TOKENS,
            )
            node_list = node_list[:_MAX_GRAPH_SEARCH_TOKENS]
        if not node_list:
            logger.info("graph search done: 0 results (no tokens), elapsed=%.2fs", _time.perf_counter() - _t0)
            return []
        search_output = self._search_graph_db(
            node_list=node_list, filters=filters
        )
        # Mark vector-channel hits; supplemental STARTS WITH hits get "contains".
        for _item in search_output:
            _item.setdefault("recall_channel", "vector")
        _vector_hits = len(search_output)

        # Supplemental recall: match query tokens against relationship type
        # (pure-Chinese types stored via backtick escaping; STARTS WITH so that
        # verb stems like "部署" match "部署于" while negative forms like
        # "不喜好" (prefix 不/未/没) stay excluded). Legacy relation_cn
        # property channel removed — new deployment does not need backward compat.
        _seen_relation_ids = set(r.get("relation_id") for r in search_output)
        _cn_label = "__Entity__" if self.use_base_label else "Node"
        _cn_uid = self._user_id(filters)

        _expanded_tokens = set()
        for token in node_list[:5]:
            _group = _RELATION_CN_SYNONYM_LOOKUP.get(token)
            if _group is not None:
                _expanded_tokens.update(_group)
            else:
                _expanded_tokens.add(token)

        if _expanded_tokens:
            _type_clauses = []
            _or_params = {}
            for _i, _token in enumerate(_expanded_tokens):
                _tpname = f"_ct{_i}"
                _type_clauses.append(f"type(r) STARTS WITH ${_tpname}")
                _or_params[_tpname] = _token

            _where_clause = (
                f"WHERE ({' OR '.join(_type_clauses)}) "
                f"AND r.{_RELATION_INVALIDATED_AT} IS NULL"
            )

            try:
                _cn_results = self.graph.query(
                    f"""
                    MATCH (a:`{_cn_label}`)-[r]->(b:`{_cn_label}`)
                    {_where_clause}
                    RETURN a.name AS source, id(a) AS source_id, type(r) AS relationship,
                           id(r) AS relation_id, b.name AS destination, id(b) AS destination_id,
                           r.relation_cn AS relation_cn
                    LIMIT {int(limit * 3)}
                    """,
                    params=_or_params,
                    user_id=_cn_uid,
                )
                for item in _cn_results:
                    rid = item.get("relation_id")
                    if rid not in _seen_relation_ids:
                        _seen_relation_ids.add(rid)
                        item["recall_channel"] = "contains"
                        search_output.append(item)
            except Exception:
                logger.debug(
                    "relation type STARTS WITH query failed for tokens %s", _expanded_tokens, exc_info=True,
                )

        _cn_hits = len(search_output) - _vector_hits

        if not search_output:
            logger.info("graph search done: 0 results (no vector hits), elapsed=%.2fs", _time.perf_counter() - _t0)
            return []

        search_outputs_sequence = [
            _tokenize_cjk(item["source"]) + _tokenize_cjk(item["relationship"]) + _tokenize_cjk(item["destination"])
            for item in search_output
        ]
        bm25 = BM25Okapi(search_outputs_sequence)

        tokenized_query = _tokenize_cjk(query)
        scores = bm25.get_scores(tokenized_query)
        top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:5]

        # CONTAINS channel hits (type-name exact/substring match) are semantically
        # stronger than vector-only hits. BM25 on tiny corpora yields flat/zero
        # scores (single-char tokenization vs word-level query), so we order
        # CONTAINS hits first, then vector hits by BM25 score.
        _cn_index_set = set()
        if _cn_hits > 0:
            # search_output layout: [vector hits..., cn hits...]
            _cn_start = _vector_hits
            for _i in range(_cn_start, len(search_output)):
                _cn_index_set.add(_i)
        top_indices = sorted(top_indices, key=lambda i: (i not in _cn_index_set, -scores[i]))

        search_results = []
        for idx in top_indices:
            # NOTE: rank_bm25 yields NEGATIVE scores on tiny corpora (df ≈ N → idf < 0),
            # so `score <= 0` must NOT be used as a hard filter — vector search already
            # confirmed relevance; BM25 only orders the candidates. Top-5 cap keeps it bounded.
            item = search_output[idx]
            src = item["source"]
            rel = item["relationship"]
            dst = item["destination"]
            if src == dst:
                continue
            if rel == "related_to":
                continue
            if len(src) < 2 or len(dst) < 2:
                continue
            search_results.append(
                {
                    "source": src,
                    "relationship": rel,
                    "destination": dst,
                    "relation_cn": item.get("relation_cn", ""),
                    "recall_channel": item.get("recall_channel", "vector"),
                }
            )

        logger.info(
            "graph search done: vector_hits=%d, cn_hits=%d, total=%d, elapsed=%.2fs",
            _vector_hits, _cn_hits, len(search_results), _time.perf_counter() - _t0,
        )
        return search_results

    def delete_all(self, filters):
        """Delete all entities and relationships for the given filters."""
        uid = self._user_id(filters)

        if not filters.get("agent_id") and not filters.get("run_id"):
            # Drop the entire user graph for clean isolation
            self.graph.delete_graph(uid)
            self._indexed_user_graphs.discard(uid)
            return

        # Partial delete within a user's graph (agent/run scoped)
        node_props_str, params = self._build_node_props(filters)
        if node_props_str:
            cypher = f"MATCH (n {self.node_label} {{{node_props_str}}}) DETACH DELETE n"
        else:
            cypher = f"MATCH (n {self.node_label}) DETACH DELETE n"
        self.graph.query(cypher, params=params, user_id=uid)

    def get_all(self, filters, limit=100):
        """Retrieve all nodes and relationships from the graph."""
        uid = self._user_id(filters)
        node_props_str, params = self._build_node_props(filters)
        if node_props_str:
            query = f"""
            MATCH (n {self.node_label} {{{node_props_str}}})-[r]->(m {self.node_label})
            WHERE r.{_RELATION_INVALIDATED_AT} IS NULL
            RETURN n.name AS source, type(r) AS relationship, m.name AS target
            LIMIT {int(limit)}
            """
        else:
            query = f"""
            MATCH (n {self.node_label})-[r]->(m {self.node_label})
            WHERE r.{_RELATION_INVALIDATED_AT} IS NULL
            RETURN n.name AS source, type(r) AS relationship, m.name AS target
            LIMIT {int(limit)}
            """
        results = self.graph.query(query, params=params, user_id=uid)

        final_results = []
        for result in results:
            final_results.append(
                {
                    "source": result["source"],
                    "relationship": result["relationship"],
                    "target": result["target"],
                }
            )

        logger.info(f"Retrieved {len(final_results)} relationships")
        return final_results

    def reset(self):
        """Reset all user graphs under this database prefix."""
        logger.warning(
            "Resetting all graphs with prefix '%s_'...", self.graph._database
        )
        self.graph.reset_all_graphs()
        self._indexed_user_graphs.clear()

    # ------------------------------------------------------------------
    # LLM-based entity extraction (reuses Mem0's tools)
    # ------------------------------------------------------------------

    def _retrieve_nodes_from_data(self, data, filters):
        """Extract all entities mentioned in the query."""
        _tools = [EXTRACT_ENTITIES_TOOL]
        if self.llm_provider in ["azure_openai_structured", "openai_structured"]:
            _tools = [EXTRACT_ENTITIES_STRUCT_TOOL]
        search_results = self.llm.generate_response(
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"你是一个理解文本中实体及其类型的智能助手。如果用户消息中包含自指代"
                        f"（如'我'、'我的'等），请使用 {filters['user_id']} 作为源实体。"
                        f"请调用 extract_entities 工具从文本中提取所有实体。"
                        f"必须用与输入相同的语言输出实体名称。"
                        f"***不要***回答文本中的问题，只提取实体。\n\n"
                        f"实体类型必须从以下白名单中选择，不要自行发明类型：\n"
                        f"- person：人物（如用户名、人名）\n"
                        f"- organization：组织、公司、团队\n"
                        f"- location：地理位置、城市、国家\n"
                        f"- tool：软件工具、命令、库、框架（如 docker、python、httpx）\n"
                        f"- concept：技术概念、方法论、抽象术语（如记忆系统、微服务）\n"
                        f"- event：事件、 incident、里程碑\n"
                        f"- metric：指标、度量值、性能数据（如 CPU 使用率、QPS）\n"
                        f"- product：产品、服务、项目名\n"
                        f"- user：仅当实体确实是用户/人且无法确定具体身份时使用\n"
                        f"- other：不属于以上任何类别的实体\n\n"
                        f"注意：关系类型名（如 related_to）、指标变量名（如 wrqm/s、r/s、us）"
                        f"不应作为实体提取。Python 模块名（如 httpx.client）应标记为 tool 而非 user。\n\n"
                        f"以下技术标识符***禁止***作为实体提取：\n"
                        f"- 代码符号：变量名、函数名、类名（如 original_memories、get_user、HttpClient）\n"
                        f"- 文件路径/行号：代码位置、文件名、地址引用（如 main.py、config.yaml、main.py:1816）\n"
                        f"- 内部表名/集合名（如 mem0_memories表、users_collection）\n\n"
                        f"区分规则：完整的技术产品/开源项目/行业术语（如 mem0、PostgreSQL、pgvector、rerank、Docker、PyTorch）"
                        f"应正常提取为 tool/concept/product；"
                        f"代码片段、内部变量、私有路径、表引用等实现细节则禁止提取。"
                    ),
                },
                {"role": "user", "content": data},
            ],
            tools=_tools,
        )

        entity_type_map = {}
        try:
            for tool_call in search_results["tool_calls"]:
                if tool_call["name"] != "extract_entities":
                    continue
                for item in tool_call["arguments"]["entities"]:
                    entity_type_map[item["entity"]] = item["entity_type"]
        except Exception as e:
            logger.exception(
                f"Error in search tool: {e}, llm_provider={self.llm_provider}, search_results={search_results}"
            )

        entity_type_map = {
            k.lower().replace(" ", "_"): v.lower().replace(" ", "_")
            for k, v in entity_type_map.items()
        }
        logger.debug(f"Entity type map: {entity_type_map}")
        return entity_type_map

    def _establish_nodes_relations_from_data(self, data, filters, entity_type_map):
        """Establish relations among the extracted nodes."""
        user_identity = f"user_id: {filters['user_id']}"
        if filters.get("agent_id"):
            user_identity += f", agent_id: {filters['agent_id']}"
        if filters.get("run_id"):
            user_identity += f", run_id: {filters['run_id']}"

        system_content = EXTRACT_RELATIONS_PROMPT.replace("USER_ID", user_identity)
        if self.config.graph_store.custom_prompt:
            system_content = system_content.replace(
                "CUSTOM_PROMPT", f"4. {self.config.graph_store.custom_prompt}"
            )
            messages = [
                {"role": "system", "content": system_content},
                {"role": "user", "content": data},
            ]
        else:
            messages = [
                {"role": "system", "content": system_content},
                {
                    "role": "user",
                    "content": f"List of entities: {list(entity_type_map.keys())}. \n\nText: {data}",
                },
            ]

        _tools = [RELATIONS_TOOL]
        if self.llm_provider in ["azure_openai_structured", "openai_structured"]:
            _tools = [RELATIONS_STRUCT_TOOL]

        extracted_entities = self.llm.generate_response(
            messages=messages,
            tools=_tools,
        )

        entities = []
        if extracted_entities.get("tool_calls"):
            entities = (
                extracted_entities["tool_calls"][0]
                .get("arguments", {})
                .get("entities", [])
            )

        entities = self._remove_spaces_from_entities(entities)
        logger.debug(f"Extracted entities: {entities}")
        return entities

    def _get_entities_to_invalidate(self, search_output, data, filters):
        """Get the entities to be invalidated from the search output.

        冲突消解：LLM 判定与新信息矛盾/过时的旧关系，返回待失效列表。
        LLM 输出工具仍名为 delete_graph_memory（工具定义在 tools.py，保持不动），
        语义上判定的是「应被失效」的关系；代码层不再物理删除，只标记失效。
        """
        search_output_string = format_entities(search_output)

        user_identity = f"user_id: {filters['user_id']}"
        if filters.get("agent_id"):
            user_identity += f", agent_id: {filters['agent_id']}"
        if filters.get("run_id"):
            user_identity += f", run_id: {filters['run_id']}"

        system_prompt, user_prompt = get_invalidate_messages(
            search_output_string, data, user_identity
        )

        _tools = [DELETE_MEMORY_TOOL_GRAPH]
        if self.llm_provider in ["azure_openai_structured", "openai_structured"]:
            _tools = [DELETE_MEMORY_STRUCT_TOOL_GRAPH]

        memory_updates = self.llm.generate_response(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            tools=_tools,
        )

        to_be_invalidated = []
        for item in memory_updates.get("tool_calls", []):
            if item.get("name") == "delete_graph_memory":
                to_be_invalidated.append(item.get("arguments"))
        to_be_invalidated = self._remove_spaces_from_entities(to_be_invalidated)
        logger.debug(f"Relationships to invalidate: {to_be_invalidated}")
        return to_be_invalidated

    # ------------------------------------------------------------------
    # FalkorDB-specific Cypher: graph search with vector similarity
    # ------------------------------------------------------------------

    def _search_graph_db(self, node_list, filters, limit=100):
        """Search similar nodes and their incoming/outgoing relations using FalkorDB vector search."""
        result_relations = []
        uid = self._user_id(filters)
        node_props_str, base_params = self._build_node_props(filters)

        _unique_nodes = list(dict.fromkeys(node_list))
        _embedding_cache = {}
        if _unique_nodes:
            _t_batch = _time.perf_counter()
            try:
                _embeddings = self.embedding_model.embed_batch(_unique_nodes, "search")
                _embedding_cache = dict(zip(_unique_nodes, _embeddings))
            except Exception as e:
                logger.warning(
                    "graph embed_batch failed (%d texts, e.g. '%s'), elapsed=%.2fs, falling back to individual embed: %s",
                    len(_unique_nodes), _unique_nodes[0][:60], _time.perf_counter() - _t_batch, e,
                )
                _n_ok = 0
                for text in _unique_nodes:
                    _t_embed = _time.perf_counter()
                    try:
                        _embedding_cache[text] = self.embedding_model.embed(text, "search")
                        _n_ok += 1
                        logger.info(
                            "graph fallback embed ok for '%s', elapsed=%.2fs",
                            text, _time.perf_counter() - _t_embed,
                        )
                    except Exception as embed_err:
                        logger.warning(
                            "graph fallback embed failed for '%s', elapsed=%.2fs: %s",
                            text, _time.perf_counter() - _t_embed, embed_err,
                        )
                logger.warning(
                    "graph fallback embed done: %d/%d texts embedded, total elapsed=%.2fs",
                    _n_ok, len(_unique_nodes), _time.perf_counter() - _t_batch,
                )

        if _embedding_cache:
            _first_embedding = next(iter(_embedding_cache.values()))
            self._ensure_vector_index(len(_first_embedding), user_id=uid)

        for node in node_list:
            n_embedding = _embedding_cache.get(node)
            if not n_embedding:
                _t_miss = _time.perf_counter()
                try:
                    n_embedding = self.embedding_model.embed(node, "search")
                except Exception as embed_err:
                    logger.warning(
                        "graph search cache-miss embed failed for '%s', elapsed=%.2fs: %s",
                        node, _time.perf_counter() - _t_miss, embed_err,
                    )
                    raise
                logger.warning(
                    "graph search cache-miss re-embed for '%s', elapsed=%.2fs",
                    node, _time.perf_counter() - _t_miss,
                )

            label = "__Entity__" if self.use_base_label else "Node"

            # Build WHERE clauses for vector search filtering
            # NOTE: FalkorDB queryNodes returns COSINE DISTANCE (0=identical, larger=less similar),
            # so threshold (similarity semantics, e.g. 0.7) must be inverted: keep distance <= 1-threshold
            where_clauses = ["score <= $max_distance"]
            if filters.get("agent_id"):
                where_clauses.append("node.agent_id = $agent_id")
            if filters.get("run_id"):
                where_clauses.append("node.run_id = $run_id")
            where_str = " AND ".join(where_clauses)

            vector_query = f"""
            CALL db.idx.vector.queryNodes('{label}', 'embedding', {int(limit)}, vecf32($n_embedding))
            YIELD node, score
            WITH node, score
            WHERE {where_str}
            RETURN id(node) AS node_id, node.name AS node_name, score
            LIMIT {int(limit)}
            """

            params = {
                "n_embedding": n_embedding,
                "max_distance": max(0.0, 1.0 - self.threshold),
                **base_params,
            }

            similar_nodes = self.graph.query(vector_query, params=params, user_id=uid)

            # For each similar node, fetch outgoing and incoming relationships
            for sn in similar_nodes:
                node_id = sn["node_id"]
                rel_params = {"node_id": node_id, **base_params}

                match_props = f" {{{node_props_str}}}" if node_props_str else ""
                out_query = f"""
                MATCH (n {self.node_label})-[r]->(m {self.node_label}{match_props})
                WHERE id(n) = $node_id AND r.{_RELATION_INVALIDATED_AT} IS NULL
                RETURN n.name AS source, id(n) AS source_id, type(r) AS relationship,
                       id(r) AS relation_id, m.name AS destination, id(m) AS destination_id,
                       r.relation_cn AS relation_cn
                """
                in_query = f"""
                MATCH (n {self.node_label})<-[r]-(m {self.node_label}{match_props})
                WHERE id(n) = $node_id AND r.{_RELATION_INVALIDATED_AT} IS NULL
                RETURN m.name AS source, id(m) AS source_id, type(r) AS relationship,
                       id(r) AS relation_id, n.name AS destination, id(n) AS destination_id,
                       r.relation_cn AS relation_cn
                """

                out_results = self.graph.query(
                    out_query, params=rel_params, user_id=uid
                )
                in_results = self.graph.query(in_query, params=rel_params, user_id=uid)

                result_relations.extend(out_results)
                result_relations.extend(in_results)

        # Deduplicate by relation_id
        seen = set()
        unique_results = []
        for r in result_relations:
            rid = r.get("relation_id")
            if rid not in seen:
                seen.add(rid)
                unique_results.append(r)

        return unique_results

    # ------------------------------------------------------------------
    # FalkorDB-specific Cypher: entity deletion
    # ------------------------------------------------------------------

    def _invalidate_entities(self, to_be_invalidated, filters):
        """Mark entities (relationships) as invalidated instead of physically deleting.

        冲突消解改为「失效保留」：给旧关系写入失效时间戳，关系本身保留在图中，
        检索路径通过 `invalidated_at IS NULL` 默认排除已失效关系。
        """
        uid = self._user_id(filters)
        results = []

        for item in to_be_invalidated:
            source = item["source"]
            destination = item["destination"]
            relationship = item["relationship"]
            _safe_relationship = relationship  # already sanitized by _remove_spaces_from_entities

            source_props_str, params = self._build_node_props(
                filters, include_name=True, name_param="source_name"
            )
            dest_props_str, _ = self._build_node_props(
                filters, include_name=True, name_param="dest_name"
            )
            params["source_name"] = source
            params["dest_name"] = destination

            cypher = f"""
            MATCH (n {self.node_label} {{{source_props_str}}})
            -[r:{_safe_relationship}]->
            (m {self.node_label} {{{dest_props_str}}})
            SET r.{_RELATION_INVALIDATED_AT} = timestamp()
            RETURN
                n.name AS source,
                m.name AS target,
                type(r) AS relationship
            """
            result = self.graph.query(cypher, params=params, user_id=uid)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # FalkorDB-specific Cypher: entity addition with vector embeddings
    # ------------------------------------------------------------------

    def _add_entities(self, to_be_added, filters, entity_type_map):
        """Add new entities to the graph. Merge nodes if they already exist."""
        uid = self._user_id(filters)
        results = []

        # 批量预计算所有 entity 的 embedding，避免循环内逐条调用 API
        _all_entity_texts = set()
        for item in to_be_added:
            _all_entity_texts.add(item["source"])
            _all_entity_texts.add(item["destination"])
        _entity_text_list = list(_all_entity_texts)
        _embedding_cache = {}
        if _entity_text_list:
            try:
                _embeddings = self.embedding_model.embed_batch(_entity_text_list, "add")
                _embedding_cache = dict(zip(_entity_text_list, _embeddings))
            except Exception as e:
                logger.warning(
                    "graph embed_batch failed (%d texts), falling back to individual embed: %s",
                    len(_entity_text_list), e,
                )
                for text in _entity_text_list:
                    try:
                        _embedding_cache[text] = self.embedding_model.embed(text, "add")
                    except Exception as embed_err:
                        logger.warning("graph entity embed failed for '%s': %s", text, embed_err)

        for item in to_be_added:
            source = item["source"]
            destination = item["destination"]
            relationship = item["relationship"]
            _safe_relationship = relationship  # already sanitized by _remove_spaces_from_entities

            source_type = entity_type_map.get(source, "__User__")
            # Node labels come from the extract_entities whitelist (ASCII);
            # sanitize_label keeps underscores intact (__User__) and
            # backtick-quotes only non-ASCII (defensive).
            _safe_source_type = sanitize_label_for_cypher(source_type)
            source_label = self.node_label if self.node_label else f":`{_safe_source_type}`"
            source_extra_set = f", source:`{_safe_source_type}`" if self.node_label else ""
            destination_type = entity_type_map.get(destination, "__User__")
            _safe_dest_type = sanitize_label_for_cypher(destination_type)
            destination_label = (
                self.node_label if self.node_label else f":`{_safe_dest_type}`"
            )
            destination_extra_set = (
                f", destination:`{_safe_dest_type}`" if self.node_label else ""
            )

            source_embedding = _embedding_cache.get(source) or self.embedding_model.embed(source)
            dest_embedding = _embedding_cache.get(destination) or self.embedding_model.embed(destination)
            self._ensure_vector_index(len(source_embedding), user_id=uid)

            source_node = self._search_node_by_embedding(source_embedding, filters)
            dest_node = self._search_node_by_embedding(dest_embedding, filters)

            if source_node is not None and dest_node is not None and source_node == dest_node:
                logger.debug(
                    "Skipping self-referencing relation: %s -[%s]-> %s (both resolve to node %s)",
                    source, relationship, destination, source_node,
                )
                continue

            if not dest_node and source_node:
                dest_merge_str, params = self._build_node_props(
                    filters, include_name=True, name_param="destination_name"
                )
                params["source_id"] = source_node
                params["destination_name"] = destination
                params["destination_embedding"] = dest_embedding
                params["relation_cn"] = item.get("relation_cn", "")

                cypher = f"""
                MATCH (source)
                WHERE id(source) = $source_id
                SET source.mentions = coalesce(source.mentions, 0) + 1
                WITH source
                MERGE (destination {destination_label} {{{dest_merge_str}}})
                ON CREATE SET
                    destination.created = timestamp(),
                    destination.mentions = 1,
                    destination.embedding = vecf32($destination_embedding)
                    {destination_extra_set}
                ON MATCH SET
                    destination.mentions = coalesce(destination.mentions, 0) + 1,
                    destination.embedding = vecf32($destination_embedding)
                WITH source, destination
                MERGE (source)-[r:{_safe_relationship}]->(destination)
                ON CREATE SET
                    r.created = timestamp(),
                    r.mentions = 1,
                    r.relation_cn = $relation_cn
                ON MATCH SET
                    r.mentions = coalesce(r.mentions, 0) + 1,
                    r.relation_cn = $relation_cn,
                    r.{_RELATION_INVALIDATED_AT} = null
                RETURN source.name AS source, type(r) AS relationship, destination.name AS target
                """

            elif dest_node and not source_node:
                src_merge_str, params = self._build_node_props(
                    filters, include_name=True, name_param="source_name"
                )
                params["destination_id"] = dest_node
                params["source_name"] = source
                params["source_embedding"] = source_embedding
                params["relation_cn"] = item.get("relation_cn", "")

                cypher = f"""
                MATCH (destination)
                WHERE id(destination) = $destination_id
                SET destination.mentions = coalesce(destination.mentions, 0) + 1
                WITH destination
                MERGE (source {source_label} {{{src_merge_str}}})
                ON CREATE SET
                    source.created = timestamp(),
                    source.mentions = 1,
                    source.embedding = vecf32($source_embedding)
                    {source_extra_set}
                ON MATCH SET
                    source.mentions = coalesce(source.mentions, 0) + 1,
                    source.embedding = vecf32($source_embedding)
                WITH source, destination
                MERGE (source)-[r:{_safe_relationship}]->(destination)
                ON CREATE SET
                    r.created = timestamp(),
                    r.mentions = 1,
                    r.relation_cn = $relation_cn
                ON MATCH SET
                    r.mentions = coalesce(r.mentions, 0) + 1,
                    r.relation_cn = $relation_cn,
                    r.{_RELATION_INVALIDATED_AT} = null
                RETURN source.name AS source, type(r) AS relationship, destination.name AS target
                """

            elif source_node and dest_node:
                _, params = self._build_node_props(filters)
                params["source_id"] = source_node
                params["destination_id"] = dest_node
                params["relation_cn"] = item.get("relation_cn", "")

                cypher = f"""
                MATCH (source)
                WHERE id(source) = $source_id
                SET source.mentions = coalesce(source.mentions, 0) + 1
                WITH source
                MATCH (destination)
                WHERE id(destination) = $destination_id
                SET destination.mentions = coalesce(destination.mentions, 0) + 1
                MERGE (source)-[r:{_safe_relationship}]->(destination)
                ON CREATE SET
                    r.created_at = timestamp(),
                    r.updated_at = timestamp(),
                    r.mentions = 1,
                    r.relation_cn = $relation_cn
                ON MATCH SET
                    r.mentions = coalesce(r.mentions, 0) + 1,
                    r.updated_at = timestamp(),
                    r.relation_cn = $relation_cn,
                    r.{_RELATION_INVALIDATED_AT} = null
                RETURN source.name AS source, type(r) AS relationship, destination.name AS target
                """

            else:
                # Neither node exists - create both
                source_props_str, params = self._build_node_props(
                    filters, include_name=True, name_param="source_name"
                )
                dest_props_str, _ = self._build_node_props(
                    filters, include_name=True, name_param="dest_name"
                )
                params["source_name"] = source
                params["dest_name"] = destination
                params["source_embedding"] = source_embedding
                params["dest_embedding"] = dest_embedding
                params["relation_cn"] = item.get("relation_cn", "")

                cypher = f"""
                MERGE (source {source_label} {{{source_props_str}}})
                ON CREATE SET source.created = timestamp(),
                            source.mentions = 1,
                            source.embedding = vecf32($source_embedding)
                            {source_extra_set}
                ON MATCH SET source.mentions = coalesce(source.mentions, 0) + 1,
                            source.embedding = vecf32($source_embedding)
                WITH source
                MERGE (destination {destination_label} {{{dest_props_str}}})
                ON CREATE SET destination.created = timestamp(),
                            destination.mentions = 1,
                            destination.embedding = vecf32($dest_embedding)
                            {destination_extra_set}
                ON MATCH SET destination.mentions = coalesce(destination.mentions, 0) + 1,
                            destination.embedding = vecf32($dest_embedding)
                WITH source, destination
                MERGE (source)-[rel:{_safe_relationship}]->(destination)
                ON CREATE SET rel.created = timestamp(), rel.mentions = 1, rel.relation_cn = $relation_cn
                ON MATCH SET rel.mentions = coalesce(rel.mentions, 0) + 1, rel.relation_cn = $relation_cn, rel.{_RELATION_INVALIDATED_AT} = null
                RETURN source.name AS source, type(rel) AS relationship, destination.name AS target
                """

            result = self.graph.query(cypher, params=params, user_id=uid)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # FalkorDB-specific Cypher: node search by embedding similarity
    # ------------------------------------------------------------------

    def _search_node_by_embedding(self, embedding, filters):
        """Search for a node by embedding similarity.

        Returns the node id (integer) if found, or None.
        Uses FalkorDB's db.idx.vector.queryNodes procedure.
        """
        uid = self._user_id(filters)
        label = "__Entity__" if self.use_base_label else "Node"

        # NOTE: FalkorDB queryNodes returns COSINE DISTANCE (0=identical, larger=less similar),
        # so threshold (similarity semantics, e.g. 0.7) must be inverted: keep distance <= 1-threshold
        where_clauses = ["score <= $max_distance"]
        if filters.get("agent_id"):
            where_clauses.append("node.agent_id = $agent_id")
        if filters.get("run_id"):
            where_clauses.append("node.run_id = $run_id")
        where_str = " AND ".join(where_clauses)

        cypher = f"""
        CALL db.idx.vector.queryNodes('{label}', 'embedding', 10, vecf32($embedding))
        YIELD node, score
        WITH node, score
        WHERE {where_str}
        RETURN id(node) AS node_id
        LIMIT 1
        """

        params = {
            "embedding": embedding,
            "max_distance": max(0.0, 1.0 - self.threshold),
        }
        if filters.get("agent_id"):
            params["agent_id"] = filters["agent_id"]
        if filters.get("run_id"):
            params["run_id"] = filters["run_id"]

        result = self.graph.query(cypher, params=params, user_id=uid)
        if result:
            return result[0]["node_id"]
        return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _remove_spaces_from_entities(self, entity_list):
        valid = []
        for item in entity_list:
            if not isinstance(item, dict):
                continue
            if "destination" not in item and "target" in item:
                item["destination"] = item.pop("target")
            if "source" not in item or "relationship" not in item or "destination" not in item:
                continue
            item["source"] = item["source"].lower().replace(" ", "_")
            item["relation_cn"] = item["relationship"]
            item["relationship"] = sanitize_relationship_for_cypher(
                item["relationship"].lower().replace(" ", "_")
            )
            item["destination"] = item["destination"].lower().replace(" ", "_")
            valid.append(item)
        return valid
