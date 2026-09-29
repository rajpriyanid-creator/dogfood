import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import json
import math
import unittest

from normalization import (
    compute_global_stats, compute_judge_stats, normalize_one, run_normalization,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURES_PATH = os.path.join(REPO_ROOT, "fixtures.json")
DEMO_WEIGHTS = {"functionality": 0.40, "quality": 0.35, "innovation": 0.25}


class TestNormalization(unittest.TestCase):

    def test_ordinary_judge_matches_hand_computed_formula(self):
        """Compute the expected value by hand from the documented
        formula and compare - not merely 'is inside 1..5'."""
        all_scores = [3.0, 3.5, 4.0, 2.5, 3.0, 4.5]
        gs = compute_global_stats(all_scores)
        judge_scores = [3.0, 4.0, 3.5, 4.5]
        js = compute_judge_stats("j1", judge_scores, gs)

        n_j = 4
        mu_j = sum(judge_scores) / n_j
        v_j = sum((x - mu_j) ** 2 for x in judge_scores) / n_j
        k = 3
        mu_g = sum(all_scores) / len(all_scores)
        v_g = sum((x - mu_g) ** 2 for x in all_scores) / len(all_scores)
        mu_star = (n_j * mu_j + k * mu_g) / (n_j + k)
        v_star = (n_j * v_j + k * v_g) / (n_j + k)
        expected_z = (4.0 - mu_star) / math.sqrt(v_star)
        expected_n = mu_g + expected_z * math.sqrt(v_g)

        value, z = normalize_one(4.0, js, gs)
        self.assertFalse(js.zero_variance)
        self.assertAlmostEqual(z, expected_z)
        self.assertAlmostEqual(value, expected_n)

    def test_n_equals_1_falls_back_to_global_mean(self):
        gs = compute_global_stats([3.0, 3.5, 4.0, 2.5, 3.0, 4.5])
        js = compute_judge_stats("j1", [5.0], gs)
        self.assertEqual(js.n, 1)
        self.assertTrue(js.zero_variance)
        value, z = normalize_one(5.0, js, gs)
        self.assertAlmostEqual(value, gs.mean)
        self.assertIsNone(z)

    def test_shrinkage_pulls_small_samples_toward_global_more_than_large(self):
        """Same judge behaviour (mean 4.5), different n: shrinkage moves
        the small-n judge's mean further from their own raw mean (toward
        the global mean) than it moves the large-n judge's."""
        gs = compute_global_stats([3.0] * 20 + [4.0] * 20)
        small = compute_judge_stats("s", [4.0, 5.0], gs)
        large = compute_judge_stats("l", [4.0, 5.0] * 10, gs)
        self.assertLess(abs(large.shrunk_mean - large.mean), abs(small.shrunk_mean - small.mean))

    def test_zero_variance_judge_falls_back_to_global_mean(self):
        gs = compute_global_stats([3.0, 3.5, 4.0, 2.5, 3.0, 4.5])
        js = compute_judge_stats("j1", [4.0, 4.0, 4.0], gs)
        self.assertTrue(js.zero_variance)
        for raw in (4.0, 4.0, 4.0):
            value, z = normalize_one(raw, js, gs)
            self.assertEqual(value, gs.mean)
            self.assertIsNone(z)

    def test_missing_reviews_never_treated_as_zero(self):
        gs = compute_global_stats([3.0, 4.0, 3.5])
        js = compute_judge_stats("j1", [4.0, 4.0], gs)
        self.assertEqual(js.n, 2)
        self.assertAlmostEqual(js.mean, 4.0)  # not (4+4+0)/3

    def test_clipping_actually_clips_high(self):
        """Construct a case where the unclipped value is provably
        above hi, then check it is clamped to exactly hi."""
        gs = compute_global_stats([4.0, 4.5, 5.0, 4.0, 4.5])
        js = compute_judge_stats("j1", [1.0, 5.0, 1.0, 5.0], gs)
        # z is large and positive for raw=5.0 against this judge, and
        # the global sd is nonzero, so the unclipped value would exceed
        # 5 if the bound is lowered below it.
        unclipped, z = normalize_one(5.0, js, gs, lo=-100, hi=100)
        self.assertGreater(z, 0)
        tight_hi = unclipped - 0.1
        clipped, _ = normalize_one(5.0, js, gs, lo=-100, hi=tight_hi)
        self.assertAlmostEqual(clipped, tight_hi)

    def test_clipping_actually_clips_low(self):
        gs = compute_global_stats([4.0, 4.5, 5.0, 4.0, 4.5])
        js = compute_judge_stats("j1", [1.0, 5.0, 1.0, 5.0], gs)
        unclipped, z = normalize_one(1.0, js, gs, lo=-100, hi=100)
        self.assertLess(z, 0)
        tight_lo = unclipped + 0.1
        clipped, _ = normalize_one(1.0, js, gs, lo=tight_lo, hi=100)
        self.assertAlmostEqual(clipped, tight_lo)

    def test_bounds_are_configurable_not_hardcoded_1_to_5(self):
        """A 0..10 rubric must be able to produce values above 5."""
        reviews = [
            {"judge_id": "a", "project_id": "p1", "criteria": {"x": 9}},
            {"judge_id": "a", "project_id": "p2", "criteria": {"x": 3}},
            {"judge_id": "b", "project_id": "p1", "criteria": {"x": 8}},
            {"judge_id": "b", "project_id": "p2", "criteria": {"x": 4}},
        ]
        _, _, enriched = run_normalization(reviews, {"x": 1.0}, lo=0, hi=10)
        self.assertGreater(max(r["normalized"] for r in enriched), 5.0)

    def test_identical_raw_scores_from_one_judge_normalize_identically(self):
        gs = compute_global_stats([3.0, 3.5, 4.0])
        js = compute_judge_stats("j1", [3.0, 4.0], gs)
        self.assertEqual(normalize_one(3.5, js, gs), normalize_one(3.5, js, gs))

    def test_reproducibility_same_inputs_same_outputs(self):
        reviews = [
            {"judge_id": "j1", "project_id": "p1", "criteria": {"functionality": 4, "quality": 3, "innovation": 5}},
            {"judge_id": "j1", "project_id": "p2", "criteria": {"functionality": 3, "quality": 3, "innovation": 3}},
            {"judge_id": "j2", "project_id": "p1", "criteria": {"functionality": 5, "quality": 5, "innovation": 5}},
        ]
        _, _, e1 = run_normalization(reviews, DEMO_WEIGHTS)
        _, _, e2 = run_normalization(reviews, DEMO_WEIGHTS)
        self.assertEqual([r["normalized"] for r in e1], [r["normalized"] for r in e2])

    # ---- against the real fixture data -------------------------------

    def setUp(self):
        with open(FIXTURES_PATH, encoding="utf-8") as f:
            self.fixture = json.load(f)
        self.reviews = [
            {"judge_id": s["judge"], "project_id": s["project"], "criteria": s["criteria"]}
            for s in self.fixture["scores"]
        ]

    def test_fixture_zero_variance_judge_jdg_07(self):
        gs, judge_stats, enriched = run_normalization(self.reviews, DEMO_WEIGHTS)
        js = judge_stats["jdg_07"]
        self.assertEqual(js.n, 3)
        self.assertTrue(js.zero_variance)
        rows = [r for r in enriched if r["judge_id"] == "jdg_07"]
        for r in rows:
            self.assertAlmostEqual(r["normalized"], gs.mean)

    def test_fixture_low_sample_judges(self):
        gs, judge_stats, enriched = run_normalization(self.reviews, DEMO_WEIGHTS)
        for jid in ("jdg_01", "jdg_23"):
            self.assertEqual(judge_stats[jid].n, 1)
            # n=1 is a degenerate zero-variance case -> shrinks to global mean
            self.assertTrue(judge_stats[jid].zero_variance)

    def test_fixture_high_volume_judges_have_real_variance(self):
        gs, judge_stats, enriched = run_normalization(self.reviews, DEMO_WEIGHTS)
        for jid in ("jdg_24", "jdg_26"):
            self.assertGreater(judge_stats[jid].n, 5)
            self.assertGreater(judge_stats[jid].variance, 0)
            self.assertFalse(judge_stats[jid].zero_variance)

    def test_fixture_duplicate_project_both_present(self):
        project_ids = {r["project_id"] for r in self.reviews}
        self.assertIn("prj_07", project_ids)
        self.assertIn("prj_41", project_ids)

    def test_fixture_empty_comments_do_not_crash(self):
        """Some fixture comments are empty strings; the normalization
        engine never touches comments at all, so this just documents
        that the raw score computation is unaffected by comment content."""
        for s in self.fixture["scores"]:
            if s.get("comment") == "":
                # Should not raise, and criteria must still be present.
                self.assertIn("criteria", s)


if __name__ == "__main__":
    unittest.main()
