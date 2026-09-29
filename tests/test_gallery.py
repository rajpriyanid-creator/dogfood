import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestGallery(VerdictLedgerTestCase):

    def test_public_access_no_auth_needed(self):
        resp = self.client.get("/projects")
        self.assertEqual(resp.status_code, 200)

    def test_fixture_projects_visible(self):
        resp = self.client.get("/projects")
        body = resp.data.decode("utf-8")
        for title in ("Glass Signal", "Small Meadow", "Deep Compass"):
            self.assertIn(title, body)

    def test_filtering_by_track(self):
        import db as db_module
        conn = db_module.get_connection()
        track = conn.execute("SELECT id FROM tracks WHERE event_id='evt_01' LIMIT 1").fetchone()
        conn.close()
        resp = self.client.get(f"/projects?track={track['id']}")
        self.assertEqual(resp.status_code, 200)

    def test_search_by_title(self):
        resp = self.client.get("/projects?q=Glass")
        body = resp.data.decode("utf-8")
        self.assertIn("Glass Signal", body)

    def test_search_excludes_non_matching(self):
        resp = self.client.get("/projects?q=zzz_no_such_project_zzz")
        body = resp.data.decode("utf-8")
        self.assertNotIn("Glass Signal", body)

    def test_draft_projects_not_shown_in_gallery(self):
        """Only submitted projects appear publicly; drafts stay hidden."""
        import db as db_module
        conn = db_module.get_connection()
        draft = conn.execute("SELECT title FROM projects WHERE status='draft' LIMIT 1").fetchone()
        conn.close()
        if draft:
            resp = self.client.get("/projects")
            body = resp.data.decode("utf-8")
            self.assertNotIn(draft["title"], body)


if __name__ == "__main__":
    unittest.main()
