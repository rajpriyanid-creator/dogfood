import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from base import VerdictLedgerTestCase
from assignment import build_assignments


class TestAssignmentEngine(unittest.TestCase):
    """Pure unit tests against the assignment module - no DB or Flask
    needed, since build_assignments takes plain data structures."""

    def test_track_eligibility_respected(self):
        projects = [{"id": "p1", "track_id": "t1", "team_id": "tm1"}]
        judges_by_track = {"t1": ["j1"], "t2": ["j2"]}
        pairs = build_assignments(projects, judges_by_track, {}, target_reviews_per_project=1)
        assigned_judges = {j for j, p in pairs}
        self.assertIn("j1", assigned_judges)
        self.assertNotIn("j2", assigned_judges)

    def test_no_self_review(self):
        projects = [{"id": "p1", "track_id": "t1", "team_id": "tm1"}]
        judges_by_track = {"t1": ["j1", "j2"]}
        team_owner_by_project = {"p1": {"j1"}}  # j1 is on the owning team
        pairs = build_assignments(projects, judges_by_track, team_owner_by_project,
                                   target_reviews_per_project=2)
        assigned_judges = {j for j, p in pairs}
        self.assertNotIn("j1", assigned_judges)
        self.assertIn("j2", assigned_judges)

    def test_target_coverage(self):
        projects = [{"id": "p1", "track_id": "t1", "team_id": "tm1"}]
        judges_by_track = {"t1": ["j1", "j2", "j3", "j4"]}
        pairs = build_assignments(projects, judges_by_track, {}, target_reviews_per_project=3)
        self.assertEqual(len(pairs), 3)

    def test_target_capped_by_available_pool(self):
        """If fewer eligible judges exist than the target, assign what's
        available rather than failing."""
        projects = [{"id": "p1", "track_id": "t1", "team_id": "tm1"}]
        judges_by_track = {"t1": ["j1"]}
        pairs = build_assignments(projects, judges_by_track, {}, target_reviews_per_project=3)
        self.assertEqual(len(pairs), 1)

    def test_workload_balancing(self):
        projects = [
            {"id": "p1", "track_id": "t1", "team_id": "tm1"},
            {"id": "p2", "track_id": "t1", "team_id": "tm2"},
            {"id": "p3", "track_id": "t1", "team_id": "tm3"},
        ]
        judges_by_track = {"t1": ["j1", "j2"]}
        pairs = build_assignments(projects, judges_by_track, {}, target_reviews_per_project=1)
        from collections import Counter
        counts = Counter(j for j, p in pairs)
        # With 3 projects and 2 judges at 1 review each, load should be
        # as even as possible: {2, 1} not {3, 0}.
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_overlap_exists_when_target_allows(self):
        """Multiple judges may review the same project - batches need
        not be disjoint."""
        projects = [{"id": "p1", "track_id": "t1", "team_id": "tm1"}]
        judges_by_track = {"t1": ["j1", "j2", "j3"]}
        pairs = build_assignments(projects, judges_by_track, {}, target_reviews_per_project=2)
        judges_on_p1 = [j for j, p in pairs if p == "p1"]
        self.assertEqual(len(judges_on_p1), 2)

    def test_determinism(self):
        projects = [{"id": f"p{i}", "track_id": "t1", "team_id": f"tm{i}"} for i in range(5)]
        judges_by_track = {"t1": [f"j{i}" for i in range(4)]}
        pairs_1 = build_assignments(projects, judges_by_track, {}, target_reviews_per_project=2)
        pairs_2 = build_assignments(projects, judges_by_track, {}, target_reviews_per_project=2)
        self.assertEqual(pairs_1, pairs_2)


class TestAssignmentEventIntegrity(VerdictLedgerTestCase):
    def test_assignment_cannot_cross_events(self):
        import db as db_module
        import sqlite3
        conn = db_module.get_connection()
        # Insert a dummy event, team, and project
        conn.execute("INSERT INTO events (id, name, kind, submissions_close, publish_state, created_at) VALUES ('evt_a', 'Event A', 'live', '2099-01-01T00:00:00Z', 'draft', '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO teams (id, event_id, name, created_at) VALUES ('tm_a', 'evt_a', 'Team A', '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO projects (id, event_id, team_id, title, status, created_at, updated_at) VALUES ('prj_a', 'evt_a', 'tm_a', 'Project A', 'draft', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')")
        
        # Insert another event and a judge
        conn.execute("INSERT INTO events (id, name, kind, submissions_close, publish_state, created_at) VALUES ('evt_b', 'Event B', 'live', '2099-01-01T00:00:00Z', 'draft', '2026-01-01T00:00:00Z')")
        conn.execute("INSERT INTO users (id, email, name, role, password_hash, created_at) VALUES ('jdg_b', 'jdg@b', 'Judge', 'judge', 'hash', '2026-01-01T00:00:00Z')")

        # Attempt to assign the project from evt_a to an assignment in evt_b
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO assignments (id, event_id, judge_id, project_id, created_at) "
                "VALUES ('asg_cross', 'evt_b', 'jdg_b', 'prj_a', '2026-01-01T00:00:00Z')"
            )
            conn.commit()


if __name__ == "__main__":
    unittest.main()
