"""Review rows 19, 20, 26, 27 and 28: retries, independent judges and usage accounting."""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest import mock

from supervisor import battery, cloudllm, judge, metrics, modelcall, panel, selfhosted, trial


class JudgeReviewTest(unittest.TestCase):
    def test_row19_e5_c13_trial_length_escalates_only_once(self):
        reply = {"choices": [{"finish_reason": "length", "message": {"content": ""}}], "usage": {"cost": .01}}
        rooms = []

        def send(req):
            rooms.append(json.loads(req.data)["max_tokens"])
            return reply, None

        with mock.patch.object(modelcall, "secrets_env", return_value={"OPENROUTER_API_KEY": "test"}), \
                mock.patch.object(cloudllm, "_send", side_effect=send), mock.patch.object(judge, "_log"), \
                mock.patch.object(trial, "judge_case", return_value="case"):
            with self.assertRaises(judge.JudgeError):
                trial.judge_pair("context", {}, {}, "row19", model="openrouter/test/model")
        self.assertEqual(rooms, [16000, 32000])

    def test_row19_native_claude_length_also_escalates_only_once(self):
        reply = {"stop_reason": "max_tokens", "usage": {"input_tokens": 10}}
        rooms = []

        def send(req, **kw):
            rooms.append(json.loads(req.data)["max_tokens"])
            return io.BytesIO(json.dumps(reply).encode())

        with mock.patch.object(modelcall, "secrets_env", return_value={"ANTHROPIC_API_KEY": "test"}), \
                mock.patch.object(modelcall.urllib.request, "urlopen", side_effect=send), \
                mock.patch.object(modelcall.time, "sleep"), mock.patch.object(judge, "_log"):
            with self.assertRaises(judge.JudgeError):
                judge.ask("system", "case", {}, "row19", model="claude-sonnet-5-5")
        self.assertEqual(rooms, [16000, 32000])

    def test_row20_second_judge_is_reserved_even_if_configured_in_main_pool(self):
        with mock.patch.object(judge, "JUDGE_MODELS", ["anthropic/claude-sonnet-5.5", "openrouter/other/model"]), \
                mock.patch.object(judge, "JUDGE2_MODEL", "claude-sonnet-5-5"):
            self.assertEqual({judge.pick() for _ in range(50)}, {"openrouter/other/model"})
            self.assertEqual(judge.rotation(), "rotation:openrouter/other/model")
            # Only when no other judge may grade (the run gets no second judgment then): graded, never stuck.
            self.assertEqual(judge.pick(exclude=["openrouter/other/model"]), "anthropic/claude-sonnet-5.5")
        with mock.patch.object(judge, "JUDGE_MODELS", ["openrouter/other/model"]):   # one model, the instance's:
            self.assertEqual(judge.pick(exclude=["openrouter/other/model"]), "openrouter/other/model")   # graded,
            self.assertEqual(judge.independence("openrouter/other/model", ["other/model"]),            # and marked
                             {"independence": "same-model"})
        with mock.patch.object(judge, "JUDGE_MODELS", ["openrouter/other/model", "openrouter/second/model"]):
            with self.assertRaises(judge.NoJudgeLeft):
                judge.pick(exclude=["openrouter/other/model", "openrouter/second/model"])

    def test_row20_default_pool_leaves_out_the_default_second_judge(self):
        from supervisor.core import KNOBS
        pool, second = KNOBS["SHAFT_JUDGE_MODELS"], KNOBS["SHAFT_JUDGE2_MODEL"]
        self.assertFalse(any(judge.same_model(m, second.default) for m in pool.parse(pool.default)))

    def test_row20_openrouter_judge_row_without_cost_keeps_provider_and_is_unpriced(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(judge, "SUP_DIR", Path(tmp)):
            judge._log("row20", "openrouter/test/model", {"prompt_tokens": 10}, 1, None, None, "Novita", 7)
            row = json.loads((Path(tmp) / "judge_usage.jsonl").read_text())
        self.assertEqual((row["usd"], row["unpriced"], row["provider"], row["reasoning_tokens"]),
                         (None, True, "Novita", 7))

    def test_row27_native_judge_row_records_priced_cache_usage(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(judge, "SUP_DIR", Path(tmp)):
            judge._log("row27", "claude-sonnet-5-5", {"input_tokens": 1000, "output_tokens": 100,
                       "cache_read_input_tokens": 500}, 1, None)
            row = json.loads((Path(tmp) / "judge_usage.jsonl").read_text())
        self.assertAlmostEqual(row["usd"], .0031)
        self.assertEqual(row["cache_read_tokens"], 500)
        self.assertFalse(row["unpriced"])

    def test_row28_i15_internal_books_have_an_explicit_beneficiary_rule(self):
        self.assertIn("Work the operator does not read or act on", metrics.JUDGE_SYSTEM)
        self.assertIn("ledger or book, is 'own_record'", metrics.JUDGE_SYSTEM)


class BatteryReviewTest(unittest.TestCase):
    def test_row26_c11_failed_grade_reuses_full_case_and_original_grader(self):
        item = {"id": "research/test", "category": "research", "prompt": "question", "key": "key"}
        result = {"answer": "a" * 5000, "steps": 3, "tools": [{"tool": "read", "result": "evidence"}]}
        era = {"version": "test", "checksum": "abc", "model": "model", "judge": "rotation:test"}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            stack.enter_context(mock.patch.object(battery, "SUP_DIR", root))
            stack.enter_context(mock.patch.object(battery, "secrets_env", return_value={}))
            broker = stack.enter_context(mock.patch.object(battery, "Broker"))
            broker.return_value.counts = {}
            stack.enter_context(mock.patch.object(battery, "_exam", return_value=result))
            grade = stack.enter_context(mock.patch.object(battery, "grade", side_effect=judge.JudgeError("offline")))
            first = battery._one_in("test", item, "control", root, root, "test-control", era, None,
                                    lambda: None, "original-grader")
            self.assertEqual(first["status"], "unjudged")
            self.assertEqual(len(first["answer"]), 4000)
            self.assertEqual(first["grading_case"]["result"], result)
            grade.side_effect = None
            grade.return_value = {"effectiveness": 8}
            with mock.patch.object(battery, "make_control", side_effect=AssertionError("exam reran")):
                second = battery._one("test", item, "control", root, era, None, lambda: None, "other-grader")
            self.assertEqual(second["status"], "scored")
            self.assertEqual(second["score"], 8)
            self.assertEqual(grade.call_args.args, (item, result, None, None))
            self.assertEqual(grade.call_args.kwargs, {"model": "original-grader"})
            self.assertNotIn("grading_case", second)

    def test_row26_c11_an_ungradable_case_is_regraded_a_bounded_number_of_times(self):
        item = {"id": "research/test", "category": "research", "prompt": "question", "key": "key"}
        era = {"version": "test", "checksum": "abc", "model": "model", "judge": "rotation:test"}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            stack.enter_context(mock.patch.object(battery, "SUP_DIR", root))
            stack.enter_context(mock.patch.object(battery, "secrets_env", return_value={}))
            stack.enter_context(mock.patch.object(battery, "Broker")).return_value.counts = {}
            stack.enter_context(mock.patch.object(battery, "_exam", return_value={"answer": "a", "steps": 1}))
            grade = stack.enter_context(mock.patch.object(battery, "grade", side_effect=judge.JudgeError("bad JSON")))
            battery._one_in("test", item, "control", root, root, "test-control", era, None, lambda: None, "g")
            stack.enter_context(mock.patch.object(battery, "make_control", side_effect=AssertionError("exam reran")))
            for _ in range(battery.GRADE_ATTEMPTS - 1):
                battery._one("test", item, "control", root, era, None, lambda: None, "g")
            self.assertEqual(grade.call_count, battery.GRADE_ATTEMPTS)
            with self.assertRaisesRegex(AssertionError, "exam reran"):   # never stuck on one case
                battery._one("test", item, "control", root, era, None, lambda: None, "g")


class TrialReviewTest(unittest.TestCase):
    def arm(self, preempted=False):
        return {"completed": not preempted, "ended": "preempted" if preempted else "ok", "exit": 0,
                "seconds": 1, "usd": .01, "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "trace": []}

    def exercise(self, change=None):
        t = {"id": "test", "instance": "test", "by": "operator", "llm": {"local": False, "model": "model"},
             "states": [{"ts": "2026-11-03T00:00:00", "t0": "2026-11-02T23:59:00", "start_commit": "abc",
                         "preempted": 0}]}
        calls, charges = [], []
        code = {"shaft/run.py": b"original"}

        def replay(*args, **kw):
            arm = kw["billed"][-1]
            calls.append(arm)
            return self.arm(arm == "B" and calls.count("B") == 1)

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for obj, attr, value in ((trial, "_files", code), (trial, "_dir", Path(tmp)),
                    (trial, "_within_caps", True), (trial, "_config", {}), (trial, "state_context", "context"),
                    (judge, "pick", "grader"), (trial, "judge_pair", {"winner": "tie", "reasoning": "same"}),
                    (trial, "_judge_usd", 0), (trial, "save", None), (trial, "log", None)):
                stack.enter_context(mock.patch.object(obj, attr, return_value=value))
            stack.enter_context(mock.patch("supervisor.core.paused", return_value=False))
            stack.enter_context(mock.patch.object(trial, "replay_arm", side_effect=replay))
            stack.enter_context(mock.patch.object(trial, "_ledger", side_effect=charges.append))
            trial.run_pair(Path(tmp), t, 0, "model")
            calls.clear()
            # Keep B complete on the second invocation, regardless of its call count.
            stack.enter_context(mock.patch.object(trial, "replay_arm", side_effect=lambda *a, **kw:
                                calls.append(kw["billed"][-1]) or self.arm()))
            model = "model"
            if change == "commit":
                t["states"][0]["start_commit"] = "def"
            elif change == "cycle":
                t["states"][0]["ts"] = "2026-11-03T01:00:00"
            elif change == "model":
                model = "other-model"
            elif change == "budget":
                stack.enter_context(mock.patch.object(trial, "CYCLE_TIMEOUT_S", trial.CYCLE_TIMEOUT_S + 1))
            elif change == "code":
                code["shaft/run.py"] = b"changed"
            elif change == "shape":
                t["cycles"] = 2
            trial.run_pair(Path(tmp), t, 0, model)
        return sorted(calls), len([r for r in charges if r["kind"] == "arm"])

    def test_row26_c12_completed_arm_is_reused_without_a_second_charge(self):
        self.assertEqual(self.exercise(), (["B"], 3))

    def test_row26_c12_fingerprint_changes_invalidate_completed_arm(self):
        for change in ("commit", "cycle", "model", "budget", "code", "shape"):
            with self.subTest(change=change):
                self.assertEqual(self.exercise(change), (["A", "B"], 4))

    def twice(self, first, second, between=lambda arms: None):
        """run_pair twice on one past cycle: `first`/`second` stand in for replay_arm on each pass; `between(arms)`
        may damage the saved arms folder. Returns the arms folder's files after the second pass."""
        t = {"id": "test", "instance": "test", "by": "operator", "llm": {"local": False, "model": "model"},
             "states": [{"ts": "2026-11-03T00:00:00", "t0": "2026-11-02T23:59:00", "start_commit": "abc",
                         "preempted": 0}]}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            for obj, attr, value in ((trial, "_files", {"shaft/run.py": b"x"}), (trial, "_dir", Path(tmp)),
                    (trial, "_within_caps", True), (trial, "_config", {}), (trial, "state_context", "context"),
                    (judge, "pick", "grader"), (trial, "judge_pair", {"winner": "tie", "reasoning": "same"}),
                    (trial, "_judge_usd", 0), (trial, "save", None), (trial, "log", None), (trial, "_ledger", None)):
                stack.enter_context(mock.patch.object(obj, attr, return_value=value))
            stack.enter_context(mock.patch("supervisor.core.paused", return_value=False))
            replay = stack.enter_context(mock.patch.object(trial, "replay_arm", side_effect=first))
            trial.run_pair(Path(tmp), t, 0, "model")
            between(Path(tmp) / "arms")
            replay.side_effect = second
            trial.run_pair(Path(tmp), t, 0, "model")
            return sorted(p.name for p in (Path(tmp) / "arms").iterdir())

    def test_row26_c12_reused_arm_shares_its_read_cache_with_the_arm_run_later(self):
        """Grok review: a reused arm kept its old trace while the arm run later read the live world, yet the judge
        is told both runs got the same web answers. The completed arm's reads must reach the later arm."""
        read = [200, {"content-type": "text/html"}, b"\x00the page A saw"]
        seen = []

        def first(*a, **kw):
            arm = kw["billed"][-1]
            if arm == "A":
                kw["cache"].setdefault(0, {})['["http", "GET", "https://example.test"]'] = tuple(read)
            return self.arm(preempted=arm == "B")

        def second(*a, **kw):
            seen.append((kw["billed"][-1], kw["cache"].get(0, {}).get('["http", "GET", "https://example.test"]')))
            return self.arm()

        files = self.twice(first, second)
        self.assertEqual(seen, [("B", read)])
        self.assertEqual(files, ["00A.json", "00B.json"])   # judged: the cache is gone, and no temp file is left

    def test_row26_c12_unreadable_arm_or_missing_cache_reruns_both_arms(self):
        """Grok review: a truncated arm file raised out of run_pair, and _pair_job marked the cycle unjudged for
        good. Unreadable (or saved without its cache) now means not reused."""
        def first(*a, **kw):
            return self.arm(preempted=kw["billed"][-1] == "B")

        for damage in (lambda d: (d / "00A.json").write_text('{"completed": tr'),
                       lambda d: (d / "00cache.json").unlink()):
            with self.subTest(damage=damage):
                ran = []
                self.twice(first, lambda *a, **kw: ran.append(kw["billed"][-1]) or self.arm(), damage)
                self.assertEqual(sorted(ran), ["A", "B"])


class CloudReviewTest(unittest.TestCase):
    def test_row27_openrouter_complete_and_stream_keep_cache_counters(self):
        usage = {"prompt_tokens": 1000, "completion_tokens": 10, "cost": .01,
                 "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 100}}
        reply = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": usage}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cloudllm, "USAGE", Path(tmp) / "usage.jsonl"), \
                mock.patch.object(cloudllm, "_send", return_value=(reply, None)):
            stream = io.BytesIO(b"data: " + json.dumps(reply).encode() + b"\n\n")
            cloudllm._openrouter("test", {"model": "openrouter/test/model"}, "test-key")
            list(cloudllm._relay("test", "openrouter/test/model", stream, time.monotonic()))
            rows = [json.loads(line) for line in cloudllm.USAGE.read_text().splitlines()]
        self.assertEqual([(r["cache_read_tokens"], r["cache_write_tokens"]) for r in rows], [(800, 100)] * 2)

    def test_row27_selfhosted_calls_and_spend_report_are_unpriced(self):
        reply = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            path = Path(tmp)
            stack.enter_context(mock.patch.object(cloudllm, "USAGE", path / "cloud_usage.jsonl"))
            stack.enter_context(mock.patch.object(selfhosted, "SERVERS", {"gpu": {}}))
            stack.enter_context(mock.patch.object(selfhosted, "open_reply", return_value=(
                io.BytesIO(json.dumps(reply).encode()), "gpu/model", time.monotonic())))
            selfhosted.complete("test", {"model": "gpu/model"})
            list(cloudllm._relay("test", "gpu/model", io.BytesIO(
                b"data: " + json.dumps(reply).encode() + b"\n\n"), time.monotonic()))
            rows = [json.loads(line) for line in cloudllm.USAGE.read_text().splitlines()]
            self.assertEqual([(r["usd"], r["unpriced"], r["error"]) for r in rows], [(None, True, None)] * 2)
            stack.enter_context(mock.patch.object(panel, "SUP_DIR", path))
            stack.enter_context(mock.patch.object(panel, "instance_names", return_value=[]))
            stack.enter_context(mock.patch.object(trial, "LEDGER", path / "trial.jsonl"))
            report = panel.costs(today=date.today())
            self.assertEqual(report["unpriced"], 2)

    def test_row27_selfhosted_call_does_not_make_the_runs_spend_unknown(self):
        """Grok review: an unpriced self-hosted call pushed None into the run's charges, so Broker._run_usd was None
        for the whole run (priced consults too) and the project read 'at risk: usage unknown'."""
        from supervisor import ledger
        from supervisor.broker import Broker
        reply = {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": .01}}
        hosted = {"choices": reply["choices"], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(mock.patch.object(cloudllm, "USAGE", Path(tmp) / "cloud_usage.jsonl"))
            stack.enter_context(mock.patch.object(selfhosted, "SERVERS", {"gpu": {}}))
            stack.enter_context(mock.patch.object(selfhosted, "open_reply", return_value=(
                io.BytesIO(json.dumps(hosted).encode()), "gpu/model", time.monotonic())))
            stack.enter_context(mock.patch.object(cloudllm, "_send", return_value=(reply, None)))
            run = mock.Mock(_project_charges=[])
            token = ledger.charges.set(run._project_charges)
            try:
                selfhosted.complete("test", {"model": "gpu/model"})
                cloudllm._openrouter("test", {"model": "openrouter/test/model"}, "test-key")
                cloudllm._record("test", "openrouter/test/model", 5, 5, None, time.monotonic(), "stop", None)
            finally:
                ledger.charges.reset(token)
        self.assertEqual(run._project_charges, [.01, None])   # a cloud call without a cost stays unknown
        self.assertIsNone(Broker._run_usd(run))
        run._project_charges.pop()
        self.assertEqual(Broker._run_usd(run), .01)
