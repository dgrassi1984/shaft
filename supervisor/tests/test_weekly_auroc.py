"""#245: weekly confidence discrimination stays visible beside pooled AUROC."""
import unittest
from datetime import datetime, timedelta
from unittest import mock

from supervisor import metacog, metrics
from supervisor.tests.test_metacog import row


NOW = datetime(2026, 11, 20, 12)


def two_weeks():
    return [row(100 * (met if week == 0 else not met), met,
                episode_ts=(NOW - timedelta(days=7 * week + 1)).isoformat())
            for week in range(2) for met in (True, False) for _ in range(5)]


class WeeklyAurocTest(unittest.TestCase):
    def test_separate_weeks(self):
        """#245: perfect and inverted weeks must not inherit pooled discrimination."""
        rows = two_weeks()
        self.assertEqual(metacog.weekly(rows, NOW, weeks=2), [
            {"end": NOW, "n": 10, "auroc": 1.0},
            {"end": NOW - timedelta(days=7), "n": 10, "auroc": 0.0},
        ])
        self.assertEqual(metacog.summary(rows)["auroc"], 0.5)

    def test_review_pooled_and_weekly(self):
        """#245: review retains pooled text and appends nonempty weekly windows."""
        self.assertEqual(metacog.review_line(two_weeks(), now=NOW),
                         "confidence AUROC 0.50 [CI unavailable] (n 20), "
                         "claim AUROC unavailable, bias +0.00, SD 50.0; "
                         "by week (ending): 11-20 1.00 (n 10); 11-13 0.00 (n 10)")

    def test_sparse_class_unavailable(self):
        """#245: a week below MIN_CLASS for either outcome has no AUROC."""
        for minority_met in (True, False):
            with self.subTest(minority_met=minority_met):
                rows = [row(100 * met, met, episode_ts=NOW.isoformat())
                        for met in (True, False)
                        for _ in range(metacog.MIN_CLASS - (met == minority_met))]
                self.assertEqual(metacog.weekly(rows, NOW, weeks=1),
                                 [{"end": NOW, "n": 9, "auroc": None}])
                self.assertIn("by week (ending): 11-20 unavailable (n 9)",
                              metacog.review_line(rows, now=NOW))

    def test_boundaries_and_scoring_filters(self):
        """#245: windows are open on the left, closed on the right, with summary filters."""
        rows = [row(80, True, episode_ts=ts.isoformat()) for ts in
                (NOW, NOW - timedelta(days=7), NOW - timedelta(days=14),
                 NOW + timedelta(seconds=1))]
        rows += [{**row(80, True, episode_ts=NOW.isoformat()), **extra} for extra in
                 ({"judge": "retired"}, {"confidence_of": None},
                  {"confidence_of": metrics.CONFIDENCE_V4}, {"confidence": None})]
        rows.append(row(20, False, episode_ts=NOW.isoformat(),
                        confidence_of=metrics.CONFIDENCE_V3, criterion_met="partial"))
        with mock.patch.object(metrics, "by_current_judge", side_effect=lambda r: r["judge"] != "retired"):
            self.assertEqual(metacog.weekly(rows, NOW, weeks=2), [
                {"end": NOW, "n": 2, "auroc": None},
                {"end": NOW - timedelta(days=7), "n": 1, "auroc": None},
            ])

    def test_section_weekly_line_and_floor(self):
        """#245: stats places weekly evidence directly after pooled AUROC and keeps its floor."""
        with mock.patch.object(metrics, "judgments_for_stats", return_value=two_weeks()) as judgments, \
                mock.patch.object(metrics, "live_episodes", return_value=[]), \
                mock.patch.object(metacog, "datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW
            lines = metacog.section("example")
            self.assertTrue(lines[1].startswith("- Confidence AUROC 0.50"))
            self.assertEqual(lines[2], "- By week (ending): 11-20 1.00 (n 10); "
                             "11-13 0.00 (n 10); 0.55 or below is chance.")
            judgments.return_value = two_weeks()[:9]
            self.assertEqual(metacog.section("example"), [])
