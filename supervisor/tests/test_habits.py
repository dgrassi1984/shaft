"""Repeated work is counted across cycles, after filtering failed and bookkeeping calls."""
from datetime import datetime
import unittest
from unittest import mock

from supervisor import commitments, habits

NOW = datetime(2026, 11, 11, 12)


def episode(day, tools):
    return {"ts": f"2026-11-{day:02d}T10:00:00", "run_started": f"2026-11-{day:02d}T09:00:00",
            "tools": [{"tool": t, "result": "ok"} if isinstance(t, str) else t for t in tools]}


class HabitsTest(unittest.TestCase):
    def setUp(self):
        self.live = self.enterContext(mock.patch.object(habits.metrics, "live_episodes", return_value=[]))
        self.events = self.enterContext(mock.patch.object(habits.surprise, "events", return_value=[]))

    def test_threshold_counts_distinct_cycles_not_repeated_occurrences(self):
        """Three cycles qualify; many occurrences in two cycles do not."""
        self.live.return_value = [episode(d, ["a", "b", "a", "b", "c", "d"] if d < 3 else ["a", "b"])
                                  for d in (1, 2, 3)]
        self.assertEqual(habits.sequences("ember", as_of=NOW),
                         [{"tools": ["a", "b"], "cycles": 3, "last": "2026-11-03T10:00:00"}])

    def test_filter_then_collapse_preserves_only_successful_work(self):
        """Bookkeeping and every failed prefix disappear before adjacency and repeat collapse."""
        tools = ["a", *commitments.BOOKKEEPING, "a"]
        tools += [{"tool": "bad", "result": prefix + ": detail"} for prefix in commitments.FAILED]
        tools += ["b", "b"]
        self.live.return_value = [episode(d, tools) for d in (1, 2, 3)]
        self.assertEqual(habits.sequences("ember", as_of=NOW),
                         [{"tools": ["a", "b"], "cycles": 3, "last": "2026-11-03T10:00:00"}])

    def test_longest_sequence_wins_but_more_frequent_subsequence_survives(self):
        """Containment suppresses only patterns with equal cycle counts, with deterministic ordering."""
        self.live.return_value = [episode(d, ["a", "b", "c", "d"]) for d in (1, 2, 3)]
        self.assertEqual(habits.sequences("ember", as_of=NOW),
                         [{"tools": ["a", "b", "c", "d"], "cycles": 3, "last": "2026-11-03T10:00:00"}])
        self.live.return_value.append(episode(4, ["a", "b"]))
        self.assertEqual(habits.sequences("ember", as_of=NOW),
                         [{"tools": ["a", "b"], "cycles": 4, "last": "2026-11-04T10:00:00"},
                          {"tools": ["a", "b", "c", "d"], "cycles": 3, "last": "2026-11-03T10:00:00"}])

    def test_window_includes_start_and_excludes_as_of(self):
        """Later and old cycles cannot raise a pattern above the threshold."""
        self.live.return_value = [episode(d, ["a", "b"]) for d in (1, 2, 3, 4)]
        self.assertEqual(habits.sequences("ember", days=2, as_of=datetime(2026, 11, 3, 10)), [])
        self.assertEqual(habits.sequences("ember", days=3, as_of=datetime(2026, 11, 4, 10))[0]["cycles"], 3)

    def test_empty_record_and_single_tool_have_no_repetition(self):
        """Repeated calls to just one tool never become a tooling recommendation."""
        self.live.return_value = [episode(d, ["a", "a"]) for d in (1, 2, 3)]
        self.assertEqual(habits.sequences("ember", as_of=NOW), [])
        self.assertIn("(nothing repeated yet)", habits.record_text("ember", as_of=NOW))

    def test_record_uses_stats_classes_and_filters_recurrence_before_cutoff(self):
        """P2 targets merge across causes, omit sleep, and cannot learn from later episodes."""
        def event(day, tool="read_file", kind="live"):
            return {"episode": f"2026-11-{day:02d}T10:00:00", "episode_kind": kind,
                    "class": f"old-cause-{day}", "flag": "error", "tool": tool,
                    "target": f"skills/file{day}.md"}
        self.events.return_value = [event(1), event(2), event(3), event(1, "run"), event(2, "run"),
                                    event(11, "later"), event(12, "later"), event(1, "sleep", "sleep"),
                                    event(2, "sleep", "sleep")]
        text = habits.record_text("ember", as_of=datetime(2026, 11, 11, 10))
        self.assertIn("error:read_file:skills/*: 2026-11-01T10:00, 2026-11-02T10:00, 2026-11-03T10:00", text)
        self.assertLess(text.index("error:read_file"), text.index("error:run"))
        self.assertNotIn("later", text)
        self.assertNotIn("error:sleep", text)
        self.assertNotIn("(nothing repeated yet)", text)
        self.assertTrue(text.endswith("this is information only."))

    def test_record_formats_sequences_and_limits_each_part(self):
        """The record preserves the requested sequence text and bounds its output."""
        self.live.return_value = [episode(d, ["a", "b"]) for d in (1, 2, 3)]
        self.assertIn("a -> b: in 3 cycles, last 2026-11-03T10:00", habits.record_text("ember", as_of=NOW))
        self.assertNotIn("a -> b:", habits.record_text("ember", limit=0, as_of=NOW))
