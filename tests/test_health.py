import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestHealthCheck(VerdictLedgerTestCase):
    """Regression coverage for the audited defect: /healthz returned a
    static {"status": "ok"} with no actual check of anything."""

    def test_healthy_database_reports_ok(self):
        resp = self.client.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["database"], "reachable")

    def test_reports_seeded_state(self):
        resp = self.client.get("/healthz")
        body = resp.get_json()
        self.assertTrue(body["seeded"])

    def test_broken_database_reports_failure(self):
        """Point the app at a database file with no schema at all and
        confirm healthz actually notices, rather than reporting ok
        regardless."""
        import db as db_module
        import tempfile
        import sqlite3
        broken_path = os.path.join(tempfile.mkdtemp(), "broken.db")
        sqlite3.connect(broken_path).close()

        original_path = db_module.DB_PATH
        db_module.DB_PATH = broken_path
        try:
            resp = self.client.get("/healthz")
            self.assertEqual(resp.status_code, 503)
            body = resp.get_json()
            self.assertEqual(body["status"], "error")
        finally:
            db_module.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
