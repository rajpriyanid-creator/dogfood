import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestAuthorization(VerdictLedgerTestCase):

    def test_participant_blocked_from_judge_endpoint(self):
        resp = self.client.get("/api/judge/scores", headers=self.auth_header(self.PARTICIPANT))
        self.assertIn(resp.status_code, (401, 403))

    def test_judge_blocked_from_organizer_endpoint(self):
        resp = self.client.get("/organizer", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 403)

    def test_judge_a_can_see_own_scores(self):
        resp = self.client.get("/api/judge/scores", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["judge_id"], "jdg_01")

    def test_judge_b_cannot_see_judge_a_via_query_param(self):
        resp = self.client.get("/api/judge/scores?judge=jdg_01",
                                headers=self.auth_header(self.JUDGE_B))
        self.assertIn(resp.status_code, (401, 403))

    def test_judge_b_cannot_see_judge_a_via_path_form(self):
        resp = self.client.get("/api/judges/jdg_01/scores",
                                headers=self.auth_header(self.JUDGE_B))
        self.assertIn(resp.status_code, (401, 403))

    def test_idor_forged_own_query_param_still_works(self):
        """Sanity check: a judge asking for their OWN id via the query
        param (not an attack) should still succeed."""
        resp = self.client.get("/api/judge/scores?judge=jdg_01",
                                headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 200)

    def test_unassigned_project_access_fails(self):
        """jdg_02 (judge_b) reviewing a project they were never assigned
        must be refused, even though they are a judge in good standing."""
        # Find a project jdg_02 is NOT assigned to.
        import sqlite3
        conn = sqlite3.connect(os.environ["VERDICT_LEDGER_DB"])
        row = conn.execute(
            "SELECT id FROM projects WHERE event_id='evt_01' AND id NOT IN "
            "(SELECT project_id FROM assignments WHERE judge_id='jdg_02') LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "expected at least one unassigned project for jdg_02")
        project_id = row[0]

        resp = self.client.post(f"/judge/review/{project_id}",
                                 data={"criterion_functionality": "5"},
                                 headers=self.auth_header(self.JUDGE_B))
        self.assertEqual(resp.status_code, 403)

    def test_csv_export_requires_organizer(self):
        resp = self.client.get("/api/export.csv?event=evt_01",
                                headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 403)

    def test_csv_export_organizer_succeeds(self):
        resp = self.client.get("/api/export.csv?event=evt_01",
                                headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 200)

    def test_visitor_cannot_reach_judge_progress(self):
        resp = self.client.get("/judge/progress")
        self.assertEqual(resp.status_code, 401)

    def test_cross_team_project_submission_is_isolated(self):
        """A participant's submission is always attributed to the team
        their session belongs to - there is no client-supplied team_id
        that lets them submit on behalf of a team they are not on.

        This is the audited fix for a test that previously only passed
        because the fixture event happens to be closed (so the request
        failed for an unrelated reason - the deadline - without ever
        actually exercising the team-spoof condition). Now that draft
        creation is not deadline-gated, this asserts the real claim
        directly: the resulting project is attached to the caller's own
        team, and never to the spoofed team_id, regardless of whether
        the request otherwise succeeds.
        """
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        member = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1").fetchone()
        token = auth_module.create_session(conn, member["user_id"])
        conn.close()

        resp = self.client.post("/projects/new", json={
            "title": "hostile-probe",
            "team_id": "live_tm_2",  # attempted spoof; must be ignored
        }, headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 201)
        project_id = resp.get_json()["id"]

        conn = db_module.get_connection()
        row = conn.execute("SELECT team_id FROM projects WHERE id=?", (project_id,)).fetchone()
        conn.close()
        self.assertEqual(row["team_id"], "live_tm_1")
        self.assertNotEqual(row["team_id"], "live_tm_2")

    def test_cross_team_submit_of_someone_elses_draft_fails(self):
        """A participant cannot submit (or edit) a draft belonging to a
        team they are not a member of, even if they guess or are given
        its id."""
        import db as db_module
        from auth import now_iso
        conn = db_module.get_connection()
        conn.execute(
            "INSERT INTO projects (id, event_id, team_id, track_id, title, "
            "summary, repo_url, status, submitted_at, created_at, updated_at) "
            "VALUES ('cross_team_probe', 'evt_live_2026', 'live_tm_2', NULL, "
            "'Not yours', NULL, NULL, 'draft', NULL, ?, ?)",
            (now_iso(), now_iso()),
        )
        conn.commit()
        conn.close()

        # PARTICIPANT is not a member of live_tm_2.
        resp = self.client.post("/projects/cross_team_probe/submit",
                                 headers=self.auth_header(self.PARTICIPANT))
        self.assertEqual(resp.status_code, 404)

        resp2 = self.client.post("/projects/cross_team_probe/edit",
                                  json={"title": "hijacked"},
                                  headers=self.auth_header(self.PARTICIPANT))
        self.assertEqual(resp2.status_code, 404)


if __name__ == "__main__":
    unittest.main()
