import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import re
import unittest
from base import VerdictLedgerTestCase


def _past(hours=1):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestPublishGating(VerdictLedgerTestCase):

    def _publish(self, event_id, token=None):
        return self.client.post(f"/organizer/publish/{event_id}",
                                headers=self.api_header(token or self.ORGANIZER))

    def _unpublish_fixture(self):
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET publish_state='draft' WHERE id='evt_01'")
        conn.commit()
        conn.close()

    def _normalize(self, event_id="evt_01"):
        return self.client.post(f"/organizer/normalize/{event_id}", headers=self.api_header(self.ORGANIZER))

    def test_cannot_publish_before_normalization(self):
        self._unpublish_fixture()
        resp = self._publish("evt_01")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("normalization", resp.get_json()["error"])
        import db as db_module
        conn = db_module.get_connection()
        state = conn.execute("SELECT publish_state FROM events WHERE id='evt_01'").fetchone()["publish_state"]
        conn.close()
        self.assertEqual(state, "draft", "a refused publish must not change state")

    def test_cannot_publish_while_event_still_open(self):
        resp = self._publish("evt_live_2026")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("still open", resp.get_json()["error"])

    def test_publish_succeeds_after_normalization_and_freezes_scoring(self):
        self._unpublish_fixture()
        self.assertEqual(self._normalize().status_code, 201)
        resp = self._publish("evt_01")
        self.assertEqual(resp.status_code, 200)
        import db as db_module
        conn = db_module.get_connection()
        state = conn.execute("SELECT publish_state FROM events WHERE id='evt_01'").fetchone()["publish_state"]
        conn.close()
        self.assertEqual(state, "published")

    def test_publish_twice_is_refused_and_not_double_audited(self):
        self._unpublish_fixture()
        self._normalize()
        self.assertEqual(self._publish("evt_01").status_code, 200)
        self.assertEqual(self._publish("evt_01").status_code, 409)
        import db as db_module
        conn = db_module.get_connection()
        ok = conn.execute("SELECT COUNT(*) AS n FROM audit_events WHERE action='result_published' "
                          "AND result='ok'").fetchone()["n"]
        conn.close()
        self.assertEqual(ok, 1)

    def test_refused_publish_is_audited_as_denied(self):
        self._unpublish_fixture()
        self._publish("evt_01")
        import db as db_module
        conn = db_module.get_connection()
        row = conn.execute("SELECT result FROM audit_events WHERE action='result_published'").fetchone()
        conn.close()
        self.assertEqual(row["result"], "denied")

    def test_unknown_event_404(self):
        self.assertEqual(self._publish("evt_nope").status_code, 404)

    def test_judge_and_participant_cannot_publish(self):
        for token in (self.JUDGE_A, self.PARTICIPANT):
            self.assertEqual(self._publish("evt_01", token).status_code, 403)

    def test_organizer_room_hides_publish_button_until_possible(self):
        self._unpublish_fixture()
        page = self.client.get("/organizer", headers=self.auth_header(self.ORGANIZER)).data.decode()
        self.assertNotIn("/organizer/publish/evt_01", page)
        self._normalize()
        page = self.client.get("/organizer", headers=self.auth_header(self.ORGANIZER)).data.decode()
        self.assertIn("/organizer/publish/evt_01", page)


class TestFullLifecycleOnLiveEvent(VerdictLedgerTestCase):
    """The product's headline flow, run end to end through the real
    routes with no direct database shortcuts for the *actions* (only
    the passage of time is simulated, by moving the close date):

        participant creates draft -> edits -> submits
        organizer invites judge + generates assignments
        judge scores          (event now closed)
        organizer normalizes -> publishes
        public sees results state; scoring is then frozen.
    """

    def test_end_to_end(self):
        import auth as auth_module
        import db as db_module
        B = self.client

        # -- participant: create, edit, submit (event open) --
        conn = db_module.get_connection()
        member = conn.execute("SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1").fetchone()
        p_token = auth_module.create_session(conn, member["user_id"])
        track = conn.execute("SELECT id FROM tracks WHERE event_id='evt_live_2026' LIMIT 1").fetchone()["id"]
        conn.close()

        r = B.post("/projects/new", json={"title": "Lifecycle Project", "track_id": track},
                   headers=self.api_header(p_token))
        self.assertEqual(r.status_code, 201)
        pid = r.get_json()["id"]
        self.assertEqual(B.post(f"/projects/{pid}/edit", json={"title": "Lifecycle Project v2", "track_id": track},
                                headers=self.api_header(p_token)).status_code, 200)
        self.assertEqual(B.post(f"/projects/{pid}/submit", headers=self.api_header(p_token)).status_code, 200)

        # -- organizer: invite a judge eligible for that track, submit the
        #    seeded drafts too so assignment has a pool to work on --
        r = B.post("/organizer/events/evt_live_2026/judges",
                   json={"name": "Lifecycle Judge", "email": "lifecycle.judge@example.org", "track_ids": [track]},
                   headers=self.api_header(self.ORGANIZER))
        self.assertEqual(r.status_code, 201)
        judge_id = r.get_json()["id"]

        # -- close submissions (time passes), then assign --
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET submissions_close=? WHERE id='evt_live_2026'", (_past(),))
        judge_token = auth_module.create_session(conn, judge_id)
        conn.close()

        r = B.post("/organizer/events/evt_live_2026/assignments", json={"target_reviews": 3},
                   headers=self.api_header(self.ORGANIZER))
        self.assertEqual(r.status_code, 200)

        conn = db_module.get_connection()
        mine = conn.execute("SELECT project_id FROM assignments WHERE judge_id=? AND event_id='evt_live_2026'",
                            (judge_id,)).fetchall()
        conn.close()
        self.assertIn(pid, [m["project_id"] for m in mine],
                      "the invited, track-eligible judge must be assigned the submitted project")

        # -- judge: sees only assigned work, scores it --
        page = B.get("/judge", headers=self.auth_header(judge_token)).data.decode()
        self.assertIn("Lifecycle Project v2", page)
        resp = B.post(f"/judge/review/{pid}",
                      data={"criterion_functionality": "4", "criterion_quality": "5", "criterion_innovation": "3"},
                      headers=self.auth_header(judge_token))
        self.assertEqual(resp.status_code, 302)

        # -- organizer: normalize, publish --
        r = B.post("/organizer/normalize/evt_live_2026", headers=self.api_header(self.ORGANIZER))
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        results = B.get("/organizer/results/evt_live_2026", headers=self.auth_header(self.ORGANIZER))
        self.assertIn(b"Lifecycle Project v2", results.data)
        self.assertEqual(B.post("/organizer/publish/evt_live_2026", headers=self.api_header(self.ORGANIZER)).status_code, 200)

        # -- after publish: scoring is frozen --
        again = B.post(f"/judge/review/{pid}",
                       data={"criterion_functionality": "1", "criterion_quality": "1", "criterion_innovation": "1"},
                       headers=self.auth_header(judge_token))
        self.assertEqual(again.status_code, 403)

        # -- and the whole story is in an intact audit chain --
        import audit as audit_module
        conn = db_module.get_connection()
        actions = {r["action"] for r in conn.execute("SELECT action FROM audit_events").fetchall()}
        chain_ok = audit_module.verify_chain(conn)
        conn.close()
        for expected in ("project_created", "project_edited", "project_submitted", "judge_invited",
                         "assignments_generated", "score_submitted", "normalization_executed",
                         "result_published"):
            self.assertIn(expected, actions)
        self.assertTrue(chain_ok)


if __name__ == "__main__":
    unittest.main()
