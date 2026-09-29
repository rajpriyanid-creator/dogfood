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

    def test_participant_home_offers_open_events_without_fixed_template_id(self):
        import auth
        conn = self._db()
        conn.execute(
            "INSERT INTO users (id,email,name,role,password_hash,created_at) "
            "VALUES ('participant_freeze_probe','probe@example.org','Probe',? ,?,?)",
            ("participant", auth.hash_password("probe-pass"), auth.now_iso()),
        )
        token = auth.create_session(conn, "participant_freeze_probe")
        conn.commit()
        conn.close()
        page = self.client.get("/team", headers=self.auth_header(token))
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('name="event_id"', body)
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
            "SELECT rubric_version_id FROM project_results WHERE project_id='prj_01' "
            "ORDER BY created_at DESC LIMIT 1"
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
