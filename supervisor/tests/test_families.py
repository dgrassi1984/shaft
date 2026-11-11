"""Goal-family reports use judged links and count yield on the family's own yardstick."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import families, metrics


class FamiliesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-families-test-")
        self.addCleanup(tmp.cleanup)
        self.sup = Path(tmp.name)
        self.rows = []
        self.episodes = []
        self.enterContext(mock.patch.object(metrics, "change_outcomes", return_value=[]))
        self.enterContext(mock.patch.object(metrics, "judgments", return_value=self.rows))
        self.enterContext(mock.patch.object(metrics, "live_episodes", return_value=self.episodes))

    def row(self, minute, resembles=None, **fields):
        ts = (datetime(2026, 1, 1) + timedelta(minutes=minute)).isoformat()
        row = {"episode_ts": ts, "goal": f"Goal {minute}", "judge": "cloud", "value": 1,
               "value_for": "operator", "novelty": 2, "delivery_supported": "no", **fields}
        if resembles is not None:
            row["resembles"] = resembles
        self.rows.append(row)
        return ts

    def chain(self, minutes, **fields):
        previous = None
        for minute in minutes:
            previous = self.row(minute, previous if previous else None, **fields)

    def log(self, directory, rows):
        path = self.sup / directory / "ember.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_transitive_chain_normalizes_link_separators(self):
        """A <- B <- C is one family even when links use spaces instead of T."""
        a = self.row(0, novelty=3, value=0)
        b = self.row(10, a[:16].replace("T", " "), value=None)
        c = self.row(20, b[:16], value=4, delivery_supported="yes", goal="x" * 130)
        group, = families.families("ember")
        self.assertEqual(group["members"], [a, b, c])
        self.assertEqual((group["id"], group["goal"], group["cycles"]), (a[:16], "x" * 120, 3))
        self.assertEqual((group["mean_value"], group["deliveries"], group["last_delivery"], group["last_new"]),
                         (2, 1, c, a))

    def test_unknown_and_absent_links_keep_singletons(self):
        """Old rows and unknown targets do not merge unrelated goals."""
        self.row(0)
        self.row(10)
        self.row(20, "2025-12-31 23:00")
        groups = families.families("ember")
        self.assertEqual([g["cycles"] for g in groups], [1, 1, 1])
        self.assertEqual([g["latest"] for g in groups], [r["episode_ts"] for r in reversed(self.rows)])

    def test_as_of_excludes_boundary_and_later_rows(self):
        """A historical report excludes the exact cutoff and later cycles."""
        self.chain([0, 10, 20])
        group, = families.families("ember", as_of=datetime(2026, 1, 1, 0, 10))
        self.assertEqual(group["members"], ["2026-01-01T00:00:00"])

    def test_changes_use_episode_windows_and_only_a_passed_probation_survives(self):
        """A change belongs to the family of the cycle whose run window holds it; a change in an unjudged cycle or
        outside every window belongs to none; only a passed probation counts as survived (never reverted, open,
        not judged or unknown)."""
        a = self.row(10, value_for="own_capability")
        b = self.row(30, a[:16], value_for="own_record")
        self.episodes[:] = [{"ts": a, "run_started": "2026-01-01T00:00:00"},
                            {"ts": "2026-01-01T00:20:00", "run_started": "2026-01-01T00:13:00"},
                            {"ts": b}]
        outcomes = {"00:00:00": "passed", "00:05:00": "passed", "00:12:00": "passed", "00:18:00": "passed",
                    "00:25:00": "reverted", "00:26:00": "on probation", "00:27:00": "kept, not judged",
                    "00:28:00": "unknown", "00:32:00": "passed", "00:32:01": "passed"}
        changes = [{"ts": f"2026-01-01T{s}", "files": ["example.py"], "commit": None, "outcome": o, "tested": False}
                   for s, o in outcomes.items()]

        def outcomes_as_of(name, as_of=None):
            return [c for c in changes if as_of is None or datetime.fromisoformat(c["ts"]) < as_of]
        with mock.patch.object(metrics, "change_outcomes", side_effect=outcomes_as_of):
            group, = families.families("ember")
            self.assertEqual((group["changes_kept"], group["changes_survived"]), (7, 3))
            self.assertEqual(families.no_yield_share("ember", datetime(2026, 1, 1)),
                             {"judged": 2, "in_no_yield_families": 0, "share": 0})
            historical, = families.families("ember", as_of=datetime(2026, 1, 1, 0, 11))
            self.assertEqual(historical["changes_kept"], 1)

    def test_capability_text_uses_probation_not_mean_value(self):
        """Both reports read capability work on its probation yardstick."""
        self.chain([0, 10], value_for="own_capability", value=0)
        text = families.record_text("ember")
        stats = "\n".join(families.stats_lines("ember"))
        for report in (text, stats):
            self.assertIn("read on its own yardstick: 0 changes kept / 0 survived probation", report)
            self.assertNotIn("mean value", report)
            self.assertNotIn("deliveries:", report)
        self.assertIn("the judge samples cycles; unjudged ones are not here", text)

    def test_capability_requires_strict_majority_of_integer_ratings(self):
        """Missing ratings and ties cannot turn operator work into capability work."""
        self.chain([0, 10], value_for="own_capability")
        self.row(20, self.rows[-1]["episode_ts"][:16], value_for="operator")
        self.row(30, self.rows[-1]["episode_ts"][:16], value=None, value_for="operator")
        self.assertTrue(families.families("ember")[0]["capability"])
        self.rows[-1]["value"] = 1
        self.assertFalse(families.families("ember")[0]["capability"])

    def test_regular_requires_five_evenly_spaced_cycles(self):
        """Short runs, zero gaps and uneven recurrence do not suggest a scheduled job; exact links retain seconds."""
        for minutes, expected, interval in [([0, 60, 120, 180, 240], True, 60),
                                             ([0, 60, 120, 180], False, None),
                                             ([0, 10, 20, 30, 100], False, 10),
                                             ([0, 0.1, 0.2, 0.3, 0.4], True, 0.1)]:
            with self.subTest(minutes=minutes):
                self.rows.clear()
                self.chain(minutes)
                group, = families.families("ember")
                self.assertEqual((group["regular"], group["interval_minutes"]), (expected, interval))
        self.rows.clear()
        self.chain([0, 0, 0, 0, 0])
        self.assertEqual([(g["cycles"], g["regular"], g["interval_minutes"]) for g in families.families("ember")],
                         [(1, False, None)] * 5)
        self.rows.clear()
        self.chain([0, 60, 120, 180, 240])
        self.assertIn("recurs about every 1 h (5 cycles): a scheduled job could run this without a model",
                      families.record_text("ember"))

    def test_no_yield_share_counts_windowed_members_using_full_families(self):
        """The denominator includes singletons, while families retain their pre-window history."""
        self.chain([0, 10, 20])
        self.chain([30, 40], value_for="own_capability", value=5)
        self.chain([50, 60], value=1, delivery_supported="yes")
        self.row(70, value=0)
        self.chain([80, 90], value=None)
        result = families.no_yield_share("ember", datetime(2026, 1, 1, 0, 10), datetime(2026, 1, 1, 1, 10))
        self.assertEqual(result, {"judged": 6, "in_no_yield_families": 4, "share": 4 / 6})
        self.assertEqual(families.no_yield_share("ember", datetime(2027, 1, 1)),
                         {"judged": 0, "in_no_yield_families": 0, "share": None})

    def test_unknown_value_and_value_two_are_not_below_threshold(self):
        """Unknown value is not evidence of low yield, and two is the exclusive threshold."""
        self.chain([0, 10], value=None)
        self.chain([20, 30], value=2)
        self.assertEqual(families.no_yield_share("ember", datetime(2026, 1, 1)),
                         {"judged": 4, "in_no_yield_families": 0, "share": 0})

    def test_record_empty_case_counts_singletons(self):
        """An empty report explains the sample and does not hide singleton cycles from its total."""
        expected = ("Goal families over my 0 judged cycles (the judge samples cycles; unjudged ones are not here). "
                    "A family is the chain of goals the judge said resemble each other.\n\n"
                    "(no family of two or more cycles yet)")
        self.assertEqual(families.record_text("ember"), expected)
        self.row(0)
        self.assertEqual(families.record_text("ember"), expected.replace("my 0", "my 1"))
        self.assertEqual(families.stats_lines("ember"), [])

    def test_stats_adds_regular_families_beyond_top_three(self):
        """Stats retain lower-ranked regular work without duplicating the top three."""
        for start in (0, 1000, 2000):
            self.chain(range(start, start + 6))
        self.chain([3000, 3060, 3120, 3180, 3240])
        self.chain([4000, 4001])
        lines = families.stats_lines("ember")
        self.assertEqual(lines[0], "## My goal families (top 3 by cycles)")
        self.assertEqual(lines[-1], "")
        self.assertEqual(len(lines), 6)
        self.assertIn("Goal 3240", lines[-2])
        self.assertEqual(families.record_text("ember", limit=2).count("mean value:"), 2)


if __name__ == "__main__":
    unittest.main()
