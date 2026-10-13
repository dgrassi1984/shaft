"""Measurement that can show improvement and autonomy (PLAN-measurement.md): judged origin, difficulty and
novelty; difficulty-weighted stats and probation; the capability battery's isolation, scoring and schedule."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, battery, judge, metrics


def judgment(ts: str, met: str, difficulty=None, origin=None, **kw) -> dict:
    return {"episode_ts": ts, "criterion_met": met, "confidence": 70, "claimed": met, "claim_supported": True,
            "criterion_quality": "checkable", "failure_mode": "", "goal": f"goal at {ts}", "reasoning": "r",
            "judge": "j", "difficulty": difficulty, "novelty": 2 if difficulty else None, "origin": origin, **kw}


class InstanceDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-meas-")).resolve()
        self.instances = self.tmp / "instances"
        self.root = self.instances / "m"
        for d in ("memory/episodes", "memory/messages", "self", "inbox"):
            (self.root / d).mkdir(parents=True)
        self.sup = self.tmp / "sup"
        self.patches = [mock.patch.object(metrics, "INSTANCES", self.instances),
                        mock.patch.object(metrics, "SUP_DIR", self.sup)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def episode(self, rec: dict) -> None:
        with (self.root / "memory" / "episodes" / "2026-10-13.jsonl").open("a") as f:
            f.write(json.dumps(rec) + "\n")


class CycleValueTest(unittest.TestCase):
    def test_easy_successes_are_worth_less_than_a_hard_partial(self):
        easy = metrics.cycle_value(judgment("t", "yes", 1))
        hard_partial = metrics.cycle_value(judgment("t", "partial", 4))
        self.assertAlmostEqual(easy, 0.2)
        self.assertAlmostEqual(hard_partial, 0.4)
        self.assertEqual(metrics.cycle_value(judgment("t", "no", None)), 0.0)
        self.assertIsNone(metrics.cycle_value(judgment("t", "yes", None)))   # unrated: never guessed


class JudgingTest(InstanceDirCase):
    VERDICT = {"criterion_met": "yes", "claim_supported": True, "criterion_quality": "checkable",
               "failure_mode": "", "reasoning": "ok", "origin": "operator", "origin_supported": True,
               "origin_evidence": "Daniele's message", "difficulty": 3, "difficulty_reason": "r", "novelty": 4}

    def test_a_judged_cycle_records_origin_difficulty_and_novelty(self):
        (self.root / "memory" / "messages" / "20261013-120000-000001-from-operator.md").write_text(
            "From: operator\n\nPlease research the llama.cpp release. MESSAGE-CANARY")
        self.episode({"ts": "2026-10-13T12:05:00", "kind": "talk", "role": "user", "content": "TALK-CANARY"})
        self.episode({"ts": "2026-10-09T12:05:00", "kind": "talk", "role": "user", "content": "TOO-OLD-CANARY"})
        self.episode({"ts": "2026-10-13T12:10:00", "kind": "live", "inbox": ["20261013-120000-000001-from-operator.md"],
                      "intent": {"goal": "research llama.cpp", "success_criterion": "note", "confidence": 60,
                                 "origin": "operator", "origin_ref": "inbox message"},
                      "finish": {"criterion_met": "yes"}, "tools": []})
        seen = {}

        def ask(system, case, schema, purpose, **kw):
            seen["case"], seen["schema"] = case, schema
            return dict(self.VERDICT)
        with mock.patch.object(judge, "available", return_value=True), mock.patch.object(judge, "ask", ask):
            self.assertEqual(metrics.judge_pending("m"), 1)
        j = metrics.judgments("m")[0]
        self.assertEqual((j["origin"], j["origin_declared"], j["difficulty"], j["novelty"]), ("operator", "operator", 3, 4))
        self.assertEqual(j["origin_ref"], "inbox message")
        self.assertIn("MESSAGE-CANARY", seen["case"])                 # the message the cycle read
        self.assertIn("TALK-CANARY", seen["case"])                    # a conversation in the last 72 h
        self.assertNotIn("TOO-OLD-CANARY", seen["case"])
        self.assertIn("difficulty", seen["schema"]["required"])

    def test_an_out_of_range_rating_leaves_the_cycle_unjudged(self):
        self.episode({"ts": "2026-10-13T12:10:00", "kind": "live", "intent": {"goal": "g", "success_criterion": "c",
                      "confidence": 50}, "finish": None, "tools": []})
        with mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "ask", return_value={**self.VERDICT, "difficulty": 9}):
            self.assertEqual(metrics.judge_pending("m"), 0)
        self.assertEqual(metrics.judgments("m"), [])                  # retried later, never written half-empty

    def test_backfill_rates_old_cycles_and_keeps_their_verdicts(self):
        self.episode({"ts": "2026-10-12T17:24:00", "kind": "live", "intent": {"goal": "reply to Daniele",
                      "success_criterion": "c", "confidence": 80}, "finish": {"criterion_met": "yes"}, "tools": []})
        path = self.sup / "judgments" / "m.jsonl"
        path.parent.mkdir(parents=True)
        old = {k: v for k, v in judgment("2026-10-12T17:24:00", "partial").items() if k not in metrics.RATINGS}
        rule = {**old, "episode_ts": "2026-10-12T18:00:00", "judge": "rule", "criterion_met": "no"}
        path.write_text(json.dumps(old) + "\n" + json.dumps(rule) + "\n")
        ratings = {k: self.VERDICT[k] for k in metrics.RATINGS}
        with mock.patch.object(judge, "ask", return_value=dict(ratings)) as ask:
            self.assertEqual(metrics.backfill("m"), 1)
            self.assertIs(ask.call_args.args[2], metrics.RATING_SCHEMA)   # ratings only, no new verdict
        a, b = metrics.judgments("m")
        self.assertEqual(a["criterion_met"], "partial")
        self.assertEqual((a["origin"], a["difficulty"]), ("operator", 3))
        self.assertIsNone(b["difficulty"])                            # rule-judged: rated empty, not asked
        with mock.patch.object(judge, "ask") as ask:
            self.assertEqual(metrics.backfill("m"), 0)                # idempotent
            ask.assert_not_called()

    def test_stats_lead_with_achieved_difficulty_and_origins(self):
        js = ([judgment(f"2026-10-13T0{i}:00:00", "yes", 1, "self", origin_declared="self") for i in range(4)]
              + [judgment("2026-10-13T05:00:00", "partial", 4, "operator", origin_declared="self")])
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.stats_text("m")
        self.assertLess(text.index("Achieved difficulty"), text.index("Judged success rate"))
        self.assertIn("**1.2**", text)                                # (4×1 + 0.5×4) / 5 cycles
        self.assertIn("myself 4 · operator 1 · another instance 0", text)
        self.assertIn("matched the judge: 4/5", text)


class ModelTest(InstanceDirCase):
    """Comparisons stay within one model: wren ran on Bonsai, Haiku and MiMo within its first day."""

    def test_a_cycle_gets_the_model_of_its_live_calls(self):
        (self.root / "logs").mkdir()
        calls = [("2026-10-13T12:00:30", "live", "bonsai"), ("2026-10-13T12:01:00", "live", "mimo"),
                 ("2026-10-13T12:02:00", "live", "mimo"), ("2026-10-13T12:02:30", "talk", "haiku"),
                 ("2026-10-13T13:00:00", "live", "haiku")]
        (self.root / "logs" / "llm_usage.jsonl").write_text(
            "".join(json.dumps({"ts": t, "purpose": pu, "model": m}) + "\n" for t, pu, m in calls))
        ep = {"ts": "2026-10-13T12:03:00", "intent": {"at": "2026-10-13T12:01:00"}}
        self.assertEqual(metrics.cycle_model("m", ep), "mimo")

    def test_the_headline_is_on_the_current_model_only(self):
        js = ([judgment(f"2026-10-13T0{i}:00:00", "yes", 5, model="haiku") for i in range(3)]
              + [judgment(f"2026-10-13T1{i}:00:00", "yes", 1, model="mimo") for i in range(2)])
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.stats_text("m")
        self.assertIn("**1.0** over my last 2 rated cycles on mimo", text)
        self.assertIn("On other models: haiku 5.0 (3 cycles)", text)


class ProbationMeasureTest(unittest.TestCase):
    """The probation tests in test_supervision.py pin the revert mechanics; these pin what it measures."""

    def check(self, before: list[dict], after: list[dict]) -> bool:
        tmp = Path(tempfile.mkdtemp(prefix="shaft-pm-")).resolve()
        try:
            js = before + after
            with mock.patch.object(metrics, "SUP_DIR", tmp), mock.patch.object(metrics, "judgments", return_value=before):
                metrics.start_probation("p", "abc123", ["shaft/prompts.py"])
            reverted = []
            with mock.patch.object(metrics, "SUP_DIR", tmp), mock.patch.object(metrics, "judgments", return_value=js), \
                 mock.patch.object(metrics, "git_wt", lambda *a, **k: reverted.append(a) or
                                   subprocess.CompletedProcess(a, 0, "", "")), \
                 mock.patch.object(metrics, "journal_note"):
                metrics.check_probation("p")
            return bool(reverted)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_easy_successes_after_harder_work_are_reverted(self):
        # Under the raw success rate this change passed: yes,yes,... after yes,yes,...
        n = metrics.PROBATION_CYCLES
        self.assertTrue(self.check([judgment(f"b{i}", "yes", 3) for i in range(n)],
                                   [judgment(f"a{i}", "yes", 1) for i in range(n)]))

    def test_harder_partials_after_easy_successes_are_kept(self):
        # Under the raw success rate this change was reverted (0.5 < 1.0 - 0.15).
        n = metrics.PROBATION_CYCLES
        self.assertFalse(self.check([judgment(f"b{i}", "yes", 1) for i in range(n)],
                                    [judgment(f"a{i}", "partial", 4) for i in range(n)]))

    def test_a_model_change_is_not_read_as_a_regression(self):
        n = metrics.PROBATION_CYCLES
        self.assertFalse(self.check([judgment(f"b{i}", "yes", 4, model="haiku") for i in range(n)],
                                    [judgment(f"a{i}", "yes", 1, model="bonsai") for i in range(n)]))

    def test_without_rated_cycles_before_it_nothing_is_reverted(self):
        n = metrics.PROBATION_CYCLES
        self.assertFalse(self.check([judgment(f"b{i}", "yes", None) for i in range(n)],
                                    [judgment(f"a{i}", "no", 2) for i in range(n)]))


class BatteryIsolationTest(unittest.TestCase):
    """An exam must leave nothing in the instance: the supervisor undoes every write, whatever its code did."""

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-bat-")).resolve()
        git = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                             check=True, capture_output=True, text=True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "b"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/b", str(self.root))
        (self.root / ".gitignore").write_text(".tmp/\nlogs/\n")
        (self.root / "memory").mkdir()
        (self.root / "memory" / "journal.md").write_text("# j\n\n---\n## old entry\n")
        (self.root / "notes").mkdir()
        (self.root / "notes" / "keep.md").write_text("keep")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "state")
        (self.root / "notes" / "uncommitted.md").write_text("an earlier run's untracked note")
        from supervisor import core
        self.wt = mock.patch.object(battery, "git_wt",
                                    lambda root, *a, **k: core.git_wt(root, *a, **{**k, "project": self.project}))
        self.wt.start()

    def tearDown(self):
        self.wt.stop()
        shutil.rmtree(self.project, ignore_errors=True)

    def test_a_planted_journal_write_and_new_files_are_undone(self):
        before = battery.snapshot(self.root)
        with (self.root / "memory" / "journal.md").open("a") as f:
            f.write("\n---\n## the battery task was: write add()\n")
        (self.root / "notes" / "keep.md").unlink()
        (self.root / "notes" / "leak.md").write_text("leaked task")
        (self.root / ".tmp").mkdir()
        (self.root / ".tmp" / "scratch.py").write_text("ignored scratch")
        undone = battery.restore(self.root, before)
        self.assertEqual((self.root / "memory" / "journal.md").read_text(), "# j\n\n---\n## old entry\n")
        self.assertEqual((self.root / "notes" / "keep.md").read_text(), "keep")
        self.assertFalse((self.root / "notes" / "leak.md").exists())
        self.assertEqual((self.root / "notes" / "uncommitted.md").read_text(), "an earlier run's untracked note")
        self.assertEqual(sorted(undone), ["+notes/leak.md", "-notes/keep.md", "~memory/journal.md"])


class HiddenTestsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-hid-")).resolve()
        self.item = self.tmp / "item"
        (self.item / "hidden").mkdir(parents=True)
        (self.item / "hidden" / "test_hidden.py").write_text(
            "import unittest\nfrom solution import add\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_small(self):\n        self.assertEqual(add(1, 2), 3)\n"
            "    def test_negative(self):\n        self.assertEqual(add(-1, -2), -3)\n")
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_tests(self):
        return battery.run_hidden_tests({"dir": str(self.item), "hidden_tests": 2}, self.scratch)

    def test_a_correct_solution_passes_every_hidden_test(self):
        (self.scratch / "solution.py").write_text("def add(a, b):\n    return a + b\n")
        r = self.run_tests()
        self.assertEqual((r["passed"], r["total"]), (2, 2), r["output"])

    def test_a_partly_wrong_solution_counts_its_failures(self):
        (self.scratch / "solution.py").write_text("def add(a, b):\n    return abs(a) + abs(b)\n")
        self.assertEqual(self.run_tests()["passed"], 1)

    def test_a_missing_solution_fails_every_expected_test(self):
        r = self.run_tests()
        self.assertEqual((r["passed"], r["total"]), (0, 2))

    def test_a_planted_hidden_test_file_is_overwritten(self):
        (self.scratch / "solution.py").write_text("def add(a, b):\n    return 0\n")
        (self.scratch / "test_hidden.py").write_text("import unittest\nclass T(unittest.TestCase):\n"
                                                     "    def test_a(self): pass\n    def test_b(self): pass\n")
        self.assertEqual(self.run_tests()["passed"], 0)


class BatteryScheduleTest(unittest.TestCase):
    """Night only, never in the sleep's way (Daniele, 2026-10-13)."""
    NAMES = ["shaft", "wren"]

    def decide(self, hhmm: str, children: dict, sleeping_due=False, due=True, awake=""):
        h, m = map(int, hhmm.split(":"))
        now = datetime(2026, 10, 14, h, m)
        return autonomy.battery_jobs(children, now, sleeping_due, self.NAMES, lambda n, t: due, lambda n: awake)

    def test_window(self):
        start, end = battery.window(datetime(2026, 10, 14, 2, 0))
        self.assertEqual((start.hour, start.minute, end.hour, end.minute), (1, 0, 3, 50))

    def test_a_running_battery_is_stopped_when_the_window_closes(self):
        stop, start = self.decide("03:50", {"battery shaft": object(), "wake wren": object()})
        self.assertEqual(stop, ["battery shaft"])
        self.assertEqual(start, ["wren"])          # due() is mocked; the real due() is false outside the window

    def test_a_due_sleep_stops_a_battery_and_starts_none(self):
        stop, start = self.decide("04:00", {"battery wren": object()}, sleeping_due=True)
        self.assertEqual((stop, start), (["battery wren"], []))

    def test_an_awake_instance_is_not_started(self):
        self.assertEqual(self.decide("01:30", {}, awake="live since 01:25")[1], [])

    def test_due_only_inside_the_window_and_every_n_nights(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "SUP_DIR", Path(tmp)), \
             mock.patch.object(battery, "EVERY_DAYS", 2):
            (Path(tmp) / "battery" / "items" / battery.VERSION).mkdir(parents=True)
            self.assertTrue(battery.due("wren", datetime(2026, 10, 14, 1, 0)))
            self.assertFalse(battery.due("wren", datetime(2026, 10, 14, 0, 59)))
            self.assertFalse(battery.due("wren", datetime(2026, 10, 14, 3, 50)))
            (Path(tmp) / "battery" / "schedule.json").write_text(json.dumps({"wren": "2026-10-14T03:40:00"}))
            self.assertFalse(battery.due("wren", datetime(2026, 10, 15, 2, 0)))     # one night later
            self.assertTrue(battery.due("wren", datetime(2026, 10, 16, 2, 0)))      # two nights later


class BatteryResultsTest(unittest.TestCase):
    def test_order_runs_unrun_items_first_then_the_oldest(self):
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        e = {"version": "v1", "checksum": "x", "model": "m", "judge": "j"}
        rows = [{**e, "item": "a", "arm": "instance", "status": "scored", "at": "2026-10-14T01:00:00"},
                {**e, "item": "c", "arm": "instance", "status": "scored", "at": "2026-10-12T01:00:00"}]
        with mock.patch.object(battery, "results", return_value=rows):
            self.assertEqual([i["id"] for i in battery.order("m", items, e)], ["b", "c", "a"])

    def test_results_from_another_era_are_never_compared(self):
        e = {"version": "v1", "checksum": "new", "model": "m", "judge": "j"}
        rows = [{**e, "checksum": "old", "item": "self/x", "category": "self", "arm": "instance", "status": "scored",
                 "score": 2.0, "at": "t"},
                {**e, "item": "self/x", "category": "self", "arm": "instance", "status": "scored", "score": 8.0, "at": "t"}]
        with mock.patch.object(battery, "results", return_value=rows), mock.patch.object(battery, "era", return_value=e):
            text = "\n".join(battery.stats_lines("m"))
        self.assertIn("self: **8.0**", text)

    def test_checksum_changes_when_an_item_changes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "SUP_DIR", Path(tmp)):
            d = battery.items_dir() / "self"
            d.mkdir(parents=True)
            (d / "model.json").write_text('{"prompt": "Which model?", "truth": "model"}')
            a = battery.checksum("m")
            (d / "model.json").write_text('{"prompt": "Which model do you run on?", "truth": "model"}')
            self.assertNotEqual(a, battery.checksum("m"))


class BrokerRootTest(unittest.TestCase):
    def test_a_control_broker_works_in_its_own_folder_and_bills_separately(self):
        from supervisor.broker import Broker
        with tempfile.TemporaryDirectory() as tmp:
            b = Broker("wren-control", Path(tmp), {}, Path(tmp) / "b.log", root=Path(tmp) / "ctrl",
                       usage_name="wren-control-battery")
            self.assertEqual(b._root(), (Path(tmp) / "ctrl").resolve())
            self.assertEqual(b.usage_name, "wren-control-battery")
            default = Broker("wren", Path(tmp), {}, Path(tmp) / "b.log")
            self.assertEqual(default._root(), (Path(tmp) / "instances" / "wren").resolve())


TEMPLATE_CODE = Path(__file__).resolve().parents[2] / "template" / "shaft"


@unittest.skipUnless((TEMPLATE_CODE / "exam.py").exists(), "needs the template's exam mode")
class BatteryEndToEndTest(unittest.TestCase):
    """One item through the real path: sandboxed `shaft exam` for the instance and its control (fake model),
    hidden tests, grading (stubbed judge), results, the instance's files restored, stats.md."""

    def setUp(self):
        from supervisor import core, runner
        from supervisor.tests.fakellm import FakeLLM
        self.project = Path(tempfile.mkdtemp(prefix="shaft-e2e-")).resolve()
        git = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                             check=True, capture_output=True, text=True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "e"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/e", str(self.root))
        shutil.copytree(TEMPLATE_CODE, self.root / "shaft", ignore=shutil.ignore_patterns("__pycache__"))
        self.fake = FakeLLM([
            # instance: writes a solution and a note outside its scratch folder (refused), then answers
            {"tool_calls": [("write_file", {"path": ".tmp/battery/coding-add/solution.py",
                                            "content": "def add(a, b):\n    return a + b\n"}),
                            ("write_file", {"path": "notes/leak.md", "content": "task: add()"})]},
            {"tool_calls": [("submit_answer", {"answer": "solution.py: add(a, b) returns a + b"})]},
            # control: answers without writing anything
            {"tool_calls": [("submit_answer", {"answer": "I could not do it"})]},
        ])
        config = f'LLM_BASE_URL="{self.fake.url}"\nLLM_MODEL="fake"\nOPERATOR_NAME="Daniele"\n'
        (self.root / "config.env").write_text(config)
        (self.root / ".gitignore").write_text(".tmp/\nlogs/\n")
        for d in ("self", "memory", "notes"):
            (self.root / d).mkdir()
        (self.root / "memory" / "journal.md").write_text("# journal\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "state")
        self.sup = self.project / "sup"
        item = self.sup / "battery" / "items" / "v1" / "coding"
        (item / "add" / "hidden").mkdir(parents=True)
        (item / "add.json").write_text(json.dumps({"prompt": "Write add(a, b) in solution.py.", "reference": "a+b",
                                                   "hidden_tests": 1, "max_steps": 4}))
        (item / "add" / "hidden" / "test_hidden.py").write_text(
            "import unittest\nfrom solution import add\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        archive = self.project / "template.tar"   # the control is made from this instead of `git archive`
        subprocess.run(["tar", "-cf", str(archive), "-C", str(self.root), "shaft"], check=True)
        real_make = battery.make_control

        def make_control(name):
            dest = battery.base() / "control" / name
            dest.mkdir(parents=True)
            subprocess.run(["tar", "-xf", str(archive), "-C", str(dest)], check=True)
            shutil.copy2(self.root / "config.env", dest / "config.env")
            for d in ("self", "memory", "notes", "inbox"):
                (dest / d).mkdir()
            return dest
        self.graded = []

        def ask(system, case, schema, purpose, **kw):
            self.graded.append(case)
            return {"correct": "yes", "effectiveness": 9, "unsupported_claims": 0, "reasoning": "fine"}
        wt = lambda root, *a, **k: core.git_wt(root, *a, **{**k, "project": self.project})  # noqa: E731
        self.patches = [
            mock.patch.object(battery, "SUP_DIR", self.sup), mock.patch.object(metrics, "SUP_DIR", self.sup),
            mock.patch.object(runner, "SUP_DIR", self.sup), mock.patch.object(metrics, "INSTANCES", self.project / "instances"),
            mock.patch.object(battery, "INSTANCES", self.project / "instances"),
            mock.patch.object(battery, "instance_root", lambda n: self.root),
            mock.patch.object(battery, "instance_config", lambda n: {"LLM_MODEL": "fake"}),
            mock.patch.object(battery, "LLM_PORTS", [self.fake.port]), mock.patch.object(battery, "secrets_env", dict),
            mock.patch.object(battery, "git_wt", wt), mock.patch.object(battery, "make_control", make_control),
            mock.patch.object(battery, "log"), mock.patch.object(metrics, "log"),
            mock.patch.object(judge, "ask", ask), mock.patch.object(judge, "JUDGE_MODEL", "judge-test"),
            mock.patch("supervisor.autonomy.describe_schedule", return_value="test"),
        ]
        for p in self.patches:
            p.start()
        self.real_make = real_make

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.fake.close()
        shutil.rmtree(self.project, ignore_errors=True)

    def test_one_item_for_the_instance_and_its_control(self):
        before = battery.snapshot(self.root)
        self.assertEqual(battery.run("e"), 2)
        rows = battery.results("e")
        inst, ctrl = [r for r in rows if r["arm"] == "instance"][0], [r for r in rows if r["arm"] == "control"][0]
        self.assertEqual((inst["status"], inst["tests"]["passed"], inst["score"]), ("scored", 1, 10.0), inst)
        self.assertEqual((ctrl["status"], ctrl["tests"]["passed"], ctrl["score"]), ("scored", 0, 0.0), ctrl)
        self.assertEqual(inst["checksum"], battery.checksum("e"))
        after = battery.snapshot(self.root)
        self.assertIn("self/stats.md", after)                                     # the supervisor's own write
        self.assertEqual({k: v for k, v in after.items() if k != "self/stats.md"}, before)   # nothing kept
        self.assertFalse((self.root / ".tmp" / "battery" / "coding-add").exists())
        self.assertFalse((self.sup / "battery" / "control" / "e").exists())        # control removed
        self.assertNotIn("write_add", (self.sup / "battery" / "broker.log").read_text() if
                         (self.sup / "battery" / "broker.log").exists() else "")
        self.assertIn("Hidden tests: 1/1 passed", self.graded[0])
        stats = (self.root / "self" / "stats.md").read_text()
        self.assertIn("coding: **10.0**", stats)
        self.assertIn("control 0.0", stats)
        self.assertEqual(len(list((self.sup / "evals").glob("battery-e-*.json"))), 1)

    def test_control_only_leaves_the_instance_alone(self):
        before = battery.snapshot(self.root)
        self.assertEqual(battery.run("e", control_only=True), 1)
        self.assertEqual([r["arm"] for r in battery.results("e")], ["control"])
        self.assertEqual(battery.snapshot(self.root), before)                      # not even stats.md
        self.assertFalse((self.sup / "battery" / "schedule.json").exists())        # not the scheduled run


if __name__ == "__main__":
    unittest.main()
