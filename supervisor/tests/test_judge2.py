"""wren, 2026-10-13: "I only see fragments of its reasoning (the middle of record(judgments) truncates), it's another
model grading me with its own blind spots … full reasoning visible, and maybe a second judge occasionally".
Daniele: GLM 5.3 Flash as the second judge, and the record fixed."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, judge, metrics

VERDICT = {"criterion_met": "partial", "claim_supported": True, "criterion_quality": "checkable",
           "failure_mode": "exit code not shown", "reasoning": "Step 5 piped the test through tail.",
           "origin": "self", "origin_supported": True, "origin_evidence": "its own G1",
           "difficulty": 2, "difficulty_reason": "a few known steps", "novelty": 2}


def _judgment(ts, reasoning="r" * 600, second=None, met="yes"):
    j = {"episode_ts": ts, "judged_at": ts, "goal": f"goal at {ts}", "claimed": "yes", "confidence": 80,
         "judge": "claude-opus-5-5", "model": "mimo", **VERDICT, "criterion_met": met, "reasoning": reasoning}
    if second:
        j["second"] = second
    return j


class RecordShowsWholeVerdictsTest(unittest.TestCase):
    def test_whole_verdicts_newest_first_never_cut(self):
        rows = [_judgment(f"2026-10-13T1{k}:00:00", reasoning=f"REASON-{k} " + "x" * 900) for k in range(10)]
        text = broker.judgments_text(rows)
        self.assertLessEqual(len(text), broker.JUDGMENTS_BUDGET + 400)    # + the paging line
        shown = [k for k in range(10) if f"REASON-{k} " in text]
        self.assertEqual(shown, sorted(shown))
        self.assertIn("REASON-9", text.split("###")[1])                    # newest first
        for k in shown:                                                   # each one whole: its end is there
            self.assertIn(f"REASON-{k} " + "x" * 900, text)
        self.assertIn(f"before='2026-10-13 1{min(shown)}:00'", text)
        older = broker.judgments_text(rows, before=f"2026-10-13 1{min(shown)}:00")
        self.assertIn(f"REASON-{min(shown) - 1}", older)
        self.assertNotIn(f"REASON-{min(shown)} ", older)

    def test_every_field_and_one_cycle_in_full(self):
        rows = [_judgment("2026-10-13T04:42:00"), _judgment("2026-10-13T05:00:00")]
        one = broker.judgments_text(rows, cycle="2026-10-13 04:42")
        for part in ("criterion met **yes**", "failure: exit code not shown", "Difficulty 2/5: a few known steps",
                     "novelty 2/5", "Origin: self", "its own G1", "My model in that cycle: mimo"):
            self.assertIn(part, one)
        self.assertNotIn("05:00", one)
        self.assertIn("no judged cycle at 2026-10-13 04:41", broker.judgments_text(rows, cycle="2026-10-13T04:41"))

    def test_the_second_judge_is_shown_with_its_agreement(self):
        second = {"judge": "openrouter/z-ai/glm-5.3-flash", **VERDICT, "criterion_met": "yes",
                  "reasoning": "The log shows exit=0."}
        text = broker.judgments_text([_judgment("2026-10-13T04:42:00", met="partial", second=second)])
        self.assertIn("Second judge (z-ai/glm-5.3-flash, blind) DISAGREES: criterion met **yes**", text)
        self.assertIn("The log shows exit=0.", text)
        text = broker.judgments_text([_judgment("2026-10-13T04:42:00", second={"judge": "g", "error": "HTTP 503"})])
        self.assertIn("Second judge (g): no verdict (HTTP 503)", text)

    def test_the_record_tool_takes_the_new_arguments(self):
        props = broker.SPECS["record"]["function"]["parameters"]["properties"]
        self.assertIn("cycle", props)
        self.assertIn("before", props)


def _reply(content, finish="stop", cost=0.002):
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}], "provider": "Novita",
            "usage": {"prompt_tokens": 15000, "completion_tokens": 900, "cost": cost}}, None


class SecondJudgeTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-j2-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        for p in (mock.patch.object(judge, "SUP_DIR", self.sup),
                  mock.patch.object(judge, "secrets_env", lambda: {"OPENROUTER_API_KEY": "k"})):
            p.start()
            self.addCleanup(p.stop)
        self.sent = []

    def ask(self, *replies):
        replies = list(replies)

        def send(req):
            self.sent.append(json.loads(req.data))
            return replies.pop(0)
        with mock.patch("supervisor.cloudllm._send", send):
            return judge.ask_second("sys", "case", metrics.JUDGE_SCHEMA, "second wren")

    def usage(self):
        return [json.loads(l) for l in (self.sup / "judge_usage.jsonl").read_text().splitlines()]

    def test_a_valid_answer_is_returned_and_its_cost_logged(self):
        out = self.ask(_reply("```json\n" + json.dumps(VERDICT) + "\n```"))
        self.assertEqual(out["criterion_met"], "partial")
        body = self.sent[0]
        self.assertEqual(body["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(body["provider"]["only"], ["novita"])            # Daniele's data policy, as for instances
        self.assertNotIn("response_format", body)                         # Novita has no structured outputs here
        (u,) = self.usage()
        self.assertEqual((u["purpose"], u["usd"], u["provider"]), ("second wren", 0.002, "Novita"))

    def test_a_cut_off_answer_is_never_parsed_and_asked_once_with_more_room(self):
        out = self.ask(_reply(json.dumps(VERDICT)[:40], finish="length"), _reply(json.dumps(VERDICT)))
        self.assertEqual(out["criterion_met"], "partial")
        self.assertEqual(self.sent[1]["max_tokens"], 2 * judge.JUDGE2_MAX_TOKENS)
        with self.assertRaisesRegex(judge.JudgeError, "out of room twice"):
            self.ask(_reply("{", finish="length"), _reply("{", finish="length"))

    def test_an_answer_that_does_not_match_is_asked_for_again(self):
        bad = dict(VERDICT, criterion_met="mostly")
        out = self.ask(_reply(json.dumps(bad)), _reply(json.dumps(VERDICT)))
        self.assertEqual(out["criterion_met"], "partial")
        self.assertIn("criterion_met='mostly'", self.sent[1]["messages"][-1]["content"])
        with self.assertRaisesRegex(judge.JudgeError, "no usable answer"):
            self.ask(_reply("not json"), _reply("still not"), _reply(json.dumps(bad)))

    def test_sampling_is_reproducible_and_follows_the_knob(self):
        with mock.patch.object(judge, "JUDGE2_EVERY", 4):
            picks = [metrics.second_sampled("wren", f"2026-10-13T{h:02d}:00:00") for h in range(24)]
            self.assertEqual(picks, [metrics.second_sampled("wren", f"2026-10-13T{h:02d}:00:00") for h in range(24)])
            self.assertTrue(0 < sum(picks) < 24)
        with mock.patch.object(judge, "JUDGE2_EVERY", 1):
            self.assertTrue(metrics.second_sampled("wren", "x"))
        with mock.patch.object(judge, "JUDGE2_EVERY", 0):
            self.assertFalse(metrics.second_sampled("wren", "x"))

    def test_a_failing_second_judge_never_blocks_the_main_verdict(self):
        with mock.patch.object(judge, "ask_second", side_effect=judge.JudgeError("HTTP 503")), \
                mock.patch.object(metrics, "log"):
            self.assertEqual(metrics.second_opinion("wren", "case"),
                             {"judge": judge.JUDGE2_MODEL, "error": "HTTP 503"})

    def test_stats_report_agreement_and_disagreements(self):
        second_yes = {"judge": "openrouter/z-ai/glm-5.3-flash", **VERDICT, "criterion_met": "yes",
                      "reasoning": "exit=0 is in the log", "difficulty": 4}
        js = [_judgment("2026-10-13T01:00:00", second={"judge": "x", **VERDICT, "criterion_met": "yes"}),
              _judgment("2026-10-13T02:00:00", met="partial", second=second_yes),
              _judgment("2026-10-13T03:00:00")]
        text = "\n".join(metrics._second_section(js))
        self.assertIn("Agreed with the main judge on whether my criterion was met: 1/2", text)
        self.assertIn("Difficulty within one point: 1/2", text)
        self.assertIn("main judge partial, second judge yes — exit=0 is in the log", text)


class JudgePendingTest(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-jp-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.ep = {"ts": "2026-10-13T04:42:00", "intent": {"goal": "g", "success_criterion": "c", "confidence": 70},
                   "finish": {"criterion_met": "yes"}, "tools": []}
        for p in (mock.patch.object(metrics, "SUP_DIR", self.sup), mock.patch.object(metrics, "log"),
                  mock.patch.object(metrics, "write_stats")):
            p.start()
            self.addCleanup(p.stop)

    def test_the_second_judge_grades_the_same_case_after_the_run_and_its_verdict_is_merged(self):
        cases = []
        with mock.patch.object(metrics, "live_episodes", return_value=[self.ep]), \
                mock.patch.object(metrics, "_written_files", return_value=""), \
                mock.patch.object(metrics, "_provenance_evidence", return_value=""), \
                mock.patch.object(metrics, "cycle_model", return_value="mimo"), \
                mock.patch.object(judge, "available", return_value=True), \
                mock.patch.object(judge, "ask", side_effect=lambda s, case, *a, **k: cases.append(case) or dict(VERDICT)), \
                mock.patch.object(judge, "ask_second") as second, \
                mock.patch.object(metrics, "second_sampled", return_value=True):
            self.assertEqual(metrics.judge_pending("wren"), 1)
            second.assert_not_called()                                    # not while the run holds the instance
        (stashed,) = (self.sup / "judgments" / "second-cases").glob("*.json")
        with mock.patch.object(judge, "ask_second",
                               side_effect=lambda s, case, *a, **k: cases.append(case) or dict(VERDICT, criterion_met="yes")):
            self.assertEqual(metrics.second_pending(), 1)
            self.assertEqual(metrics.second_pending(), 0)                 # graded once
        self.assertFalse(stashed.exists())
        (j,) = metrics.judgments("wren")
        self.assertEqual(j["criterion_met"], "partial")                   # the main judge alone scores
        self.assertEqual(j["second"]["criterion_met"], "yes")
        self.assertEqual(j["second"]["judge"], judge.JUDGE2_MODEL)
        self.assertEqual(cases[0], cases[1])                              # blind, on exactly the same case

    def test_a_case_another_process_took_is_skipped(self):
        metrics.stash_second("wren", "2026-10-13T04:42:00", "case")
        (path,) = (self.sup / "judgments" / "second-cases").glob("*.json")
        path.rename(path.with_suffix(".working-1"))
        with mock.patch.object(judge, "ask_second") as second:
            self.assertEqual(metrics.second_pending(), 0)
        second.assert_not_called()


class ReleasedBeforeTheSecondJudgeTest(unittest.TestCase):
    """The second judge takes minutes (128 s on a real case): the instance is let go first, so a message from
    Daniele never waits for it, and the wake that ran the cycle does not linger."""

    def test_the_lock_is_released_before_the_detached_second_judge_starts(self):
        from supervisor import runner
        order = []
        held = mock.Mock(acquire=lambda wait: order.append("acquire"), release=lambda: order.append("release"))
        with mock.patch.object(runner, "_Held", return_value=held), \
                mock.patch.object(runner, "_run", side_effect=lambda *a, **k: order.append("run") or 0), \
                mock.patch.object(runner, "instance_root"), \
                mock.patch.object(runner, "_second_judge_later", side_effect=lambda: order.append("second")):
            runner.run_agent("wren", "live", ["live"], False)
            runner.run_agent("wren", "talk", ["talk"], True)
        self.assertEqual(order, ["acquire", "run", "release", "second", "acquire", "run", "release"])

    def test_it_is_started_detached_only_when_a_case_waits(self):
        from supervisor import runner
        sup = Path(tempfile.mkdtemp(prefix="shaft-j2l-"))
        self.addCleanup(shutil.rmtree, sup, True)
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(runner.subprocess, "Popen") as popen:
            runner._second_judge_later()
            popen.assert_not_called()
            (sup / "judgments" / "second-cases").mkdir(parents=True)
            (sup / "judgments" / "second-cases" / "wren@x.json").write_text("{}")
            runner._second_judge_later()
        args, kw = popen.call_args
        self.assertEqual(args[0][-3:], ["supervisor", "judge", "second"])
        self.assertTrue(kw["start_new_session"])


if __name__ == "__main__":
    unittest.main()
