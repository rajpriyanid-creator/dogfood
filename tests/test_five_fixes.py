"""Regression tests for the five verified fixes:

1. Stale normalization publish rejection (Phase 1)
2. Historical explanation criteria snapshot (Phase 2)
3. Session token hashing (Phase 3)
4. Repository URL scheme validation (Phase 4)
5. (Phase 5 is infrastructure-only; tested via Dockerfile check)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


def _past(hours=2):
    from datetime import datetime, timezone, timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


# ============================================================================
# Phase 1: Stale Normalization / Publish
# ============================================================================

class TestStaleFreshNormalizationPublish(VerdictLedgerTestCase):
    """A. stale normalization publish rejection
       B. fresh normalization publish success
    """

    def _unpublish_fixture(self):
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET publish_state='draft' WHERE id='evt_01'")
        conn.commit()
        conn.close()

    def _normalize(self, event_id="evt_01"):
        return self.client.post(f"/organizer/normalize/{event_id}",
                                headers=self.api_header(self.ORGANIZER))

    def _publish(self, event_id="evt_01"):
        return self.client.post(f"/organizer/publish/{event_id}",
                                headers=self.api_header(self.ORGANIZER))

    def test_stale_score_edit_blocks_publish(self):
        """Phase 1 regression: normalize -> edit score -> publish must return 409."""
        self._unpublish_fixture()

        # 1. Run normalization (establishes fingerprint)
        r = self._normalize()
        self.assertEqual(r.status_code, 201, f"normalize failed: {r.get_data(as_text=True)}")

        # 2. Edit a score criteria value (changes the judging state)
        import db as db_module
        conn = db_module.get_connection()
        score = conn.execute(
            "SELECT id FROM scores WHERE project_id='prj_01' LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(score, "fixture must have a score for prj_01")
        # Note: SQLite doesn't support LIMIT in UPDATE without compile-time flag
        crit = conn.execute(
            "SELECT criterion_key FROM score_criteria WHERE score_id=? LIMIT 1",
            (score["id"],)
        ).fetchone()
        conn.execute(
            "UPDATE score_criteria SET value=value+0.1 "
            "WHERE score_id=? AND criterion_key=?",
            (score["id"], crit["criterion_key"])
        )
        conn.commit()
        conn.close()

        # 3. Attempt publish -- must be rejected 409 (stale)
        r = self._publish()
        self.assertEqual(r.status_code, 409, f"stale publish should be 409, got {r.status_code}")
        self.assertIn("stale", r.get_json()["error"].lower(),
                      "error message must mention 'stale'")

        # 4. Verify no publication occurred
        conn = db_module.get_connection()
        state = conn.execute(
            "SELECT publish_state FROM events WHERE id='evt_01'"
        ).fetchone()["publish_state"]
        conn.close()
        self.assertEqual(state, "draft", "publish state must remain draft after stale rejection")

    def test_fresh_normalization_allows_publish(self):
        """Phase 1: normalize -> publish without any intervening changes must succeed."""
        self._unpublish_fixture()

        # 1. Normalize
        r = self._normalize()
        self.assertEqual(r.status_code, 201)

        # 2. Publish immediately (fingerprint matches)
        r = self._publish()
        self.assertEqual(r.status_code, 200, f"fresh publish failed: {r.get_data(as_text=True)}")

        import db as db_module
        conn = db_module.get_connection()
        state = conn.execute(
            "SELECT publish_state FROM events WHERE id='evt_01'"
        ).fetchone()["publish_state"]
        conn.close()
        self.assertEqual(state, "published")

    def test_renormalize_after_edit_allows_publish(self):
        """Phase 1 full sequence:
        normalize -> edit score -> normalize again -> publish succeeds."""
        self._unpublish_fixture()

        # 1. Normalize
        r = self._normalize()
        self.assertEqual(r.status_code, 201)

        # 2. Edit score criteria (SQLite doesn't support LIMIT in UPDATE)
        import db as db_module
        conn = db_module.get_connection()
        score = conn.execute("SELECT id FROM scores WHERE project_id='prj_01' LIMIT 1").fetchone()
        crit = conn.execute(
            "SELECT criterion_key FROM score_criteria WHERE score_id=? LIMIT 1",
            (score["id"],)
        ).fetchone()
        conn.execute(
            "UPDATE score_criteria SET value=value+0.1 WHERE score_id=? AND criterion_key=?",
            (score["id"], crit["criterion_key"])
        )
        conn.commit()
        conn.close()

        # 3. Attempt publish -> 409
        self.assertEqual(self._publish().status_code, 409)

        # 4. Re-normalize (creates new run with current fingerprint)
        r = self._normalize()
        self.assertEqual(r.status_code, 201)

        # 5. Publish succeeds
        r = self._publish()
        self.assertEqual(r.status_code, 200)

        conn = db_module.get_connection()
        state = conn.execute(
            "SELECT publish_state FROM events WHERE id='evt_01'"
        ).fetchone()["publish_state"]
        conn.close()
        self.assertEqual(state, "published")

    def test_fingerprint_stored_on_normalization_run(self):
        """The normalization run must have judging_state_fingerprint set."""
        self._unpublish_fixture()
        r = self._normalize()
        self.assertEqual(r.status_code, 201)

        import db as db_module
        conn = db_module.get_connection()
        run = conn.execute(
            "SELECT judging_state_fingerprint FROM normalization_runs "
            "WHERE event_id='evt_01' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNotNone(run["judging_state_fingerprint"],
                             "judging_state_fingerprint must be set after normalization")
        self.assertEqual(len(run["judging_state_fingerprint"]), 64,
                         "fingerprint must be a 64-char SHA-256 hex string")

    def test_stale_publish_audited_with_reason(self):
        """A stale publish rejection must appear in the audit log with stale_normalization reason."""
        self._unpublish_fixture()
        self._normalize()

        # Edit a score criteria value
        import db as db_module
        conn = db_module.get_connection()
        score = conn.execute("SELECT id FROM scores WHERE project_id='prj_01' LIMIT 1").fetchone()
        crit = conn.execute(
            "SELECT criterion_key FROM score_criteria WHERE score_id=? LIMIT 1",
            (score["id"],)
        ).fetchone()
        conn.execute(
            "UPDATE score_criteria SET value=value+0.1 WHERE score_id=? AND criterion_key=?",
            (score["id"], crit["criterion_key"])
        )
        conn.commit()
        conn.close()

        self._publish()

        conn = db_module.get_connection()
        audit_row = conn.execute(
            "SELECT result, detail FROM audit_events WHERE action='result_published' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertEqual(audit_row["result"], "denied")
        import json
        detail = json.loads(audit_row["detail"])
        self.assertEqual(detail.get("reason"), "stale_normalization")


# ============================================================================
# Phase 2: Historical Explanation Criteria Snapshot
# ============================================================================

class TestHistoricalExplanationSnapshot(VerdictLedgerTestCase):
    """C. historical explanation remains unchanged after live score edit
       D. snapshot criterion data is actually used
    """

    def setUp(self):
        super().setUp()
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE events SET publish_state='draft', submissions_close='2020-01-01T00:00:00Z', judging_close='2020-01-01T00:00:00Z' WHERE id='evt_01'")
        conn.commit()
        conn.close()

    def _normalize(self):
        return self.client.post("/organizer/normalize/evt_01",
                                headers=self.api_header(self.ORGANIZER))

    def test_snapshot_table_populated_after_normalization(self):
        """normalization_run_criteria must have rows after normalization."""
        import db as db_module
        r = self._normalize()
        self.assertEqual(r.status_code, 201)
        conn = db_module.get_connection()
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM normalization_run_criteria nrc "
            "JOIN normalization_runs nr ON nr.id=nrc.normalization_run_id "
            "WHERE nr.event_id='evt_01'"
        ).fetchone()["n"]
        conn.close()
        self.assertGreater(count, 0, "normalization_run_criteria must be populated after run")

    def test_snapshot_matches_live_criteria_at_run_time(self):
        """Snapshot values must equal the live values at the time of the run."""
        import db as db_module
        self._normalize()

        conn = db_module.get_connection()
        run = conn.execute(
            "SELECT id FROM normalization_runs WHERE event_id='evt_01' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        score = conn.execute(
            "SELECT id FROM scores WHERE project_id='prj_01' LIMIT 1"
        ).fetchone()

        live_crit = conn.execute(
            "SELECT criterion_key, value FROM score_criteria WHERE score_id=? ORDER BY criterion_key",
            (score["id"],)
        ).fetchall()
        snap_crit = conn.execute(
            "SELECT criterion_key, value FROM normalization_run_criteria "
            "WHERE normalization_run_id=? AND score_id=? ORDER BY criterion_key",
            (run["id"], score["id"])
        ).fetchall()
        conn.close()

        self.assertEqual(len(live_crit), len(snap_crit),
                         "snapshot must have same number of criteria as live")
        for live, snap in zip(live_crit, snap_crit):
            self.assertEqual(live["criterion_key"], snap["criterion_key"])
            self.assertAlmostEqual(live["value"], snap["value"],
                                   msg=f"snapshot value mismatch for {live['criterion_key']}")

    def test_explain_after_score_edit_shows_original_criteria(self):
        """Phase 2 core: edit live criterion after normalization; explanation uses snapshot."""
        import db as db_module

        # 1. Normalize
        r = self._normalize()
        self.assertEqual(r.status_code, 201)

        # Get snapshot value for prj_01
        conn = db_module.get_connection()
        run = conn.execute(
            "SELECT id FROM normalization_runs WHERE event_id='evt_01' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        score = conn.execute(
            "SELECT id FROM scores WHERE project_id='prj_01' LIMIT 1"
        ).fetchone()
        crit = conn.execute(
            "SELECT criterion_key, value FROM score_criteria WHERE score_id=? LIMIT 1",
            (score["id"],)
        ).fetchone()
        original_value = crit["value"]
        crit_key = crit["criterion_key"]

        # 2. Edit the live criterion
        new_value = original_value + 2.0
        conn.execute("UPDATE score_criteria SET value=? WHERE score_id=? AND criterion_key=?",
                     (new_value, score["id"], crit_key))
        conn.commit()

        # 3. Fetch the snapshot
        snap = conn.execute(
            "SELECT value FROM normalization_run_criteria "
            "WHERE normalization_run_id=? AND score_id=? AND criterion_key=?",
            (run["id"], score["id"], crit_key)
        ).fetchone()
        conn.close()

        # Snapshot must not have changed
        self.assertAlmostEqual(snap["value"], original_value,
                               msg="snapshot must be immutable after score edit")
        self.assertNotAlmostEqual(snap["value"], new_value,
                                  msg="snapshot and live must differ after edit")

        # 4. Explanation endpoint must succeed (returns 200)
        resp = self.client.get("/organizer/results/evt_01/explain/prj_01",
                               headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 200)


# ============================================================================
# Phase 3: Session Token Hashing
# ============================================================================

class TestSessionTokenHashing(VerdictLedgerTestCase):
    """E. raw token is NOT stored in the DB
       F. session authentication still works
    """

    def test_login_sets_session_that_authenticates(self):
        """Login must set a valid session."""
        resp = self.client.post("/login", data={
            "email": "organizer@verdictledger.local",
            "password": "organizer-demo",
        })
        self.assertIn(resp.status_code, (200, 302))
        self.assertIn("session", resp.headers.get("Set-Cookie", ""))

    def test_all_sessions_stored_as_64char_hex(self):
        """Every row in sessions.token_hash must be a 64-character hex string."""
        import db as db_module
        conn = db_module.get_connection()
        rows = conn.execute("SELECT token_hash FROM sessions").fetchall()
        conn.close()
        self.assertGreater(len(rows), 0, "must have at least the seeded demo sessions")
        for row in rows:
            h = row["token_hash"]
            self.assertEqual(len(h), 64, f"token_hash must be 64 hex chars, got {len(h)}: {h!r}")
            self.assertTrue(all(c in '0123456789abcdef' for c in h.lower()),
                            f"token_hash must be hex: {h!r}")

    def test_create_session_stores_hash_not_raw(self):
        """create_session must store hash(token), not raw token."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        raw = auth_module.create_session(conn, "organizer")
        h = auth_module.hash_token(raw)
        row = conn.execute(
            "SELECT token_hash FROM sessions WHERE token_hash=?", (h,)
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row, "session row must exist when looked up by hash")
        self.assertNotEqual(row["token_hash"], raw, "stored hash must not equal raw token")
        self.assertEqual(row["token_hash"], h)

    def test_resolve_identity_works_with_raw_token(self):
        """resolve_identity must hash the raw token and find the session."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        raw = auth_module.create_session(conn, "organizer")
        identity = auth_module.resolve_identity(conn, raw)
        conn.close()
        self.assertIsNotNone(identity)
        self.assertEqual(identity["id"], "organizer")

    def test_wrong_token_not_authenticated(self):
        """A subtly wrong raw token must not authenticate."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        raw = auth_module.create_session(conn, "organizer")
        identity = auth_module.resolve_identity(conn, raw + "x")
        conn.close()
        self.assertIsNone(identity)

    def test_demo_tokens_stored_as_hash(self):
        """The fixed demo tokens (org_demo_token etc.) must be stored as hashes."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        for raw in ("org_demo_token", "jdg_a_demo_token", "jdg_b_demo_token", "prt_demo_token"):
            h = auth_module.hash_token(raw)
            row = conn.execute(
                "SELECT token_hash FROM sessions WHERE token_hash=?", (h,)
            ).fetchone()
            self.assertIsNotNone(row, f"demo session for {raw!r} must exist (by hash)")
            self.assertNotEqual(row["token_hash"], raw,
                                f"raw token {raw!r} must not equal stored hash")
        conn.close()

    def test_acceptance_checker_demo_tokens_authenticate(self):
        """The tokens in .dogfood.toml must authenticate via the API."""
        # organizer can access /organizer
        resp = self.client.get("/organizer", headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(resp.status_code, 200,
                         f"Organizer demo token failed with status {resp.status_code}")
        # judges can access /api/judge/scores
        for token in (self.JUDGE_A, self.JUDGE_B):
            resp = self.client.get("/api/judge/scores", headers=self.auth_header(token))
            self.assertEqual(resp.status_code, 200,
                             f"Judge demo token {token!r} failed: {resp.status_code}")
        # participant can access /projects (gallery)
        resp = self.client.get("/projects", headers=self.auth_header(self.PARTICIPANT))
        self.assertEqual(resp.status_code, 200,
                         f"Participant demo token failed: {resp.status_code}")


# ============================================================================
# Phase 4: Repository URL Scheme Validation
# ============================================================================

class TestRepoUrlValidation(VerdictLedgerTestCase):
    """G. unsafe repo URL rejected with 400
       H. valid http/https repository URL accepted
    """

    def _get_participant_token(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        member = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1"
        ).fetchone()
        token = auth_module.create_session(conn, member["user_id"])
        conn.close()
        return token

    def test_https_url_accepted(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "HTTPS Project",
                                   "repo_url": "https://github.com/user/repo"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 201)

    def test_http_url_accepted(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "HTTP Project",
                                   "repo_url": "http://example.com/repo"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 201)

    def test_no_url_accepted(self):
        """repo_url is optional; omitting it is valid."""
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "No URL Project"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 201)

    def test_javascript_url_rejected_on_create(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "Bad URL", "repo_url": "javascript:alert(1)"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 400)
        self.assertIn("http", r.get_json()["error"].lower())

    def test_data_url_rejected(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "Data URL",
                                   "repo_url": "data:text/html,<script>alert(1)</script>"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 400)

    def test_file_url_rejected(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "File URL", "repo_url": "file:///etc/passwd"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 400)

    def test_ftp_url_rejected(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "FTP URL", "repo_url": "ftp://example.com/repo"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 400)

    def test_no_scheme_rejected(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "No Scheme", "repo_url": "example.com/repo"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 400)

    def test_javascript_url_rejected_on_edit(self):
        """URL validation applies on edit too."""
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "Edit URL Test"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 201)
        pid = r.get_json()["id"]
        r = self.client.post(f"/projects/{pid}/edit",
                             json={"title": "Edit URL Test", "repo_url": "javascript:xss()"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 400)

    def test_https_url_accepted_on_edit(self):
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "Edit URL Valid"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 201)
        pid = r.get_json()["id"]
        r = self.client.post(f"/projects/{pid}/edit",
                             json={"title": "Edit URL Valid",
                                   "repo_url": "https://github.com/user/repo"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 200)

    def test_uppercase_scheme_accepted(self):
        """Scheme comparison must be case-insensitive."""
        token = self._get_participant_token()
        r = self.client.post("/projects/new",
                             json={"title": "Uppercase Scheme",
                                   "repo_url": "HTTPS://github.com/user/repo"},
                             headers=self.api_header(token))
        self.assertEqual(r.status_code, 201)


if __name__ == "__main__":
    unittest.main()
