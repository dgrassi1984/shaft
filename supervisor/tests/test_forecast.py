"""Tests for template/shaft/forecast.py (PLAN-consciousness M9/D1): resource forecasts before choosing work."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import forecast   # noqa: E402


def _ep(ts, goal, origin="self", steps=5, at=None, tools=None, ended_by="control", finish=None):
    intent = {"goal": goal, "origin": origin}
    if at is not None:
        intent["at"] = at
    return {"ts": ts, "kind": "live", "intent": intent, "ended_by": ended_by,
            "steps": steps, "tools": tools or [], "finish": finish}


class ForecastFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-forecast-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "memory" / "episodes").mkdir(parents=True)
        self.now = datetime(2026, 10, 26, 12, 0)

    def append(self, ep):
        with (self.root / "memory" / "episodes" / "2026-10-26.jsonl").open("a") as f:
            f.write(json.dumps(ep) + "\n")

    def write_verdicts(self, rows):
        with (self.root / "memory" / "verdicts.jsonl").open("a") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")

    def goals(self):
        return [ep["intent"]["goal"] for ep in forecast.comparable(
            self.root, "map the orchard and count trees", "self", self.now)]


class ComparableTest(ForecastFixture):
    def test_ranking_origin_tie_break_and_zero_overlap_excluded(self):
        self.append(_ep("2026-10-20T10:00:00", "map the orchard", "self", at="2026-10-20T09:00:00"))
        self.append(_ep("2026-10-25T10:00:00", "map the orchard", "self", at="2026-10-25T09:00:00"))
        self.append(_ep("2026-10-24T10:00:00", "count trees", "operator", at="2026-10-24T09:00:00"))
        self.append(_ep("2026-10-23T10:00:00", "map the orchard and count trees", "self",
                        at="2026-10-23T09:00:00"))
        self.append(_ep("2026-10-22T10:00:00", "repair the fence", "self", at="2026-10-22T09:00:00"))
        self.assertEqual(self.goals(), [
            "map the orchard and count trees",   # overlap 1.0
            "map the orchard",                    # 0.5, same origin, newest of the two
            "map the orchard",                    # 0.5, same origin, older
            "count trees",                        # 0.5, different origin
        ])

    def test_k_cut(self):
        for i in range(8):
            self.append(_ep(f"2026-10-{i + 1:02d}T10:00:00", f"map the orchard {i}", "self",
                            at=f"2026-10-{i + 1:02d}T09:00:00"))
        got = forecast.comparable(self.root, "map the orchard", "self", self.now, k=3)
        self.assertEqual(len(got), 3)

    def test_only_ended_before_now_and_declared(self):
        self.append(_ep("2026-10-26T12:00:00", "map the orchard", "self", at="2026-10-26T11:00:00"))   # not before
        self.append(_ep("2026-10-25T10:00:00", "map the orchard", "self", at="2026-10-25T09:00:00"))
        self.append({"ts": "2026-10-24T10:00:00", "kind": "sleep"})                                  # not live
        self.append({"ts": "2026-10-23T10:00:00", "kind": "live", "tools": []})                      # no intent
        self.append(_ep("not-a-date", "map the orchard", "self"))                                     # malformed ts
        self.assertEqual(self.goals(), ["map the orchard"])


class SummariseTest(ForecastFixture):
    def test_medians_even_counts(self):
        eps = [
            _ep("2026-10-01T09:30:00", "g", steps=2, at="2026-10-01T09:00:00"),   # 30 min
            _ep("2026-10-01T10:20:00", "g", steps=8, at="2026-10-01T10:00:00"),   # 20 min
            _ep("2026-10-01T11:10:00", "g", steps=4, at="2026-10-01T11:00:00"),   # 10 min
            _ep("2026-10-01T12:40:00", "g", steps=6, at="2026-10-01T12:00:00"),   # 40 min
        ]
        s = forecast.summarise(eps, self.root)
        self.assertEqual(s["turns"], 5.0)     # (4 + 6) / 2
        self.assertEqual(s["minutes"], 25.0)  # (20 + 30) / 2

    def test_minutes_skip_missing_intent_at(self):
        eps = [
            _ep("2026-10-01T09:30:00", "g", at="2026-10-01T09:00:00"),   # 30 min
            _ep("2026-10-01T10:20:00", "g", at="2026-10-01T10:00:00"),   # 20 min
            _ep("2026-10-01T11:10:00", "g"),                              # no intent.at: skipped
            _ep("2026-10-01T12:40:00", "g", at="2026-10-01T12:00:00"),   # 40 min
        ]
        self.assertEqual(forecast.summarise(eps, self.root)["minutes"], 30.0)

    def test_tool_calls_exclude_controls_and_ran_out(self):
        eps = [
            _ep("2026-10-01T09:30:00", "g", tools=[{"tool": "web_search"}, {"tool": "web_search"},
                                                    {"tool": "web_search"}, {"tool": "declare_intent"}],
                ended_by="control", finish={"criterion_met": "yes"}),
            _ep("2026-10-01T10:20:00", "g", tools=[{"tool": "write_file"}, {"tool": "write_file"},
                                                    {"tool": "confirm_confidence"}, {"tool": "finish_cycle"}],
                ended_by="steps", finish={"criterion_met": "no"}),
            _ep("2026-10-01T11:10:00", "g", tools=[], ended_by="repeating", finish={"criterion_met": "partial"}),
            _ep("2026-10-01T12:40:00", "g", tools=[{"tool": "web_search"}], ended_by="control", finish=None),
        ]
        s = forecast.summarise(eps, self.root)
        self.assertEqual(s["tool_calls"], 1.5)   # [3, 2, 0, 1] -> (1 + 2) / 2
        self.assertEqual(s["ran_out"], 3)        # steps, repeating, and the one without a finish

    def test_met_and_judged_from_verdicts(self):
        eps = [{"ts": t, "kind": "live", "intent": {"goal": "g", "origin": "self"},
                "steps": 1, "tools": [], "ended_by": "control", "finish": {"criterion_met": "yes"}}
               for t in ("A", "B", "C", "D", "E")]
        self.write_verdicts([
            {"episode_ts": "A", "criterion_met": "yes"},
            {"episode_ts": "B", "criterion_met": "no"},
            {"episode_ts": "C", "criterion_met": "partial"},
            {"episode_ts": "D", "left_out": True},   # not judged
        ])
        s = forecast.summarise(eps, self.root)
        self.assertEqual(s["judged"], 3)
        self.assertEqual(s["met"], 1)


class TextTest(ForecastFixture):
    def test_off_and_unknown_mode_are_empty(self):
        self.assertEqual(forecast.text("off", self.root, "g", "self", self.now), "")
        self.assertEqual(forecast.text("bogus", self.root, "g", "self", self.now), "")

    def test_reserve_is_the_fixed_rule(self):
        self.assertEqual(forecast.text("reserve", self.root, "g", "self", self.now), forecast.RESERVE)

    def test_under_min_comparable(self):
        self.append(_ep("2026-10-20T10:00:00", "map the orchard", "self", at="2026-10-20T09:00:00"))
        self.assertEqual(forecast.text("on", self.root, "map the orchard", "self", self.now),
                         "No comparable past cycles to forecast from yet.")

    def _cycles(self):
        # 3 comparable cycles: turns median 9, minutes median 34, tool calls median 14, ran_out 0.
        self.append(_ep("2026-10-20T10:34:00", "map the orchard", "self", steps=9, at="2026-10-20T10:00:00",
                        tools=[{"tool": "web_search"}] * 14, finish={"criterion_met": "yes"}))
        self.append(_ep("2026-10-21T10:34:00", "map the orchard trees", "self", steps=7, at="2026-10-21T10:00:00",
                        tools=[{"tool": "web_search"}] * 10, finish={"criterion_met": "yes"}))
        self.append(_ep("2026-10-22T10:34:00", "map and count the orchard", "self", steps=11,
                        at="2026-10-22T10:00:00", tools=[{"tool": "web_search"}] * 18,
                        finish={"criterion_met": "no"}))

    def test_full_paragraph_and_no_warning(self):
        self._cycles()
        out = forecast.text("on", self.root, "map the orchard", "self", self.now,
                            turns_left=12, minutes_left=50, calls_left=58)
        self.assertTrue(out.startswith(
            "Forecast from my 3 most comparable past cycles: median 9 turns, 34 minutes, 14 tool calls; "
            "0 of 3 ran out of turns or time; the judge found the criterion met in 0 of 0 judged."))
        self.assertIn("This cycle has 12 turns, about 50 minutes and 58 supervisor calls left.", out)
        self.assertNotIn("Size the goal", out)

    def test_warning_on_turns(self):
        self._cycles()
        out = forecast.text("on", self.root, "map the orchard", "self", self.now,
                            turns_left=10, minutes_left=50, calls_left=58)   # 9 turns > 80% of 10
        self.assertIn("Size the goal so a checkable piece is done well within that.", out)

    def test_warning_on_minutes(self):
        self._cycles()
        out = forecast.text("on", self.root, "map the orchard", "self", self.now,
                            turns_left=12, minutes_left=40, calls_left=58)   # 34 min > 80% of 40
        self.assertIn("Size the goal", out)

    def test_warning_on_ran_out(self):
        self._cycles()
        for ts in ("2026-10-23T10:00:00", "2026-10-24T10:00:00", "2026-10-25T10:00:00"):
            self.append(_ep(ts, "map the orchard", "self", steps=3, at=ts[:11] + "09:00:00",
                            ended_by="steps", finish={"criterion_met": "no"}))
        out = forecast.text("on", self.root, "map the orchard", "self", self.now,
                            turns_left=100, minutes_left=100, calls_left=100)
        self.assertIn("Size the goal", out)

    def test_warning_on_ran_out_is_a_share(self):
        """The warning reads a share (30%), not a count of 3: 2 of 5 comparable cycles running out warns, 1 of 4
        does not (the first draft needed 3 whatever the number of comparable cycles)."""
        self._cycles()
        self.append(_ep("2026-10-25T11:00:00", "map the orchard", "self", steps=3, at="2026-10-25T10:50:00",
                        ended_by="steps", finish=None))
        quiet = forecast.text("on", self.root, "map the orchard", "self", self.now,
                              turns_left=100, minutes_left=100, calls_left=100)
        self.assertIn("1 of 4 ran out", quiet)
        self.assertNotIn("Size the goal", quiet)
        self.append(_ep("2026-10-25T12:00:00", "map the orchard", "self", steps=3, at="2026-10-25T11:50:00",
                        ended_by="repeating", finish=None))
        out = forecast.text("on", self.root, "map the orchard", "self", self.now,
                            turns_left=100, minutes_left=100, calls_left=100)
        self.assertIn("2 of 5 ran out", out)
        self.assertIn("Size the goal", out)

    def test_missing_left_values_left_out(self):
        self._cycles()
        out = forecast.text("on", self.root, "map the orchard", "self", self.now)
        self.assertNotIn("This cycle has", out)
        partial = forecast.text("on", self.root, "map the orchard", "self", self.now, turns_left=12)
        self.assertIn("This cycle has 12 turns left.", partial)
        self.assertNotIn("about ", partial)
        self.assertNotIn("supervisor calls", partial)


if __name__ == "__main__":
    unittest.main()
