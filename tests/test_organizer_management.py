import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase


class TestRubricValidatorProduction(VerdictLedgerTestCase):
    """Tests the PRODUCTION validator (app.validate_rubric_criteria)
    directly. The audit flagged the previous rubric test for defining a
    local validate() inside the test and asserting against that copy -
    such a test passes even if production has no validator at all."""

    def _v(self, criteria):
        return self.app_module.validate_rubric_criteria(criteria)

    def _crit(self, key, weight, lo=1, hi=5):
        return {"key": key, "label": key, "weight": weight,
                "min_value": lo, "max_value": hi}

    def test_valid_rubric_accepted(self):
        errs = self._v([self._crit("a", 0.4), self._crit("b", 0.35), self._crit("c", 0.25)])
        self.assertEqual(errs, [])

    def test_weights_summing_over_one_rejected(self):
        errs = self._v([self._crit("a", 0.5), self._crit("b", 0.6)])
        self.assertTrue(any("sum to 1.0" in e for e in errs))

    def test_weights_summing_under_one_rejected(self):
        errs = self._v([self._crit("a", 0.3), self._crit("b", 0.3)])
        self.assertTrue(any("sum to 1.0" in e for e in errs))

    def test_negative_weight_rejected(self):
        errs = self._v([self._crit("a", -0.1), self._crit("b", 1.1)])
        self.assertTrue(any(">= 0" in e for e in errs))

    def test_duplicate_keys_rejected(self):
        errs = self._v([self._crit("a", 0.5), self._crit("a", 0.5)])
        self.assertTrue(any("duplicated" in e for e in errs))

    def test_empty_rubric_rejected(self):
        self.assertTrue(self._v([]))

    def test_inverted_range_rejected(self):
        errs = self._v([self._crit("a", 1.0, lo=5, hi=1)])
        self.assertTrue(any("greater than min_value" in e for e in errs))

    def test_float_tolerance_on_sum(self):
        """0.1 + 0.2 + 0.7 is not exactly 1.0 in binary floating point;
        it must still be accepted."""
        errs = self._v([self._crit("a", 0.1), self._crit("b", 0.2), self._crit("c", 0.7)])
        self.assertEqual(errs, [])

    def test_non_numeric_weight_rejected(self):
        errs = self._v([{"key": "a", "weight": "abc"}])
        self.assertTrue(errs)


class TestRubricBuilderRoute(VerdictLedgerTestCase):

    def test_organizer_can_save_new_rubric_version(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/rubric",
            json={"criteria": [
                {"key": "impact", "label": "Impact", "weight": 0.6},
                {"key": "craft", "label": "Craft", "weight": 0.4},
            ]},
            headers=self.auth_header(self.ORGANIZER),
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.get_json()["version"], 2)

    def test_invalid_rubric_rejected_by_backend(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/rubric",
            json={"criteria": [{"key": "a", "weight": 0.9}]},
            headers=self.auth_header(self.ORGANIZER),
        )
        self.assertEqual(resp.status_code, 400)

    def test_new_version_does_not_mutate_old_version(self):
        import db as db_module
        conn = db_module.get_connection()
        before = conn.execute(
            "SELECT key, weight FROM rubric_criteria WHERE rubric_version_id='evt_live_2026_rubric_v1' "
            "ORDER BY key"
        ).fetchall()
        before = [(r["key"], r["weight"]) for r in before]
        conn.close()

        self.client.post(
            "/organizer/events/evt_live_2026/rubric",
            json={"criteria": [{"key": "only", "weight": 1.0}]},
            headers=self.auth_header(self.ORGANIZER),
        )

        conn = db_module.get_connection()
        after = conn.execute(
            "SELECT key, weight FROM rubric_criteria WHERE rubric_version_id='evt_live_2026_rubric_v1' "
            "ORDER BY key"
        ).fetchall()
        after = [(r["key"], r["weight"]) for r in after]
        conn.close()
        self.assertEqual(before, after)

    def test_judge_cannot_use_rubric_builder(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/rubric",
            json={"criteria": [{"key": "a", "weight": 1.0}]},
            headers=self.auth_header(self.JUDGE_A),
        )
        self.assertEqual(resp.status_code, 403)

    def test_participant_cannot_use_rubric_builder(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/rubric",
            json={"criteria": [{"key": "a", "weight": 1.0}]},
            headers=self.auth_header(self.PARTICIPANT),
        )
        self.assertEqual(resp.status_code, 403)


class TestEventCreation(VerdictLedgerTestCase):

    def _create(self, **overrides):
        body = {
            "name": "New Hack",
            "start_at": "2027-01-01T00:00:00Z",
            "submissions_close": "2027-01-03T00:00:00Z",
            "judging_close": "2027-01-05T00:00:00Z",
            "tracks": ["Web", "Data"],
            "prizes": ["Grand Prize"],
        }
        body.update(overrides)
        return self.client.post("/organizer/events/new", json=body,
                                 headers=self.auth_header(self.ORGANIZER))

    def test_valid_event_created_with_tracks_and_prizes(self):
        resp = self._create()
        self.assertEqual(resp.status_code, 201)
        event_id = resp.get_json()["id"]

        import db as db_module
        conn = db_module.get_connection()
        tracks = conn.execute("SELECT name FROM tracks WHERE event_id=?", (event_id,)).fetchall()
        prizes = conn.execute("SELECT name FROM prizes WHERE event_id=?", (event_id,)).fetchall()
        conn.close()
        self.assertEqual({t["name"] for t in tracks}, {"Web", "Data"})
        self.assertEqual({p["name"] for p in prizes}, {"Grand Prize"})

    def test_empty_name_rejected(self):
        self.assertEqual(self._create(name="  ").status_code, 400)

    def test_close_before_start_rejected(self):
        resp = self._create(start_at="2027-01-05T00:00:00Z",
                            submissions_close="2027-01-01T00:00:00Z")
        self.assertEqual(resp.status_code, 400)

    def test_invalid_timestamp_rejected(self):
        self.assertEqual(self._create(submissions_close="not-a-date").status_code, 400)

    def test_judging_close_before_submission_close_rejected(self):
        resp = self._create(judging_close="2027-01-02T00:00:00Z")
        self.assertEqual(resp.status_code, 400)

    def test_duplicate_track_names_rejected(self):
        self.assertEqual(self._create(tracks=["Web", "Web"]).status_code, 400)

    def test_judge_cannot_create_event(self):
        resp = self.client.post("/organizer/events/new", json={"name": "x"},
                                 headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(resp.status_code, 403)

    def test_event_creation_is_audited(self):
        self._create()
        import db as db_module
        conn = db_module.get_connection()
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events WHERE action='event_created'"
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(n, 1)


class TestJudgeInvitationAndAssignments(VerdictLedgerTestCase):

    def test_organizer_can_invite_judge_with_track_eligibility(self):
        import db as db_module
        conn = db_module.get_connection()
        track = conn.execute(
            "SELECT id FROM tracks WHERE event_id='evt_live_2026' LIMIT 1"
        ).fetchone()
        conn.close()

        resp = self.client.post(
            "/organizer/events/evt_live_2026/judges",
            json={"name": "New Judge", "email": "newjudge@example.org",
                  "track_ids": [track["id"]]},
            headers=self.auth_header(self.ORGANIZER),
        )
        self.assertEqual(resp.status_code, 201)
        judge_id = resp.get_json()["id"]

        conn = db_module.get_connection()
        elig = conn.execute(
            "SELECT track_id FROM judge_track_eligibility WHERE user_id=?", (judge_id,)
        ).fetchall()
        conn.close()
        self.assertEqual([e["track_id"] for e in elig], [track["id"]])

    def test_invite_rejects_cross_event_track(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/judges",
            json={"name": "X", "email": "x@example.org", "track_ids": ["trk_01"]},
            headers=self.auth_header(self.ORGANIZER),
        )
        self.assertEqual(resp.status_code, 400)

    def test_invite_rejects_email_of_non_judge(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/judges",
            json={"name": "X", "email": "organizer@verdictledger.local"},
            headers=self.auth_header(self.ORGANIZER),
        )
        self.assertEqual(resp.status_code, 400)

    def test_judge_cannot_invite_judges(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/judges",
            json={"name": "X", "email": "x@example.org"},
            headers=self.auth_header(self.JUDGE_A),
        )
        self.assertEqual(resp.status_code, 403)

    def test_assignment_generation_respects_no_self_review(self):
        """Submit the live event's drafts, generate assignments through
        the real route, and confirm no judge is on the team of a
        project they were assigned (and every assignment is
        track-eligible)."""
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE projects SET status='submitted', submitted_at=datetime('now') "
                     "WHERE event_id='evt_live_2026'")
        conn.commit()
        conn.close()

        resp = self.client.post(
            "/organizer/events/evt_live_2026/assignments",
            json={"target_reviews": 2},
            headers=self.auth_header(self.ORGANIZER),
        )
        self.assertEqual(resp.status_code, 200)

        conn = db_module.get_connection()
        rows = conn.execute(
            "SELECT a.judge_id, a.project_id, p.track_id, p.team_id FROM assignments a "
            "JOIN projects p ON p.id=a.project_id WHERE a.event_id='evt_live_2026'"
        ).fetchall()
        self.assertGreater(len(rows), 0)
        for r in rows:
            member = conn.execute(
                "SELECT 1 FROM team_members WHERE team_id=? AND user_id=?",
                (r["team_id"], r["judge_id"]),
            ).fetchone()
            self.assertIsNone(member, "judge assigned to their own team's project")
            elig = conn.execute(
                "SELECT 1 FROM judge_track_eligibility WHERE user_id=? AND track_id=?",
                (r["judge_id"], r["track_id"]),
            ).fetchone()
            self.assertIsNotNone(elig, "judge assigned outside their eligible tracks")
        conn.close()

    def test_assignment_generation_is_idempotent(self):
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE projects SET status='submitted', submitted_at=datetime('now') "
                     "WHERE event_id='evt_live_2026'")
        conn.commit()
        conn.close()

        url = "/organizer/events/evt_live_2026/assignments"
        self.client.post(url, json={"target_reviews": 2}, headers=self.auth_header(self.ORGANIZER))
        conn = db_module.get_connection()
        n1 = conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]
        conn.close()
        self.client.post(url, json={"target_reviews": 2}, headers=self.auth_header(self.ORGANIZER))
        conn = db_module.get_connection()
        n2 = conn.execute("SELECT COUNT(*) AS n FROM assignments").fetchone()["n"]
        conn.close()
        self.assertEqual(n1, n2)

    def test_participant_cannot_generate_assignments(self):
        resp = self.client.post(
            "/organizer/events/evt_live_2026/assignments",
            json={"target_reviews": 2},
            headers=self.auth_header(self.PARTICIPANT),
        )
        self.assertEqual(resp.status_code, 403)


class TestParticipantOnlyRoutes(VerdictLedgerTestCase):
    """Audited HIGH: any authenticated role could use participant
    team/submission routes. Permission model (documented): only
    role='participant' may create/join teams, create/edit/submit
    projects. Admin is NOT implicitly a participant."""

    def _admin_token(self):
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        token = auth_module.create_session(conn, "admin")
        conn.close()
        return token

    def test_judge_denied_team_create(self):
        r = self.client.post("/team/create", data={"name": "x"},
                              headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(r.status_code, 403)

    def test_organizer_denied_team_create(self):
        r = self.client.post("/team/create", data={"name": "x"},
                              headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(r.status_code, 403)

    def test_admin_denied_team_create(self):
        r = self.client.post("/team/create", data={"name": "x"},
                              headers=self.auth_header(self._admin_token()))
        self.assertEqual(r.status_code, 403)

    def test_judge_denied_project_create(self):
        r = self.client.post("/projects/new", json={"title": "x"},
                              headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(r.status_code, 403)

    def test_organizer_denied_project_create(self):
        r = self.client.post("/projects/new", json={"title": "x"},
                              headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(r.status_code, 403)

    def test_visitor_denied_project_create(self):
        r = self.client.post("/projects/new", json={"title": "x"})
        self.assertEqual(r.status_code, 401)

    def test_judge_denied_team_join(self):
        r = self.client.post("/team/join", data={"invite_code": "x"},
                              headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(r.status_code, 403)

    def test_participant_still_allowed(self):
        """A participant on an OPEN event can create a project."""
        import auth as auth_module
        import db as db_module
        conn = db_module.get_connection()
        member = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1").fetchone()
        token = auth_module.create_session(conn, member["user_id"])
        conn.close()
        r = self.client.post("/projects/new", json={"title": "ok"},
                              headers=self.auth_header(token))
        self.assertEqual(r.status_code, 201)


class TestAssignmentAuditTrail(VerdictLedgerTestCase):

    def _generate(self):
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("UPDATE projects SET status='submitted', submitted_at=datetime('now') "
                     "WHERE event_id='evt_live_2026'")
        conn.commit()
        conn.close()
        return self.client.post("/organizer/events/evt_live_2026/assignments",
                                json={"target_reviews": 2},
                                headers=self.auth_header(self.ORGANIZER))

    def test_each_new_assignment_is_audited_with_judge_and_project(self):
        import db as db_module
        conn = db_module.get_connection()
        before = {(r["judge_id"], r["project_id"]) for r in conn.execute(
            "SELECT judge_id, project_id FROM assignments WHERE event_id='evt_live_2026'")}
        conn.close()

        self.assertEqual(self._generate().status_code, 200)

        import json as _json
        conn = db_module.get_connection()
        after = {(r["judge_id"], r["project_id"]) for r in conn.execute(
            "SELECT judge_id, project_id FROM assignments WHERE event_id='evt_live_2026'")}
        audited = {(_json.loads(r["detail"])["judge_id"], r["resource"]) for r in conn.execute(
            "SELECT resource, detail FROM audit_events WHERE action='judge_assigned'")}
        conn.close()
        self.assertEqual(audited, after - before)

    def test_rerun_creates_no_new_judge_assigned_rows(self):
        self._generate()
        import db as db_module
        conn = db_module.get_connection()
        n1 = conn.execute("SELECT COUNT(*) AS n FROM audit_events WHERE action='judge_assigned'").fetchone()["n"]
        conn.close()
        self._generate()
        conn = db_module.get_connection()
        n2 = conn.execute("SELECT COUNT(*) AS n FROM audit_events WHERE action='judge_assigned'").fetchone()["n"]
        conn.close()
        self.assertEqual(n1, n2)


if __name__ == "__main__":
    unittest.main()
