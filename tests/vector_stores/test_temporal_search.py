import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from mem0.vector_stores.pgvector import PGVector

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
EFF_EXPR = "COALESCE(NULLIF(payload->>'temporal_date',''), substr(payload->>'created_at',1,10))"


class TestTemporalSearch(unittest.TestCase):
    def setUp(self):
        self.mock_cursor = MagicMock()

    def _make_pgvector(self):
        """Build a PGVector whose _get_cursor is mocked, skipping collection setup."""
        self._conn_pool_patcher = patch("mem0.vector_stores.pgvector.ConnectionPool")
        self._version_patcher = patch("mem0.vector_stores.pgvector.PSYCOPG_VERSION", 3)
        self._cursor_patcher = patch.object(PGVector, "_get_cursor")
        self._conn_pool_patcher.start()
        self._version_patcher.start()
        mock_get_cursor = self._cursor_patcher.start()
        self.addCleanup(self._cursor_patcher.stop)
        self.addCleanup(self._version_patcher.stop)
        self.addCleanup(self._conn_pool_patcher.stop)

        mock_get_cursor.return_value.__enter__.return_value = self.mock_cursor
        mock_get_cursor.return_value.__exit__.return_value = None
        self.mock_cursor.fetchall.return_value = []

        pgvector = PGVector(
            dbname="test_db",
            collection_name="test_collection",
            embedding_model_dims=3,
            user="test_user",
            password="test_pass",
            host="localhost",
            port=5432,
            diskann=False,
            hnsw=False,
            minconn=1,
            maxconn=4,
        )
        pgvector._collection_ensured = True
        return pgvector

    def _shanghai_today(self):
        return datetime.now(SHANGHAI_TZ).date()

    def _created_at_utc(self, days_ago, hour=4):
        """created_at whose Asia/Shanghai date is today-days_ago (UTC 04:00 = Shanghai 12:00)."""
        day = self._shanghai_today() - timedelta(days=days_ago)
        return f"{day.isoformat()}T{hour:02d}:00:00+00:00"

    def _temporal_execute(self):
        """Return the (query, params) of the single temporal_search execute call."""
        call_args = self.mock_cursor.execute.call_args
        self.assertIsNotNone(call_args, "temporal_search did not execute any SQL")
        return str(call_args[0][0]), call_args[0][1]

    # --- SQL param / clause assertions -------------------------------------

    def test_temporal_search_bounded_params(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-1", {"user_id": "alice", "temporal_date": "2026-08-12"}),
        ]
        results = pgvector.temporal_search(
            filters={"user_id": "alice"}, start="2026-08-10", end="2026-08-12", top_k=2
        )
        sql_text, params = self._temporal_execute()
        self.assertEqual(params, ("user_id", "alice", "2026-08-09", "2026-08-12", self._shanghai_today().isoformat(), 60))
        self.assertEqual(sql_text.count(EFF_EXPR), 3)  # two WHERE bounds + ORDER BY
        self.assertIn("ORDER BY", sql_text)
        self.assertIn("LIMIT", sql_text)
        self.assertEqual(len(results), 1)

    def test_temporal_search_start_only(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = []
        pgvector.temporal_search(start="2026-08-10", end=None)
        sql_text, params = self._temporal_execute()
        self.assertEqual(params, ("2026-08-09", self._shanghai_today().isoformat(), 60))
        self.assertEqual(sql_text.count(EFF_EXPR), 2)  # one bound + ORDER BY

    def test_temporal_search_end_only(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = []
        pgvector.temporal_search(start=None, end="2026-08-12")
        sql_text, params = self._temporal_execute()
        self.assertEqual(params, ("2026-08-12", self._shanghai_today().isoformat(), 60))
        self.assertEqual(sql_text.count(EFF_EXPR), 2)  # one bound + ORDER BY

    def test_temporal_search_no_bounds_latest(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-old", {"created_at": self._created_at_utc(3)}),
            ("mem-new", {"created_at": self._created_at_utc(0)}),
        ]
        results = pgvector.temporal_search(top_k=1)
        sql_text, params = self._temporal_execute()
        self.assertEqual(params, (self._shanghai_today().isoformat(), 60))
        self.assertEqual(sql_text.count(EFF_EXPR), 1)  # ORDER BY only, no bounds
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "mem-new")

    # --- effective date resolution ------------------------------------------

    def test_temporal_search_temporal_date_priority(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-td", {"temporal_date": "2026-08-10", "created_at": "2026-08-01T00:00:00+00:00"}),
            ("mem-ca", {"created_at": "2026-08-11T20:00:00+00:00"}),
        ]
        results = pgvector.temporal_search(start="2026-08-09", end="2026-08-12")
        ids = [r.id for r in results]
        # Both rows: mem-td effective date 08-10 (temporal_date wins over old created_at),
        # mem-ca effective date 08-12 (Shanghai), both inside [08-09, 08-12].
        self.assertEqual(sorted(ids), ["mem-ca", "mem-td"])

    def test_temporal_search_timezone_conversion(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-tz", {"created_at": "2026-08-11T20:00:00+00:00"}),
        ]
        # UTC 2026-08-11T20:00 = Shanghai 2026-08-12 04:00 -> effective date 08-12.
        results = pgvector.temporal_search(start="2026-08-12", end="2026-08-12")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "mem-tz")

    def test_temporal_search_precise_filter_excludes_utc_edge(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-utc", {"created_at": "2026-08-12T10:00:00+00:00"}),
        ]
        # Coarse SQL (start_lo=08-12) would keep this row; Shanghai date 08-12 is
        # outside [08-13, 08-14], so Python precise filtering must exclude it.
        results = pgvector.temporal_search(start="2026-08-13", end="2026-08-14")
        self.assertEqual(results, [])

    def test_temporal_search_dirty_created_at_substr_fallback(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-dirty", {"created_at": "2026-08-15Tnot-iso"}),
        ]
        # fromisoformat fails; substr(created_at,1,10) = "2026-08-15" is used.
        results = pgvector.temporal_search(end="2026-08-15")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "mem-dirty")

    def test_temporal_search_dirty_created_at_no_throw(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-garbage", {"created_at": "garbage"}),
        ]
        results = pgvector.temporal_search()  # no bounds -> must not raise, row kept
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].id, "mem-garbage")
        self.assertEqual(results[0].score, 0.0)

    # --- scoring --------------------------------------------------------------

    def test_temporal_search_scores(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            ("mem-d7", {"created_at": self._created_at_utc(7)}),
            ("mem-d1", {"created_at": self._created_at_utc(1)}),
            ("mem-d0", {"created_at": self._created_at_utc(0)}),
        ]
        results = pgvector.temporal_search(top_k=3, half_life_hours=168)
        by_id = {r.id: r.score for r in results}
        self.assertAlmostEqual(by_id["mem-d0"], 1.0, places=6)
        self.assertAlmostEqual(by_id["mem-d7"], 0.5, places=6)
        self.assertAlmostEqual(by_id["mem-d1"], 0.5 ** (24 / 168), places=6)
        scores = [r.score for r in results]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for score in scores:
            self.assertGreater(score, 0.0)
            self.assertLessEqual(score, 1.0)

    def test_temporal_search_limit_and_order(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = [
            (f"mem-{i}", {"created_at": self._created_at_utc(i)}) for i in (4, 0, 2, 1, 3)
        ]
        results = pgvector.temporal_search(top_k=2)
        _, params = self._temporal_execute()
        self.assertEqual(params[-1], 60)  # max(2*3, 60)
        self.assertEqual([r.id for r in results], ["mem-0", "mem-1"])

    # --- expiration -------------------------------------------------------------

    def test_temporal_search_expired_filter_param(self):
        pgvector = self._make_pgvector()
        self.mock_cursor.fetchall.return_value = []
        pgvector.temporal_search(start="2026-08-10", end="2026-08-12")
        sql_text, params = self._temporal_execute()
        self.assertIn("payload->>'expiration_date' IS NULL OR payload->>'expiration_date' >= %s", sql_text)
        self.assertIn(self._shanghai_today().isoformat(), params)

    # --- index -------------------------------------------------------------------

    def test_ensure_temporal_index(self):
        pgvector = self._make_pgvector()
        pgvector.ensure_temporal_index()
        calls = [str(c) for c in self.mock_cursor.execute.call_args_list]
        self.assertTrue(
            any("CREATE INDEX IF NOT EXISTS idx_mem0_temporal_ts" in c and EFF_EXPR in c for c in calls),
            f"expected temporal index SQL, got: {calls}",
        )

    def test_create_col_calls_ensure_temporal_index(self):
        pgvector = self._make_pgvector()
        pgvector.create_col()
        calls = [str(c) for c in self.mock_cursor.execute.call_args_list]
        self.assertTrue(
            any("CREATE INDEX IF NOT EXISTS idx_mem0_temporal_ts" in c for c in calls),
            f"expected temporal index SQL in create_col, got: {calls}",
        )


if __name__ == "__main__":
    unittest.main()
