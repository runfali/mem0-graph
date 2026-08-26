import json
import logging
import os
import re
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional
from zoneinfo import ZoneInfo
from urllib.parse import parse_qsl, urlencode, urlsplit

from pydantic import BaseModel

# Try to import psycopg (psycopg3) first, then fall back to psycopg2
try:
    from psycopg import sql
    from psycopg.types.json import Json
    from psycopg_pool import ConnectionPool
    PSYCOPG_VERSION = 3
    logger = logging.getLogger(__name__)
    logger.info("Using psycopg (psycopg3) with ConnectionPool for PostgreSQL connections")
except ImportError:
    try:
        from psycopg2 import sql
        from psycopg2.extras import Json, execute_values
        from psycopg2.pool import ThreadedConnectionPool as ConnectionPool
        PSYCOPG_VERSION = 2
        logger = logging.getLogger(__name__)
        logger.info("Using psycopg2 with ThreadedConnectionPool for PostgreSQL connections")
    except ImportError:
        raise ImportError(
            "Neither 'psycopg' nor 'psycopg2' library is available. "
            "Please install one of them using 'pip install psycopg[pool]' or 'pip install psycopg2'"
        )

from mem0.vector_stores.base import VectorStoreBase

logger = logging.getLogger(__name__)

OPERATOR_SQL_MAP = {
    "eq": ("payload->>%s = %s", False),
    "ne": ("payload->>%s != %s", False),
    "gt": ("(payload->>%s)::numeric > %s", True),
    "gte": ("(payload->>%s)::numeric >= %s", True),
    "lt": ("(payload->>%s)::numeric < %s", True),
    "lte": ("(payload->>%s)::numeric <= %s", True),
    "in": ("payload->>%s = ANY(%s)", False),
    "nin": ("NOT (payload->>%s = ANY(%s))", False),
    "contains": ("payload->>%s LIKE %s", False),
    "icontains": ("payload->>%s ILIKE %s", False),
}


def _build_filter_conditions(filters):
    """Translate a processed filter dict into SQL WHERE fragments and parameter list."""
    conditions = []
    params = []

    if not filters:
        return conditions, params

    for key, value in filters.items():
        if key == "$or":
            or_groups = []
            for or_filter in value:
                sub_conds, sub_params = _build_filter_conditions(or_filter)
                if sub_conds:
                    or_groups.append("(" + " AND ".join(sub_conds) + ")")
                    params.extend(sub_params)
            if or_groups:
                conditions.append("(" + " OR ".join(or_groups) + ")")
            continue

        if key == "$not":
            not_groups = []
            for not_filter in value:
                sub_conds, sub_params = _build_filter_conditions(not_filter)
                if sub_conds:
                    not_groups.append("(" + " AND ".join(sub_conds) + ")")
                    params.extend(sub_params)
            if not_groups:
                conditions.append("NOT (" + " OR ".join(not_groups) + ")")
            continue

        if value == "*":
            conditions.append("payload ? %s")
            params.append(key)
            continue

        if isinstance(value, dict):
            for op, op_value in value.items():
                if op not in OPERATOR_SQL_MAP:
                    raise ValueError(f"Unsupported filter operator: {op}")
                template, is_numeric = OPERATOR_SQL_MAP[op]
                if op in ("in", "nin"):
                    if not isinstance(op_value, list):
                        raise ValueError(
                            f"Filter operator {op!r} for key {key!r} requires a list value, got {type(op_value).__name__}"
                        )
                    str_list = [str(v) for v in op_value]
                    conditions.append(template)
                    params.extend([key, str_list])
                elif op in ("contains", "icontains"):
                    escaped = str(op_value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    conditions.append(template + " ESCAPE '\\'")
                    params.extend([key, f"%{escaped}%"])
                else:
                    conditions.append(template)
                    if is_numeric:
                        params.extend([key, float(op_value)])
                    else:
                        params.extend([key, str(op_value)])
        elif isinstance(value, list):
            conditions.append("payload->>%s = ANY(%s)")
            params.extend([key, [str(v) for v in value]])
        else:
            conditions.append("payload->>%s = %s")
            if isinstance(value, bool):
                params.extend([key, json.dumps(value)])
            else:
                params.extend([key, str(value)])

    return conditions, params


def _with_sslmode(connection_string: str, sslmode: str) -> str:
    """Add or replace sslmode in URI and keyword conninfo strings.

    Keyword conninfo values are assumed not to contain nested ``sslmode=``
    substrings, such as inside an ``options`` value.
    """
    if "://" in connection_string:
        parsed = urlsplit(connection_string)
        query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "sslmode"]
        query.append(("sslmode", sslmode))
        return parsed._replace(query=urlencode(query)).geturl()

    if re.search(r"(^|\s)sslmode=", connection_string):
        return re.sub(r"(^|\s)sslmode=\S+", lambda match: f"{match.group(1)}sslmode={sslmode}", connection_string)

    return f"{connection_string} sslmode={sslmode}"


_EFFECTIVE_DATE_EXPR = (
    "COALESCE(NULLIF(payload->>'temporal_date',''), substr(payload->>'created_at',1,10))"
)


class OutputData(BaseModel):
    id: Optional[str]
    score: Optional[float]
    payload: Optional[dict]


class PGVector(VectorStoreBase):
    def __init__(
        self,
        dbname,
        collection_name,
        embedding_model_dims=None,
        user=None,
        password=None,
        host=None,
        port=None,
        diskann=None,
        hnsw=None,
        minconn=int(os.environ.get("MEM0_VECTOR_MINCONN", "3")),
        maxconn=int(os.environ.get("MEM0_VECTOR_MAXCONN", "10")),
        sslmode=None,
        connection_string=None,
        connection_pool=None,
    ):
        """
        Initialize the PGVector database.

        Args:
            dbname (str): Database name
            collection_name (str): Collection name
            embedding_model_dims (int, optional): Dimension of the embedding vector. Auto-detected from first insert if None.
            user (str): Database user
            password (str): Database password
            host (str, optional): Database host
            port (int, optional): Database port
            diskann (bool, optional): Use DiskANN for faster search
            hnsw (bool, optional): Use HNSW for faster search
            minconn (int): Minimum number of connections to keep in the connection pool
            maxconn (int): Maximum number of connections allowed in the connection pool
            sslmode (str, optional): SSL mode for PostgreSQL connection (e.g., 'require', 'prefer', 'disable')
            connection_string (str, optional): PostgreSQL connection string (overrides individual connection parameters)
            connection_pool (Any, optional): psycopg2 connection pool object (overrides connection string and individual parameters)
        """
        self.collection_name = collection_name
        self.use_diskann = diskann
        self.use_hnsw = hnsw
        self.embedding_model_dims = embedding_model_dims
        self.connection_pool = None
        self._collection_ensured = False

        # Connection setup with priority: connection_pool > connection_string > individual parameters
        if connection_pool is not None:
            # Use provided connection pool
            self.connection_pool = connection_pool
        elif connection_string:
            if sslmode:
                connection_string = _with_sslmode(connection_string, sslmode)
        else:
            connection_string = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
            if sslmode:
                connection_string = _with_sslmode(connection_string, sslmode)
        
        if self.connection_pool is None:
            if PSYCOPG_VERSION == 3:
                # open=False avoids blocking when DB DNS is not yet resolvable (e.g. Docker startup)
                self.connection_pool = ConnectionPool(
                    conninfo=connection_string,
                    min_size=minconn,
                    max_size=maxconn,
                    open=False,
                )
                self.connection_pool.open(wait=False)
            else:
                # psycopg2 ThreadedConnectionPool
                self.connection_pool = ConnectionPool(minconn=minconn, maxconn=maxconn, dsn=connection_string)

    def _get_table_vector_dim(self) -> Optional[int]:
        """Return the vector dimension of the existing table, or None if table doesn't exist."""
        try:
            with self._get_cursor() as cur:
                cur.execute(
                    sql.SQL("""
                    SELECT atttypmod
                    FROM pg_attribute a
                    JOIN pg_class c ON a.attrelid = c.oid
                    JOIN pg_namespace n ON c.relnamespace = n.oid
                    WHERE n.nspname = 'public'
                      AND c.relname = %s
                      AND a.attname = 'vector'
                    """),
                    (self.collection_name,),
                )
                row = cur.fetchone()
                if row is None or row[0] is None:
                    return None
                # pgvector typmod IS the dimension — DO NOT subtract VARHDRSZ
                return row[0]
        except Exception:
            return None

    def _ensure_collection(self):
        if self._collection_ensured and self.embedding_model_dims is not None:
            return
        collections = self.list_cols()
        if self.collection_name not in collections:
            if self.embedding_model_dims is not None:
                self.create_col()
            # else: defer table creation to first insert, when dims become known
        else:
            existing_dim = self._get_table_vector_dim()
            if self.embedding_model_dims is not None and existing_dim is not None and existing_dim != self.embedding_model_dims:
                logger.info(
                    f"Vector dimension mismatch: table has {existing_dim}, model expects {self.embedding_model_dims}. Rebuilding table."
                )
                self.delete_col()
                self.create_col()
        self._collection_ensured = True

    @contextmanager
    def _get_cursor(self, commit: bool = False):
        """
        Unified context manager to get a cursor from the appropriate pool.
        Auto-commits or rolls back based on exception, and returns the connection to the pool.
        """
        if PSYCOPG_VERSION == 3:
            # psycopg3 auto-manages commit/rollback and pool return
            with self.connection_pool.connection() as conn:
                with conn.cursor() as cur:
                    try:
                        yield cur
                        if commit:
                            conn.commit()
                    except Exception:
                        conn.rollback()
                        logger.error("Error in cursor context (psycopg3)", exc_info=True)
                        raise
        else:
            # psycopg2 manual getconn/putconn
            conn = self.connection_pool.getconn()
            cur = conn.cursor()
            try:
                yield cur
                if commit:
                    conn.commit()
            except Exception as exc:
                conn.rollback()
                logger.error(f"Error occurred: {exc}")
                raise exc
            finally:
                cur.close()
                self.connection_pool.putconn(conn)

    def _col(self) -> "sql.Identifier":
        """Return a safely-quoted SQL identifier for the collection table."""
        return sql.Identifier(self.collection_name)

    def create_col(self, vector_size: Optional[int] = None) -> None:
        """
        Create a new collection (table in PostgreSQL).
        Will also initialize vector search index if specified.

        Args:
            vector_size: Explicit vector dimension. Falls back to self.embedding_model_dims.
        """
        dims = vector_size if vector_size is not None else self.embedding_model_dims
        if dims is None:
            raise ValueError("Cannot create collection: embedding_model_dims is not set and no vector_size provided.")
        self.embedding_model_dims = dims
        with self._get_cursor(commit=True) as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                sql.SQL("""
                CREATE TABLE IF NOT EXISTS {} (
                    id UUID PRIMARY KEY,
                    vector vector({}),
                    payload JSONB
                );
                """).format(self._col(), sql.Literal(dims))
            )
            if self.use_diskann and self.embedding_model_dims < 2000:
                cur.execute("SELECT * FROM pg_extension WHERE extname = 'vectorscale'")
                if cur.fetchone():
                    # Create DiskANN index if extension is installed for faster search
                    cur.execute(
                        sql.SQL("""
                        CREATE INDEX IF NOT EXISTS {} ON {}
                        USING diskann (vector);
                        """).format(
                            sql.Identifier(f"{self.collection_name}_diskann_idx"),
                            self._col(),
                        )
                    )
            elif self.use_hnsw:
                cur.execute(
                    sql.SQL("""
                    CREATE INDEX IF NOT EXISTS {} ON {}
                    USING hnsw (vector vector_cosine_ops)
                    """).format(
                        sql.Identifier(f"{self.collection_name}_hnsw_idx"),
                        self._col(),
                    )
                )
            cur.execute(
                sql.SQL("""
                CREATE INDEX IF NOT EXISTS {} ON {}
                USING gin(to_tsvector('simple', payload->>'text_lemmatized'));
                """).format(
                    sql.Identifier(f"{self.collection_name}_text_lemmatized_idx"),
                    self._col(),
                )
            )
        self.ensure_temporal_index()

    def insert(self, vectors: list[list[float]], payloads=None, ids=None) -> None:
        if vectors and self.embedding_model_dims is None:
            inferred_dim = len(vectors[0])
            existing_dim = self._get_table_vector_dim()
            if existing_dim is not None and existing_dim != inferred_dim:
                logger.info(
                    f"Insert vector dim {inferred_dim} differs from table dim {existing_dim}. Rebuilding table."
                )
                self.embedding_model_dims = inferred_dim
                self.delete_col()
                self.create_col(vector_size=inferred_dim)
            elif existing_dim is None:
                self.embedding_model_dims = inferred_dim
                self.create_col(vector_size=inferred_dim)
            else:
                self.embedding_model_dims = inferred_dim

        self._ensure_collection()
        logger.info(f"Inserting {len(vectors)} vectors into collection {self.collection_name}")
        json_payloads = [json.dumps(payload) for payload in payloads]

        data = [(id, vector, payload) for id, vector, payload in zip(ids, vectors, json_payloads)]
        if PSYCOPG_VERSION == 3:
            with self._get_cursor(commit=True) as cur:
                cur.executemany(
                    sql.SQL(
                        "INSERT INTO {} (id, vector, payload) VALUES (%s, %s, %s) "
                        "ON CONFLICT (id) DO UPDATE SET vector = EXCLUDED.vector, payload = EXCLUDED.payload"
                    ).format(self._col()),
                    data,
                )
        else:
            with self._get_cursor(commit=True) as cur:
                execute_values(
                    cur,
                    sql.SQL(
                        "INSERT INTO {} (id, vector, payload) VALUES %s "
                        "ON CONFLICT (id) DO UPDATE SET vector = EXCLUDED.vector, payload = EXCLUDED.payload"
                    ).format(self._col()),
                    data,
                )

    def search(
        self,
        query: str,
        vectors: list[float],
        top_k: Optional[int] = 5,
        filters: Optional[dict] = None,
    ) -> List[OutputData]:
        """
        Search for similar vectors.

        Args:
            query (str): Query.
            vectors (List[float]): Query vector.
            top_k (int, optional): Number of results to return. Defaults to 5.
            filters (Dict, optional): Filters to apply to the search. Defaults to None.

        Returns:
            list: Search results.
        """
        self._ensure_collection()
        # When embedding_model_dims is None, table creation is deferred to first
        # insert().  Return empty results without hitting UndefinedTable.
        if self._get_table_vector_dim() is None:
            return []
        filter_conditions, filter_params = _build_filter_conditions(filters)
        filter_clause = sql.SQL("WHERE " + " AND ".join(filter_conditions)) if filter_conditions else sql.SQL("")

        with self._get_cursor() as cur:
            cur.execute(
                sql.SQL("""
                SELECT id, vector <=> %s::vector AS distance, payload
                FROM {}
                {}
                ORDER BY distance
                LIMIT %s
                """).format(self._col(), filter_clause),
                (vectors, *filter_params, top_k),
            )

            results = cur.fetchall()
        return [OutputData(id=str(r[0]), score=max(0.0, 1.0 - float(r[1])), payload=r[2]) for r in results]

    @staticmethod
    def _effective_date(payload) -> Optional[date]:
        """Resolve the memory's effective date (Asia/Shanghai) for temporal filtering.

        temporal_date wins; otherwise created_at is converted to the Shanghai date.
        Malformed created_at falls back to its first-10-character date portion; None
        when nothing usable remains (dirty data must never raise here).
        """
        if not isinstance(payload, dict):
            return None
        temporal = payload.get("temporal_date")
        if temporal:
            try:
                return date.fromisoformat(str(temporal)[:10])
            except ValueError:
                pass
        created = payload.get("created_at")
        if created:
            raw = str(created)
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(ZoneInfo("Asia/Shanghai")).date()
            except ValueError:
                try:
                    return date.fromisoformat(raw[:10])
                except ValueError:
                    return None
        return None

    def temporal_search(
        self,
        filters: Optional[dict] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        top_k: int = 20,
        half_life_hours: int = 168,
    ) -> List[OutputData]:
        """Search memories by absolute time range [start, end] ("YYYY-MM-DD" or None).

        The SQL filter uses a relaxed lower bound (start - 1 day) so the expression
        index stays usable; results are re-checked in Python against Asia/Shanghai
        dates, then scored by recency: time_boost = 0.5 ** (age_hours / half_life_hours).
        """
        self._ensure_collection()
        conditions, params = _build_filter_conditions(filters)
        conditions = list(conditions)
        params = list(params)

        today_shanghai = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        today_str = today_shanghai.isoformat()

        if start is not None:
            start_lo = (date.fromisoformat(start) - timedelta(days=1)).isoformat()
            conditions.append(f"({_EFFECTIVE_DATE_EXPR}) >= %s")
            params.append(start_lo)
        if end is not None:
            conditions.append(f"({_EFFECTIVE_DATE_EXPR}) <= %s")
            params.append(end)
        conditions.append("(payload->>'expiration_date' IS NULL OR payload->>'expiration_date' >= %s)")
        params.append(today_str)

        where_clause = sql.SQL("WHERE " + " AND ".join(conditions))

        with self._get_cursor() as cur:
            cur.execute(
                sql.SQL("""
                SELECT id, payload
                FROM {}
                {}
                ORDER BY {} DESC
                LIMIT %s
                """).format(self._col(), where_clause, sql.SQL(_EFFECTIVE_DATE_EXPR)),
                (*params, max(top_k * 3, 60)),
            )
            rows = cur.fetchall()

        start_date = date.fromisoformat(start) if start is not None else None
        end_date = date.fromisoformat(end) if end is not None else None

        scored = []
        for row in rows:
            effective = self._effective_date(row[1])
            if effective is None:
                if start_date is not None or end_date is not None:
                    continue
                boost = 0.0
            else:
                if start_date is not None and effective < start_date:
                    continue
                if end_date is not None and effective > end_date:
                    continue
                age_days = (today_shanghai - effective).days
                boost = 0.5 ** (age_days * 24 / half_life_hours)
            scored.append((boost, row))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            OutputData(id=str(row[0]), score=boost, payload=row[1])
            for boost, row in scored[:top_k]
        ]

    def ensure_temporal_index(self) -> None:
        """Create the expression index backing temporal_search (idempotent)."""
        with self._get_cursor(commit=True) as cur:
            cur.execute(
                sql.SQL("""
                CREATE INDEX IF NOT EXISTS idx_mem0_temporal_ts ON {}
                (({}))
                """).format(self._col(), sql.SQL(_EFFECTIVE_DATE_EXPR))
            )

    def keyword_search(self, query, top_k=5, filters=None):
        """
        Search using PostgreSQL full-text search on lemmatized text.

        Args:
            query (str): The search query text.
            top_k (int, optional): Number of results to return. Defaults to 5.
            filters (dict, optional): Filters to apply to the search. Defaults to None.

        Returns:
            List[OutputData]: Search results ranked by text relevance.
        """
        self._ensure_collection()
        filter_conditions, filter_params = _build_filter_conditions(filters)
        filter_clause = sql.SQL("AND " + " AND ".join(filter_conditions)) if filter_conditions else sql.SQL("")

        try:
            with self._get_cursor() as cur:
                cur.execute(
                    sql.SQL("""
                    SELECT id, ts_rank_cd(to_tsvector('simple', payload->>'text_lemmatized'), plainto_tsquery('simple', %s)) AS score, payload
                    FROM {}
                    WHERE to_tsvector('simple', payload->>'text_lemmatized') @@ plainto_tsquery('simple', %s)
                    {}
                    ORDER BY score DESC
                    LIMIT %s
                    """).format(self._col(), filter_clause),
                    (query, query, *filter_params, top_k),
                )

                results = cur.fetchall()
            return [OutputData(id=str(r[0]), score=float(r[1]), payload=r[2]) for r in results]
        except Exception as e:
            logger.debug(f"Keyword search failed: {e}")
            return None

    def delete(self, vector_id: str) -> None:
        """
        Delete a vector by ID.

        Args:
            vector_id (str): ID of the vector to delete.
        """
        self._ensure_collection()
        with self._get_cursor(commit=True) as cur:
            cur.execute(sql.SQL("DELETE FROM {} WHERE id = %s").format(self._col()), (vector_id,))

    def update(
        self,
        vector_id: str,
        vector: Optional[list[float]] = None,
        payload: Optional[dict] = None,
    ) -> None:
        """
        Update a vector and its payload.

        Args:
            vector_id (str): ID of the vector to update.
            vector (List[float], optional): Updated vector.
            payload (Dict, optional): Updated payload.
        """
        self._ensure_collection()
        with self._get_cursor(commit=True) as cur:
            if vector is not None:
               cur.execute(
                    sql.SQL("UPDATE {} SET vector = %s WHERE id = %s").format(self._col()),
                    (vector, vector_id),
                )
            if payload is not None:
                # Handle JSON serialization based on psycopg version
                if PSYCOPG_VERSION == 3:
                    # psycopg3 uses psycopg.types.json.Json
                    cur.execute(
                        sql.SQL("UPDATE {} SET payload = %s WHERE id = %s").format(self._col()),
                        (Json(payload), vector_id),
                    )
                else:
                    # psycopg2 uses psycopg2.extras.Json
                    cur.execute(
                        sql.SQL("UPDATE {} SET payload = %s WHERE id = %s").format(self._col()),
                        (Json(payload), vector_id),
                    )


    def get(self, vector_id: str) -> OutputData:
        """
        Retrieve a vector by ID.

        Args:
            vector_id (str): ID of the vector to retrieve.

        Returns:
            OutputData: Retrieved vector.
        """
        self._ensure_collection()
        with self._get_cursor() as cur:
            cur.execute(
                sql.SQL("SELECT id, payload FROM {} WHERE id = %s").format(self._col()),
                (vector_id,),
            )
            result = cur.fetchone()
            if not result:
                return None
            return OutputData(id=str(result[0]), score=None, payload=result[1])

    def list_cols(self) -> List[str]:
        """
        List all collections.

        Returns:
            List[str]: List of collection names.
        """
        with self._get_cursor() as cur:
            cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            return [row[0] for row in cur.fetchall()]

    def delete_col(self) -> None:
        """Delete a collection."""
        with self._get_cursor(commit=True) as cur:
            cur.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(self._col()))

    def col_info(self) -> dict[str, Any]:
        """
        Get information about a collection.

        Returns:
            Dict[str, Any]: Collection information.
        """
        self._ensure_collection()
        with self._get_cursor() as cur:
            cur.execute(
                sql.SQL("""
                SELECT
                    table_name,
                    (SELECT COUNT(*) FROM {}) as row_count,
                    (SELECT pg_size_pretty(pg_total_relation_size({}::regclass))) as total_size
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            """).format(self._col(), sql.Literal(self.collection_name)),
                (self.collection_name,),
            )
            result = cur.fetchone()
        return {"name": result[0], "count": result[1], "size": result[2]}

    def list(
        self,
        filters: Optional[dict] = None,
        top_k: Optional[int] = 100,
        order_by: Optional[str] = None,
    ) -> List[OutputData]:
        """
        List all vectors in a collection.

        Args:
            filters (Dict, optional): Filters to apply to the list.
            top_k (int, optional): Number of vectors to return. Defaults to 100.
            order_by (str, optional): Named sort mode, one of:
                "updated_at_desc" — ORDER BY COALESCE(updated_at, created_at) DESC NULLS LAST.
                None — no ORDER BY (physical scan order, historical default).

        Returns:
            List[OutputData]: List of vectors.
        """
        self._ensure_collection()
        filter_conditions, filter_params = _build_filter_conditions(filters)
        filter_clause = sql.SQL("WHERE " + " AND ".join(filter_conditions)) if filter_conditions else sql.SQL("")
        # 排序白名单：只允许内部命名的排序模式，杜绝任意列名拼接注入面
        order_clause = sql.SQL("")
        if order_by == "updated_at_desc":
            order_clause = sql.SQL(
                "ORDER BY COALESCE(payload->>'updated_at', payload->>'created_at') DESC NULLS LAST"
            )

        with self._get_cursor() as cur:
            cur.execute(
                sql.SQL("""
                SELECT id, payload
                FROM {}
                {}
                {}
                LIMIT %s
                """).format(self._col(), filter_clause, order_clause),
                (*filter_params, top_k),
            )
            results = cur.fetchall()
        return [[OutputData(id=str(r[0]), score=None, payload=r[1]) for r in results]]

    def __del__(self) -> None:
        """
        Close the database connection pool when the object is deleted.
        """
        try:
            # Close pool appropriately
            if PSYCOPG_VERSION == 3:
                self.connection_pool.close()
            else:
                self.connection_pool.closeall()
        except Exception:
            pass

    def reset(self) -> None:
        """Reset the index by deleting and recreating it."""
        self._ensure_collection()
        logger.warning(f"Resetting index {self.collection_name}...")
        self.delete_col()
        self.create_col()
