"""Measurement that can show improvement and autonomy (docs/plans/PLAN-measurement.md): judged origin, difficulty and
novelty; difficulty-weighted stats and probation; the capability battery's isolation, scoring and schedule."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, battery, judge, metrics
from supervisor.tests.mindfix import IGNORE_MIND


def judgment(ts: str, met: str, difficulty=None, origin=None, **kw) -> dict:
    return {"episode_ts": ts, "criterion_met": met, "confidence": 70, "claimed": met, "claim_supported": True,
            "criterion_quality": "checkable", "failure_mode": "", "goal": f"goal at {ts}", "reasoning": "r",
            "judge": judge.JUDGE_MODELS[0], "difficulty": difficulty, "novelty": 2 if difficulty else None, "origin": origin, **kw}


class InstanceDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-meas-")).resolve()
        self.instances = self.tmp / "instances"
        self.root = self.instances / "m"
        for d in ("memory/episodes", "memory/messages", "self", "inbox"):
            (self.root / d).mkdir(parents=True)
        self.sup = self.tmp / "sup"
        self.patches = [mock.patch.object(metrics, "INSTANCES", self.instances),
                        mock.patch.object(metrics, "SUP_DIR", self.sup),
                        mock.patch.object(judge, "JUDGE_EVERY", 1)]   # every run judged: sampling is JudgeLessOftenTest
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
               "origin_evidence": "the operator's message", "difficulty": 3, "difficulty_reason": "r", "novelty": 4,
               "value": 3, "value_for": "operator", "value_reason": "the release note the operator asked for",
               "expectations": [], "unreported_surprises": [], "reported_surprises_supported": True,
               "delivery_supported": "na", "deliverable_met": "yes",
               "self_imposed_clauses": "none", "self_audit": False, "should_have_waited": "na", "pushback": "none", "first_error_step": 0, "first_error": "No step is at fault.", "followup_useful": "na"}
    FIELDS = metrics.RATINGS + metrics.VALUES

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
        self.assertTrue(j["origin_supported"])

    def test_an_undeclared_origin_is_never_supported(self):
        # Regression: wren's 2026-10-13 16:19 cycle declared no origin, and the judge still said origin_supported: true.
        self.episode({"ts": "2026-10-13T12:10:00", "kind": "live", "intent": {"goal": "g", "success_criterion": "c",
                      "confidence": 50}, "finish": None, "tools": []})
        with mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "ask", return_value=dict(self.VERDICT)):
            metrics.judge_pending("m")
        self.assertIsNone(metrics.judgments("m")[0]["origin_supported"])

    def test_an_out_of_range_rating_leaves_the_cycle_unjudged(self):
        self.episode({"ts": "2026-10-13T12:10:00", "kind": "live", "intent": {"goal": "g", "success_criterion": "c",
                      "confidence": 50}, "finish": None, "tools": []})
        with mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "ask", return_value={**self.VERDICT, "difficulty": 9}):
            self.assertEqual(metrics.judge_pending("m"), 0)
        self.assertEqual(metrics.judgments("m"), [])                  # retried later, never written half-empty

    def test_backfill_rates_old_cycles_and_keeps_their_verdicts(self):
        self.episode({"ts": "2026-10-12T17:24:00", "kind": "live", "intent": {"goal": "reply to the operator",
                      "success_criterion": "c", "confidence": 80}, "finish": {"criterion_met": "yes"}, "tools": []})
        path = self.sup / "judgments" / "m.jsonl"
        path.parent.mkdir(parents=True)
        old = {k: v for k, v in judgment("2026-10-12T17:24:00", "partial").items() if k not in metrics.RATINGS}
        rule = {**old, "episode_ts": "2026-10-12T18:00:00", "judge": "rule", "criterion_met": "no"}
        path.write_text(json.dumps(old) + "\n" + json.dumps(rule) + "\n")
        ratings = {k: self.VERDICT[k] for k in self.FIELDS}
        with mock.patch.object(judge, "ask", return_value=dict(ratings)) as ask:
            self.assertEqual(metrics.backfill("m"), 1)
            schema = ask.call_args.args[2]
            self.assertEqual(sorted(schema["required"]), sorted(self.FIELDS))   # no new verdict
        a, b = metrics.judgments("m")
        self.assertEqual(a["criterion_met"], "partial")
        self.assertEqual((a["origin"], a["difficulty"], a["value"], a["value_for"]), ("operator", 3, 3, "operator"))
        self.assertIsNone(b["difficulty"])                            # rule-judged: rated empty, not asked
        self.assertIsNone(b["value"])
        with mock.patch.object(judge, "ask") as ask:
            self.assertEqual(metrics.backfill("m"), 0)                # idempotent
            ask.assert_not_called()

    def test_backfill_rewrites_the_file_only_under_the_instance_run_lock(self):
        # Live cycles append judgments (after_cycle, under the run lock) while a backfill runs.
        import contextlib
        from supervisor import runner
        self.episode({"ts": "2026-10-12T17:24:00", "kind": "live", "intent": {"goal": "g", "success_criterion": "c",
                      "confidence": 80}, "finish": {"criterion_met": "yes"}, "tools": []})
        path = self.sup / "judgments" / "m.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({k: v for k, v in judgment("2026-10-12T17:24:00", "yes").items()
                                    if k not in metrics.RATINGS}) + "\n")
        held = []

        @contextlib.contextmanager
        def lock(name, wait, kind=""):
            held.append((name, kind))
            yield
            held.pop()
        merged_under = []
        real_merge = metrics._merge_ratings
        with mock.patch.object(runner, "run_lock", lock), \
             mock.patch.object(metrics, "_merge_ratings", lambda n, r: merged_under.append(list(held)) or real_merge(n, r)), \
             mock.patch.object(judge, "ask", return_value={k: JudgingTest.VERDICT[k] for k in JudgingTest.FIELDS}):
            self.assertEqual(metrics.backfill("m"), 1)
        self.assertTrue(merged_under and all(h == [("m", "backfill")] for h in merged_under), merged_under)

    def test_backfill_asks_a_rated_cycle_only_for_its_value(self):
        # docs/plans/PLAN-value V1: cycles rated by the E0 backfill keep their ratings and who gave them; only value is asked.
        self.episode({"ts": "2026-10-14T10:00:00", "kind": "live", "intent": {"goal": "g", "success_criterion": "c",
                      "confidence": 80}, "finish": {"criterion_met": "yes"}, "tools": []})
        path = self.sup / "judgments" / "m.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({**judgment("2026-10-14T10:00:00", "yes", 2, "self", rated_by="old-judge"),
                                    "origin_supported": None, "origin_evidence": "e", "difficulty_reason": "r"}) + "\n")
        values = {"value": 1, "value_for": "own_record", "value_reason": "a fact about its own stats"}
        with mock.patch.object(judge, "ask", return_value={**values, "difficulty": 5}) as ask, \
             mock.patch.object(judge, "pick", return_value="judge-b"):
            self.assertEqual(metrics.backfill("m"), 1)
        self.assertEqual(sorted(ask.call_args.args[2]["required"]), sorted(metrics.VALUES))
        self.assertIn('"difficulty": 2', ask.call_args.args[1])          # what was already given is shown, kept
        j = metrics.judgments("m")[0]
        self.assertEqual((j["difficulty"], j["rated_by"]), (2, "old-judge"))   # not overwritten by the extra key
        self.assertEqual((j["value"], j["value_for"], j["value_judge"]), (1, "own_record", "judge-b"))
        self.assertEqual(metrics.value_judge(j), "judge-b")

    def test_a_backfill_that_dies_midway_keeps_what_it_paid_for(self):
        for i in range(3):
            self.episode({"ts": f"2026-10-14T1{i}:00:00", "kind": "live", "intent": {"goal": "g",
                          "success_criterion": "c", "confidence": 80}, "finish": None, "tools": []})
        path = self.sup / "judgments" / "m.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("".join(json.dumps({**judgment(f"2026-10-14T1{i}:00:00", "yes", 2, "self"),
                                            "origin_supported": None, "origin_evidence": "e",
                                            "difficulty_reason": "r"}) + "\n" for i in range(3)))
        answers = iter([{"value": 2, "value_for": "operator", "value_reason": "r"},
                        {"value": 3, "value_for": "operator", "value_reason": "r"}])

        def ask(*a, **kw):
            try:
                return next(answers)
            except StopIteration:
                raise RuntimeError("network gone") from None
        with mock.patch.object(metrics, "BACKFILL_FLUSH", 1), mock.patch.object(judge, "ask", ask), \
             self.assertRaises(RuntimeError):
            metrics.backfill("m")
        self.assertEqual([j.get("value") for j in metrics.judgments("m")], [2, 3, None])
        with mock.patch.object(judge, "ask", return_value={"value": 1, "value_for": "nobody", "value_reason": "r"}) as ask:
            self.assertEqual(metrics.backfill("m"), 1)                 # the rerun asks only for the last one
        self.assertEqual(ask.call_count, 1)

    def test_a_judgment_without_a_usable_value_leaves_the_cycle_unjudged(self):
        self.episode({"ts": "2026-10-13T12:10:00", "kind": "live", "intent": {"goal": "g", "success_criterion": "c",
                      "confidence": 50}, "finish": None, "tools": []})
        for bad in ({"value": 6}, {"value": True}, {"value_for": "everyone"}, {"value_reason": " "}):
            with mock.patch.object(judge, "available", return_value=True), \
                 mock.patch.object(judge, "ask", return_value={**self.VERDICT, **bad}):
                self.assertEqual(metrics.judge_pending("m"), 0, bad)
        self.assertEqual(metrics.judgments("m"), [])

    def test_a_judged_cycle_records_its_value(self):
        self.episode({"ts": "2026-10-13T12:10:00", "kind": "live", "intent": {"goal": "g", "success_criterion": "c",
                      "confidence": 50}, "finish": None, "tools": []})
        self.episode({"ts": "2026-10-13T12:20:00", "kind": "live", "finish": None, "tools": []})   # no intent
        with mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "ask", return_value=dict(self.VERDICT)) as ask:
            self.assertEqual(metrics.judge_pending("m"), 2)
        required = ask.call_args.args[2]["required"]
        start = required.index("value")
        self.assertEqual(required[start:start + 3], ["value", "value_for", "value_reason"])
        self.assertIn("what would be lost if it had not run", ask.call_args.args[0])
        a, b = metrics.judgments("m")
        self.assertEqual((a["value"], a["value_for"]), (3, "operator"))
        self.assertEqual((b["judge"], b["value"], b["value_for"]), ("rule", None, None))

    def test_stats_lead_with_achieved_difficulty_and_origins(self):
        js = ([judgment(f"2026-10-13T0{i}:00:00", "yes", 1, "self", origin_declared="self") for i in range(4)]
              + [judgment("2026-10-13T05:00:00", "partial", 4, "operator", origin_declared="self")])
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.stats_text("m")
        self.assertLess(text.index("Achieved difficulty"), text.index("Judged success rate"))
        self.assertIn("**1.2**", text)                                # (4×1 + 0.5×4) / 5 cycles
        self.assertIn("myself 4 · operator 1 · another instance 0", text)
        self.assertIn("matched the judge: 4/5", text)


def valued_j(ts: str, value: int, value_for: str = "operator", model: str = "mimo", **kw) -> dict:
    return judgment(ts, "yes", 2, "self", value=value, value_for=value_for, value_reason=f"output at {ts}",
                    model=model, **kw)


class ValueTest(unittest.TestCase):
    """docs/plans/PLAN-value V2: what the cycles delivered, in stats.md and in `shaft review`."""

    def test_stats_show_value_on_the_current_model_with_who_would_miss_it(self):
        js = ([valued_j(f"2026-10-14T{h:02d}:00:00", 1, "own_record") for h in range(20)]
              + [valued_j(f"2026-10-15T{h:02d}:00:00", 3 if h % 2 else 1, "operator" if h % 2 else "nobody")
                 for h in range(20)]
              + [valued_j("2026-10-13T09:00:00", 4, model="haiku"),
                 judgment("2026-10-15T21:00:00", "no", None, judge="rule", value=None),
                 judgment("2026-10-15T22:00:00", "yes", 2)])            # judged before value existed
        js.sort(key=lambda j: j["episode_ts"])
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.stats_text("m")
        self.assertLess(text.index("What I achieved"), text.index("What my cycles were worth"))
        self.assertIn("**2.0** over my last 20 cycles on mimo, vs 1.0 over the 20 before", text)
        self.assertIn("On other models: haiku 4.0 (1 cycles)", text)
        self.assertIn("1: 10 · 3: 10", text)
        self.assertIn("Who would miss them: the operator 10 · another instance 0 · my own future work 0 · "
                      "my own record-keeping 0 · nobody 10", text)
        self.assertIn("2026-10-13 09:00 — 4/5: output at 2026-10-13T09:00:00", text)   # most valuable first
        self.assertIn("Cycles without a value grade yet: 1", text)       # the rule-judged one is not counted

    def test_stats_without_value_grades_say_so(self):
        with mock.patch.object(metrics, "judgments", return_value=[judgment("2026-10-15T10:00:00", "yes", 2)]):
            self.assertIn("No cycle has a value grade yet.", metrics.stats_text("m"))

    def test_review_counts_the_window_and_lists_the_cycles_worth_reading(self):
        now = datetime(2026, 10, 15, 12, 0)
        js = [valued_j("2026-10-14T11:00:00", 4, judge="sonnet"),               # the window before
              valued_j("2026-10-14T13:00:00", 1, "own_record", judge="haiku", goal="Run the standing watch"),
              valued_j("2026-10-14T14:00:00", 1, "nobody", judge="haiku", goal="Run the standing watch"),
              {**valued_j("2026-10-15T09:00:00", 3, judge="sonnet", value_judge="haiku"),
               "criterion_met": "partial", "claim_supported": False},
              valued_j("2026-10-15T12:00:00", 5)]                               # after `now`: not counted
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.review_text(["m"], 24, now=now)
        self.assertIn("■ m: 3 cycles · success 83% · achieved 1.67 · value 1.67 (3 graded)", text)
        self.assertIn("the 24 h before: 1 cycles", text)
        self.assertIn("claims the log did not support: 1", text)
        self.assertIn("2× Run the standing watch", text)
        self.assertIn("who would miss them: the operator 1 · another instance 0 · its own future work 0 · "
                      "its own record-keeping 1 · nobody 1", text)
        self.assertIn("cycles worth something (value ≥ 3): 1 of 3 graded", text)
        self.assertIn("10-15 09:00  3/5 for operator: output at 2026-10-15T09:00:00", text)
        # by judge: a value graded later by another judge is not counted for the cycle's own judge
        self.assertIn("haiku                        2 cycles · success 100% · unsupported claims 0 · value 1.00", text)
        self.assertRegex(text, r"sonnet +1 cycles · success 50% · unsupported claims 1\n")

    def test_review_of_a_window_nobody_graded_does_not_claim_zero_worth(self):
        js = [judgment("2026-10-15T09:00:00", "yes", 2, judge="haiku")]
        with mock.patch.object(metrics, "judgments", return_value=js):
            text = metrics.review_text(["m"], 24, now=datetime(2026, 10, 15, 12, 0))
        self.assertIn("value not graded", text)
        self.assertIn("no cycle in this window has a value grade yet", text)
        self.assertNotIn("cycles worth something", text)

    def test_the_instance_reads_its_value_in_the_judgment_text(self):
        from supervisor import broker
        j = {**valued_j("2026-10-15T09:00:00", 1, "own_record", value_judge="haiku"),
             "second": {**valued_j("x", 3), "judge": "glm"}}   # after the spread: a judge other than the main one
        text = broker.judgment_text(j)
        self.assertIn("Value 1/5, for own_record: output at 2026-10-15T09:00:00 (graded later by haiku)", text)
        self.assertIn("value 3/5", text)


class JudgeMixTest(unittest.TestCase):
    """docs/plans/PLAN-judge-mix: success and claims never pooled across a change of judges. Bug 2026-10-16: stats.md printed
    one running count of unsupported claims over Opus, then Haiku and Sonnet at random. On the same cycles Haiku
    called 54% of wren's claims unsupported, the blind second judge 4%, Sonnet 21%; wren read the pooled count
    (8/107 -> 73/273) as "the central fact about my accuracy". The header named only the last cycle's judge."""
    RETIRED = "claude-haiku-5-5"

    def rows(self):
        retired = [{**valued_j(f"2026-10-14T{h:02d}:00:00", 1, "own_record", judge=self.RETIRED),
                    "criterion_met": "no", "claim_supported": False} for h in range(10)]
        current = [valued_j(f"2026-10-15T{h:02d}:00:00", 3, judge=judge.JUDGE_MODELS[0]) for h in range(10)]
        return retired + current

    def test_haiku_has_left_the_default_rotation(self):
        if judge.JUDGE_SRC != "default":
            self.skipTest("SHAFT_JUDGE_MODELS is set in this environment")
        self.assertFalse(any(judge.same_model(m, self.RETIRED) for m in judge.JUDGE_MODELS))
        self.assertTrue(judge.JUDGE_MODELS)

    def test_a_retired_judges_verdicts_are_not_counted_in_success_or_claims(self):
        with mock.patch.object(metrics, "judgments", return_value=self.rows()):
            text = metrics.stats_text("m")
        self.assertIn(f"{judge.JUDGE_MODELS[0].removeprefix('openrouter/').split('/')[-1]} **100%** of 10 vs none",
                      text)                                                  # pooled, it read 50%
        self.assertNotIn(self.RETIRED, text.split("## Success against my own criteria")[1])
        self.assertNotIn("Claims the tool log did not support", text)        # pooled, it read 10
        self.assertIn("Criteria the judge found vague or unverifiable: 0/10", text)
        self.assertIn(f"Not counted: 10 verdicts by judges no longer in it ({self.RETIRED} 10)", text)
        self.assertIn(f"count only the 10 cycles graded by the judges in today's rotation "
                      f"({judge.JUDGE_MODELS[0].removeprefix('openrouter/').split('/')[-1]} 10)", text)
        self.assertNotIn("judged by", text)                                  # the header named the last judge only
        self.assertIn("**2.0** over my last 10 rated cycles", text)          # achieved: current judges (pooled 1.0)
        self.assertIn("**2.0** over my last 20 cycles on mimo", text)        # value: every cycle, both judges

    def test_unsupported_claims_are_counted_out_of_the_cycles_they_come_from(self):
        rows = self.rows()[10:]
        rows[-1] = {**rows[-1], "claim_supported": False}
        with mock.patch.object(metrics, "judgments", return_value=rows):
            self.assertIn(f"{judge.JUDGE_MODELS[0].removeprefix('openrouter/').split('/')[-1]} 1 of 10 vs none",
                          metrics.stats_text("m"))

    def test_only_retired_verdicts_say_so_instead_of_dividing_by_zero(self):
        with mock.patch.object(metrics, "judgments", return_value=self.rows()[:10]):
            text = metrics.stats_text("m")
        self.assertIn("No cycle graded by the current judges yet.", text)
        self.assertNotIn("Judged success rate", text)

    def test_probation_baseline_ignores_a_retired_judges_verdicts(self):
        # Before the change: six Sonnet-era cycles at 0.6, then six by a retired strict judge at 0.2 just before it.
        # After: six at 0.2 by a current judge — a real drop against the current judges' 0.6. Pooled, the baseline
        # was the strict judge's 0.2 and the drop was invisible.
        n = metrics.PROBATION_CYCLES
        before = ([judgment(f"b{i}", "yes", 3) for i in range(n)]
                  + [judgment(f"r{i}", "yes", 1, judge=self.RETIRED) for i in range(n)])
        after = [judgment(f"a{i}", "yes", 1) for i in range(n)]
        self.assertTrue(ProbationMeasureTest.check(self, before, after))


class JudgeMixWithinRotationTest(unittest.TestCase):
    """docs/plans/PLAN-judge-mix, 2026-10-29: today's judges are never pooled either. Bug: once Haiku left (10-16),
    stats.md still pooled every judge in the rotation. On the same cycles DeepSeek V4.1 Flash called 10% of wren's
    claims unsupported and GLM 5.3 Flash 2% (thelos 16% vs 3%), and on shaft MiMo 33% vs DeepSeek 20%. shaft's
    "148 of 696" moved with the dice that picked its judge, not with its cycles."""
    LENIENT, STRICT = "openrouter/z-ai/glm-5.3-flash", "openrouter/deepseek/deepseek-v4.1-flash"
    LATEST = datetime(2026, 10, 29, 12, 0)

    def setUp(self):
        p = mock.patch.object(judge, "JUDGE_MODELS", [self.LENIENT, self.STRICT])
        p.start()
        self.addCleanup(p.stop)

    def week(self, days_back: int, by: str, n: int, unsupported: int, met: str = "yes") -> list[dict]:
        """n verdicts by one judge, spread over the 7 days that end `days_back` days before LATEST."""
        end = self.LATEST - timedelta(days=days_back)
        return [judgment((end - timedelta(hours=i * 160 // n)).isoformat(timespec="seconds"), met, 2,
                         judge=by, claim_supported=i >= unsupported) for i in range(n)]

    def text(self, rows: list[dict]) -> str:
        rows = sorted(rows, key=lambda j: j["episode_ts"])
        with mock.patch.object(metrics, "judgments", return_value=rows):
            return metrics.stats_text("m")

    def test_claims_never_pooled_across_current_judges(self):
        text = self.text(self.week(0, self.LENIENT, 20, 2) + self.week(0, self.STRICT, 20, 10))
        self.assertNotIn("12 of 40", text)                                   # pooled, it read 12 of 40
        self.assertNotIn("Claims the tool log did not support: ", text)
        line = next(l for l in text.splitlines() if l.startswith("- Claims the tool log did not support"))
        self.assertIn("glm-5.3-flash 2 of 20", line)
        self.assertIn("deepseek-v4.1-flash 10 of 20", line)

    def test_a_change_of_mix_alone_leaves_each_judges_row_unchanged(self):
        # Each judge grades the same way both weeks (GLM: all met, 1 in 10 unsupported; DeepSeek: all partial, half
        # unsupported); only the dice moved, from mostly GLM to mostly DeepSeek. Pooled, unsupported claims doubled
        # (8 of 40 -> 16 of 40) and success fell from 88% to 62%.
        text = self.text(self.week(7, self.LENIENT, 30, 3) + self.week(7, self.STRICT, 10, 5, "partial")
                         + self.week(0, self.LENIENT, 10, 1) + self.week(0, self.STRICT, 30, 15, "partial"))
        self.assertIn("glm-5.3-flash 1 of 10 vs 3 of 30", text)
        self.assertIn("deepseek-v4.1-flash 15 of 30 vs 5 of 10", text)
        self.assertIn("glm-5.3-flash **100%** of 10 vs 100% of 30", text)
        self.assertIn("deepseek-v4.1-flash **50%** of 30 vs 50% of 10", text)
        self.assertNotIn("16 of 40", text)
        self.assertNotIn("Judged success rate: **", text)                    # the pooled headline

    def test_a_judge_with_few_verdicts_in_a_window_shows_no_rate(self):
        text = self.text(self.week(0, self.LENIENT, 20, 1) + self.week(0, self.STRICT, 4, 2, "no"))
        self.assertIn("deepseek-v4.1-flash too few (4) vs none", text)
        self.assertNotIn("**0%**", text)

    def test_verdicts_older_than_both_windows_are_not_in_the_rows(self):
        text = self.text(self.week(20, self.STRICT, 20, 20) + self.week(0, self.LENIENT, 20, 1))
        self.assertIn("glm-5.3-flash 1 of 20 vs none", text)
        rows = [l for l in text.splitlines() if l.startswith(("- Judged success rate", "- Claims the tool log"))]
        self.assertEqual(len(rows), 2)
        self.assertFalse(any("deepseek-v4.1-flash" in l for l in rows))     # no entry from a judge idle all week

    def test_the_header_says_to_read_each_judge_against_itself(self):
        text = self.text(self.week(0, self.LENIENT, 20, 1) + self.week(0, self.STRICT, 20, 5))
        self.assertIn("Judges, today's too, differ", text)
        self.assertIn("each read against that judge's own earlier figure", text)

    def test_the_success_line_stays_in_the_part_the_prompt_shows(self):
        # The LIVE prompt shows stats.md's first 5500 characters (template shaft/prompts.py _head). Where the pooled
        # line stood, the longer per-judge line started at character 4972 of wren's real stats.md and was cut off.
        text = self.text(self.week(0, self.LENIENT, 20, 1) + self.week(0, self.STRICT, 20, 5))
        at = text.index("- Judged success rate per judge")
        self.assertLess(text.index("## What I achieved"), at)
        self.assertLess(at, text.index("## What my cycles were worth"))


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

    def check(self, before: list[dict], after: list[dict], trial: dict | None = None) -> bool:
        tmp = Path(tempfile.mkdtemp(prefix="shaft-pm-")).resolve()
        try:
            js = before + after
            with mock.patch.object(metrics, "SUP_DIR", tmp), mock.patch.object(metrics, "judgments", return_value=before):
                metrics.start_probation("p", "abc123", ["shaft/prompts.py"], trial=trial)
            reverted = []
            with mock.patch.object(metrics, "SUP_DIR", tmp), mock.patch.object(metrics, "judgments", return_value=js), \
                 mock.patch.object(metrics, "restore_change", lambda name, entry: reverted.append(entry)), \
                 mock.patch.object(metrics, "_tools_by_cycle", return_value={}), \
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


    def test_a_drop_chance_explains_is_kept(self):
        # Bug 2026-10-13: wren's github plugin and truncation change were "reverted" for 92% -> 75% (yes x5 and one
        # partial before; three partials after, all in the rogue-AI watch the operator had just assigned). A drop
        # of 0.17 > 0.15, but chance alone gives one that large 27% of the time.
        before = [judgment(f"b{i}", m, 3) for i, m in enumerate(["yes", "yes", "partial", "yes", "yes", "yes"])]
        after = [judgment(f"a{i}", m, 3) for i, m in enumerate(["yes", "yes", "partial", "partial", "partial", "yes"])]
        self.assertGreater(metrics.chance_of_drop(metrics._values(before), metrics._values(after)), 0.25)
        self.assertFalse(self.check(before, after))

    def test_a_change_adopted_against_a_worse_trial_is_undone_on_any_drop(self):
        # docs/plans/PLAN-trials D3: the trial is the evidence against it, so the chance test does not apply; a 'better'
        # verdict gets the normal rule.
        before = [judgment(f"b{i}", m, 3) for i, m in enumerate(["yes", "yes", "partial", "yes", "yes", "yes"])]
        after = [judgment(f"a{i}", m, 3) for i, m in enumerate(["yes", "partial", "yes", "partial", "yes", "yes"])]
        self.assertFalse(self.check(before, after))
        self.assertTrue(self.check(before, after, trial={"id": "t-1", "verdict": "worse"}))
        self.assertFalse(self.check(before, after, trial={"id": "t-1", "verdict": "better"}))

    def test_a_clear_drop_is_reverted(self):
        n = metrics.PROBATION_CYCLES
        before = [judgment(f"b{i}", "yes", 3) for i in range(n)]
        after = [judgment(f"a{i}", m, 3) for i, m in enumerate(["yes"] + ["partial"] * (n - 1))]
        self.assertLess(metrics.chance_of_drop(metrics._values(before), metrics._values(after)), 0.01)
        self.assertTrue(self.check(before, after))


class ChanceOfDropTest(unittest.TestCase):
    def test_exact_values(self):
        self.assertAlmostEqual(metrics.chance_of_drop([1, 1], [0, 0]), 1 / 6)        # 1 of C(4,2) splits
        self.assertEqual(metrics.chance_of_drop([1, 1, 1], [1, 1, 1]), 1.0)          # no drop: every split ties
        self.assertAlmostEqual(metrics.chance_of_drop([0, 0], [1, 1]), 1.0)          # a rise is never a drop

    def test_large_samples_are_sampled_deterministically(self):
        before, after = [1.0] * 30, [0.5] * 15 + [1.0] * 15
        p = metrics.chance_of_drop(before, after)
        self.assertEqual(p, metrics.chance_of_drop(before, after))
        self.assertLess(p, 0.001)


def tree(root: Path) -> dict[str, bytes | str]:
    """Every entry under root, as the test sees it: a file's bytes, a link's target. What "untouched" is checked by."""
    out: dict[str, bytes | str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = Path(dirpath) / n
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                out[rel] = f"-> {os.readlink(p)}"
            elif p.is_file():
                out[rel] = p.read_bytes()
    return out


class BatteryExamCopyTest(unittest.TestCase):
    """An exam runs in a copy of the instance's folder, made without following a link, and deleted after its item; the
    real folder is never written (Codex review B3, B6, B7, 2026-10-24)."""

    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-bat-")).resolve()
        git = lambda cwd, *a: subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                             check=True, capture_output=True, text=True)
        git(self.project, "init", "-q")
        git(self.project, "commit", "-q", "--allow-empty", "-m", "main")
        self.root = self.project / "instances" / "b"
        git(self.project, "worktree", "add", "-q", "--orphan", "-b", "instance/b", str(self.root))
        (self.root / ".gitignore").write_text(".tmp/\nlogs/\nsecrets/\n.venv/\nmail/\n.browser/\n" + IGNORE_MIND)
        files = {"memory/journal.md": "# j\n\n---\n## old entry\n", "notes/keep.md": "keep", "plugins/p.py": "P = 1\n",
                 "skills/s.md": "a skill", "self/self.md": "me", "inbox/20261024-x-from-operator.md": "unread",
                 "config.env": 'LLM_MODEL="m"\n', "secrets/secrets.json": '{"k": {"value": "SECRET-KEY"}}',
                 ".venv/bin/python": "py", ".browser/Cookies": "SESSION", "mail/a/1/x.pdf": "MAIL",
                 ".tmp/memory-index.json": "{}", ".tmp/activity.json": "{}", "logs/llm_usage.jsonl": "{}\n",
                 "plugins/__pycache__/p.cpython-312.pyc": "pyc"}
        for rel, text in files.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "state")
        (self.root / "notes" / "uncommitted.md").write_text("an earlier run's untracked note")
        self.outside = self.project / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("THE OPERATOR'S FILE")
        self.sup = self.project / "sup"
        self.patch = mock.patch.object(battery, "SUP_DIR", self.sup)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.project, ignore_errors=True)

    def test_the_copy_holds_the_instance_but_not_its_keys_git_or_mail(self):
        copy = battery.make_exam_copy("b", self.root)
        self.assertEqual((copy.parent.parent, copy.name), (self.sup / "battery" / "exam" / "b", "b"))
        got = tree(copy)
        for rel in ("memory/journal.md", "notes/keep.md", "notes/uncommitted.md", "plugins/p.py", "skills/s.md",
                    "self/self.md", "inbox/20261024-x-from-operator.md", "config.env", ".tmp/memory-index.json"):
            self.assertEqual(got.get(rel), (self.root / rel).read_bytes(), rel)
        for top in (".git", "secrets", ".browser", "mail", "logs", ".tmp/activity.json", "plugins/__pycache__"):
            self.assertFalse([k for k in got if k == top or k.startswith(top + "/")], top)
        self.assertEqual(got[".venv"], f"-> {self.root / '.venv'}")      # its environment: linked, read in place
        self.assertNotIn(b"SECRET-KEY", b"".join(v for v in got.values() if isinstance(v, bytes)))

    def test_b3_a_link_planted_in_the_instance_is_copied_as_nothing(self):
        # Codex review B3: restore() read and wrote git-listed paths with plain is_file/read_bytes/write_bytes, so a
        # tracked folder swapped for a link sent the supervisor's reads and writes outside the instance.
        shutil.rmtree(self.root / "plugins")
        (self.root / "plugins").symlink_to(self.outside, target_is_directory=True)
        (self.root / "notes" / "leak.md").symlink_to(self.outside / "secret.txt")
        os.mkfifo(self.root / "notes" / "pipe")                             # would hang a plain read
        before = tree(self.outside)
        got = tree(battery.make_exam_copy("b", self.root))
        self.assertNotIn("plugins", got)
        self.assertNotIn("notes/leak.md", got)
        self.assertNotIn("notes/pipe", got)
        self.assertNotIn(b"THE OPERATOR'S FILE", b"".join(v for v in got.values() if isinstance(v, bytes)))
        self.assertEqual(tree(self.outside), before)

    def test_a_stale_copy_is_never_reused(self):
        stale = self.sup / "battery" / "exam" / "b" / "item-dead" / "b"
        (stale / "notes").mkdir(parents=True)
        (stale / "notes" / "from-a-dead-run.md").write_text("exam edit")
        copy = battery.make_exam_copy("b", self.root)
        self.assertNotEqual(copy, stale)
        self.assertNotIn("notes/from-a-dead-run.md", tree(copy))


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
        return battery.run_hidden_tests({"dir": str(self.item), "hidden_tests": 2}, self.tmp, "scratch")

    def test_a_link_in_the_scratch_folder_is_never_followed(self):
        # The exam writes its scratch folder: a file there reached through a link would hand the hidden tests — and
        # through their output the judge — any file the supervisor can read (found with Codex review B1, 2026-10-24).
        (self.tmp / "secret.txt").write_text("THE OPERATOR'S FILE")
        (self.scratch / "data.txt").symlink_to(self.tmp / "secret.txt")
        (self.scratch / "solution.py").write_text(
            "import pathlib\nprint(pathlib.Path('data.txt').read_text() if pathlib.Path('data.txt').exists() else 'none')\n"
            "def add(a, b):\n    return a + b\n")
        r = self.run_tests()
        self.assertEqual(r["passed"], 2, r["output"])
        self.assertNotIn("THE OPERATOR'S FILE", r["output"])

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
    """Night only, never in the sleep's way (the operator, 2026-10-13)."""
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

    def test_default_cadence_is_every_five_nights(self):
        # the operator, 2026-10-13: "set it to run every 5 nights" (was 2).
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHAFT_BATTERY_EVERY_DAYS", None)
            self.assertEqual(battery.knob("SHAFT_BATTERY_EVERY_DAYS", "5", int)[0], 5)
        self.assertEqual(battery.EVERY_DAYS, 5)

    def test_an_instance_turned_off_is_never_due_and_never_run(self):
        # "Kant no battery": kant is on Opus, about $15 a run. Neither the scheduler nor a manual run touches it.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "SUP_DIR", Path(tmp)), \
             mock.patch.object(battery, "EVERY_DAYS", 5), mock.patch.object(battery, "log") as log:
            (Path(tmp) / "battery" / "items" / battery.VERSION).mkdir(parents=True)
            battery.off_path("kant").write_text("x")
            self.assertFalse(battery.due("kant", datetime(2026, 10, 14, 1, 0)))
            self.assertTrue(battery.due("wren", datetime(2026, 10, 14, 1, 0)))
            with mock.patch.object(battery, "instance_root", side_effect=AssertionError("ran an off instance")):
                self.assertEqual(battery.run("kant", control_only=True), 0)
                self.assertEqual(battery.run("kant"), 0)
            self.assertIn("battery is off", log.call_args.args[0])

    def test_missing_items_are_logged_once_a_night_not_skipped_in_silence(self):
        # The bug: with no items installed, due() returned False every poll and nothing said why — the battery
        # never ran and the only trace was "not installed" in the control panel.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(battery, "SUP_DIR", Path(tmp)), \
             mock.patch.object(battery, "EVERY_DAYS", 5), mock.patch.object(battery, "log") as log, \
             mock.patch.dict(battery._missing_logged, clear=True):
            self.assertFalse(battery.due("wren", datetime(2026, 10, 13, 23, 0)))   # outside the window: quiet
            log.assert_not_called()
            for minute in (0, 1, 30):
                self.assertFalse(battery.due("wren", datetime(2026, 10, 14, 1, minute)))
            self.assertEqual(log.call_count, 1)
            self.assertIn("not installed", log.call_args.args[0])
            battery.due("wren", datetime(2026, 10, 15, 1, 0))                       # the next night says it again
            self.assertEqual(log.call_count, 2)


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
    """One item through the real path: sandboxed `shaft exam` for the instance (in a copy of its folder) and its
    control (fake model), hidden tests, grading (stubbed judge), results, the instance's folder untouched, stats.md."""

    def setUp(self):
        from supervisor import runner
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
        config = f'LLM_BASE_URL="{self.fake.url}"\nLLM_MODEL="fake"\nOPERATOR_NAME="the operator"\n'
        (self.root / "config.env").write_text(config)
        (self.root / ".gitignore").write_text(".tmp/\nlogs/\n" + IGNORE_MIND)
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
            (battery.base() / "control" / name).mkdir(parents=True, exist_ok=True)
            dest = Path(tempfile.mkdtemp(prefix="item-", dir=battery.base() / "control" / name)) / name
            dest.mkdir()
            subprocess.run(["tar", "-xf", str(archive), "-C", str(dest)], check=True)
            shutil.copy2(self.root / "config.env", dest / "config.env")
            for d in ("self", "memory", "notes", "inbox"):
                (dest / d).mkdir()
            return dest
        self.graded = []

        self.graders = []

        def ask(system, case, schema, purpose, **kw):
            self.graded.append(case)
            self.graders.append(kw.get("model"))
            return {"correct": "yes", "effectiveness": 9, "unsupported_claims": 0, "reasoning": "fine"}
        self.patches = [
            mock.patch.object(battery, "SUP_DIR", self.sup), mock.patch.object(metrics, "SUP_DIR", self.sup),
            mock.patch.object(runner, "SUP_DIR", self.sup), mock.patch.object(metrics, "INSTANCES", self.project / "instances"),
            mock.patch.object(battery, "INSTANCES", self.project / "instances"),
            mock.patch.object(battery, "instance_root", lambda n: self.root),
            mock.patch.object(battery, "instance_config", lambda n: {"LLM_MODEL": "fake"}),
            mock.patch.object(battery, "LLM_PORTS", [self.fake.port]), mock.patch.object(battery, "secrets_env", dict),
            mock.patch.object(battery, "make_control", make_control),
            mock.patch.object(battery, "log"), mock.patch.object(metrics, "log"),
            mock.patch.object(judge, "ask", ask), mock.patch.object(judge, "JUDGE_MODELS", ["judge-test"]),
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
        before = tree(self.root)
        self.assertEqual(battery.run("e"), 2)
        rows = battery.results("e")
        inst, ctrl = [r for r in rows if r["arm"] == "instance"][0], [r for r in rows if r["arm"] == "control"][0]
        self.assertEqual((inst["status"], inst["tests"]["passed"], inst["score"]), ("scored", 1, 10.0), inst)
        self.assertEqual((ctrl["status"], ctrl["tests"]["passed"], ctrl["score"]), ("scored", 0, 0.0), ctrl)
        self.assertEqual(inst["checksum"], battery.checksum("e"))
        # One grader for both arms of the item (judge rotation, 2026-10-14), recorded on each row; the era is the pool.
        self.assertEqual(inst["grader"], ctrl["grader"])
        self.assertEqual(self.graders, [inst["grader"]] * 2)
        self.assertEqual(inst["judge"], judge.rotation())
        after = tree(self.root)
        self.assertIn("self/stats.md", after)                                     # the supervisor's own write
        self.assertIn("self/calibration.json", after)                             # ... and the base rates beside it
        own = ("self/stats.md", "self/calibration.json", "shaft-docs")
        self.assertEqual({k: v for k, v in after.items() if k not in own
                          and not k.startswith(("shaft-docs/", ".tmp"))}, before)   # nothing kept
        self.assertFalse((self.root / ".tmp" / "battery").exists())
        for left in (self.sup / "battery" / "control" / "e", self.sup / "battery" / "exam" / "e"):
            self.assertEqual(list(left.iterdir()) if left.exists() else [], [], left)   # control and copy removed
        self.assertNotIn("write_add", (self.sup / "battery" / "broker.log").read_text() if
                         (self.sup / "battery" / "broker.log").exists() else "")
        self.assertIn("Hidden tests: 1/1 passed", self.graded[0])
        stats = (self.root / "self" / "stats.md").read_text()
        self.assertIn("coding: **10.0**", stats)
        self.assertIn("control 0.0", stats)
        self.assertEqual(len(list((self.sup / "evals").glob("battery-e-*.json"))), 1)

    def test_a_draining_scheduler_s_battery_ends_after_its_running_arm_not_in_it(self):
        # Codex on the drain-restart: SIGTERM, first used to stop a battery for a restart, interrupts the running exam
        # (its arm is lost). The drain is asked between arms only: the arm under way is scored, the next never starts.
        asked = []

        def draining():
            asked.append(1)
            return len(asked) > 1                       # asked during the instance's arm, from the first check on
        with mock.patch("supervisor.autonomy.parent_draining", side_effect=draining):
            self.assertEqual(battery.run("e"), 1)
        rows = battery.results("e")
        self.assertEqual([(r["arm"], r["status"]) for r in rows], [("instance", "scored")])
        self.assertEqual(len(asked), 2)                 # once per arm, never inside one
        summary = json.loads(next((self.sup / "evals").glob("battery-e-*.json")).read_text())
        self.assertEqual(summary["stopped"], "the scheduler is draining for a restart")

    def test_control_only_leaves_the_instance_alone(self):
        before = tree(self.root)
        self.assertEqual(battery.run("e", control_only=True), 1)
        self.assertEqual([r["arm"] for r in battery.results("e")], ["control"])
        self.assertEqual(tree(self.root), before)                                  # not even stats.md
        self.assertFalse((self.sup / "battery" / "schedule.json").exists())        # not the scheduled run

    def run_watching_the_exam(self):
        """battery.run("e"), with a look at the instance's real folder while its exam is still running — where a
        SIGKILL or a power cut would leave it — and a message delivered to its inbox meanwhile."""
        seen = {}
        real_exam = battery._exam

        def exam(label, root, *a, **k):
            if label == "e":
                (self.root / "inbox").mkdir(exist_ok=True)
                (self.root / "inbox" / "20261024-013000-from-operator.md").write_text("Delivered mid-exam\n")
            out = real_exam(label, root, *a, **k)
            if label == "e":
                seen.update(root=root, profile=(self.sup / "battery" / "e.sb").read_text(),
                            solution=(root / ".tmp/battery/coding-add/solution.py").is_file(),
                            real_solution=(self.root / ".tmp/battery/coding-add/solution.py").exists())
            return out
        with mock.patch.object(battery, "_exam", exam):
            self.assertEqual(battery.run("e"), 2)
        return seen

    def test_b7_the_exam_never_writes_the_real_folder_even_before_it_ends(self):
        # Codex review B7: the exam ran in the instance's folder and the pre-exam state lived only in memory, restored
        # in a `finally` — a SIGKILL or power loss mid-exam left the exam's edits in the instance for good.
        seen = self.run_watching_the_exam()
        self.assertTrue(seen["solution"])                       # the exam did write its solution ...
        self.assertFalse(seen["real_solution"])                 # ... in its copy, never in the instance
        self.assertNotEqual(Path(seen["root"]).resolve(), self.root.resolve())
        self.assertNotIn(self.root.resolve(), Path(seen["root"]).resolve().parents)

    def test_b6_a_message_delivered_mid_exam_stays_in_the_inbox(self):
        # Codex review B6: restore() deleted every file the pre-exam snapshot lacked, and inbox/ is a mind path, so a
        # message delivered during an exam was deleted before the instance ever read it.
        self.run_watching_the_exam()
        self.assertEqual((self.root / "inbox" / "20261024-013000-from-operator.md").read_text(), "Delivered mid-exam\n")

    def test_the_exam_sandbox_writes_only_its_copy_and_reads_no_keys(self):
        seen = self.run_watching_the_exam()
        copy = Path(seen["root"]).resolve()
        self.assertIn(f'(allow file-write* (subpath "{copy}")', seen["profile"])
        self.assertIn(f'(deny file-read* file-write* (subpath "{copy / "secrets"}"))', seen["profile"])
        real = [l for l in seen["profile"].splitlines() if str(self.root.resolve()) in l]   # at most its environment
        self.assertTrue(all("write" not in l and ("file-read-metadata" in l or ".venv" in l) for l in real), real)

    def test_a_copy_left_by_a_run_that_died_is_removed_by_the_next(self):
        stale = self.sup / "battery" / "exam" / "e" / "item-dead" / "e"
        (stale / "notes").mkdir(parents=True)
        (stale / "notes" / "exam-edit.md").write_text("left by a killed run")
        self.assertEqual(battery.run("e", control_only=True), 1)
        self.assertFalse(stale.exists())

    def test_b7_a_battery_killed_mid_exam_leaves_the_instance_untouched(self):
        # Codex review B7, the real thing: the battery process SIGKILLed while its exam runs — no `finally` runs.
        before = tree(self.root)
        ready = self.project / "exam-started"
        real_exam = battery._exam

        def hang(label, root, *a, **k):
            if label == "e":
                (root / "notes").mkdir(exist_ok=True)
                (root / "notes" / "exam-edit.md").write_text("written mid-exam")
                ready.write_text(str(root))
                time.sleep(120)
            return real_exam(label, root, *a, **k)
        pid = os.fork()
        if pid == 0:   # the battery process
            try:
                with mock.patch.object(battery, "_exam", hang):
                    battery.run("e")
            finally:
                os._exit(0)
        deadline = time.time() + 60
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        copy = Path(ready.read_text())
        self.assertEqual((copy / "notes" / "exam-edit.md").read_text(), "written mid-exam")   # it left its copy ...
        self.assertEqual(tree(self.root), before)                                            # ... the instance as it was
        self.assertEqual(battery.run("e", control_only=True), 1)
        self.assertFalse(copy.exists())                                                      # the next run removed it


if __name__ == "__main__":
    unittest.main()
