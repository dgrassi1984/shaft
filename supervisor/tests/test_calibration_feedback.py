"""Calibration feedback at declare time (docs/plans/PLAN-calibration-feedback.md): version-safe scoring, the v3
question, and the paired before/after score. Each test fails if its bug returns."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from supervisor import metrics


def row(confidence, criterion_met="partial", **extra):
    return {"episode_ts": "2026-10-25T10:00:00", "confidence": confidence, "criterion_met": criterion_met, **extra}


def v3(confidence, first, criterion_met):
    return row(confidence, criterion_met, confidence_of=metrics.CONFIDENCE_V3, confidence_first=first)


class CalibrationFeedbackTest(unittest.TestCase):
    def test_unknown_question_is_excluded_not_v1(self):
        # Bug: any confidence_of other than v2 was pooled into v1, so a future question corrupted the v1 score.
        rows = [row(50, confidence_of="something-new"), row(50)]
        cal = metrics.calibration(rows)
        self.assertEqual(cal["v1"]["n"], 1)
        self.assertEqual(cal["excluded"], 1)
        self.assertEqual((cal["v2"]["n"], cal["v3"]["n"], cal["v3_first"]["n"]), (0, 0, 0))

    def test_v3_scored_binary_on_final_and_first_separately(self):
        cal = metrics.calibration([v3(60, 90, "partial")])
        self.assertAlmostEqual(cal["v3"]["brier"], 0.36)           # (0.6 - 0)^2: partial is not met
        self.assertAlmostEqual(cal["v3_first"]["brier"], 0.81)     # (0.9 - 0)^2
        self.assertEqual(cal["v3"]["n"], 1)
        self.assertEqual(cal["v2"]["n"], 0)                        # v3 never pools into v2
        self.assertEqual(cal["excluded"], 0)

    def test_v3_first_needs_confidence_first(self):
        cal = metrics.calibration([row(60, confidence_of=metrics.CONFIDENCE_V3)])
        self.assertEqual((cal["v3"]["n"], cal["v3_first"]["n"]), (1, 0))

    def test_paired_v3_counts_closer_and_further(self):
        paired = metrics.paired_v3([v3(60, 90, "partial"), v3(80, 50, "yes"), v3(70, 70, "no"),
                                    row(50), row(50, confidence_of=metrics.CONFIDENCE_V2)])
        self.assertEqual((paired["n"], paired["closer"], paired["further"], paired["changed"]), (3, 2, 0, 2))
        self.assertAlmostEqual(paired["mean_change"], 0.0)         # (-30 + 30 + 0) / 3
        self.assertAlmostEqual(paired["brier_first"], (0.81 + 0.25 + 0.49) / 3)
        self.assertAlmostEqual(paired["brier_final"], (0.36 + 0.04 + 0.49) / 3)

    def test_paired_v3_counts_further(self):
        paired = metrics.paired_v3([v3(90, 60, "no")])
        self.assertEqual((paired["closer"], paired["further"], paired["changed"]), (0, 1, 1))
        self.assertAlmostEqual(paired["mean_change"], 30.0)

    def test_paired_v3_empty(self):
        paired = metrics.paired_v3([row(50)])
        self.assertEqual(paired["n"], 0)
        self.assertIsNone(paired["brier_first"])
        self.assertIsNone(paired["brier_final"])
        self.assertIsNone(paired["mean_change"])

    def test_bands_constant_drives_calibration_bands(self):
        bands = metrics.calibration([row(50)])["v1"]["bands"]
        self.assertEqual([(b["lo"], b["hi"]) for b in bands],
                         [(lo, min(hi, 100)) for lo, hi in metrics.BANDS])

    def test_review_survives_v3_and_excluded(self):
        # Bug guard: review iterated calibration().items() and indexed c["n"]; "excluded" is an int, v3 a new key.
        rows = [{**r, "episode_ts": "2026-10-24T10:00:00", "judged_at": "2026-10-24T10:00:00"}
                for r in (row(50), v3(60, 90, "partial"), row(50, confidence_of="something-new"))]
        with mock.patch.object(metrics, "judgments", return_value=rows):
            text = metrics.review_text(["x"], now=datetime(2026, 10, 24, 11))
        self.assertIn("confidence v1 (probability of success, yes=1/partial=0.5) beforehand 50% vs judged 50%", text)


if __name__ == "__main__":
    unittest.main()
