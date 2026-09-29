import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestAuthentication(VerdictLedgerTestCase):

    def test_valid_session_resolves_identity(self):
        resp = self.client.get("/api/judge/scores", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 200)

    def test_invalid_session_rejected(self):
        resp = self.client.get("/api/judge/scores",
                                headers=self.auth_header("not-a-real-token"))
        self.assertEqual(resp.status_code, 401)

    def test_no_session_rejected_on_protected_route(self):
        resp = self.client.get("/api/judge/scores")
        self.assertEqual(resp.status_code, 401)

    def test_role_resolution_is_server_side(self):
        """The role used for authorization must come from the users table
        via the session, not from anything the client sends."""
        # A judge token cannot access organizer-only routes even though
        # nothing in the request claims a role explicitly - there is no
        # client-supplied role field for it to lie about.
        resp = self.client.get("/organizer", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 403)

    def test_login_with_correct_password(self):
        resp = self.client.post("/login", data={
            "email": "organizer@verdictledger.local",
            "password": "organizer-demo",
        })
        self.assertIn(resp.status_code, (302, 200))
        self.assertIn("session", resp.headers.get("Set-Cookie", ""))

    def test_login_with_wrong_password_rejected(self):
        resp = self.client.post("/login", data={
            "email": "organizer@verdictledger.local",
            "password": "wrong-password",
        })
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
