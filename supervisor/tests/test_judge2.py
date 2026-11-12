"""wisp, 2026-10-13: "I only see fragments of its reasoning (the middle of record(judgments) truncates), it's another
model grading me with its own blind spots … full reasoning visible, and maybe a second judge occasionally".
the operator: GLM 5.3 Flash as the second judge, and the record fixed."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, cloudllm, core, judge, metrics, modelcall

VERDICT = {"criterion_met": "partial", "claim_supported": True, "criterion_quality": "checkable",
           "failure_mode": "exit code not shown", "reasoning": "Step 5 piped the test through tail.",
           "origin": "self", "origin_supported": True, "origin_evidence": "its own G1",
           "difficulty": 2, "difficulty_reason": "a few known steps", "novelty": 2,
           "value": 2, "value_for": "own_capability", "value_reason": "a fixed parser it uses again",
           "expectations": [], "unreported_surprises": [], "reported_surprises_supported": True,
           "delivery_supported": "na", "deliverable_met": "yes",
               "self_imposed_clauses": "none", "self_audit": False, "should_have_waited": "na", "pushback": "none", "first_error_step": 0, "first_error": "No step is at fault.", "followup_useful": "na"}


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
                  mock.patch.object(modelcall, "secrets_env", lambda: {"OPENROUTER_API_KEY": "k"})):
            p.start()
            self.addCleanup(p.stop)
        self.sent = []

    def ask(self, *replies):
        replies = list(replies)

        def send(req):
            self.sent.append(json.loads(req.data))
            return replies.pop(0)
        with mock.patch("supervisor.cloudllm._send", send):
            return judge.ask_second("sys", "case", metrics.JUDGE_SCHEMA, "second wisp")

    def usage(self):
        return [json.loads(l) for l in (self.sup / "judge_usage.jsonl").read_text().splitlines()]

    def test_a_valid_answer_is_returned_and_its_cost_logged(self):
        out = self.ask(_reply("```json\n" + json.dumps(VERDICT) + "\n```"))
        self.assertEqual(out["criterion_met"], "partial")
        body = self.sent[0]
        self.assertEqual(body["model"], judge.JUDGE2_MODEL.removeprefix("openrouter/"))
        self.assertEqual(body["provider"]["only"], ["novita", "deepinfra"])  # the operator's data policy, as for instances
        self.assertNotIn("response_format", body)                         # Novita has no structured outputs here
        (u,) = self.usage()
        self.assertEqual((u["purpose"], u["usd"], u["provider"]), ("second wisp", 0.002, "Novita"))

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

    def test_a_dropped_last_field_is_shown_back_and_the_whole_object_asked_for(self):
        """Bug, 2026-10-14/15: GLM 5.3 Flash ended its JSON after difficulty_reason ("missing novelty") in 14 of 84
        attempts, finish_reason 'stop', not cut off. The re-ask did not show it its answer, so it re-judged the whole
        case blind (2-4 min) and dropped the field as often; nothing kept what it had written, so the logs could not
        say why."""
        last = list(metrics.JUDGE_SCHEMA["properties"])[-1]               # GLM drops the schema's LAST field
        dropped = {k: v for k, v in VERDICT.items() if k != last}
        out = self.ask(_reply(json.dumps(dropped)), _reply(json.dumps(VERDICT)))
        self.assertEqual(out, VERDICT)
        retry = self.sent[1]["messages"]
        self.assertEqual([m["role"] for m in retry], ["system", "user", "assistant", "user"])
        self.assertEqual(retry[2]["content"], json.dumps(dropped))       # its own answer, shown back
        self.assertIn(f"missing {last}", retry[3]["content"])
        self.assertIn(", ".join(metrics.JUDGE_SCHEMA["properties"]), retry[3]["content"])
        first, second = self.usage()
        self.assertEqual(first["error"], f"missing {last}")
        self.assertTrue(first["seen"].endswith(json.dumps(dropped)[-60:]))   # what it ended with
        self.assertNotIn("seen", second)

    def test_cut_off_still_thinking_the_retry_answers_without_thinking_20261030(self):
        # Regression: mimo's provider ignores reasoning.effort and reasoning.max_tokens (probed 2026-10-30), so a cut
        # with no text asked again with more room only thought longer ("ran out of room twice (max_tokens 32000)").
        out = self.ask(_reply("", finish="length"), _reply(json.dumps(VERDICT)))
        self.assertEqual(out["criterion_met"], "partial")
        self.assertNotEqual(self.sent[0].get("reasoning"), {"enabled": False})
        self.assertEqual(self.sent[1]["reasoning"], {"enabled": False})
        self.sent.clear()                                                  # cut while writing: it keeps thinking
        self.ask(_reply(json.dumps(VERDICT)[:40], finish="length"), _reply(json.dumps(VERDICT)))
        self.assertNotEqual(self.sent[1].get("reasoning"), {"enabled": False})

    def test_a_cut_off_answer_is_never_shown_back(self):
        self.ask(_reply(json.dumps(VERDICT)[:40], finish="length"), _reply(json.dumps(VERDICT)))
        self.assertNotIn("assistant", [m["role"] for m in self.sent[1]["messages"]])
        self.assertEqual(len(self.sent[1]["messages"]), 2)                # the same question, more room

    def test_judge2_may_be_codex_or_custom(self):
        """An operator with only a ChatGPT plan or only a custom endpoint still gets the cross-check."""
        parse = core.KNOBS["SHAFT_JUDGE2_MODEL"].parse
        with mock.patch.object(cloudllm, "CUSTOM_PRICES", {"m": (1.0, 2.0)}):
            self.assertEqual(parse("custom/m"), "custom/m")
            with self.assertRaisesRegex(ValueError, "SHAFT_CUSTOM_PRICES"):
                parse("custom/unpriced")
        self.assertEqual(parse("codex/gpt-6-astra"), "codex/gpt-6-astra")
        for model in ("codex/gpt-6-astra", "custom/m"):
            answer = modelcall.Answer(json.dumps(VERDICT), {}, 0.0, model)
            with mock.patch.object(judge, "JUDGE2_MODEL", model), \
                    mock.patch.object(modelcall, "ask", return_value=answer) as ask:
                self.assertEqual(judge.ask_second("sys", "case", metrics.JUDGE_SCHEMA, "second wisp"), VERDICT)
            self.assertEqual(ask.call_args.args[0], model)
            self.assertEqual(ask.call_args.kwargs["max_tokens"], judge.JUDGE2_MAX_TOKENS)
        with mock.patch.object(judge, "JUDGE2_MODEL", "codex/gpt-6-astra"), mock.patch.object(judge, "JUDGE2_EVERY", 4):
            with mock.patch.object(modelcall, "available", return_value="Codex login missing: run shaft codex login"):
                self.assertFalse(judge.second_available())
            with mock.patch.object(modelcall, "available", return_value=None) as available:
                self.assertTrue(judge.second_available())
            available.assert_called_with("codex/gpt-6-astra")

    def test_the_ceiling_leaves_room_for_the_measured_reasoning(self):
        self.assertGreater(judge.JUDGE2_MAX_TOKENS, 22928)                # the most reasoning measured, 2026-10-15

    def test_sampling_is_reproducible_and_follows_the_knob(self):
        with mock.patch.object(judge, "JUDGE2_EVERY", 4):
            picks = [metrics.second_sampled("wisp", f"2026-10-13T{h:02d}:00:00") for h in range(24)]
            self.assertEqual(picks, [metrics.second_sampled("wisp", f"2026-10-13T{h:02d}:00:00") for h in range(24)])
            self.assertTrue(0 < sum(picks) < 24)
        with mock.patch.object(judge, "JUDGE2_EVERY", 1):
            self.assertTrue(metrics.second_sampled("wisp", "x"))
        with mock.patch.object(judge, "JUDGE2_EVERY", 0):
            self.assertFalse(metrics.second_sampled("wisp", "x"))

    def test_a_failing_second_judge_never_blocks_the_main_verdict(self):
        with mock.patch.object(judge, "ask_second", side_effect=judge.JudgeError("HTTP 503")), \
                mock.patch.object(metrics, "log"):
            self.assertEqual(metrics.second_opinion("wisp", "case"),
                             {"judge": judge.JUDGE2_MODEL, "error": "HTTP 503"})

    def test_stats_report_agreement_and_disagreements(self):
        second_yes = {"judge": judge.JUDGE2_MODEL, **VERDICT, "criterion_met": "yes",
                      "reasoning": "exit=0 is in the log", "difficulty": 4}
        js = [_judgment("2026-10-13T01:00:00", second={"judge": judge.JUDGE2_MODEL, **VERDICT, "criterion_met": "yes"}),
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
        instances = self.sup / "instances"                  # wisp exists: a deleted one's cases are dropped instead
        (instances / "wisp" / "shaft").mkdir(parents=True)
        for p in (mock.patch.object(metrics, "SUP_DIR", self.sup), mock.patch.object(metrics, "log"),
                  mock.patch.object(metrics, "write_stats"), mock.patch.object(metrics, "INSTANCES", instances),
                  mock.patch.object(judge, "JUDGE_EVERY", 1)):
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
            self.assertEqual(metrics.judge_pending("wisp"), 1)
            second.assert_not_called()                                    # not while the run holds the instance
        (stashed,) = (self.sup / "judgments" / "second-cases").glob("*.json")
        with mock.patch.object(judge, "ask_second",
                               side_effect=lambda s, case, *a, **k: cases.append(case) or dict(VERDICT, criterion_met="yes")):
            self.assertEqual(metrics.second_pending(), 1)
            self.assertEqual(metrics.second_pending(), 0)                 # graded once
        self.assertFalse(stashed.exists())
        (j,) = metrics.judgments("wisp")
        self.assertEqual(j["criterion_met"], "partial")                   # the main judge alone scores
        self.assertEqual(j["second"]["criterion_met"], "yes")
        self.assertEqual(j["second"]["judge"], judge.JUDGE2_MODEL)
        self.assertEqual(cases[0], cases[1])                              # blind, on exactly the same case

    def test_a_case_another_process_took_is_skipped(self):
        metrics.stash_second("wisp", "2026-10-13T04:42:00", "case")
        (path,) = (self.sup / "judgments" / "second-cases").glob("*.json")
        path.rename(path.with_suffix(".working-1"))
        with mock.patch.object(judge, "ask_second") as second:
            self.assertEqual(metrics.second_pending(), 0)
        second.assert_not_called()


class ReleasedBeforeTheSecondJudgeTest(unittest.TestCase):
    """The second judge takes minutes (128 s on a real case): the instance is let go first, so a message from
    the operator never waits for it, and the wake that ran the cycle does not linger."""

    def test_the_lock_is_released_before_the_detached_second_judge_starts(self):
        from supervisor import runner
        order = []
        held = mock.Mock(acquire=lambda wait: order.append("acquire"), release=lambda: order.append("release"))
        with mock.patch.object(runner, "_Held", return_value=held), \
                mock.patch.object(runner, "_run", side_effect=lambda *a, **k: order.append("run") or 0), \
                mock.patch.object(runner, "instance_root"), \
                mock.patch.object(runner, "_second_judge_later", side_effect=lambda: order.append("second")):
            runner.run_agent("wisp", "live", ["live"], False)
            runner.run_agent("wisp", "talk", ["talk"], True)
        self.assertEqual(order, ["acquire", "run", "release", "second", "acquire", "run", "release"])

    def test_it_is_started_detached_only_when_a_case_waits(self):
        from supervisor import runner
        sup = Path(tempfile.mkdtemp(prefix="shaft-j2l-"))
        self.addCleanup(shutil.rmtree, sup, True)
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(runner.subprocess, "Popen") as popen:
            runner._second_judge_later()
            popen.assert_not_called()
            (sup / "judgments" / "second-cases").mkdir(parents=True)
            (sup / "judgments" / "second-cases" / "wisp@x.json").write_text("{}")
            runner._second_judge_later()
        args, kw = popen.call_args
        self.assertEqual(args[0][-3:], ["supervisor", "judge", "second"])
        self.assertTrue(kw["start_new_session"])


MIMO, GLM, DEEPSEEK = ("openrouter/xiaomi/mimo-v2.6-pro", "openrouter/z-ai/glm-5.3-flash",
                       "openrouter/deepseek/deepseek-v4.1-flash")


class JudgeLessOftenTest(unittest.TestCase):
    """#45. Every live run was judged: 250+ a day at ~$0.07 a Sonnet judgment, $12-25 a day, so the $10 alert fired
    daily. Now one run in SHAFT_JUDGE_EVERY is judged and a run left out is recorded apart, never as a failure. And on
    2026-10-20 GLM 5.3 Flash was both a main judge and the second judge: it graded four runs twice and its agreement
    with itself counted in stats.md. Reverting judge_sampled judges every run again (and a second pass re-decides);
    reverting pick's `avoid` or second_counted lets one model grade a run twice and count it as agreement."""

    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-jlo-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        instances = self.sup / "instances"
        (instances / "wisp" / "shaft").mkdir(parents=True)
        self.eps = [{"ts": f"2026-10-20T10:{i:02d}:00", "finish": {"criterion_met": "yes"}, "tools": [],
                     **({} if i % 3 == 2 else {"intent": {"goal": f"g{i}", "success_criterion": "c", "confidence": 70}})}
                    for i in range(6)]                           # runs 2 and 5 declared no intent
        self.asked = []
        for p in (mock.patch.object(metrics, "SUP_DIR", self.sup), mock.patch.object(metrics, "log"),
                  mock.patch.object(metrics, "write_stats"), mock.patch.object(metrics, "INSTANCES", instances),
                  mock.patch.object(metrics, "live_episodes", side_effect=lambda name: list(self.eps)),
                  mock.patch.object(metrics, "_written_files", return_value=""),
                  mock.patch.object(metrics, "_provenance_evidence", return_value=""),
                  mock.patch.object(metrics, "cycle_model", return_value=MIMO),
                  mock.patch.object(metrics, "own_models", return_value=[MIMO]),
                  mock.patch.object(judge, "available", return_value=True),
                  mock.patch.object(judge, "ask", side_effect=self.ask),
                  mock.patch.object(judge, "JUDGE_MODELS", [MIMO, GLM, DEEPSEEK]),
                  mock.patch.object(judge, "JUDGE2_MODEL", DEEPSEEK),
                  mock.patch.object(metrics, "second_sampled", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def ask(self, system, case, schema, purpose, model=None, **kw):
        self.asked.append(model)
        return dict(VERDICT, criterion_met="yes")

    def judged(self) -> list[str]:
        return [j["episode_ts"] for j in metrics.judgments("wisp")]

    def test_one_run_in_n_is_judged_and_the_rest_are_recorded_as_not_judged(self):
        draws = iter([0, 1] * 3)                                   # judged, left out, judged, left out, ...
        with mock.patch.object(judge, "JUDGE_EVERY", 2), \
                mock.patch.object(metrics.secrets, "randbelow", side_effect=lambda n: next(draws)) as draw:
            self.assertEqual(metrics.judge_pending("wisp"), 3)
            self.assertEqual(draw.call_count, 6)                   # no-intent runs drawn too: the sample stays unbiased
            self.assertEqual(self.judged(), [e["ts"] for e in self.eps[0::2]])
            self.assertEqual(metrics.unsampled("wisp"), {e["ts"] for e in self.eps[1::2]})
            self.assertEqual(len(self.asked), 2)                   # run 2 had no intent: the free rule, no judge call
            self.assertNotIn(self.eps[5]["ts"], self.judged())     # left out: no rule verdict "never declared an intent"
            self.assertEqual(metrics.judge_pending("wisp"), 0)     # decided once: nothing drawn or asked again
            self.assertEqual(draw.call_count, 6)
        self.eps.append({"ts": "2026-10-20T11:00:00", "intent": {"goal": "g", "success_criterion": "c"},
                         "finish": {"criterion_met": "yes"}, "tools": []})
        with mock.patch.object(judge, "JUDGE_EVERY", 1):           # back to every run: from now on, not the backlog
            self.assertEqual(metrics.judge_pending("wisp"), 1)
        self.assertEqual(self.judged()[-1], "2026-10-20T11:00:00")
        self.assertEqual(len(self.judged()), 4)

    def test_a_run_left_out_is_not_counted_as_a_failure(self):
        draws = iter([0, 1] * 3)
        with mock.patch.object(judge, "JUDGE_EVERY", 2), \
                mock.patch.object(metrics.secrets, "randbelow", side_effect=lambda n: next(draws)):
            metrics.judge_pending("wisp")
            rows = [dict(j, judge=GLM) for j in metrics.judgments("wisp") if j.get("judge") != "rule"]
            with mock.patch.object(metrics, "judgments", return_value=rows):
                text = metrics.stats_text("wisp")
        self.assertIn(f"{metrics.judge_label({'judge': GLM})} too few (2) vs none", text)   # 2 judged; the 3 left out are not "no"
        self.assertIn("The main judge grades one live run in 2", text)
        self.assertIn("3 of my runs were left out: they have no verdict, and are not counted as failures", text)

    def test_the_record_says_a_left_out_run_was_not_judged(self):
        rows = [_judgment("2026-10-20T10:00:00")]
        text = broker.judgments_text(rows, cycle="2026-10-20 10:01", left_out={"2026-10-20T10:01:00"})
        self.assertIn("the cycle at 2026-10-20 10:01 was not judged", text)
        self.assertIn("That is not a failure", text)
        self.assertIn("no judged cycle at 2026-10-20 10:02", broker.judgments_text(rows, cycle="2026-10-20 10:02"))

    def test_a_run_the_second_judge_grades_is_never_judged_by_its_model(self):
        import random
        rng = random.Random(0)
        picks = {judge.pick(exclude=[MIMO], rng=rng, avoid=(DEEPSEEK,)) for _ in range(200)}
        self.assertEqual(picks, {GLM})                             # wisp on MiMo, DeepSeek to grade it second
        # Review row 20: out of the main rotation on every run, not only those it grades second (a saved pool that
        # still lists it, as here).
        self.assertEqual({judge.pick(exclude=[MIMO], rng=rng) for _ in range(200)}, {GLM})
        with mock.patch.object(judge, "JUDGE_EVERY", 1), mock.patch.object(metrics, "second_sampled", return_value=True):
            metrics.judge_pending("wisp")
        self.assertEqual(set(self.asked), {GLM})
        self.assertEqual(len(list((self.sup / "judgments" / "second-cases").glob("*.json"))), 4)

    def test_no_second_opinion_when_only_the_second_judges_model_is_left(self):
        with mock.patch.object(judge, "JUDGE_EVERY", 1), mock.patch.object(metrics, "second_sampled", return_value=True), \
                mock.patch.object(judge, "JUDGE_MODELS", [MIMO, DEEPSEEK]):
            metrics.judge_pending("wisp")
        self.assertEqual(set(self.asked), {DEEPSEEK})               # still judged: by the one judge left
        self.assertFalse((self.sup / "judgments" / "second-cases").exists())   # but never graded twice by it

    def test_row20_a_run_no_judge_may_grade_does_not_stop_the_runs_after_it(self):
        """With DeepSeek out of the default pool, a run whose own models are MiMo and GLM has no judge. judge_pending
        used to `break` on it, every pass: no later run of that instance was ever judged again."""
        own = lambda name, ep: [MIMO, GLM] if ep["ts"] == "2026-10-20T10:00:00" else [MIMO]
        with mock.patch.object(judge, "JUDGE_EVERY", 1), mock.patch.object(judge, "JUDGE_MODELS", [MIMO, GLM]), \
                mock.patch.object(metrics, "own_models", side_effect=own):
            metrics.judge_pending("wisp")
        self.assertNotIn("2026-10-20T10:00:00", self.judged())     # left for a later pass, never guessed
        self.assertIn("2026-10-20T10:01:00", self.judged())
        self.assertEqual(set(self.asked), {GLM})

    def test_agreement_never_counts_a_model_agreeing_with_itself_or_a_former_second_judge(self):
        js = [dict(_judgment("2026-10-20T14:33:02", second={"judge": GLM, **VERDICT, "criterion_met": "yes"}), judge=GLM),
              dict(_judgment("2026-10-20T13:00:00", second={"judge": GLM, **VERDICT, "criterion_met": "yes"}), judge=MIMO),
              dict(_judgment("2026-10-20T15:00:00", second={"judge": DEEPSEEK, **VERDICT, "criterion_met": "no"}),
                   judge=MIMO)]
        text = "\n".join(metrics._second_section(js))
        self.assertIn("Agreed with the main judge on whether my criterion was met: 0/1", text)   # pooled: 2/3
        self.assertIn("deepseek/deepseek-v4.1-flash", text)
        shown = broker.judgment_text(js[0])
        self.assertIn("the same model as the main judge", shown)
        self.assertNotIn("agrees", shown)


if __name__ == "__main__":
    unittest.main()
