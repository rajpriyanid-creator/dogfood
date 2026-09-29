import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestTrackValidation(VerdictLedgerTestCase):
    """Regression coverage for the audited HIGH defect: a project could
    be created/edited with a track_id belonging to a different event
    entirely, silently corrupting track-based filtering and judge
    track-eligibility across events."""

    def _live_participant_token(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1"
        ).fetchone()
        token = auth_module.create_session(conn, row["user_id"])
        conn.close()
        return token

    def test_cross_event_track_injection_rejected_on_create(self):
        """live_tm_1 is on evt_live_2026; trk_01 belongs to evt_01. This
        must be rejected, not silently accepted."""
        token = self._live_participant_token()
        resp = self.client.post("/projects/new", json={
            "title": "cross-event-track-probe",
            "track_id": "trk_01",
        }, headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 400)

    def test_same_event_track_accepted_on_create(self):
        token = self._live_participant_token()
        import db as db_module
        conn = db_module.get_connection()
        real_track = conn.execute(
            "SELECT id FROM tracks WHERE event_id='evt_live_2026' LIMIT 1"
        ).fetchone()
        conn.close()

        resp = self.client.post("/projects/new", json={
            "title": "same-event-track-probe",
            "track_id": real_track["id"],
        }, headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 201)

    def test_nonexistent_track_rejected(self):
        token = self._live_participant_token()
        resp = self.client.post("/projects/new", json={
            "title": "fake-track-probe",
            "track_id": "trk_does_not_exist",
        }, headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 400)

    def test_cross_event_track_injection_rejected_on_edit(self):
        token = self._live_participant_token()
        create = self.client.post("/projects/new", json={"title": "edit-track-probe"},
                                   headers=self.auth_header(token))
        project_id = create.get_json()["id"]

        resp = self.client.post(f"/projects/{project_id}/edit", json={
            "title": "edit-track-probe",
            "track_id": "trk_01",
        }, headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 400)

    def test_no_track_is_valid(self):
        """Tracks are optional - omitting one must still work."""
        token = self._live_participant_token()
        resp = self.client.post("/projects/new", json={"title": "no-track-probe"},
                                 headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 201)


if __name__ == "__main__":
    unittest.main()
