"""Tests for the transparent likely-near-miss review heuristic."""
import unittest

import rank_likely_near_misses as ranker


class LikelyNearMissRankingTests(unittest.TestCase):
    def score(self, row):
        fields = {ranker.key(name): name for name in row}
        return ranker.score_row(row, fields)

    def test_all_evidence_gates_produce_likely_review_label(self):
        row = {
            "observed_min_separation_in_window_m": "100",
            "distance_threshold_used_m": "200",
            "minimum_dcpa_m": "100",
            "baseline_course_difference_deg": "21",
            "tcpa_at_minimum_dcpa_s": "180",
            "maneuver_evidence_a": "True",
            "maneuver_evidence_b": "False",
            "synchronized_baseline_samples": "4",
            "synchronized_encounter_samples": "6",
        }
        result = self.score(row)
        self.assertEqual(result["likely_near_miss_score_8"], 8)
        self.assertEqual(result["likely_near_miss_classification"], "likely_near_miss_for_human_review")

    def test_close_pass_without_avoidance_is_not_labeled_likely(self):
        row = {
            "observed_min_separation_in_window_m": "100",
            "distance_threshold_used_m": "200",
            "minimum_dcpa_m": "100",
            "baseline_course_difference_deg": "21",
            "tcpa_at_minimum_dcpa_s": "180",
            "maneuver_evidence_a": "False",
            "maneuver_evidence_b": "False",
            "synchronized_baseline_samples": "4",
            "synchronized_encounter_samples": "6",
        }
        result = self.score(row)
        self.assertEqual(result["likely_near_miss_score_8"], 6)
        self.assertEqual(result["likely_near_miss_classification"], "elevated_priority_candidate")

    def test_inadequate_track_coverage_prevents_likely_label(self):
        row = {
            "observed_min_separation_in_window_m": "100",
            "distance_threshold_used_m": "200",
            "minimum_dcpa_m": "100",
            "baseline_course_difference_deg": "21",
            "tcpa_at_minimum_dcpa_s": "180",
            "maneuver_evidence_a": "True",
            "maneuver_evidence_b": "False",
            "synchronized_baseline_samples": "0",
            "synchronized_encounter_samples": "6",
        }
        self.assertNotEqual(self.score(row)["likely_near_miss_classification"],
                            "likely_near_miss_for_human_review")


if __name__ == "__main__":
    unittest.main()
