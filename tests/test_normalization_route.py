import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestNormalizationRoute(VerdictLedgerTestCase):

    def _run(self, event_id="evt_01"):
        return self.client.post(f"/organizer/normalize/{event_id}",
                                 headers={**self.auth_header(self.ORGANIZER),
                                          "Accept": "application/json"})

    def test_run_persists_per_review_results_with_z_scores(self):
        resp = self._run()
        self.assertEqual(resp.status_code, 201)
        run_id = resp.get_json()["run_id"]

        import db as db_module
        conn = db_module.get_connection()
        n_reviews = conn.execute(
            "SELECT COUNT(*) AS n FROM review_normalizations WHERE normalization_run_id=?",
            (run_id,)).fetchone()["n"]
        n_z_null = conn.execute(
            "SELECT COUNT(*) AS n FROM review_normalizations "
            "WHERE normalization_run_id=? AND z_score IS NULL", (run_id,)).fetchone()["n"]
        conn.close()
        self.assertEqual(n_reviews, 126)
        # Zero-variance / n=1 judges legitimately have no z-score; the
        # rest must. jdg_07 (3), jdg_01 (1), jdg_23 (1) = 5 at minimum.
        self.assertGreaterEqual(n_z_null, 5)
        self.assertLess(n_z_null, 126)

    def test_run_records_score_bounds_and_k(self):
        run_id = self._run().get_json()["run_id"]
        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT * FROM normalization_runs WHERE id=?", (run_id,)).fetchone()
        conn.close()
        self.assertEqual(row["k_param"], 3)
        self.assertEqual((row["score_lo"], row["score_hi"]), (1, 5))

    def test_zero_variance_judge_reviews_equal_global_mean_in_db(self):
        run_id = self._run().get_json()["run_id"]
        import db as db_module
        conn = db_module.get_connection()
        gm = conn.execute("SELECT global_mean FROM normalization_runs WHERE id=?",
                          (run_id,)).fetchone()["global_mean"]
        rows = conn.execute(
            "SELECT rn.normalized_value, rn.z_score FROM review_normalizations rn "
            "JOIN scores s ON s.id=rn.score_id "
            "WHERE rn.normalization_run_id=? AND s.judge_id='jdg_07'", (run_id,)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertAlmostEqual(r["normalized_value"], gm)
            self.assertIsNone(r["z_score"])

    def test_rerun_is_reproducible_and_versioned(self):
        r1 = self._run().get_json()
        r2 = self._run().get_json()
        self.assertEqual((r1["version"], r2["version"]), (1, 2))
        import db as db_module
        conn = db_module.get_connection()
        rows = conn.execute(
            "SELECT normalization_run_id, project_id, normalized_score FROM project_results "
            "WHERE project_id='prj_01' ORDER BY normalization_run_id").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(rows[0]["normalized_score"], rows[1]["normalized_score"])

    def test_rubric_version_mixing_uses_each_scores_own_weights(self):
        """THE key regression. After an organizer publishes rubric v2
        with wildly different weights, existing scores stay pinned to
        v1. Normalization must still compute each score's raw value with
        v1's weights, not v2's. If it used the newest rubric for
        everything, every raw score in this run would change."""
        baseline = self._run().get_json()["run_id"]

        import db as db_module
        conn = db_module.get_connection()
        before = {r["score_id"]: r["raw_weighted"] for r in conn.execute(
            "SELECT score_id, raw_weighted FROM review_normalizations "
            "WHERE normalization_run_id=?", (baseline,)).fetchall()}
        conn.close()

        # Publish rubric v2 on evt_01 with same criteria but very
        # different weights and the SAME 1..5 range.
        resp = self.client.post(
            "/organizer/events/evt_01/rubric",
            json={"criteria": [
                {"key": "functionality", "weight": 0.05},
                {"key": "quality", "weight": 0.05},
                {"key": "innovation", "weight": 0.90},
            ]},
            headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 201)

        second = self._run().get_json()["run_id"]
        conn = db_module.get_connection()
        after = {r["score_id"]: r["raw_weighted"] for r in conn.execute(
            "SELECT score_id, raw_weighted FROM review_normalizations "
            "WHERE normalization_run_id=?", (second,)).fetchall()}
        conn.close()

        self.assertEqual(len(before), 126)
        for score_id, raw in before.items():
            self.assertAlmostEqual(after[score_id], raw,
                                   msg="raw score changed after a new rubric version was published")

    def test_mixed_score_ranges_are_refused_not_pooled(self):
        """A review scored on a 1..5 rubric and one on a 0..10 rubric
        cannot share a global mean. The run must refuse (409)."""
        import db as db_module
        from auth import now_iso
        conn = db_module.get_connection()
        conn.execute("INSERT INTO rubric_versions (id, event_id, version, created_at) "
                     "VALUES ('evt_01_rubric_wide', 'evt_01', 99, ?)", (now_iso(),))
        conn.execute("INSERT INTO rubric_criteria (id, rubric_version_id, key, label, weight, "
                     "min_value, max_value) VALUES ('c_wide', 'evt_01_rubric_wide', "
                     "'functionality', 'F', 1.0, 0, 10)")
        conn.execute("UPDATE scores SET rubric_version_id='evt_01_rubric_wide' "
                     "WHERE judge_id='jdg_24' AND project_id="
                     "(SELECT project_id FROM scores WHERE judge_id='jdg_24' LIMIT 1)")
        conn.commit()
        conn.close()

        resp = self._run()
        self.assertEqual(resp.status_code, 409)
        self.assertIn("cannot be pooled", resp.get_json()["error"])

    def test_incomplete_review_is_refused_not_silently_reduced(self):
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("DELETE FROM score_criteria WHERE rowid IN ("
                     "SELECT rowid FROM score_criteria WHERE criterion_key='quality' LIMIT 1)")
        conn.commit()
        conn.close()
        resp = self._run()
        self.assertEqual(resp.status_code, 409)
        self.assertIn("missing", resp.get_json()["error"])

    def test_cannot_normalize_while_event_still_open(self):
        resp = self._run("evt_live_2026")
        self.assertEqual(resp.status_code, 409)

    def test_unknown_event_is_404(self):
        self.assertEqual(self._run("evt_nope").status_code, 404)

    def test_event_with_no_scores_is_a_clean_400_not_a_crash(self):
        import db as db_module
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET submissions_close=? WHERE id='evt_live_2026'", (past,))
        conn.commit()
        conn.close()
        self.assertEqual(self._run("evt_live_2026").status_code, 400)

    def test_judge_cannot_run_normalization(self):
        resp = self.client.post("/organizer/normalize/evt_01",
                                 headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 403)

    def test_get_is_not_allowed(self):
        resp = self.client.get("/organizer/normalize/evt_01",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 405)

    def test_explain_view_reads_persisted_run_values(self):
        self._run()
        resp = self.client.get("/organizer/results/evt_01/explain/prj_07",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn("Normalized value N", body)
        # prj_07 is reviewed by jdg_01 (n=1): the fallback explanation
        # must be rendered, and no literal None/undefined leaks out.
        self.assertIn("falls back to the global mean", body)
        self.assertNotIn(">None<", body)
        self.assertNotIn("undefined", body)

    def test_explain_is_stable_when_score_edited_after_run(self):
        """The explanation must reflect what the run recorded, not the
        live scores table."""
        self._run()
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE scores SET raw_weighted=1.0 WHERE project_id='prj_07'")
        conn.commit()
        conn.close()
        resp = self.client.get("/organizer/results/evt_01/explain/prj_07",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertNotIn("1.000</span></div>\n      <div class=\"calc-line\"><span class=\"k\">Judge sample size",
                         resp.data.decode())


if __name__ == "__main__":
    unittest.main()
