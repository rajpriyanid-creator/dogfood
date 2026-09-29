import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import re
import unittest
from base import VerdictLedgerTestCase


class TestParticipantBrowserFlow(VerdictLedgerTestCase):
    """The draft -> edit -> submit lifecycle driven the way a browser
    drives it: form-encoded posts, redirects, and rendered HTML. The
    JSON-API tests in test_deadlines.py prove the backend; these prove
    the feature is actually reachable by a person. (An earlier revision
    had a correct backend but a UI whose only form still posted a
    one-shot 'Submit project', so no browser user could ever edit or
    submit a draft.)"""

    def _live_token(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        member = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1").fetchone()
        token = auth_module.create_session(conn, member["user_id"])
        conn.close()
        return token

    def _other_team_token(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        member = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_2' LIMIT 1").fetchone()
        token = auth_module.create_session(conn, member["user_id"])
        conn.close()
        return token

    def _create_draft_via_form(self, token, title="Browser Draft"):
        resp = self.client.post("/projects/new",
                                data={"title": title, "summary": "first"},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 302)
        m = re.search(r"/projects/(prj_[0-9a-f]+)", resp.headers["Location"])
        self.assertIsNotNone(m, resp.headers["Location"])
        return m.group(1)

    def test_new_project_form_creates_a_draft_not_a_submission(self):
        token = self._live_token()
        pid = self._create_draft_via_form(token)
        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT status, submitted_at FROM projects WHERE id=?", (pid,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "draft")
        self.assertIsNone(row["submitted_at"])

    def test_new_project_page_has_no_submit_button_yet(self):
        token = self._live_token()
        page = self.client.get("/projects/new", headers=self.auth_header(token)).data.decode()
        self.assertIn("Save draft", page)
        self.assertNotIn("Submit project", page)

    def test_draft_owner_sees_edit_link_on_detail_and_dashboard(self):
        token = self._live_token()
        pid = self._create_draft_via_form(token)
        detail = self.client.get(f"/projects/{pid}", headers=self.auth_header(token)).data.decode()
        self.assertIn(f"/projects/{pid}/edit", detail)
        self.assertIn("DRAFT", detail)
        dash = self.client.get("/team", headers=self.auth_header(token)).data.decode()
        self.assertIn(f"/projects/{pid}/edit", dash)

    def test_edit_page_prefills_and_offers_save_and_submit(self):
        token = self._live_token()
        pid = self._create_draft_via_form(token, title="Prefilled Title")
        page = self.client.get(f"/projects/{pid}/edit", headers=self.auth_header(token)).data.decode()
        self.assertIn('value="Prefilled Title"', page)
        self.assertIn("Save draft", page)
        self.assertIn(f"/projects/{pid}/submit", page)

    def test_full_browser_lifecycle_edit_then_submit(self):
        token = self._live_token()
        pid = self._create_draft_via_form(token, title="v1")

        edit = self.client.post(f"/projects/{pid}/edit",
                                data={"title": "v2", "summary": "edited", "repo_url": "https://example.org/r"},
                                headers=self.auth_header(token))
        self.assertEqual(edit.status_code, 302)

        submit = self.client.post(f"/projects/{pid}/submit", data={}, headers=self.auth_header(token))
        self.assertEqual(submit.status_code, 302, "a form-encoded submit should redirect, not return JSON")

        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT title, status, submitted_at FROM projects WHERE id=?", (pid,)).fetchone()
        conn.close()
        self.assertEqual((row["title"], row["status"]), ("v2", "submitted"))
        self.assertIsNotNone(row["submitted_at"])

    def test_submitted_project_becomes_public_and_loses_edit_link(self):
        token = self._live_token()
        pid = self._create_draft_via_form(token, title="Goes Public")
        self.assertEqual(self.client.get(f"/projects/{pid}").status_code, 404)  # draft: hidden
        self.client.post(f"/projects/{pid}/submit", data={}, headers=self.auth_header(token))

        anon = self.client.get(f"/projects/{pid}")
        self.assertEqual(anon.status_code, 200)
        self.assertIn(b"Goes Public", anon.data)
        owner = self.client.get(f"/projects/{pid}", headers=self.auth_header(token)).data.decode()
        self.assertNotIn(f"/projects/{pid}/edit", owner)
        gallery = self.client.get("/projects?q=Goes+Public").data.decode()
        self.assertIn("Goes Public", gallery)

    def test_other_teams_member_sees_no_edit_link_and_cannot_open_editor(self):
        owner = self._live_token()
        pid = self._create_draft_via_form(owner)
        other = self._other_team_token()
        self.assertEqual(self.client.get(f"/projects/{pid}", headers=self.auth_header(other)).status_code, 404)
        self.assertEqual(self.client.get(f"/projects/{pid}/edit", headers=self.auth_header(other)).status_code, 404)
        self.assertEqual(self.client.post(f"/projects/{pid}/edit", data={"title": "hijack"},
                                          headers=self.auth_header(other)).status_code, 404)

    def test_dashboard_hides_edit_links_after_close(self):
        token = self._live_token()
        pid = self._create_draft_via_form(token)
        import db as db_module
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET submissions_close=? WHERE id='evt_live_2026'", (past,))
        conn.commit()
        conn.close()
        dash = self.client.get("/team", headers=self.auth_header(token)).data.decode()
        self.assertIn("SUBMISSION CLOSED", dash)
        self.assertNotIn(f"/projects/{pid}/edit", dash)

    def test_no_none_or_undefined_leaks_into_draft_pages(self):
        token = self._live_token()
        pid = self._create_draft_via_form(token)  # summary given, repo_url/track absent
        for path in (f"/projects/{pid}", f"/projects/{pid}/edit", "/team", "/projects/new"):
            body = self.client.get(path, headers=self.auth_header(token)).data.decode()
            self.assertNotIn(">None<", body, path)
            self.assertNotIn('value="None"', body, path)
            self.assertNotIn("undefined", body, path)


if __name__ == "__main__":
    unittest.main()
