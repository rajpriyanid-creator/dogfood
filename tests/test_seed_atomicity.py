import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import importlib
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = os.path.join(REPO_ROOT, "src", "backend")
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
for p in (BACKEND_DIR, SCRIPTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestSeedAtomicity(unittest.TestCase):
    """Regression coverage for the audited P0 defect: seed_fixture_event,
    seed_live_event, and create_bootstrap_sessions each committed their
    own work independently, so a failure partway through left a
    partially-seeded database that the old is_seeded() check ("any
    events exist") would mistake for fully seeded on the next boot."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.db")
        os.environ["VERDICT_LEDGER_DB"] = self.db_path

        for mod_name in ("db", "seed"):
            if mod_name in sys.modules:
                importlib.reload(sys.modules[mod_name])

        import db as db_module
        db_module.DB_PATH = self.db_path
        db_module.init_schema()
        self.db_module = db_module

        import seed as seed_module
        importlib.reload(seed_module)
        seed_module.FIXTURES_PATH = os.path.join(REPO_ROOT, "fixtures.json")
        self.seed_module = seed_module

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_seed_marks_seed_complete(self):
        self.seed_module.main()
        conn = self.db_module.get_connection()
        self.assertTrue(self.db_module.is_seeded(conn))
        conn.close()

    def test_second_seed_is_a_no_op(self):
        self.seed_module.main()
        conn = self.db_module.get_connection()
        before = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        conn.close()

        self.seed_module.main()
        conn = self.db_module.get_connection()
        after = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        conn.close()
        self.assertEqual(before, after)

    def test_failure_partway_through_rolls_back_completely(self):
        """The exact regression the audit asked for: force
        seed_live_event to raise partway through, and confirm the
        database ends up with ZERO events, ZERO users, ZERO of
        anything - not a half-loaded fixture event."""
        original_seed_live_event = self.seed_module.seed_live_event

        def failing_seed_live_event(conn):
            conn.execute(
                "INSERT INTO events (id, name, description, kind, "
                "submissions_close, publish_state, created_at) "
                "VALUES ('evt_doomed', 'doomed', NULL, 'live', "
                "datetime('now'), 'draft', datetime('now'))"
            )
            raise RuntimeError("simulated failure partway through seeding")

        self.seed_module.seed_live_event = failing_seed_live_event
        try:
            with self.assertRaises(RuntimeError):
                self.seed_module.main()
        finally:
            self.seed_module.seed_live_event = original_seed_live_event

        conn = self.db_module.get_connection()
        for table in ("events", "users", "teams", "projects", "scores", "meta"):
            n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            self.assertEqual(n, 0, f"table {table} was not rolled back (has {n} rows)")
        self.assertFalse(self.db_module.is_seeded(conn))
        conn.close()

    def test_retry_after_failed_seed_succeeds_cleanly(self):
        """After a failed (and rolled-back) seed attempt, a subsequent
        real seed call must succeed and produce a fully-seeded database
        - proving the rollback didn't leave residue that a clean
        re-insert would collide with."""
        original_seed_live_event = self.seed_module.seed_live_event

        def failing_once(conn):
            raise RuntimeError("simulated failure")

        self.seed_module.seed_live_event = failing_once
        with self.assertRaises(RuntimeError):
            self.seed_module.main()
        self.seed_module.seed_live_event = original_seed_live_event

        self.seed_module.main()
        conn = self.db_module.get_connection()
        self.assertTrue(self.db_module.is_seeded(conn))
        n_projects = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        conn.close()
        self.assertEqual(n_projects, 43)  # 41 fixture + 2 live demo


if __name__ == "__main__":
    unittest.main()
