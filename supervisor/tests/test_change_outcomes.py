"""Kept changes retain probation outcomes and historical reports cannot see later verdicts."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import core, metrics

NOW = datetime(2026, 11, 11, 12)
FILES = ["shaft/example.py"]


class ChangeOutcomesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-change-outcomes-")
        self.addCleanup(tmp.cleanup)
        self.sup = Path(tmp.name)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(metrics, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(core, "INSTANCES", self.sup / "agents"))
        self.enterContext(mock.patch.object(metrics, "INSTANCES", self.sup / "agents"))
        self.enterContext(mock.patch.object(metrics, "log"))
        self.enterContext(mock.patch.object(metrics, "judgments_for_stats", return_value=[]))
        self.enterContext(mock.patch.object(metrics, "_tools_by_cycle", return_value={}))

    def write(self, folder, rows, name="ember"):
        path = self.sup / folder / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def change(self, at=NOW):
        return {"ts": at.isoformat(timespec="seconds"), "files": FILES}

    def event(self, outcome="started", since=NOW, at=None, commit="a" * 40, trial=None):
        return {"at": (at or since).isoformat(timespec="seconds"), "since": since.isoformat(timespec="seconds"),
                "commit": commit, "files": FILES, "outcome": outcome, "trial": trial}

    def log_lines(self, lines):
        (self.sup / "supervisor.log").write_text("\n".join(
            f"[{at:%Y-%m-%d %H:%M:%S}] {name}: {message}" for at, name, message in lines) + "\n")

    def test_history_started_and_all_terminal_outcomes_and_trial_flag(self):
        for terminal, expected in [(None, "on probation"), ("passed", "passed"),
                                   ("kept, not judged", "kept, not judged"), ("reverted", "reverted"),
                                   ("not on probation", "not on probation")]:
            for trial in (None, "better", "worse"):
                with self.subTest(terminal=terminal, trial=trial):
                    self.write("code_changes", [self.change()])
                    start = self.event("not on probation" if terminal == "not on probation" else "started", trial=trial)
                    rows = [start]
                    if terminal and terminal != "not on probation":
                        rows.append(self.event(terminal, at=NOW + timedelta(hours=1), trial=trial))
                    self.write("probation", rows)
                    self.assertEqual(metrics.change_outcomes("ember"), [{"ts": NOW.isoformat(), "files": FILES,
                        "commit": "a" * 8, "outcome": expected, "tested": trial is not None}])

    def test_change_without_probation_is_unknown_and_not_claimed_untested(self):
        self.write("code_changes", [self.change()])
        self.assertEqual(metrics.change_outcomes("ember"), [{"ts": NOW.isoformat(), "files": FILES,
            "commit": None, "outcome": "unknown", "tested": None}])

    def test_tested_flag_comes_from_start_not_later_event(self):
        self.write("code_changes", [self.change()])
        self.write("probation", [self.event(), self.event("passed", at=NOW + timedelta(hours=1), trial="better")])
        self.assertFalse(metrics.change_outcomes("ember")[0]["tested"])

    def test_matching_window_includes_start_and_limit_but_excludes_distant_events(self):
        self.write("code_changes", [self.change()])
        for seconds, expected in [(-60, "unknown"), (0, "on probation"),
                                   (metrics.CHANGE_MATCH_S, "on probation"), (metrics.CHANGE_MATCH_S + 1, "unknown")]:
            with self.subTest(seconds=seconds):
                self.write("probation", [self.event(since=NOW + timedelta(seconds=seconds))])
                self.assertEqual(metrics.change_outcomes("ember")[0]["outcome"], expected)

    def test_probation_opened_before_change_does_not_match_it(self):
        self.write("code_changes", [self.change()])
        self.write("probation", [self.event(since=NOW - timedelta(seconds=1))])
        self.assertEqual(metrics.change_outcomes("ember")[0]["outcome"], "unknown")

    def test_matching_skips_earlier_and_much_later_probations(self):
        self.write("code_changes", [self.change()])
        self.write("probation", [self.event(since=NOW - timedelta(minutes=1), commit="b" * 40),
                                 self.event(since=NOW + timedelta(seconds=10), commit="c" * 40),
                                 self.event(since=NOW + timedelta(minutes=10), commit="d" * 40)])
        self.assertEqual(metrics.change_outcomes("ember")[0]["commit"], "c" * 8)

    def test_as_of_returns_open_state_before_later_pass_and_excludes_cutoff_change(self):
        self.write("code_changes", [self.change(), self.change(NOW + timedelta(hours=2))])
        self.write("probation", [self.event(trial="better"), self.event("passed", at=NOW + timedelta(hours=1))])
        self.assertEqual(metrics.change_outcomes("ember", NOW), [])
        row, = metrics.change_outcomes("ember", NOW + timedelta(minutes=30))
        self.assertEqual((row["outcome"], row["tested"]), ("on probation", True))
        row, = metrics.change_outcomes("ember", NOW + timedelta(hours=1))
        self.assertEqual(row["outcome"], "on probation")
        row, = metrics.change_outcomes("ember", NOW + timedelta(hours=2))
        self.assertEqual(row["outcome"], "passed")

    def test_torn_json_lines_do_not_hide_surrounding_history_or_changes(self):
        changes = self.write("code_changes", [self.change()])
        history = self.write("probation", [self.event()])
        for path in (changes, history):
            with path.open("a") as out:
                out.write('garbage\n{"torn":\n')
        with history.open("a") as out:
            out.write(json.dumps(self.event("passed", at=NOW + timedelta(hours=1))) + "\n")
        self.assertEqual(metrics.change_outcomes("ember")[0]["outcome"], "passed")

    def test_log_fallback_keeps_instance_outcomes_and_trials_separate(self):
        changes, lines = [], []
        cases = [("aaaaaaaa", "probation aaaaaaaa passed (0.50 vs baseline 0.40, chance 0.800, 6 cycles that used it)", "passed"),
                 ("bbbbbbbb", "probation bbbbbbbb: used in 0 of 24 rated cycles since — kept, too little use to judge it", "kept, not judged"),
                 ("cccccccc", "probation cccccccc: no baseline (0 rated cycles on local before it) — kept", "kept, not judged"),
                 ("dddddddd", "probation dddddddd failed (0.10 < 0.50): reverted — restored shaft/example.py to dddddddd^", "reverted")]
        for i, (commit, close, expected) in enumerate(cases):
            at = NOW + timedelta(hours=i)
            changes.append(self.change(at))
            lines += [(at, "ember", f"change {commit} on probation (baseline 0.4, n=6)"),
                      (at, "peer", f"change {commit} on probation (baseline 0.4, n=6)"),
                      (at, "peer", "kept change is trial peer-trial's (worse)"),
                      (at, "ember", "kept change is trial example-trial's (better)"),
                      (at + timedelta(minutes=10), "ember", close),
                      (at + timedelta(minutes=11), "peer", f"probation {commit} passed (0.9 vs baseline 0.4, chance 1.0, 6 cycles that used it)")]
        at = NOW + timedelta(hours=4)
        changes.append(self.change(at))
        lines.append((at, "ember", "change eeeeeeee not on probation: no cycle runs tests/test_example.py"))
        self.write("code_changes", changes)
        self.log_lines(lines)
        rows = metrics.change_outcomes("ember")
        self.assertEqual([r["outcome"] for r in rows], [c[2] for c in cases] + ["not on probation"])
        self.assertEqual([r["tested"] for r in rows], [True] * 4 + [False])
        self.assertEqual([r["commit"] for r in rows], [c[0] for c in cases] + ["eeeeeeee"])

    def test_log_restore_failure_alone_does_not_close_probation(self):
        self.write("code_changes", [self.change()])
        self.log_lines([(NOW, "ember", "change aaaaaaaa on probation (baseline 0.5, n=6)"),
                        (NOW + timedelta(hours=1), "ember",
                         "probation aaaaaaaa failed but its restore could not be done — kept on probation: gate failed")])
        self.assertEqual(metrics.change_outcomes("ember")[0]["outcome"], "on probation")

    def test_log_failed_verdict_then_restore_failure_stays_on_probation(self):
        # _verdict logs a failed measurement before check_probation tries the restore.
        self.write("code_changes", [self.change()])
        self.log_lines([(NOW, "ember", "change aaaaaaaa on probation (baseline 0.5, n=6)"),
                        (NOW + timedelta(hours=1), "ember",
                         "probation aaaaaaaa failed (0.10 vs baseline 0.50, chance 0.001, 6 cycles that used it)"),
                        (NOW + timedelta(hours=1), "ember",
                         "probation aaaaaaaa failed but its restore could not be done — kept on probation: gate failed")])
        self.assertEqual(metrics.change_outcomes("ember")[0]["outcome"], "on probation")

    def test_history_merges_with_log_instead_of_hiding_log_closes(self):
        """Creating history must not hide either older or newer passes in the log."""
        later = NOW + timedelta(hours=2)
        self.write("code_changes", [self.change(), self.change(later)])
        self.log_lines([(NOW, "ember", "change aaaaaaaa on probation (baseline 0.4, n=6)"),
                        (NOW + timedelta(minutes=10), "ember", "probation aaaaaaaa passed (0.5 vs baseline 0.4, chance 1.0, 6 cycles that used it)"),
                        (later, "ember", "change bbbbbbbb on probation (baseline 0.4, n=6)"),
                        (later + timedelta(minutes=10), "ember", "probation bbbbbbbb passed (0.5 vs baseline 0.4, chance 1.0, 6 cycles that used it)")])
        self.write("probation", [self.event(since=later, commit="b" * 40)])
        rows = metrics.change_outcomes("ember")
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["outcome"] for r in rows], ["passed", "passed"])

    def test_start_and_check_probation_append_history_for_open_and_closed_windows(self):
        metrics.start_probation("ember", "a" * 40, FILES, trial={"id": "example", "verdict": "better"})
        path = self.sup / "probation/ember.jsonl"
        opened, = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual((opened["commit"], opened["files"], opened["outcome"], opened["trial"]),
                         ("a" * 40, FILES, "started", "better"))
        with mock.patch.object(metrics, "_verdict", return_value={"keep": True, "rate": .5}):
            metrics.check_probation("ember")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual([r["outcome"] for r in rows], ["started", "passed"])
        self.assertEqual(rows[1]["since"], rows[0]["since"])
        self.assertEqual(json.loads((self.sup / "probation/ember.json").read_text()), [])

    def test_check_probation_records_unjudged_and_reverted_but_not_failed_restore(self):
        for verdict, why, expected in [({"keep": True}, None, ["started", "kept, not judged"]),
                                      ({"keep": False, "rate": .1, "base": .5, "n_after": 6, "n_before": 6, "p": .001},
                                       None, ["started", "reverted"]),
                                      ({"keep": False}, "gate failed", ["started"])]:
            with self.subTest(expected=expected):
                for suffix in ("json", "jsonl"):
                    (self.sup / f"probation/ember.{suffix}").unlink(missing_ok=True)
                metrics.start_probation("ember", "a" * 40, FILES)
                with mock.patch.object(metrics, "_verdict", return_value=verdict), \
                     mock.patch.object(metrics, "restore_change", return_value=why), \
                     mock.patch.object(metrics, "journal_note"):
                    metrics.check_probation("ember")
                rows = [json.loads(line) for line in (self.sup / "probation/ember.jsonl").read_text().splitlines()]
                self.assertEqual([r["outcome"] for r in rows], expected)
                self.assertEqual(len(json.loads((self.sup / "probation/ember.json").read_text())), int(bool(why)))

    def test_nonruntime_change_records_not_on_probation_without_open_window(self):
        metrics.start_probation("ember", "b" * 40, ["tests/test_example.py"])
        path = self.sup / "probation/ember.jsonl"
        row, = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(row["outcome"], "not on probation")
        self.assertFalse((self.sup / "probation/ember.json").exists())

    def test_log_only_start_and_history_only_close_complete_one_probation(self):
        """Starting history mid-probation must not turn its eventual pass into unknown."""
        self.write("code_changes", [self.change()])
        self.log_lines([(NOW, "ember", "change aaaaaaaa on probation (baseline 0.4, n=6)")])
        self.write("probation", [self.event("passed", at=NOW + timedelta(minutes=10))])
        self.assertEqual(metrics.change_outcomes("ember"), [{"ts": NOW.isoformat(), "files": FILES,
            "commit": "aaaaaaaa", "outcome": "passed", "tested": False}])

    def test_duplicate_sources_cannot_assign_one_probation_to_two_changes(self):
        """A duplicated start must not consume a second change; history metadata wins."""
        self.write("code_changes", [self.change(), self.change(NOW + timedelta(seconds=1))])
        self.log_lines([(NOW, "ember", "change aaaaaaaa on probation (baseline 0.4, n=6)"),
                        (NOW + timedelta(minutes=10), "ember", "probation aaaaaaaa passed (0.5 vs baseline 0.4)")])
        self.write("probation", [self.event(since=NOW + timedelta(seconds=2), trial="better"),
                                 self.event("passed", at=NOW + timedelta(minutes=20))])
        rows = metrics.change_outcomes("ember", NOW + timedelta(minutes=15))
        self.assertEqual([(r["outcome"], r["tested"]) for r in rows],
                         [("on probation", True), ("unknown", None)])
        self.assertEqual([r["outcome"] for r in metrics.change_outcomes("ember")], ["passed", "unknown"])

    def test_trial_marks_immediate_nonruntime_change_not_older_runtime_probation(self):
        """A test-only change's trial must not be attributed to an older open probation."""
        later = NOW + timedelta(seconds=30)
        self.write("code_changes", [self.change(), {"ts": later.isoformat(), "files": ["tests/test_example.py"]}])
        self.log_lines([(NOW, "ember", "change aaaaaaaa on probation (baseline 0.4, n=6)"),
                        (later, "ember", "change bbbbbbbb not on probation: no cycle runs tests/test_example.py"),
                        (later, "ember", "kept change is trial example-trial's (better)")])
        self.assertEqual([(r["outcome"], r["tested"]) for r in metrics.change_outcomes("ember")],
                         [("on probation", False), ("not on probation", True)])


if __name__ == "__main__":
    unittest.main()
