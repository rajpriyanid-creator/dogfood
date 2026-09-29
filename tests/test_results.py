import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestResults(VerdictLedgerTestCase):

    def setUp(self):
        super().setUp()
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET publish_state='draft', submissions_close='2020-01-01T00:00:00Z', judging_close='2020-01-01T00:00:00Z' WHERE id='evt_01'")
        conn.commit()
        conn.close()

    def test_normalization_run_creates_results(self):
        resp = self.client.post("/organizer/normalize/evt_01",
                                 headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 302)

        import db as db_module
        conn = db_module.get_connection()
        run = conn.execute(
            "SELECT * FROM normalization_runs WHERE event_id='evt_01' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(run)
        results = conn.execute(
            "SELECT * FROM project_results WHERE normalization_run_id=?",
            (run["id"],),
        ).fetchall()
        conn.close()
        self.assertGreater(len(results), 0)

    def test_raw_and_normalized_both_retained(self):
        self.client.post("/organizer/normalize/evt_01",
                          headers=self.auth_header(self.ORGANIZER))
        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute(
            "SELECT r.* FROM project_results r "
            "JOIN normalization_runs n ON n.id=r.normalization_run_id "
            "WHERE r.project_id='prj_01' ORDER BY n.version DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["raw_score"])
        self.assertIsNotNone(row["normalized_score"])

    def test_reproducibility_rerun_same_scores_same_result(self):
        """Running normalization twice against unchanged scores produces
        the same normalized value each time (a new run/version, but an
        identical number) - this is the reproducibility guarantee."""
        self.client.post("/organizer/normalize/evt_01",
                          headers=self.auth_header(self.ORGANIZER))
        self.client.post("/organizer/normalize/evt_01",
                          headers=self.auth_header(self.ORGANIZER))

        import db as db_module
        conn = db_module.get_connection()
        rows = conn.execute(
            "SELECT r.normalized_score, r.raw_score FROM project_results r "
            "JOIN normalization_runs n ON n.id = r.normalization_run_id "
            "WHERE r.project_id='prj_01' AND n.event_id='evt_01' "
            "ORDER BY n.version ASC"
        ).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["normalized_score"], rows[1]["normalized_score"])
        self.assertAlmostEqual(rows[0]["raw_score"], rows[1]["raw_score"])

    def test_explain_view_requires_normalization_run_first(self):
        resp = self.client.get("/organizer/results/evt_01/explain/prj_01",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 400)

    def test_explain_view_after_run_succeeds(self):
        self.client.post("/organizer/normalize/evt_01",
                          headers=self.auth_header(self.ORGANIZER))
        resp = self.client.get("/organizer/results/evt_01/explain/prj_01",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
