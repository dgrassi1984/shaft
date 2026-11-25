"""Battery recall questions are redrawn when a comparison period changes anyway, from recent and old entries (#9).

Before #9 the six questions were drawn once and frozen. Weeks later every question asked about an entry over
RECALL_OLD_DAYS old, so "recall by the age of what was asked" compared nothing: the recent side read "no answer yet"
forever. These tests pin when a set is redrawn, that a new set spans both sides of RECALL_OLD_DAYS, and that a row
keeps its own entry's date so a later set (which reuses the item ids) never re-dates earlier answers.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import battery, judge

NOW = datetime(2026, 11, 25, 2, 0)
FMT = "%Y-%m-%d %H:%M"


class RecallRefreshTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="shaft-recall-")).resolve()
        self.addCleanup(shutil.rmtree, self.base, True)
        self.sup = self.base / "sup"
        self.instances = self.base / "instances"
        (self.instances / "rc" / "memory").mkdir(parents=True)
        self.era = {"version": "v1", "checksum": "c0", "model": "m1", "judge": "rotation:j1"}
        for p in (mock.patch.object(battery, "SUP_DIR", self.sup), mock.patch.object(battery, "INSTANCES", self.instances),
                  mock.patch.object(battery, "VERSION", "v1"),
                  mock.patch.object(battery, "instance_config", lambda n: {"LLM_MODEL": "m1",
                                                                            "CONTEXT_JOURNAL_CHARS": "100"}),
                  mock.patch.object(battery, "era", lambda n: dict(self.era)),
                  mock.patch.object(battery, "log", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        battery.items_dir().mkdir(parents=True)
        self.path = battery.items_dir() / "recall-rc.json"

    # -- helpers --------------------------------------------------------------------------------
    def write_set(self, generated: datetime, drawn_for: dict | None = None):
        self.path.write_text(json.dumps({"generated": generated.isoformat(timespec="seconds"),
                                         **({"for": drawn_for} if drawn_for else {}), "questions": [
            {"id": f"rc-{i}", "prompt": "q", "answer": "a", "entry": "2026-10-01 10:00", "max_steps": 8}
            for i in range(1, 7)]}))

    def write_rows(self, *rows: dict):
        p = battery.results_path("rc")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps({"item": "coding/x", "category": "coding", "arm": "instance",
                                         "status": "scored", "score": 5, **self.era, **r}) + "\n" for r in rows))

    def journal(self, days_ago: list[int]):
        entries = "".join(f"\n---\n## {(NOW - timedelta(days=d)).strftime(FMT)} — entry {d}\n\n{'x' * 200}\n"
                          for d in sorted(days_ago, reverse=True))
        (self.instances / "rc" / "memory" / "journal.md").write_text("# journal\n" + entries)

    # -- when a set is redrawn ---------------------------------------------------------------------
    def test_no_set_yet_is_drawn(self):
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "no recall questions yet")

    def test_a_set_in_the_same_period_under_28_days_is_kept(self):
        self.write_set(NOW - timedelta(days=20))
        self.write_rows({"at": (NOW - timedelta(days=5)).isoformat()})
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "")

    def test_a_set_28_days_old_is_redrawn(self):
        self.write_set(NOW - timedelta(days=28))
        self.write_rows({"at": (NOW - timedelta(days=5)).isoformat()})
        self.assertIn("28 days old", battery.recall_refresh_why("rc", NOW))

    def test_a_model_change_since_the_set_was_drawn_redraws(self):
        self.write_set(NOW - timedelta(days=10), {"model": "m0", "judge": "rotation:j1", "version": "v1"})
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "the model changed since the set was drawn")

    def test_a_judge_or_version_change_redraws(self):
        self.write_set(NOW - timedelta(days=10), {"model": "m1", "judge": "rotation:j0", "version": "v0"})
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "the judge, version changed since the set was drawn")

    def test_a_set_drawn_for_this_period_is_kept_whatever_ran_before(self):
        """A run cut short before its first row: the set drawn for the new period stays."""
        self.write_set(NOW - timedelta(days=1), {"model": "m1", "judge": "rotation:j1", "version": "v1"})
        self.write_rows({"at": (NOW - timedelta(days=5)).isoformat(), "model": "m0"})
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "")

    def test_a_refresh_that_could_not_be_drawn_stays_due_after_new_rows(self):
        """Review finding: too few entries kept the old set, the run wrote rows under the new model, and the next
        night compared with those rows and saw no change."""
        self.write_set(NOW - timedelta(days=10), {"model": "m0", "judge": "rotation:j1", "version": "v1"})
        self.write_rows({"at": (NOW - timedelta(hours=1)).isoformat()})   # already under m1
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "the model changed since the set was drawn")

    def test_a_legacy_set_compares_with_the_previous_run(self):
        self.write_set(NOW - timedelta(days=10))
        self.write_rows({"at": (NOW - timedelta(days=5)).isoformat(), "model": "m0"})
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "the model changed since the set was drawn")
        self.write_set(NOW - timedelta(days=1))   # drawn after that run: kept
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "")

    def test_the_control_arm_is_not_the_previous_run(self):
        self.write_set(NOW - timedelta(days=10))
        self.write_rows({"at": (NOW - timedelta(days=6)).isoformat()},
                        {"at": (NOW - timedelta(days=5)).isoformat(), "arm": "control", "model": "m0"})
        self.assertEqual(battery.recall_refresh_why("rc", NOW), "")

    # -- what a new set is drawn from ----------------------------------------------------------------
    def test_a_new_set_draws_from_both_sides_of_14_days(self):
        """The bug: a set drawn from an instance's whole history, then frozen, was all over 14 days old."""
        self.journal([3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 35, 40, 45, 50, 0])   # the newest stays in context
        asked = {}

        def ask(_system, prompt, _schema, **_kw):
            asked["stamps"] = [l.split("## Entry ")[1] for l in prompt.splitlines() if l.startswith("## Entry ")]
            return {"questions": [{"entry": s, "question": "q", "answer": "a"} for s in asked["stamps"]]}
        with mock.patch.object(judge, "pick", return_value="j"), mock.patch.object(judge, "ask", ask), \
                mock.patch.object(judge, "independence", return_value={}):
            self.assertEqual(battery.make_recall("rc", now=NOW), self.path)
        old_at = (NOW - timedelta(days=battery.RECALL_OLD_DAYS)).strftime(FMT)
        data = json.loads(self.path.read_text())
        self.assertEqual(data["for"], {"model": "m1", "judge": "rotation:j1", "version": "v1"})
        stamps = [q["entry"] for q in data["questions"]]
        self.assertEqual(stamps, asked["stamps"])
        self.assertEqual(sum(s >= old_at for s in stamps), 3, stamps)   # three recent ...
        self.assertEqual(sum(s < old_at for s in stamps), 3, stamps)    # ... and three old
        self.assertNotIn((NOW - timedelta(days=0)).strftime(FMT), stamps)

    def test_questions_naming_other_entries_are_refused(self):
        """Review finding: six questions all dated to one old entry would pass and read as all old."""
        self.journal([3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 35, 40, 45, 50, 0])

        def ask(_system, prompt, _schema, **_kw):
            first = [l.split("## Entry ")[1] for l in prompt.splitlines() if l.startswith("## Entry ")][0]
            return {"questions": [{"entry": first, "question": "q", "answer": "a"}] * 6}
        with mock.patch.object(judge, "pick", return_value="j"), mock.patch.object(judge, "ask", ask), \
                mock.patch.object(judge, "independence", return_value={}), self.assertRaises(judge.JudgeError):
            battery.make_recall("rc", now=NOW)
        self.assertFalse(self.path.exists())

    def test_exactly_14_days_is_old_when_drawn_as_when_read(self):
        cands = [((NOW - timedelta(days=d)).strftime(FMT), "t") for d in (40, 35, 30, 25, 20, 14)]
        old_at = (NOW - timedelta(days=battery.RECALL_OLD_DAYS)).strftime(FMT)
        self.assertTrue(all(s <= old_at for s, _ in battery.pick_recall(cands, NOW)))
        line = battery.recall_by_age("rc", [{"item": "recall/x", "category": "recall", "arm": "instance", "score": 5,
                                             "at": NOW.isoformat(), "entry": cands[-1][0]}])
        self.assertIn("14 days or older 5.0 (n=1)", line)

    def test_pick_fills_from_the_other_side_when_one_runs_short(self):
        cands = [((NOW - timedelta(days=d)).strftime(FMT), "t") for d in (40, 35, 30, 25, 20, 3)]
        got = battery.pick_recall(cands, NOW)
        self.assertEqual(len(got), battery.RECALL_ITEMS)
        self.assertEqual(sum(s >= (NOW - timedelta(days=14)).strftime(FMT) for s, _ in got), 1)

    def test_a_kept_set_is_not_redrawn(self):
        self.write_set(NOW - timedelta(days=3))
        before = self.path.read_text()
        with mock.patch.object(judge, "ask", side_effect=AssertionError("a kept set was redrawn")):
            self.assertEqual(battery.make_recall("rc", now=NOW), self.path)
        self.assertEqual(self.path.read_text(), before)

    def test_a_due_refresh_without_enough_entries_keeps_the_old_set(self):
        self.write_set(NOW - timedelta(days=40))
        self.journal([3, 4])
        with mock.patch.object(judge, "ask", side_effect=AssertionError("drawn from too few entries")):
            self.assertEqual(battery.make_recall("rc", now=NOW), self.path)
            self.assertIsNone(battery.make_recall("rc", force=True, now=NOW))

    def test_a_control_check_or_a_busy_instance_never_redraws_the_set(self):
        """Review finding: run() redrew the set before taking the instance's lock and before skipping recall for a
        control check, so either could pay for new questions and change the live checksum."""
        from supervisor import runner

        class Busy(Exception):
            pass

        class Held:
            def __init__(self, *_a):
                pass

            def acquire(self, wait=False):
                raise Busy

            def release(self):
                pass
        never = mock.patch.object(battery, "make_recall", side_effect=AssertionError("the recall set was redrawn"))
        with never, mock.patch.object(battery, "instance_root", lambda n: self.instances / n), \
                mock.patch.object(battery, "load_items", lambda n: []), \
                mock.patch.object(runner, "_Held", Held), mock.patch.object(runner, "Busy", Busy), \
                mock.patch.object(battery.signal, "signal", lambda *_: None):
            self.assertEqual(battery.run("rc", control_only=True), 0)
            self.assertEqual(battery.run("rc"), 0)   # busy: skipped before any redraw

    # -- reading recall by age ------------------------------------------------------------------------
    def test_a_row_keeps_its_entry_date(self):
        row = battery._row({"id": "recall/rc-1", "category": "recall", "entry": "2026-11-20 09:00"}, "instance",
                           self.era)
        self.assertEqual(row["entry"], "2026-11-20 09:00")
        self.assertNotIn("entry", battery._row({"id": "coding/x", "category": "coding"}, "instance", self.era))

    def test_by_age_uses_the_rows_entry_and_states_both_counts(self):
        self.write_set(NOW - timedelta(days=3))   # the current set's entries are all 2026-10-01
        with mock.patch.object(battery, "checksum", return_value="c0"):
            rows = [{"item": "recall/rc-1", "category": "recall", "arm": "instance", "score": 8,
                     "at": NOW.isoformat(), "entry": (NOW - timedelta(days=3)).strftime(FMT), "checksum": "c0"},
                    {"item": "recall/rc-2", "category": "recall", "arm": "instance", "score": 2,
                     "at": NOW.isoformat(), "checksum": "c0"},                       # an older row: the set's date
                    {"item": "recall/rc-3", "category": "recall", "arm": "instance", "score": 0,
                     "at": NOW.isoformat(), "checksum": "old"}]                      # another set's: not re-dated
            line = battery.recall_by_age("rc", rows)
        self.assertEqual(line, "recall by the age of what was asked: under 14 days old 8.0 (n=1); "
                               "14 days or older 2.0 (n=1)")

    def test_by_age_says_plainly_when_one_side_has_no_question(self):
        self.write_set(NOW - timedelta(days=3))
        with mock.patch.object(battery, "checksum", return_value="c0"):
            line = battery.recall_by_age("rc", [{"item": "recall/rc-1", "category": "recall", "arm": "instance",
                                                 "score": 6, "at": NOW.isoformat(), "checksum": "c0"}])
        self.assertIn("under 14 days old: no scored question (n=0)", line)
        self.assertIn("14 days or older 6.0 (n=1)", line)


if __name__ == "__main__":
    unittest.main()
