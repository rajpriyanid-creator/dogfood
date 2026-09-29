import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestDraftVisibility(VerdictLedgerTestCase):
    """Regression coverage for the audited CRITICAL defect: /projects/<id>
    had no visibility check at all, so a draft project's detail page was
    fully public by predictable ID."""

    def _make_draft_project(self):
        """Create a real draft (not submitted) project directly against
        the schema, owned by live_tm_1 (whose member is what
        self.PARTICIPANT resolves to after joining, or we can insert a
        member directly)."""
        import db as db_module
        from auth import now_iso
        conn = db_module.get_connection()
        conn.execute(
            "INSERT INTO projects (id, event_id, team_id, track_id, title, "
            "summary, repo_url, status, submitted_at, created_at, updated_at) "
            "VALUES ('draft_probe_1', 'evt_live_2026', 'live_tm_1', NULL, "
            "'Secret Draft', 'not yet public', NULL, 'draft', NULL, ?, ?)",
            (now_iso(), now_iso()),
        )
        conn.commit()
        conn.close()

    def test_draft_not_visible_to_anonymous_visitor(self):
        self._make_draft_project()
        resp = self.client.get("/projects/draft_probe_1")
        self.assertEqual(resp.status_code, 404)

    def test_draft_not_visible_to_unrelated_participant(self):
        self._make_draft_project()
        resp = self.client.get("/projects/draft_probe_1",
                                headers=self.auth_header(self.PARTICIPANT))
        self.assertEqual(resp.status_code, 404)

    def test_draft_not_visible_to_judge(self):
        self._make_draft_project()
        resp = self.client.get("/projects/draft_probe_1",
                                headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 404)

    def test_draft_visible_to_own_team_member(self):
        self._make_draft_project()
        import db as db_module
        import auth as auth_module
        conn = db_module.get_connection()
        row = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1"
        ).fetchone()
        token = auth_module.create_session(conn, row["user_id"])
        conn.close()

        resp = self.client.get("/projects/draft_probe_1", headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Secret Draft", resp.data)

    def test_draft_visible_to_organizer(self):
        self._make_draft_project()
        resp = self.client.get("/projects/draft_probe_1",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 200)

    def test_submitted_project_remains_public(self):
        resp = self.client.get("/projects/prj_01")
        self.assertEqual(resp.status_code, 200)

    def test_draft_excluded_from_duplicate_detection_of_others(self):
        """A draft must not appear as a 'duplicate candidate' on another
        project's public page - it isn't public yet itself."""
        import db as db_module
        from auth import now_iso
        conn = db_module.get_connection()
        # Give the draft the same title as a real submitted fixture project.
        conn.execute(
            "INSERT INTO projects (id, event_id, team_id, track_id, title, "
            "summary, repo_url, status, submitted_at, created_at, updated_at) "
            "VALUES ('draft_probe_2', 'evt_01', "
            "(SELECT team_id FROM projects WHERE id='prj_02'), NULL, "
            "(SELECT title FROM projects WHERE id='prj_01'), 'x', NULL, "
            "'draft', NULL, ?, ?)",
            (now_iso(), now_iso()),
        )
        conn.commit()
        conn.close()

        resp = self.client.get("/projects/prj_01")
        body = resp.data.decode("utf-8")
        self.assertNotIn("draft_probe_2", body)


if __name__ == "__main__":
    unittest.main()
