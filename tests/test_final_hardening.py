import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from base import VerdictLedgerTestCase


class TestFinalHardening(VerdictLedgerTestCase):

    def _db(self):
        import db
        return db.get_connection()

    def test_team_creation_requires_explicit_event(self):
        response = self.client.post(
            "/team/create", data={"name": "No Event"},
            headers=self.auth_header(self.PARTICIPANT),
        )
        self.assertEqual(response.status_code, 400)

    def test_participant_home_renders_dynamically_created_open_event(self):
        import auth
        from datetime import datetime, timedelta, timezone
        conn = self._db()
        conn.execute(
            "INSERT INTO users (id,email,name,role,password_hash,created_at) "
            "VALUES ('participant_freeze_probe','probe@example.org','Probe',? ,?,?)",
            ("participant", auth.hash_password("probe-pass"), auth.now_iso()),
        )
        token = auth.create_session(conn, "participant_freeze_probe")
        conn.commit()
        conn.close()
        now = datetime.now(timezone.utc)
        created = self.client.post(
            "/organizer/events/new",
            json={
                "name": "Dynamic Audit Event",
                "start_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "submissions_close": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tracks": ["Dynamic Track"],
                "prizes": [],
            },
            headers=self.api_header(self.ORGANIZER),
        )
        self.assertEqual(created.status_code, 201)
        dynamic_event_id = created.get_json()["id"]
        page = self.client.get("/team", headers=self.auth_header(token))
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('name="event_id"', body)
        self.assertIn(f'value="{dynamic_event_id}"', body)
        self.assertIn("Dynamic Audit Event", body)

    def test_participant_with_closed_single_team_can_switch_to_open_event(self):
        import auth
        conn = self._db()
        user = conn.execute(
            "SELECT tm.user_id FROM team_members tm JOIN teams t ON t.id=tm.team_id "
            "WHERE t.event_id='evt_01' LIMIT 1"
        ).fetchone()["user_id"]
        token = auth.create_session(conn, user)
        conn.close()
        page = self.client.get("/team", headers=self.auth_header(token))
        body = page.get_data(as_text=True)
        self.assertIn('id="event_context"', body)
        self.assertIn('value="evt_live_2026"', body)

    def test_gallery_order_is_id_deterministic(self):
        page = self.client.get("/projects")
        self.assertEqual(page.status_code, 200)
        import re
        ids = re.findall(r'<span class="id">(prj_[^<]+)</span>', page.get_data(as_text=True))
        self.assertEqual(ids, sorted(ids))

    def test_csv_requires_explicit_event(self):
        response = self.client.get("/api/export.csv", headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(response.status_code, 400)

    def test_new_judge_gets_one_time_usable_credential_and_hash_only(self):
        response = self.client.post(
            "/organizer/events/evt_live_2026/judges",
            json={"name": "Freeze Judge", "email": "freeze.judge@example.org"},
            headers=self.api_header(self.ORGANIZER),
        )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        password = payload["initial_password"]
        self.assertTrue(password)
        conn = self._db()
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (payload["id"],)).fetchone()
        conn.close()
        self.assertNotEqual(row["password_hash"], password)
        from auth import verify_password
        self.assertTrue(verify_password(password, row["password_hash"]))
        login = self.client.post("/login", data={"email": payload["email"], "password": password})
        self.assertEqual(login.status_code, 302)

    def test_invalid_track_and_frozen_event_reject_judge_invitation(self):
        invalid = self.client.post(
            "/organizer/events/evt_live_2026/judges",
            json={"name": "Bad Track", "email": "bad.track@example.org", "track_ids": ["trk_01"]},
            headers=self.api_header(self.ORGANIZER),
        )
        self.assertEqual(invalid.status_code, 400)
        frozen = self.client.post(
            "/organizer/events/evt_01/judges",
            json={"name": "Frozen", "email": "frozen@example.org"},
            headers=self.api_header(self.ORGANIZER),
        )
        self.assertEqual(frozen.status_code, 403)

    def test_published_event_cannot_normalize_again(self):
        response = self.client.post(
            "/organizer/normalize/evt_01",
            headers=self.api_header(self.ORGANIZER),
        )
        self.assertEqual(response.status_code, 409)

    def test_historical_comment_snapshot_survives_live_comment_edit(self):
        conn = self._db()
        conn.execute("UPDATE events SET publish_state='draft' WHERE id='evt_01'")
        score = conn.execute("SELECT id, comment FROM scores WHERE project_id='prj_01' LIMIT 1").fetchone()
        old = score["comment"] or "No comment provided"
        conn.commit()
        conn.close()
        run = self.client.post("/organizer/normalize/evt_01", headers=self.api_header(self.ORGANIZER))
        self.assertEqual(run.status_code, 201)
        conn = self._db()
        conn.execute("UPDATE scores SET comment='LIVE EDIT' WHERE id=?", (score["id"],))
        conn.commit()
        conn.close()
        page = self.client.get("/organizer/results/evt_01/explain/prj_01", headers=self.auth_header(self.ORGANIZER))
        body = page.get_data(as_text=True)
        self.assertIn(old, body)
        self.assertNotIn("LIVE EDIT", body)

    def test_mixed_rubric_versions_leave_result_rubric_id_null(self):
        conn = self._db()
        import auth
        conn.execute(
            "INSERT INTO rubric_versions (id,event_id,version,created_at) VALUES (?,?,?,?)",
            ("evt_01_mixed_probe", "evt_01", 99, auth.now_iso()),
        )
        criteria = conn.execute(
            "SELECT key,label,description,weight,min_value,max_value FROM rubric_criteria "
            "WHERE rubric_version_id=(SELECT rubric_version_id FROM scores WHERE project_id='prj_01' LIMIT 1)"
        ).fetchall()
        for i, criterion in enumerate(criteria):
            conn.execute(
                "INSERT INTO rubric_criteria (id,rubric_version_id,key,label,description,weight,min_value,max_value) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"mixed_c_{i}", "evt_01_mixed_probe", criterion["key"], criterion["label"],
                 criterion["description"], criterion["weight"], criterion["min_value"], criterion["max_value"]),
            )
        score = conn.execute("SELECT id FROM scores WHERE project_id='prj_01' LIMIT 1").fetchone()
        conn.execute("UPDATE scores SET rubric_version_id=? WHERE id=?", ("evt_01_mixed_probe", score["id"]))
        conn.execute("UPDATE events SET publish_state='draft' WHERE id='evt_01'")
        conn.commit()
        conn.close()
        response = self.client.post("/organizer/normalize/evt_01", headers=self.api_header(self.ORGANIZER))
        self.assertEqual(response.status_code, 201)
        conn = self._db()
        result = conn.execute(
            "SELECT r.rubric_version_id FROM project_results r "
            "JOIN normalization_runs n ON n.id=r.normalization_run_id "
            "WHERE r.project_id='prj_01' ORDER BY n.version DESC LIMIT 1"
        ).fetchone()
        conn.close()
        self.assertIsNone(result["rubric_version_id"])

    def test_assignment_backed_score_read_hides_orphaned_score(self):
        conn = self._db()
        score = conn.execute("SELECT id, project_id FROM scores WHERE judge_id='jdg_01' LIMIT 1").fetchone()
        conn.execute("UPDATE scores SET assignment_id=NULL WHERE id=?", (score["id"],))
        conn.commit()
        conn.close()
        response = self.client.get("/api/judge/scores", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(score["project_id"], [r["project_id"] for r in response.get_json()["scores"]])

    def test_nonfinite_rubric_values_are_rejected(self):
        validator = self.app_module.validate_rubric_criteria
        for field, value in (("weight", "NaN"), ("weight", "Infinity"),
                             ("min_value", "-Infinity"), ("max_value", "NaN")):
            criteria = [{"key": "x", "label": "X", "weight": 1.0, "min_value": 1, "max_value": 5}]
            criteria[0][field] = value
            self.assertTrue(validator(criteria), (field, value))

    def test_repo_url_requires_hostname_for_http_schemes(self):
        validate = self.app_module._validate_repo_url
        self.assertIsNone(validate(""))
        self.assertIsNotNone(validate("https:///missing-host"))
        self.assertIsNotNone(validate("ftp://example.org/repo"))

    def test_weighted_rubric_bounds_use_each_criterion_weight(self):
        bounds = self.app_module.weighted_score_bounds
        criteria = [
            {"weight": 0.5, "min_value": 1, "max_value": 5},
            {"weight": 0.5, "min_value": 0, "max_value": 10},
        ]
        self.assertEqual(bounds(criteria), (0.5, 7.5))
        self.assertEqual(bounds(list(reversed(criteria))), (0.5, 7.5))

    def test_submission_without_track_is_rejected_but_draft_is_allowed(self):
        import auth
        conn = self._db()
        user = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1"
        ).fetchone()["user_id"]
        token = auth.create_session(conn, user)
        conn.close()
        created = self.client.post("/projects/new", json={"title": "Track required draft"},
                                   headers=self.auth_header(token))
        self.assertEqual(created.status_code, 201)
        project_id = created.get_json()["id"]
        submitted = self.client.post(f"/projects/{project_id}/submit",
                                     headers=self.api_header(token))
        self.assertEqual(submitted.status_code, 400)

    def test_project_payload_rejects_non_string_fields_with_400(self):
        import auth
        conn = self._db()
        user = conn.execute(
            "SELECT user_id FROM team_members WHERE team_id='live_tm_1' LIMIT 1"
        ).fetchone()["user_id"]
        token = auth.create_session(conn, user)
        conn.close()
        for field, value in (("title", []), ("summary", {}),
                             ("repo_url", []), ("track_id", {})):
            response = self.client.post(
                "/projects/new",
                json={"title": "Valid title", field: value},
                headers=self.auth_header(token),
            )
            self.assertEqual(response.status_code, 400, (field, response.get_data(as_text=True)))
        malformed = self.client.post(
            "/projects/new", json=["not", "an", "object"],
            headers=self.auth_header(token),
        )
        self.assertEqual(malformed.status_code, 400)

    def test_event_creation_rejects_malformed_json_lists(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        base = {
            "name": "Payload Validation Event",
            "start_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "submissions_close": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        for field, value in (("tracks", "not-a-list"), ("tracks", {}),
                             ("prizes", "not-a-list"), ("prizes", {}),
                             ("tracks", ["ok", 3])):
            payload = {**base, field: value}
            response = self.client.post("/organizer/events/new", json=payload,
                                        headers=self.api_header(self.ORGANIZER))
            self.assertEqual(response.status_code, 400, (field, value))
        valid = self.client.post("/organizer/events/new", json={**base, "tracks": [" Web "], "prizes": [" Prize "]},
                                 headers=self.api_header(self.ORGANIZER))
        self.assertEqual(valid.status_code, 201)

    def test_zero_assignment_eligible_judge_appears_in_progress(self):
        conn = self._db()
        live_track = conn.execute("SELECT id FROM tracks WHERE event_id='evt_live_2026' LIMIT 1").fetchone()["id"]
        conn.close()
        response = self.client.post(
            "/organizer/events/evt_live_2026/judges",
            json={"name": "Unassigned Judge", "email": "unassigned@example.org",
                  "track_ids": [live_track]},
            headers=self.api_header(self.ORGANIZER),
        )
        self.assertEqual(response.status_code, 201)
        judge_id = response.get_json()["id"]
        page = self.client.get("/judge/progress/evt_live_2026", headers=self.auth_header(self.ORGANIZER))
        body = page.get_data(as_text=True)
        self.assertIn(judge_id, body)
        row_start = body.index(judge_id)
        self.assertIn(">0<", body[row_start:row_start + 250])

    def test_judge_home_requires_event_context_when_judge_has_multiple_events(self):
        import auth
        conn = self._db()
        conn.execute("INSERT INTO events (id,name,kind,submissions_close,publish_state,created_at) VALUES ('evt_judge_b','Judge B','live','2099-01-01T00:00:00Z','draft',?)", (auth.now_iso(),))
        conn.execute("INSERT INTO tracks (id,event_id,name) VALUES ('trk_judge_b','evt_judge_b','Track B')")
        conn.execute("INSERT INTO judge_track_eligibility (user_id,track_id) VALUES ('jdg_01','trk_judge_b')")
        conn.execute("INSERT INTO teams (id,event_id,name,created_at) VALUES ('tm_judge_b','evt_judge_b','Team B',?)", (auth.now_iso(),))
        conn.execute("INSERT INTO projects (id,event_id,team_id,track_id,title,status,submitted_at,created_at,updated_at) VALUES ('prj_judge_b','evt_judge_b','tm_judge_b','trk_judge_b','Judge B Project','submitted',?,?,?)", (auth.now_iso(), auth.now_iso(), auth.now_iso()))
        conn.execute("INSERT INTO assignments (id,event_id,judge_id,project_id,created_at) VALUES ('asg_judge_b','evt_judge_b','jdg_01','prj_judge_b',?)", (auth.now_iso(),))
        conn.commit()
        token = auth.create_session(conn, "jdg_01")
        conn.close()
        page = self.client.get("/judge", headers=self.auth_header(token))
        body = page.get_data(as_text=True)
        self.assertIn("Judge B", body)
        self.assertNotIn("Glass Signal", body)

    def test_project_team_event_integrity_is_enforced(self):
        import sqlite3
        conn = self._db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO projects (id,event_id,team_id,title,status,submitted_at,created_at,updated_at) "
                "VALUES ('cross_event_fk','evt_01','live_tm_1','Cross event','submitted',?,?,?)",
                ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            )
        conn.close()


if __name__ == "__main__":
    unittest.main()
