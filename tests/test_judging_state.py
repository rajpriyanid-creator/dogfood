import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestJudgingStateAndRubricVersioning(VerdictLedgerTestCase):
    """Regression coverage for two audited defects:
    - CRITICAL #12: updating a score with a newer rubric left the old
      rubric_version_id in place while recomputing raw_weighted against
      the new weights.
    - CRITICAL #13: no server-side judging-state enforcement existed at
      all - scores could be created/edited regardless of event state.
    """

    def _open_live_event_for_scoring(self):
        """Force evt_live_2026 into SUBMISSIONS_CLOSED so scoring is
        actually open on it (it's seeded OPEN, which the new
        scoring_is_open() correctly refuses to score against)."""
        import db as db_module
        from datetime import datetime, timezone, timedelta
        conn = db_module.get_connection()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn.execute(
            "UPDATE events SET submissions_close=?, judging_close=? WHERE id='evt_live_2026'",
            (past, future),
        )
        conn.commit()
        conn.close()

    def test_scoring_blocked_while_event_still_open(self):
        """evt_live_2026 is seeded OPEN (submissions still being
        accepted) - scoring must not be possible yet."""
        import db as db_module
        conn = db_module.get_connection()
        assignment = conn.execute(
            "SELECT * FROM assignments WHERE event_id='evt_live_2026' LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(assignment, "seed must provide a live-event assignment")

        import auth as auth_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, assignment["judge_id"])
        conn.close()

        resp = self.client.post(f"/judge/review/{assignment['project_id']}",
                                 data={"criterion_functionality": "4",
                                       "criterion_quality": "4",
                                       "criterion_innovation": "4"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 403)

    def test_scoring_allowed_once_submissions_closed(self):
        self._open_live_event_for_scoring()
        import db as db_module
        conn = db_module.get_connection()
        assignment = conn.execute(
            "SELECT * FROM assignments WHERE event_id='evt_live_2026' LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(assignment, "seed must provide a live-event assignment")

        import auth as auth_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, assignment["judge_id"])
        conn.close()

        resp = self.client.post(f"/judge/review/{assignment['project_id']}",
                                 data={"criterion_functionality": "4",
                                       "criterion_quality": "4",
                                       "criterion_innovation": "4"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 302)

    def test_scoring_blocked_once_published(self):
        """evt_01 is seeded published - a fresh score attempt against
        it must be refused even though the judge is genuinely assigned.
        Constructs its own unscored assignment rather than relying on
        one existing unscored pair in the fixture (which may not exist,
        since the fixture's scores happen to cover every assignment)."""
        import db as db_module
        from auth import now_iso
        conn = db_module.get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO assignments (id, event_id, judge_id, project_id, "
            "version, created_at) VALUES ('asg_probe_published', 'evt_01', 'jdg_01', "
            "'prj_02', 1, ?)",
            (now_iso(),),
        )
        conn.commit()
        # Make sure jdg_01 has no existing score on prj_02 (it wouldn't,
        # since this assignment did not exist until just now, but guard
        # against fixture drift).
        conn.execute("DELETE FROM scores WHERE judge_id='jdg_01' AND project_id='prj_02'")
        conn.commit()
        conn.close()

        resp = self.client.post("/judge/review/prj_02",
                                 data={"criterion_functionality": "4",
                                       "criterion_quality": "4",
                                       "criterion_innovation": "4"},
                                 headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 403)

    def test_missing_criterion_rejected_not_silently_scored_low(self):
        """The audited HIGH fix: omitting a required criterion must be
        a 400, not a weighted sum computed over only the criteria that
        were present."""
        self._open_live_event_for_scoring()
        import db as db_module
        conn = db_module.get_connection()
        assignment = conn.execute(
            "SELECT * FROM assignments WHERE event_id='evt_live_2026' LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(assignment, "seed must provide a live-event assignment")

        import auth as auth_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, assignment["judge_id"])
        conn.close()

        resp = self.client.post(f"/judge/review/{assignment['project_id']}",
                                 data={"criterion_functionality": "4"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 400)

    def test_rubric_version_stays_pinned_across_an_update(self):
        """The core CRITICAL regression: create a score under rubric
        version N, then simulate an organizer publishing version N+1,
        then update the existing score. The stored rubric_version_id on
        the score must remain N, and raw_weighted must be computed with
        N's weights - never silently drift to N+1."""
        self._open_live_event_for_scoring()
        import db as db_module
        from auth import now_iso
        conn = db_module.get_connection()
        assignment = conn.execute(
            "SELECT * FROM assignments WHERE event_id='evt_live_2026' LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(assignment, "seed must provide a live-event assignment")

        import auth as auth_module
        token = auth_module.create_session(conn, assignment["judge_id"])

        original_rubric = conn.execute(
            "SELECT id, version FROM rubric_versions WHERE event_id='evt_live_2026' "
            "ORDER BY version DESC LIMIT 1"
        ).fetchone()
        conn.close()

        create_resp = self.client.post(
            f"/judge/review/{assignment['project_id']}",
            data={"criterion_functionality": "2", "criterion_quality": "2",
                  "criterion_innovation": "2"},
            headers=self.auth_header(token),
        )
        self.assertEqual(create_resp.status_code, 302)

        conn = db_module.get_connection()
        score_before = conn.execute(
            "SELECT * FROM scores WHERE judge_id=? AND project_id=?",
            (assignment["judge_id"], assignment["project_id"]),
        ).fetchone()
        self.assertEqual(score_before["rubric_version_id"], original_rubric["id"])
        original_raw = score_before["raw_weighted"]

        new_rubric_id = "evt_live_2026_rubric_v2"
        conn.execute(
            "INSERT INTO rubric_versions (id, event_id, version, created_at) "
            "VALUES (?, 'evt_live_2026', ?, ?)",
            (new_rubric_id, original_rubric["version"] + 1, now_iso()),
        )
        for key, weight in (("functionality", 0.1), ("quality", 0.1), ("innovation", 0.8)):
            conn.execute(
                "INSERT INTO rubric_criteria (id, rubric_version_id, key, label, "
                "weight, min_value, max_value) VALUES (?, ?, ?, ?, ?, 1, 5)",
                (f"{new_rubric_id}_{key}", new_rubric_id, key, key, weight),
            )
        conn.commit()
        conn.close()

        update_resp = self.client.post(
            f"/judge/review/{assignment['project_id']}",
            data={"criterion_functionality": "2", "criterion_quality": "2",
                  "criterion_innovation": "2"},
            headers=self.auth_header(token),
        )
        self.assertEqual(update_resp.status_code, 302)

        conn = db_module.get_connection()
        score_after = conn.execute(
            "SELECT * FROM scores WHERE judge_id=? AND project_id=?",
            (assignment["judge_id"], assignment["project_id"]),
        ).fetchone()
        conn.close()

        self.assertEqual(score_after["rubric_version_id"], original_rubric["id"])
        self.assertNotEqual(score_after["rubric_version_id"], new_rubric_id)
        self.assertAlmostEqual(score_after["raw_weighted"], original_raw)
        self.assertAlmostEqual(score_after["raw_weighted"], 2.0)


if __name__ == "__main__":
    unittest.main()
