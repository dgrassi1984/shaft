"""#15: own_capability cycles graded on use seven days later (supervisor/use_regrade.py), replacing the same-cycle cap.
Each test names what reverting the change would break."""
import json
from datetime import datetime, timedelta
from unittest import mock

from supervisor import metrics, use_regrade
from supervisor.tests.test_idle_exit import IsolatedCase, NOW

BUILT = datetime(2026, 11, 2, 10, 0)


def call(tool, result="ok", **args):
    return {"tool": tool, "args": json.dumps(args), "result": result}


class UseRegradeTest(IsolatedCase):
    def setUp(self):
        super().setUp()
        self.episodes, self.judged = [], []

    def cycle(self, at, tools, kind="live"):
        self.episodes.append({"ts": at.isoformat(timespec="seconds"), "kind": kind, "tools": tools,
                              "intent": {"goal": "g", "origin": "self"}} if kind == "live" else
                             {"ts": at.isoformat(timespec="seconds"), "kind": kind, "tools": tools})
        self.write(self.root / "memory/episodes/2026-11-02.jsonl", self.episodes)

    def judge(self, at, value=3, uncapped=5, value_for="own_capability"):
        row = {"episode_ts": at.isoformat(timespec="seconds"), "judged_at": at.isoformat(), "criterion_met": "yes",
               "value": value, "value_for": value_for, "value_reason": "built a tool"}
        if uncapped is not None:
            row["value_uncapped"] = uncapped
        self.judged.append(row)
        self.write(self.sup / "judgments/ember.jsonl", self.judged)

    def changed(self, at, files):
        p = self.sup / "code_changes/ember.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({"ts": at.isoformat(timespec="seconds"), "files": files}) + "\n")

    def built_plugin(self):
        (self.root / "plugins/stale_rows").mkdir(parents=True)
        (self.root / "plugins/stale_rows/main.py").write_text("def run(**a): return ''\n")
        self.cycle(BUILT, [call("write_file", "wrote 30 characters", path="plugins/stale_rows/main.py"),
                           call("write_file", "wrote", path="tests/test_stale_rows.py")])
        self.changed(BUILT + timedelta(minutes=9), ["plugins/stale_rows/main.py", "tests/test_stale_rows.py"])
        self.judge(BUILT)

    def regrade(self):
        return use_regrade.run("ember", NOW), metrics.judgments("ember")[0]

    def test_kept_and_used_takes_the_uncapped_grade_and_both_are_kept(self):
        """Without it the same-cycle cap stands: a plugin called three days later stays at 3."""
        self.built_plugin()
        self.cycle(BUILT + timedelta(days=3), [call("stale_rows")])
        n, j = self.regrade()
        self.assertEqual(n, 1)
        self.assertEqual((j["value"], j["value_provisional"]), (5, 3))
        self.assertEqual(j["use"]["basis"], "kept and used")
        self.assertIn("plugins/stale_rows/main.py: stale_rows in live", j["use"]["used"][0])
        raw = json.loads((self.sup / "judgments/ember.jsonl").read_text())
        self.assertEqual(raw["value"], 3)   # the provisional grade stays in the judgments file
        self.assertIn("re-graded on use seven days on (the grades above): 1 — kept and used 1; value 3.0 provisional "
                      "→ 5.0 on use", metrics.use_regrade_line(metrics.judgments("ember")))
        self.assertEqual(use_regrade.run("ember", NOW), 0)   # graded once

    def test_kept_but_never_used_within_the_window_falls_to_one(self):
        self.built_plugin()
        self.cycle(BUILT + timedelta(days=8), [call("stale_rows")])   # after the seven days: does not count
        _, j = self.regrade()
        self.assertEqual((j["value"], j["use"]["basis"]), (1, "kept, not used within 7 days"))

    def test_a_job_running_the_plugin_is_use(self):
        self.built_plugin()
        day = lambda n: (BUILT + timedelta(days=n)).isoformat()   # noqa: E731
        self.write(self.sup / "jobs/ember.jsonl", [
            {"ts": day(1), "plugin": "stale_rows", "refused": "layout", "done": day(1)},   # Codex's review: never ran
            {"ts": day(2), "plugin": "stale_rows", "exit": 1, "timed_out": False}])
        self.assertEqual(self.regrade()[1]["value"], 1)
        (self.sup / "use_regrade/ember.jsonl").unlink()
        with (self.sup / "jobs/ember.jsonl").open("a") as f:
            f.write(json.dumps({"ts": day(3), "plugin": "stale_rows", "exit": 0, "timed_out": False}) + "\n")
        _, j = self.regrade()
        self.assertEqual((j["value"], j["use"]["used"]), (5, ["plugins/stale_rows/main.py: a job ran stale_rows"]))

    def test_code_the_gate_refused_is_not_kept(self):
        """No code_changes row for the run: the gate refused it, so nothing was kept, whatever a later call shows."""
        (self.root / "plugins/stale_rows").mkdir(parents=True)
        (self.root / "plugins/stale_rows/main.py").write_text("x")
        self.cycle(BUILT, [call("write_file", "wrote", path="plugins/stale_rows/main.py")])
        self.judge(BUILT)
        self.cycle(BUILT + timedelta(hours=1), [call("stale_rows")])
        self.changed(BUILT + timedelta(hours=1, minutes=5), ["plugins/stale_rows/main.py"])   # the next run's change
        _, j = self.regrade()
        self.assertEqual((j["value"], j["use"]["basis"], j["use"]["kept"]), (1, "not kept", []))

    def test_a_change_reverted_by_probation_is_not_kept(self):
        self.built_plugin()
        self.cycle(BUILT + timedelta(days=3), [call("stale_rows")])
        since = (BUILT + timedelta(minutes=9, seconds=2)).isoformat(timespec="seconds")
        self.write(self.sup / "probation/ember.jsonl", [
            {"at": since, "commit": "c" * 40, "since": since, "files": ["plugins/stale_rows/main.py"], "outcome": "started"},
            {"at": (BUILT + timedelta(days=2)).isoformat(), "commit": "c" * 40, "since": since,
             "files": ["plugins/stale_rows/main.py"], "outcome": "reverted"}])
        _, j = self.regrade()
        self.assertEqual((j["value"], j["use"]["basis"]), (1, "not kept"))

    def test_a_note_read_later_in_a_conversation_is_used(self):
        (self.root / "notes").mkdir()
        (self.root / "notes/queue-audit.md").write_text("rows")
        self.cycle(BUILT, [call("write_file", "wrote", path="notes/queue-audit.md")])
        self.judge(BUILT, value=2, uncapped=4)
        self.cycle(BUILT + timedelta(days=2), [call("read_file", "rows", path="notes/queue-audit.md")], kind="talk")
        _, j = self.regrade()
        self.assertEqual((j["value"], j["use"]["basis"]), (4, "kept and used"))

    def test_a_verdict_from_before_the_uncapped_grade_is_never_regraded(self):
        """Re-grading the history could only lower it (no uncapped grade was asked): the before/after measure of #15
        would compare a deflated past with a present that can rise."""
        self.built_plugin()
        del self.judged[0]["value_uncapped"]
        self.write(self.sup / "judgments/ember.jsonl", self.judged)
        n, j = self.regrade()
        self.assertEqual((n, j["value"]), (0, 3))
        self.assertNotIn("use", j)

    def test_a_new_module_counts_when_called_by_name_and_bookkeeping_is_not_traced(self):
        (self.root / "shaft/stale_scan.py").write_text("x")
        self.cycle(BUILT, [call("write_file", "wrote", path="shaft/stale_scan.py"),
                           call("append_file", "ok", path="memory/notes.md")])
        self.changed(BUILT + timedelta(minutes=3), ["shaft/stale_scan.py"])
        self.judge(BUILT)
        self.cycle(BUILT + timedelta(days=1), [call("run", "0 rows", command="python3 -m shaft.stale_scan")])
        _, j = self.regrade()
        self.assertEqual(j["use"]["artifacts"], ["shaft/stale_scan.py"])
        self.assertEqual(j["value"], 5)

    def test_nothing_written_keeps_the_provisional_grade(self):
        self.cycle(BUILT, [call("search_memory", "hits", query="x")])
        self.judge(BUILT)
        _, j = self.regrade()
        self.assertEqual((j["value"], j["use"]["basis"]), (3, "nothing to trace: no file written"))

    def test_only_own_capability_and_only_after_seven_days(self):
        self.built_plugin()
        self.judged[0]["value_for"] = "operator"
        self.write(self.sup / "judgments/ember.jsonl", self.judged)
        self.assertEqual(use_regrade.run("ember", NOW), 0)
        self.judged[0]["value_for"] = "own_capability"
        self.write(self.sup / "judgments/ember.jsonl", self.judged)
        self.assertEqual(use_regrade.run("ember", BUILT + timedelta(days=6)), 0)
        self.assertEqual(use_regrade.run("ember", NOW), 1)

    def test_a_plugin_removed_before_the_seventh_day_is_not_kept(self):
        self.built_plugin()
        self.cycle(BUILT + timedelta(days=2), [call("stale_rows")])
        (self.root / "plugins/stale_rows/main.py").unlink()
        _, j = self.regrade()
        self.assertEqual((j["value"], j["use"]["basis"]), (1, "not kept"))

    def test_whole_names_only_and_writing_a_name_is_not_use(self):
        """Codex's review: plugins/stale_rows matched plugins/stale_rows-v2, and a note naming it counted as use."""
        self.built_plugin()
        self.cycle(BUILT + timedelta(days=1), [call("read_file", "x", path="plugins/stale_rows-v2/main.py"),
                                               call("append_file", "ok", path="notes/todo.md",
                                                    content="look at plugins/stale_rows again"),
                                               call("read_file", "ERROR: no such file", path="plugins/stale_rows/x.py")])
        self.assertEqual(self.regrade()[1]["value"], 1)

    def test_a_later_runs_change_is_not_this_cycles_and_a_pause_settle_is(self):
        """Codex's review: an episode's ts is its end; a change settled two hours later belonged to another run, and
        one settled at a pause, before the end, to this one."""
        (self.root / "plugins/stale_rows").mkdir(parents=True)
        (self.root / "plugins/stale_rows/main.py").write_text("x")
        self.episodes.append({"ts": BUILT.isoformat(), "kind": "live", "run_started": (BUILT - timedelta(minutes=20)).isoformat(),
                              "tools": [], "intent": {"goal": "g"}})
        self.write(self.root / "memory/episodes/2026-11-02.jsonl", self.episodes)
        self.judge(BUILT)
        self.changed(BUILT + timedelta(hours=2), ["plugins/stale_rows/main.py"])
        self.cycle(BUILT + timedelta(days=1), [call("stale_rows")])
        self.assertEqual(self.regrade()[1]["use"]["basis"], "nothing to trace: no file written")
        (self.sup / "use_regrade/ember.jsonl").unlink()
        self.changed(BUILT - timedelta(minutes=5), ["plugins/stale_rows/main.py"])   # settled at a pause
        self.assertEqual(self.regrade()[1]["value"], 5)

    def test_a_malformed_episode_skips_only_its_own_cycle(self):
        self.built_plugin()
        self.episodes.append({"ts": (BUILT + timedelta(hours=1)).isoformat(), "kind": "live", "tools": [None, "x"]})
        self.episodes.append({"ts": "2026-11-03T10:00:00+01:00", "kind": "talk", "tools": [call("stale_rows")]})
        self.write(self.root / "memory/episodes/2026-11-02.jsonl", self.episodes)
        self.judge(BUILT + timedelta(hours=1))
        self.assertEqual(use_regrade.run("ember", NOW), 2)
        self.assertEqual([j["value"] for j in metrics.judgments("ember")], [5, 3])

    def test_the_regrades_live_apart_from_any_judgments_file(self):
        """Codex's review: judgments/<name>-use.jsonl is the judgments file of an instance named <name>-use."""
        self.built_plugin()
        use_regrade.run("ember", NOW)
        self.assertFalse((self.sup / "judgments/ember-use.jsonl").exists())
        self.assertTrue((self.sup / "use_regrade/ember.jsonl").exists())
        from supervisor import delete
        with mock.patch.object(delete, "SUP_DIR", self.sup):
            self.assertIn(self.sup / "use_regrade/ember.jsonl", delete.own_files("ember"))

    def test_a_replay_before_the_regrade_never_sees_it(self):
        """Codex's review: a trial replaying day two must not learn on day two what day seven found."""
        self.built_plugin()
        self.cycle(BUILT + timedelta(days=3), [call("stale_rows")])
        use_regrade.run("ember", NOW - timedelta(hours=1))
        live = json.loads(self.box()._record("judgments", format="json", since="2026-11-01 00:00"))["text"]
        self.assertIn("Value 5/5", live)
        with mock.patch.object(metrics, "judgments", wraps=metrics.judgments):
            replayed = self.box(replay=True)
            replayed.as_of = NOW - timedelta(hours=2)
            text = json.loads(replayed._record("judgments", format="json", since="2026-11-01 00:00"))["text"]
        self.assertIn("Value 3/5", text)

    def test_value_regrade_compares_with_the_judges_own_grade(self):
        """Codex's review: the dry run read the grade on use as the stored judge's and reported 5 → 3 as a fall."""
        self.built_plugin()
        self.cycle(BUILT + timedelta(days=3), [call("stale_rows")])
        use_regrade.run("ember", NOW)
        self.judged[0]["judge"] = "openrouter/x/judge-a"
        self.write(self.sup / "judgments/ember.jsonl", self.judged)
        with mock.patch.object(metrics.judge, "ask", return_value={"value": 3, "value_for": "own_capability",
                                                                   "value_reason": "a plugin"}), \
                mock.patch.object(metrics, "own_models", return_value=[]):
            out = metrics.value_regrade("ember")
        self.assertIn("| 3 → 3 |", out)
        self.assertIn("0 fell to <=3", out)

    def test_after_cycle_runs_it(self):
        self.built_plugin()
        metrics.after_cycle("ember")
        self.assertIn("use", metrics.judgments("ember")[0])


class UncappedTest(IsolatedCase):
    def test_the_uncapped_grade_is_never_below_value_nor_guessed(self):
        for raw, want in ((5, 5), (3, 3), (2, 3), (6, 3), ("5", 3), (None, 3), (True, 3)):
            with self.subTest(raw=raw):
                verdict = {"value": 3, "value_uncapped": raw}
                metrics._uncapped(verdict)
                self.assertEqual(verdict["value_uncapped"], want)
        verdict = {"value": None, "value_uncapped": 4}
        metrics._uncapped(verdict)
        self.assertIsNone(verdict["value_uncapped"])
        self.assertIn("value_uncapped is the value the work would get without that cap", metrics.JUDGE_SYSTEM)
