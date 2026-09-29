import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestSessionExpiration(VerdictLedgerTestCase):
    """Regression coverage for the audited HIGH defect: sessions never
    expired and logout only cleared the client-side cookie without
    revoking anything server-side, so a captured token kept working
    forever regardless of logout."""

    def test_valid_session_allowed(self):
        resp = self.client.get("/api/judge/scores", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 200)

    def test_expired_session_rejected(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.new_session_token()
        past = "2020-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, 'jdg_01', datetime('now'), ?)",
            (auth_module.hash_token(token), past),
        )
        conn.commit()
        conn.close()

        resp = self.client.get("/api/judge/scores", headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 401)

    def test_future_expiry_still_allowed(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.new_session_token()
        future = "2099-01-01T00:00:00Z"
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, 'jdg_01', datetime('now'), ?)",
            (auth_module.hash_token(token), future),
        )
        conn.commit()
        conn.close()

        resp = self.client.get("/api/judge/scores", headers=self.auth_header(token))
        self.assertEqual(resp.status_code, 200)

    def test_new_login_session_has_a_real_expiry(self):
        """A freshly created session (via create_session, as login()
        uses) is not a forever-token - it must have a non-NULL
        expires_at, unlike the fixed demo bootstrap tokens."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, "jdg_01")
        token_h = auth_module.hash_token(token)
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE token_hash=?", (token_h,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row["expires_at"])

    def test_logout_revokes_session_server_side(self):
        """The core regression: after logging out, the SAME token must
        no longer authenticate anywhere - not just the browser's cookie
        being cleared, but the session row itself being invalidated."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, "jdg_01")
        conn.close()

        resp1 = self.client.get("/api/judge/scores", headers=self.auth_header(token))
        self.assertEqual(resp1.status_code, 200)

        logout_resp = self.client.post("/logout", headers=self.auth_header(token))
        self.assertEqual(logout_resp.status_code, 302)

        resp2 = self.client.get("/api/judge/scores", headers=self.auth_header(token))
        self.assertEqual(resp2.status_code, 401)

    def test_revoked_session_row_is_kept_not_deleted(self):
        """Revocation marks the row, it does not delete it - so the
        prior existence of the session remains inspectable."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, "jdg_01")
        auth_module.revoke_session(conn, token)
        token_h = auth_module.hash_token(token)
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash=?", (token_h,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["revoked_at"])

    def test_invalid_token_rejected(self):
        resp = self.client.get("/api/judge/scores",
                                headers=self.auth_header("not-a-real-token-at-all"))
        self.assertEqual(resp.status_code, 401)

    def test_bootstrap_demo_tokens_never_expire(self):
        """The fixed demo tokens (.dogfood.toml relies on these staying
        valid across restarts) deliberately have expires_at=NULL and
        must keep working."""
        resp = self.client.get("/api/judge/scores", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 200)
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        # The raw token is not stored; look up by its hash.
        token_h = auth_module.hash_token("jdg_a_demo_token")
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE token_hash=?", (token_h,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "bootstrap session row must exist (found by hash)")
        self.assertIsNone(row["expires_at"])

    # ---------------------------------------------------------------
    # New tests: raw token must NOT be stored in the database
    # ---------------------------------------------------------------

    def test_raw_token_not_stored_in_db(self):
        """Regression: the sessions table must NOT contain plaintext
        raw tokens. After creating a session, the raw token string
        must not appear in any row of the sessions table."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, "jdg_01")
        # token_hash column stores the hash, not the raw value
        row = conn.execute(
            "SELECT token_hash FROM sessions WHERE token_hash=?",
            (auth_module.hash_token(token),),
        ).fetchone()
        self.assertIsNotNone(row, "session row must exist (found by hash)")
        self.assertNotEqual(row["token_hash"], token,
                            "the stored token_hash must not equal the raw token")
        conn.close()

    def test_hashed_token_lookup_works(self):
        """resolve_identity() must work with the raw token and correctly
        look up the hashed row."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, "jdg_01")
        identity = auth_module.resolve_identity(conn, token)
        conn.close()
        self.assertIsNotNone(identity)
        self.assertEqual(identity["id"], "jdg_01")

    def test_wrong_raw_token_rejected(self):
        """An almost-identical but wrong raw token must not authenticate
        (it would produce a different hash and find no row)."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, "jdg_01")
        wrong = token + "x"
        identity = auth_module.resolve_identity(conn, wrong)
        conn.close()
        self.assertIsNone(identity)


if __name__ == "__main__":
    unittest.main()
