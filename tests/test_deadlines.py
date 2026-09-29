import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestDeadlines(VerdictLedgerTestCase):
    """These exercise the real draft -> edit -> submit lifecycle
    (app.py: submit_project / edit_project / submit_draft), not a
    one-shot submission - see the audited T1 lifecycle defect."""

    def _make_live_participant_token(self):
        """A fresh session for the seeded live-event participant, whose
        event (evt_live_2026) is open."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1"
        ).fetchone()
        token = auth_module.create_session(conn, row["user_id"])
        conn.close()
        return token

    def _close_live_event(self):
        """Move evt_live_2026's submissions_close into the past, as the
        passage of time would in production."""
        import db as db_module
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET submissions_close=? WHERE id='evt_live_2026'", (past,))
        conn.commit()
        conn.close()

    def _live_track(self):
        import db as db_module
        conn = db_module.get_connection()
        track = conn.execute("SELECT id FROM tracks WHERE event_id='evt_live_2026' LIMIT 1").fetchone()["id"]
        conn.close()
        return track

    # --- creating a project on a closed event is refused -----------------

    def test_creating_project_on_closed_event_is_refused(self):
        """The official checker's own T1 test, reproduced: a participant
        on the closed fixture event POSTs to /projects/new and must get
        a 4xx. (Regression: an earlier revision allowed draft creation
        after close, which the official checker caught as a bypass.)"""
        resp = self.client.post("/projects/new", json={"title": "late-probe"},
                                 headers=self.auth_header(self.PARTICIPANT))
        self.assertEqual(resp.status_code, 403)

        import db as db_module
        conn = db_module.get_connection()
        n = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE title='late-probe'").fetchone()["n"]
        conn.close()
        self.assertEqual(n, 0, "a refused request must not have created a row")

    def test_creating_project_before_event_starts_is_refused(self):
        import db as db_module
        from datetime import datetime, timezone, timedelta
        token = self._make_live_participant_token()
        future = (datetime.now(timezone.utc) + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET start_at=? WHERE id='evt_live_2026'", (future,))
        conn.commit()
        conn.close()
        resp = self.client.post("/projects/new", json={"title": "too-early"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 403)

    # --- the actual deadline gate: submitting a draft ---------------------

    def test_submit_after_deadline_fails(self):
        """A draft created while the event was open cannot be submitted
        once the deadline has passed."""
        token = self._make_live_participant_token()
        create = self.client.post("/projects/new", json={"title": "late-submit-probe", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        self.assertEqual(create.status_code, 201)
        project_id = create.get_json()["id"]
        self._close_live_event()

        resp = self.client.post(f"/projects/{project_id}/submit",
                                 headers=self.api_header(token))
        self.assertEqual(resp.status_code, 403)

        # And the project must still be in draft status in the DB - a
        # failed submit must not have silently flipped it anyway.
        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "draft")

    def test_submit_before_deadline_succeeds(self):
        token = self._make_live_participant_token()
        create = self.client.post("/projects/new", json={"title": "fresh-live-submission", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        self.assertEqual(create.status_code, 201)
        project_id = create.get_json()["id"]

        resp = self.client.post(f"/projects/{project_id}/submit",
                                 headers=self.api_header(token))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "submitted")

    def test_deadline_is_computed_server_side_not_client_supplied(self):
        """There is no request field that lets the client assert the
        event is still open; server always re-derives it from stored
        submissions_close vs its own clock."""
        token = self._make_live_participant_token()
        create = self.client.post("/projects/new", json={"title": "spoofed-open-draft", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        project_id = create.get_json()["id"]
        self._close_live_event()

        resp = self.client.post(f"/projects/{project_id}/submit", json={
            "event_status": "open",   # not a real field; must be ignored
            "submissions_close": "2099-01-01T00:00:00Z",  # ignored too
        }, headers=self.api_header(token))
        self.assertEqual(resp.status_code, 403)

    # --- edit after deadline ------------------------------------------

    def test_edit_after_deadline_rejected(self):
        token = self._make_live_participant_token()
        create = self.client.post("/projects/new", json={"title": "edit-me-late", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        project_id = create.get_json()["id"]
        self._close_live_event()

        resp = self.client.post(f"/projects/{project_id}/edit",
                                 json={"title": "edited-after-deadline"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 403)

        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT title FROM projects WHERE id=?", (project_id,)).fetchone()
        conn.close()
        self.assertEqual(row["title"], "edit-me-late", "a refused edit must not change the row")

    def test_edit_before_deadline_succeeds(self):
        token = self._make_live_participant_token()
        create = self.client.post("/projects/new", json={"title": "edit-me-live", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        project_id = create.get_json()["id"]

        resp = self.client.post(f"/projects/{project_id}/edit",
                                 json={"title": "edited title", "summary": "new summary"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 200)

        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT title FROM projects WHERE id=?", (project_id,)).fetchone()
        conn.close()
        self.assertEqual(row["title"], "edited title")

    def test_full_draft_edit_submit_lifecycle(self):
        """create -> edit -> submit, end to end, against the open event."""
        token = self._make_live_participant_token()

        create = self.client.post("/projects/new", json={"title": "v1 title", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        self.assertEqual(create.status_code, 201)
        project_id = create.get_json()["id"]

        edit = self.client.post(f"/projects/{project_id}/edit",
                                 json={"title": "v2 title", "summary": "now with a summary", "track_id": self._live_track()},
                                 headers=self.auth_header(token))
        self.assertEqual(edit.status_code, 200)

        submit = self.client.post(f"/projects/{project_id}/submit",
                                   headers=self.api_header(token))
        self.assertEqual(submit.status_code, 200)

        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT title, status, submitted_at FROM projects WHERE id=?",
                            (project_id,)).fetchone()
        conn.close()
        self.assertEqual(row["title"], "v2 title")
        self.assertEqual(row["status"], "submitted")
        self.assertIsNotNone(row["submitted_at"])

    def test_cannot_edit_after_submit(self):
        token = self._make_live_participant_token()
        create = self.client.post("/projects/new", json={"title": "lock-me", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        project_id = create.get_json()["id"]
        self.client.post(f"/projects/{project_id}/submit", headers=self.api_header(token))

        resp = self.client.post(f"/projects/{project_id}/edit",
                                 json={"title": "too late"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 409)

    def test_cannot_submit_twice(self):
        token = self._make_live_participant_token()
        create = self.client.post("/projects/new", json={"title": "submit-once", "track_id": self._live_track()},
                                   headers=self.auth_header(token))
        project_id = create.get_json()["id"]
        first = self.client.post(f"/projects/{project_id}/submit", headers=self.api_header(token))
        self.assertEqual(first.status_code, 200)

        second = self.client.post(f"/projects/{project_id}/submit", headers=self.api_header(token))
        self.assertEqual(second.status_code, 409)


if __name__ == "__main__":
    unittest.main()
