import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
import unittest
from normalization import weighted_raw_score


class TestRubric(unittest.TestCase):

    def test_raw_score_calculation(self):
        weights = {"functionality": 0.4, "quality": 0.35, "innovation": 0.25}
        criteria = {"functionality": 4, "quality": 3, "innovation": 5}
        raw = weighted_raw_score(criteria, weights)
        expected = 0.4 * 4 + 0.35 * 3 + 0.25 * 5
        self.assertAlmostEqual(raw, expected)

    def test_raw_score_ignores_unknown_criteria(self):
        weights = {"functionality": 1.0}
        criteria = {"functionality": 4, "extra_field": 999}
        raw = weighted_raw_score(criteria, weights)
        self.assertAlmostEqual(raw, 4.0)


if __name__ == "__main__":
    unittest.main()
