import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import json
import re
import statistics
import unittest
from collections import defaultdict
from base import VerdictLedgerTestCase

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _fixture():
    with open(os.path.join(REPO_ROOT, "fixtures.json"), encoding="utf-8") as f:
        return json.load(f)


ROW = re.compile(
    r'<td>[^<]*? <span class="id">(jdg_\d+)</span></td>\s*<td>(\d+)</td>\s*'
    r'<td class="mono">([\d.]+)</td>\s*<td class="mono">([\d.]+)</td>\s*<td>(.*?)</td>', re.S)


class TestIntegrityDashboard(VerdictLedgerTestCase):
    """The audited P0: a bare COUNT(*) with no GROUP BY collapsed the
    whole event to ONE row. These tests parse the rendered per-judge
    table and pin exact values, so a collapsed or arbitrary result
    cannot pass by merely containing the right judge ids somewhere on
    the page. Expected values are recomputed independently from
    fixtures.json, not copied from the app's output."""

    def setUp(self):
        super().setUp()
        page = self.client.get("/organizer/integrity/evt_01",
                               headers=self.auth_header(self.ORGANIZER)).data.decode()
        self.page = page
        self.rows = {jid: {"n": int(n), "mean": float(mean), "var": float(var),
                           "flags": re.findall(r">([a-z0-9 ]+)</span>", flags)}
                     for jid, n, mean, var, flags in ROW.findall(page)}

    def test_one_row_per_judge_not_one_collapsed_row(self):
        self.assertEqual(len(self.rows), 30)

    def test_review_counts_match_fixture_exactly_for_every_judge(self):
        expected = defaultdict(int)
        for s in _fixture()["scores"]:
            expected[s["judge"]] += 1
        self.assertEqual({j: r["n"] for j, r in self.rows.items()}, dict(expected))
        self.assertEqual(sum(r["n"] for r in self.rows.values()), 126)

    def test_named_fixture_cases(self):
        for jid, n in (("jdg_07", 3), ("jdg_01", 1), ("jdg_23", 1), ("jdg_24", 11), ("jdg_26", 10)):
            self.assertEqual(self.rows[jid]["n"], n, jid)

    def test_means_and_variances_match_independent_computation(self):
        w = {"functionality": 0.40, "quality": 0.35, "innovation": 0.25}
        by = defaultdict(list)
        for s in _fixture()["scores"]:
            by[s["judge"]].append(sum(w[k] * v for k, v in s["criteria"].items()))
        for jid, vals in by.items():
            mean = sum(vals) / len(vals)
            var = sum((x - mean) ** 2 for x in vals) / len(vals)
            self.assertAlmostEqual(self.rows[jid]["mean"], mean, places=3, msg=jid)
            self.assertAlmostEqual(self.rows[jid]["var"], var, places=3, msg=jid)

    def test_zero_variance_flag_only_on_jdg_07(self):
        flagged = {j for j, r in self.rows.items() if "zero variance" in r["flags"]}
        self.assertEqual(flagged, {"jdg_07"})
        self.assertIn("Zero-variance judge — 3 reviews, all weighted raw scores = 4.00", self.page)

    def test_low_sample_flag_exactly_on_single_review_judges(self):
        flagged = {j for j, r in self.rows.items() if "1 review" in r["flags"]}
        singles = {j for j, r in self.rows.items() if r["n"] == 1}
        self.assertEqual(flagged, singles)
        self.assertEqual(flagged, {"jdg_01", "jdg_23"})

    def test_single_review_judge_is_not_also_called_zero_variance_in_flags(self):
        """n=1 has zero variance trivially; the dashboard distinguishes
        it as a low-sample case instead of double-flagging."""
        for jid in ("jdg_01", "jdg_23"):
            self.assertNotIn("zero variance", self.rows[jid]["flags"])

    def test_high_workload_matches_independent_median_rule(self):
        counts = defaultdict(int)
        for s in _fixture()["scores"]:
            counts[s["judge"]] += 1
        med = statistics.median(counts.values())
        expected = {j for j, c in counts.items() if c > 1.5 * med}
        flagged = {j for j, r in self.rows.items() if "high workload" in r["flags"]}
        self.assertEqual(flagged, expected)
        self.assertTrue({"jdg_24", "jdg_26"} <= flagged)

    def test_coverage_distribution_matches_fixture(self):
        per_project = defaultdict(int)
        for s in _fixture()["scores"]:
            per_project[s["project"]] += 1
        expected = defaultdict(int)
        for p in _fixture()["projects"]:
            expected[per_project.get(p["id"], 0)] += 1
        got = {int(a): int(b) for a, b in re.findall(r"<tr><td>(\d+)</td><td>(\d+)</td></tr>", self.page)}
        self.assertEqual(got, dict(expected))
        self.assertEqual(sum(got.values()), 41)

    def test_duplicate_candidate_lists_both_ids(self):
        block = self.page.split("Duplicate candidates")[1].split("Zero-variance judges")[0]
        self.assertIn("Dry Harbour", block)
        self.assertIn("prj_07", block)
        self.assertIn("prj_41", block)
        self.assertEqual(block.count("Potential duplicate submission"), 1)

    def test_missing_comment_count_matches_fixture(self):
        empty = sum(1 for s in _fixture()["scores"] if not (s.get("comment") or "").strip())
        m = re.search(r"(\d+) of (\d+) reviews have no comment", self.page)
        self.assertEqual((int(m.group(1)), int(m.group(2))), (empty, 126))

    def test_no_python_none_or_undefined_rendered(self):
        for bad in (">None<", "undefined", "nan"):
            self.assertNotIn(bad, self.page)

    def test_no_track_mismatches_in_the_fixture(self):
        block = self.page.split("Track mismatches")[1].split("Missing comments")[0]
        self.assertIn("None found", block)


class TestIntegrityEdgeCases(VerdictLedgerTestCase):

    def _get(self, event_id, token=None):
        return self.client.get(f"/organizer/integrity/{event_id}",
                               headers=self.auth_header(token or self.ORGANIZER))

    def test_unknown_event_is_404_not_an_empty_page(self):
        self.assertEqual(self._get("evt_nope").status_code, 404)

    def test_event_with_no_reviews_renders_cleanly(self):
        resp = self._get("evt_live_2026")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"No reviews recorded for this event.", resp.data)

    def test_track_mismatch_is_actually_detected(self):
        """Proves the mismatch panel is not permanently empty: register
        jdg_24 as eligible for nothing but a track they never reviewed,
        and their reviews in other tracks must surface."""
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("DELETE FROM judge_track_eligibility WHERE user_id='jdg_24'")
        other = conn.execute(
            "SELECT id FROM tracks WHERE event_id='evt_01' AND id NOT IN "
            "(SELECT p.track_id FROM scores s JOIN projects p ON p.id=s.project_id "
            " WHERE s.judge_id='jdg_24')").fetchone()
        conn.execute("INSERT INTO judge_track_eligibility (user_id, track_id) VALUES ('jdg_24', ?)",
                     (other["id"],))
        conn.commit()
        conn.close()

        page = self._get("evt_01").data.decode()
        block = page.split("Track mismatches")[1].split("Missing comments")[0]
        self.assertNotIn("None found", block)
        self.assertIn("jdg_24", block)
        self.assertEqual(block.count("jdg_24"), 11 + 0, "one mismatch per review jdg_24 made")

    def test_score_without_assignment_row_is_still_counted(self):
        """Regression for the second flaw in the old query: it joined on
        assignments, so a score with no assignment row vanished."""
        import db as db_module
        conn = db_module.get_connection()
        conn.execute("DELETE FROM assignments WHERE judge_id='jdg_07'")
        conn.commit()
        conn.close()
        page = self._get("evt_01").data.decode()
        rows = {jid: int(n) for jid, n, *_ in ROW.findall(page)}
        self.assertEqual(rows.get("jdg_07"), 3)

    def test_draft_duplicates_are_not_reported(self):
        import db as db_module
        from auth import now_iso
        conn = db_module.get_connection()
        team = conn.execute("SELECT team_id FROM projects WHERE id='prj_02'").fetchone()["team_id"]
        conn.execute(
            "INSERT INTO projects (id, event_id, team_id, title, status, created_at, updated_at) "
            "VALUES ('dup_draft', 'evt_01', ?, "
            "(SELECT title FROM projects WHERE id='prj_02'), 'draft', ?, ?)",
            (team, now_iso(), now_iso()))
        conn.commit()
        conn.close()
        block = self._get("evt_01").data.decode().split("Duplicate candidates")[1].split("Zero-variance")[0]
        self.assertNotIn("dup_draft", block)

    def test_only_organizers_may_view(self):
        self.assertEqual(self._get("evt_01", self.JUDGE_A).status_code, 403)
        self.assertEqual(self._get("evt_01", self.PARTICIPANT).status_code, 403)
        self.assertEqual(self.client.get("/organizer/integrity/evt_01").status_code, 401)


if __name__ == "__main__":
    unittest.main()
