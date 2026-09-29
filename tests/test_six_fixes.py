import unittest
from base import VerdictLedgerTestCase

class TestSixFixes(VerdictLedgerTestCase):
    def test_manage_judges_event_leakage(self):
        """Test that manage_judges only shows tracks for the requested event."""
        import db as db_module
        conn = db_module.get_connection()
        # 1. Create a judge in evt_01
        conn.execute("INSERT INTO tracks (id, event_id, name) VALUES ('trk_six', 'evt_01', 'Track 6')")
        conn.execute("INSERT INTO judge_track_eligibility (user_id, track_id) VALUES ('jdg_01', 'trk_six')")
        conn.commit()
        conn.close()

        # 2. Query judges for evt_live_2026
        r = self.client.get("/organizer/events/evt_live_2026/judges", headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(r.status_code, 200)
        
        # 3. Ensure 'trk_six' does not appear in the HTML
        self.assertNotIn("trk_six", r.get_data(as_text=True))

    def test_timezone_aware_timestamps(self):
        """Test that event creation requires timezone-aware timestamps."""
        payload = {
            "name": "TZ Event",
            "start_at": "2026-10-01T12:00:00", # naive
            "submissions_close": "2026-10-02T12:00:00Z" # aware
        }
        r = self.client.post("/organizer/events/new", json=payload, headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a valid timestamp", r.get_data(as_text=True))

        payload["start_at"] = "2026-10-01T12:00:00+00:00" # aware
        r2 = self.client.post("/organizer/events/new", json=payload, headers=self.auth_header(self.ORGANIZER))
        self.assertEqual(r2.status_code, 201)

    def test_missing_rubric_graceful(self):
        """Test that judge_review handles a missing rubric with 409."""
        import db as db_module
        conn = db_module.get_connection()
        org_id = conn.execute("SELECT id FROM users WHERE role='organizer' LIMIT 1").fetchone()["id"]
        # 1. Create a dummy event and project
        event_id = "evt_norubric"
        conn.execute("INSERT INTO events (id, name, kind, start_at, submissions_close, judging_close, publish_state, created_by, created_at) VALUES (?, 'No Rubric', 'live', '2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z', '2026-03-01T00:00:00Z', 'draft', ?, '2026-01-01T00:00:00Z')", (event_id, org_id))
        conn.execute("INSERT INTO teams (id, event_id, name, created_at) VALUES ('tm_nr', ?, 'Team NR', '2026-01-01T00:00:00Z')", (event_id,))
        conn.execute("INSERT INTO tracks (id, event_id, name) VALUES ('trk_nr', ?, 'Track NR')", (event_id,))
        conn.execute("INSERT INTO projects (id, event_id, team_id, track_id, title, summary, repo_url, status, submitted_at, created_at, updated_at) VALUES ('prj_nr', ?, 'tm_nr', 'trk_nr', 'NR', 'NR', 'http://repo', 'submitted', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (event_id,))
        conn.execute("INSERT INTO assignments (id, event_id, judge_id, project_id, version, created_at) VALUES ('asg_nr', ?, 'jdg_01', 'prj_nr', 1, '2026-02-02T00:00:00Z')", (event_id,))
        conn.commit()
        conn.close()

        # 2. Try to judge it (no rubric_versions exist for evt_norubric)
        r = self.client.get("/judge/review/prj_nr", headers=self.auth_header(self.JUDGE_A))
        self.assertEqual(r.status_code, 409)
        self.assertIn("no rubric configured", r.get_data(as_text=True))

    def test_freeze_event_configuration(self):
        """Test that event configuration is frozen once judging starts."""
        import db as db_module
        conn = db_module.get_connection()
        org_id = conn.execute("SELECT id FROM users WHERE role='organizer' LIMIT 1").fetchone()["id"]
        # 1. Create event and set it to JUDGING (both submissions and judging close in past)
        event_id = "evt_frozen"
        conn.execute("INSERT INTO events (id, name, kind, start_at, submissions_close, judging_close, publish_state, created_by, created_at) VALUES (?, 'Frozen Event', 'live', '2020-01-01T00:00:00Z', '2020-02-01T00:00:00Z', '2020-03-01T00:00:00Z', 'draft', ?, '2020-01-01T00:00:00Z')", (event_id, org_id))
        conn.commit()
        conn.close()

        # Try to modify configuration
        client = self.client
        hdrs = self.auth_header(self.ORGANIZER)
        
        # Add track
        r = client.post(f"/organizer/events/{event_id}/tracks", json={"name": "New Track"}, headers=hdrs)
        self.assertEqual(r.status_code, 403)
        self.assertIn("configuration is frozen", r.get_data(as_text=True))

        # Add prize
        r = client.post(f"/organizer/events/{event_id}/prizes", json={"name": "New Prize"}, headers=hdrs)
        self.assertEqual(r.status_code, 403)
        
        # Manage judges
        r = client.post(f"/organizer/events/{event_id}/judges", json={"name": "J", "email": "j@example.com"}, headers=hdrs)
        self.assertEqual(r.status_code, 403)
        
        # Rubric
        r = client.post(f"/organizer/events/{event_id}/rubric", json={"criteria": [{"key": "a", "weight": 1, "min_value": 1, "max_value": 5}]}, headers=hdrs)
        self.assertEqual(r.status_code, 403)
        
        # Assignments
        r = client.post(f"/organizer/events/{event_id}/assignments", json={"target_reviews": 3}, headers=hdrs)
        self.assertEqual(r.status_code, 403)


