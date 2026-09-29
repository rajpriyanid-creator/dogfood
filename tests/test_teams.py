import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestTeams(VerdictLedgerTestCase):
    """Team creation/joining rules. The audit flagged the previous
    version of this file: two tests were the same 'bad invite code ->
    404' check under different names, and none exercised an actual
    invitation between two distinct users or any membership rule."""

    def _new_participant(self, email):
        """A fresh participant account (not on any team) with a session."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        uid = "prt_" + email.split("@")[0]
        conn.execute(
            "INSERT INTO users (id, email, name, role, password_hash, created_at) "
            "VALUES (?, ?, ?, 'participant', ?, datetime('now'))",
            (uid, email, uid, auth_module.hash_password("x")),
        )
        conn.commit()
        token = auth_module.create_session(conn, uid)
        conn.close()
        return uid, token

    def _members(self, team_id):
        import db as db_module
        conn = db_module.get_connection()
        rows = conn.execute("SELECT user_id FROM team_members WHERE team_id=?",
                            (team_id,)).fetchall()
        conn.close()
        return {r["user_id"] for r in rows}

    def _team_code(self, team_id):
        import db as db_module
        conn = db_module.get_connection()
        code = conn.execute("SELECT invite_code FROM teams WHERE id=?", (team_id,)).fetchone()["invite_code"]
        conn.close()
        return code

    def test_invitation_between_two_distinct_users(self):
        """A brand-new participant joins an existing team using its real
        invite code; membership then contains both users."""
        before = self._members("live_tm_1")
        uid, token = self._new_participant("newcomer@example.org")
        self.assertNotIn(uid, before)

        resp = self.client.post("/team/join",
                                data={"invite_code": self._team_code("live_tm_1")},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 302)

        after = self._members("live_tm_1")
        self.assertIn(uid, after)
        self.assertTrue(before.issubset(after), "existing members must be preserved")

    def test_wrong_invite_code_does_not_add_member(self):
        uid, token = self._new_participant("wrongcode@example.org")
        resp = self.client.post("/team/join", data={"invite_code": "not-a-real-code"},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 404)
        for team in ("live_tm_1", "live_tm_2"):
            self.assertNotIn(uid, self._members(team))

    def test_one_teams_code_does_not_grant_another_team(self):
        """Joining team 1 with team 1's code must not add the user to
        team 2 (guards against a join that keys on the wrong row)."""
        uid, token = self._new_participant("scoped@example.org")
        self.client.post("/team/join", data={"invite_code": self._team_code("live_tm_1")},
                         headers=self.auth_header(token))
        self.assertIn(uid, self._members("live_tm_1"))
        self.assertNotIn(uid, self._members("live_tm_2"))

    def test_cannot_join_second_team_in_same_event(self):
        uid, token = self._new_participant("twoteams@example.org")
        first = self.client.post("/team/join",
                                 data={"invite_code": self._team_code("live_tm_1")},
                                 headers=self.auth_header(token))
        self.assertEqual(first.status_code, 302)

        second = self.client.post("/team/join",
                                  data={"invite_code": self._team_code("live_tm_2")},
                                  headers=self.auth_header(token))
        self.assertEqual(second.status_code, 409)
        self.assertNotIn(uid, self._members("live_tm_2"))

    def test_rejoining_own_team_is_harmless(self):
        uid, token = self._new_participant("rejoin@example.org")
        code = self._team_code("live_tm_1")
        self.client.post("/team/join", data={"invite_code": code}, headers=self.auth_header(token))
        again = self.client.post("/team/join", data={"invite_code": code},
                                 headers=self.auth_header(token))
        self.assertEqual(again.status_code, 302)

    def test_cannot_join_team_of_closed_event(self):
        """The fixture event is closed; its teams accept no new members
        even with a valid invite code."""
        import db as db_module
        conn = db_module.get_connection()
        team = conn.execute("SELECT id, invite_code FROM teams WHERE event_id='evt_01' LIMIT 1").fetchone()
        conn.close()
        uid, token = self._new_participant("toolate@example.org")
        resp = self.client.post("/team/join", data={"invite_code": team["invite_code"]},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn(uid, self._members(team["id"]))

    def test_create_team_makes_creator_owner_and_member(self):
        uid, token = self._new_participant("founder@example.org")
        resp = self.client.post("/team/create",
                                data={"name": "Founders", "event_id": "evt_live_2026"},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 302)
        import db as db_module
        conn = db_module.get_connection()
        team = conn.execute("SELECT * FROM teams WHERE name='Founders'").fetchone()
        conn.close()
        self.assertEqual(team["owner_id"], uid)
        self.assertIn(uid, self._members(team["id"]))
        self.assertTrue(team["invite_code"])

    def test_cannot_create_team_on_closed_event(self):
        uid, token = self._new_participant("latefounder@example.org")
        resp = self.client.post("/team/create",
                                data={"name": "TooLate", "event_id": "evt_01"},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 403)

    def test_cannot_create_team_on_unknown_event(self):
        uid, token = self._new_participant("nowhere@example.org")
        resp = self.client.post("/team/create",
                                data={"name": "Ghost", "event_id": "evt_nope"},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 404)

    def test_cannot_create_second_team_in_same_event(self):
        uid, token = self._new_participant("greedy@example.org")
        self.client.post("/team/create", data={"name": "One", "event_id": "evt_live_2026"},
                         headers=self.auth_header(token))
        resp = self.client.post("/team/create", data={"name": "Two", "event_id": "evt_live_2026"},
                                headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 409)

    def test_join_denials_and_successes_are_audited(self):
        uid, token = self._new_participant("audited@example.org")
        self.client.post("/team/join", data={"invite_code": "bogus"}, headers=self.auth_header(token))
        self.client.post("/team/join", data={"invite_code": self._team_code("live_tm_1")},
                         headers=self.auth_header(token))
        import db as db_module
        conn = db_module.get_connection()
        rows = conn.execute("SELECT result FROM audit_events WHERE action='team_joined' "
                            "AND actor_id=? ORDER BY seq", (uid,)).fetchall()
        conn.close()
        self.assertEqual([r["result"] for r in rows], ["denied", "ok"])


if __name__ == "__main__":
    unittest.main()
