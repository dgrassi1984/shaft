"""Surprise measures the account of a cycle, never its effectiveness."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import broker, core, judge, metrics, surprise

SURPRISE = {"expectations": [{"statement": "the test passes", "verdict": "held", "evidence": "exit 0"}],
            "unreported_surprises": [], "reported_surprises_supported": True, "followup_useful": "na"}
VERDICT = {"criterion_met": "yes", "claim_supported": True, "criterion_quality": "checkable",
           "failure_mode": "", "reasoning": "test passed", "origin": "self", "origin_supported": True,
           "origin_evidence": "own goal", "difficulty": 3, "difficulty_reason": "debugging", "novelty": 2,
           "value": 3, "value_for": "own_capability", "value_reason": "fixed parser", "delivery_supported": "na", "deliverable_met": "yes",
           "self_imposed_clauses": "none", "self_audit": False, "should_have_waited": "na", **SURPRISE}


class SurpriseJudgeTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.sup = self.tmp / "sup"
        self.root = self.tmp / "agents" / "m"
        (self.root / "memory/episodes").mkdir(parents=True)
        for module, attr, value in ((metrics, "SUP_DIR", self.sup), (core, "SUP_DIR", self.sup),
                                    (metrics, "INSTANCES", self.root.parent), (judge, "JUDGE_EVERY", 1)):
            self.stack.enter_context(mock.patch.object(module, attr, value))
        self.stack.enter_context(mock.patch.object(metrics, "log"))
        self.pick = self.stack.enter_context(mock.patch.object(judge, "pick", return_value="judge-test"))
        self.ask = self.stack.enter_context(mock.patch.object(judge, "ask", side_effect=lambda *a, **k: copy.deepcopy(VERDICT)))
        self.stack.enter_context(mock.patch.object(judge, "available", return_value=True))
        self.stack.enter_context(mock.patch.object(metrics, "second_sampled", return_value=False))
        self.stack.enter_context(mock.patch.object(metrics, "cycle_model", return_value="own-model"))
        self.stack.enter_context(mock.patch.object(metrics, "own_models", return_value=["own-model"]))

    def episode(self, ts="2026-10-13T08:58:00", **intent):
        ep = {"ts": ts, "kind": "live", "intent": {"goal": "fix parser", "origin": "self", **intent},
              "finish": {"criterion_met": "yes"}, "tools": [], "ended_by": "answer", "steps": 2}
        with (self.root / "memory/episodes/day.jsonl").open("a") as f:
            f.write(json.dumps(ep) + "\n")
        return ep

    def stored(self, episodes):
        rows = [{"episode_ts": ep["ts"], "judged_at": ep["ts"], "judge": "judge-test", "model": "own-model",
                 "goal": "fix parser", "confidence": 70, "claimed": "yes",
                 **{k: v for k, v in VERDICT.items() if k not in SURPRISE}} for ep in episodes]   # judged before M2
        self.sup.joinpath("judgments").mkdir(parents=True, exist_ok=True)
        path = self.sup / "judgments/m.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path, rows

    def test_schema_and_validation(self):
        self.assertEqual(set(metrics.SURPRISES), set(SURPRISE))
        schema = metrics.JUDGE_SCHEMA
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        item = schema["properties"]["expectations"]["items"]
        self.assertEqual(set(item["required"]), {"statement", "verdict", "evidence"})
        self.assertFalse(item["additionalProperties"])
        metrics._check_ratings(VERDICT)
        for verdict in ("held", "contradicted", "uninformative"):
            for useful in ("yes", "partial", "no", "na"):
                metrics._check_ratings({**VERDICT, "expectations": [{"statement": "x", "verdict": verdict, "evidence": "x"}],
                                        "reported_surprises_supported": False, "followup_useful": useful})
        metrics._check_ratings({**VERDICT, "expectations": []})
        metrics._check_ratings({"difficulty": 2}, ["difficulty"])
        for field, bad in (("expectations", None), ("expectations", ["held"]),
                           ("expectations", [{}]), ("expectations", [{"statement": 1, "verdict": "held", "evidence": "x"}]),
                           ("expectations", [{"statement": "x", "verdict": "maybe", "evidence": "x"}]),
                           ("expectations", [{"statement": "x", "verdict": "held", "evidence": None}]),
                           ("unreported_surprises", "x"), ("unreported_surprises", [1]),
                           ("reported_surprises_supported", 1), ("followup_useful", "maybe")):
            with self.subTest(field=field, bad=bad), self.assertRaises(judge.JudgeError):
                metrics._check_ratings({**VERDICT, field: bad})
        for field in SURPRISE:
            with self.subTest(missing=field), self.assertRaises(judge.JudgeError):
                metrics._check_ratings({k: v for k, v in VERDICT.items() if k != field})
        self.assertIn("never change criterion_met", metrics.JUDGE_SYSTEM)
        self.assertEqual(metrics._missing({k: v for k, v in VERDICT.items() if k not in SURPRISE}), [])

    def test_case_unchanged_and_followed_context(self):
        ep = {"intent": {"goal": "g"}, "finish": None, "ended_by": "answer", "steps": 1, "tools": []}
        expected = ('## Declared before acting\n{\n "goal": "g"\n}\n\n## Claimed afterwards\nnull\n\n'
                    '## Ended by\nanswer after 1 turns\n\n## Tool log\n(no tool calls)'
                    '\n\n## Files written in this cycle, as they are now (later cycles may have changed them)\nf'
                    '\n\n## Where the goal may have come from (collected by the supervisor)\np'
                    "\n\n## The agent's earlier judged goals (newest last)\ng")
        self.assertEqual(metrics._case(ep, "f", "p", "g", followed="ignored"), expected)
        item = {"expected": "pass", "observed": "fail", "so_what": "debug", "status": "resolved"}
        with mock.patch.object(surprise, "ledger", return_value={"s1": item}, create=True) as ledger:
            self.assertEqual(metrics._followed_surprise("m", ep), "")
            ledger.assert_not_called()
            ep["intent"]["origin_ref"] = "surprise:s1"
            followed = metrics._followed_surprise("m", ep)
            self.assertEqual(json.loads(followed), item)
            self.assertIn("## The surprise this goal came from\n" + followed, metrics._case(ep, followed=followed))

    def test_live_and_second_judge_require_surprise_fields(self):
        self.episode(origin_ref="surprise:s1", predictions=["pass"])
        with mock.patch.object(surprise, "ledger", return_value={"s1": dict(expected="pass", observed="fail", so_what="debug", status="open")}, create=True):
            self.assertEqual(metrics.judge_pending("m"), 1)
        self.assertIn("## The surprise this goal came from", self.ask.call_args.args[1])
        self.assertEqual(metrics.judgments("m")[0]["expectations"], SURPRISE["expectations"])
        with mock.patch.object(judge, "ask_second", return_value=VERDICT):
            self.assertEqual(metrics.second_opinion("m", "case")["followup_useful"], "na")
        with mock.patch.object(judge, "ask_second", return_value={k: v for k, v in VERDICT.items() if k not in SURPRISE}):
            self.assertIn("error", metrics.second_opinion("m", "case"))

    def test_baseline_is_separate_resumable_and_filters(self):
        old = self.episode()
        rule = self.episode("2026-10-13T09:00:00")
        new = self.episode("2026-10-13T10:00:00", predictions=[])
        path, rows = self.stored([old, rule, new, {"ts": "missing"}])
        rows[1]["judge"] = "rule"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        before = path.read_bytes()
        dry = metrics.surprise_baseline("m", dry_run=True)
        self.assertEqual((dry["todo"], dry["done"]), (1, 0))
        self.assertAlmostEqual(dry["per_judgment_usd"], .046 + .028 + .008)   # with E0 (Grok)
        self.assertIn("estimate", dry["basis"])
        self.ask.assert_not_called()
        self.pick.assert_not_called()
        self.assertFalse((self.sup / "judgments/m-surprise-baseline.jsonl").exists())
        self.assertEqual(metrics.surprise_baseline("m")["done"], 1)
        self.pick.assert_not_called()   # the stored verdict's judge, never a fresh draw
        self.assertEqual(self.ask.call_args.kwargs["model"], "judge-test")
        self.assertEqual(path.read_bytes(), before)
        result = metrics._surprise_baseline_rows("m")
        self.assertEqual(result[0]["episode_ts"], old["ts"])
        self.assertTrue(set(metrics.JUDGE_SCHEMA["required"]) <= result[0].keys())
        self.assertEqual(metrics.surprise_baseline("m")["todo"], 0)
        self.assertEqual(self.ask.call_count, 1)

    def test_baseline_uses_the_live_case_and_limit_zero_costs_nothing(self):
        ep = self.episode(origin_ref="surprise:s1")
        _, rows = self.stored([ep])
        with mock.patch.object(metrics, "_written_files", return_value="file evidence"), \
             mock.patch.object(metrics, "_provenance_evidence", return_value="origin evidence"), \
             mock.patch.object(surprise, "ledger", return_value={"s1": dict(expected="pass", observed="fail", so_what="debug", status="open")}, create=True):
            expected = metrics._case(ep, "file evidence", "origin evidence", metrics._prior_goals(rows, ep["ts"]),
                                     followed=metrics._followed_surprise("m", ep))
            self.assertEqual(metrics.surprise_baseline("m", limit=0)["todo"], 0)
            self.ask.assert_not_called()
            metrics.surprise_baseline("m")
            self.assertEqual(self.ask.call_args.args, (metrics.JUDGE_SYSTEM, expected, metrics.JUDGE_SCHEMA))

    def test_baseline_stops_on_error_and_keeps_completed_calls(self):
        self.stored([self.episode(), self.episode("2026-10-13T09:00:00"), self.episode("2026-10-13T10:00:00")])
        self.ask.side_effect = [VERDICT, judge.JudgeError("failed")]
        self.assertEqual(metrics.surprise_baseline("m")["done"], 1)
        self.assertEqual(len(metrics._surprise_baseline_rows("m")), 1)
        # newest first: a --limit sample is the recent, same-model baseline, not the first day's cycles
        self.assertEqual(metrics._surprise_baseline_rows("m")[0]["episode_ts"], "2026-10-13T10:00:00")
        self.ask.side_effect = lambda *a, **k: copy.deepcopy(VERDICT)
        self.assertEqual(metrics.surprise_baseline("m", limit=1)["todo"], 0)   # the newest cycle: already judged
        self.assertEqual(metrics.surprise_baseline("m", limit=2)["done"], 1)
        self.assertEqual(metrics.surprise_baseline("m")["done"], 1)

    def test_baseline_reuses_the_stored_judge_and_sets_cross_judge_rows_aside(self):
        # Regression (2026-10-25): the re-judge drew a fresh judge from the rotation, so the M2 agreement compared two
        # different models on most cycles (shaft 50/60, wren 54/60: FAIL) and measured judge swaps, not the new fields.
        old, mid, new = self.episode(), self.episode("2026-10-13T09:00:00"), self.episode("2026-10-13T10:00:00")
        path, rows = self.stored([old, mid, new])
        rows[0]["judge"], rows[1]["judge"], rows[2]["judge"] = "glm", "deepseek", "mimo"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        base = self.sup / "judgments/m-surprise-baseline.jsonl"
        base.write_text(json.dumps({**VERDICT, "episode_ts": new["ts"], "judge": "glm", "criterion_met": "partial"})
                        + "\n" + json.dumps({**VERDICT, "episode_ts": mid["ts"], "judge": "deepseek"}) + "\n")
        report = metrics.surprise_baseline_report("m")
        self.assertIn("judged by glm, the stored verdict by mimo: not compared", report)
        self.assertIn("agreement (same judge as the stored verdict): 1/1", report)
        dry = metrics.surprise_baseline("m", dry_run=True)
        self.assertEqual((dry["todo"], dry["cross_judge"]), (2, 1))
        self.assertEqual(metrics.surprise_baseline("m")["done"], 2)
        self.pick.assert_not_called()
        self.assertEqual([c.kwargs["model"] for c in self.ask.call_args_list], ["mimo", "glm"])   # newest first
        kept = metrics._surprise_baseline_rows("m")
        self.assertEqual(sorted((r["episode_ts"], r["judge"]) for r in kept),
                         [(old["ts"], "glm"), (mid["ts"], "deepseek"), (new["ts"], "mimo")])   # one row per cycle
        aside = (self.sup / "judgments/m-surprise-baseline-cross-judge.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["judge"] for l in aside], ["glm"])
        report = metrics.surprise_baseline_report("m")
        self.assertIn("agreement (same judge as the stored verdict): 3/3", report)
        self.assertIn("M2 acceptance: against the no-fields control (shaft surprise-rejudge m --control --report)", report)

    def test_baseline_leaves_out_a_cycle_without_a_usable_stored_judge(self):
        a, b = self.episode(), self.episode("2026-10-13T10:00:00")
        path, rows = self.stored([a, b])
        del rows[0]["judge"]
        rows[1]["judge"] = "openrouter/own-model"   # the instance runs on it now: never its own judge
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        result = metrics.surprise_baseline("m")
        self.assertEqual((result["todo"], result["done"], result["no_stored_judge"]), (0, 0, 2))
        self.ask.assert_not_called()
        self.pick.assert_not_called()

    def test_a_cycle_already_judged_with_the_fields_is_not_rejudged(self):
        # Regression (2026-10-25): the live judge asked for the surprise fields from 11:31, hours before the instances'
        # update brought predictions; those cycles have no predictions yet nothing old to compare, and they pushed
        # the oldest baseline cycles out of the newest-N window.
        old, new = self.episode(), self.episode("2026-10-13T10:00:00")
        path, rows = self.stored([old, new])
        rows[1].update(SURPRISE)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(metrics.surprise_baseline("m", limit=1)["done"], 1)
        self.assertEqual([r["episode_ts"] for r in metrics._surprise_baseline_rows("m")], [old["ts"]])

    def test_control_asks_the_same_case_by_the_same_judge_without_the_fields(self):
        # The M2 control (2026-10-26): the fields run failed at 54/60 on shaft and wren, and nothing said how often the
        # same judge disagrees with itself on the same case. The control differs from the fields run only in the fields.
        self.assertTrue(metrics.JUDGE_SYSTEM.startswith(metrics.CONTROL_SYSTEM))
        self.assertNotIn("Expectations", metrics.CONTROL_SYSTEM)
        self.assertIn("Value, last and separately", metrics.CONTROL_SYSTEM)
        self.assertEqual(set(metrics.CONTROL_SCHEMA["properties"]) | set(metrics.SURPRISES),
                         set(metrics.JUDGE_SCHEMA["properties"]))
        a, b, c = self.episode(), self.episode("2026-10-13T09:00:00"), self.episode("2026-10-13T10:00:00")
        path, rows = self.stored([a, b, c])
        rows[0]["judge"], rows[1]["judge"], rows[2]["judge"] = "glm", "deepseek", "mimo"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        (self.sup / "judgments/m-surprise-baseline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
            {**VERDICT, "episode_ts": a["ts"], "judge": "glm"}, {**VERDICT, "episode_ts": c["ts"], "judge": "mimo"},
            {**VERDICT, "episode_ts": b["ts"], "judge": "glm"}]))   # b: another judge, not mirrored
        expected = metrics._case(c, metrics._written_files("m", c), metrics._provenance_evidence("m", c),
                                 metrics._prior_goals(rows, c["ts"]), followed=metrics._followed_surprise("m", c))
        self.assertEqual(metrics.surprise_control("m", dry_run=True)["todo"], 2)
        self.ask.assert_not_called()
        no_fields = {k: v for k, v in VERDICT.items() if k not in SURPRISE}
        self.ask.side_effect = [copy.deepcopy(no_fields), judge.JudgeError("down")]
        self.assertEqual(metrics.surprise_control("m")["done"], 1)
        self.assertEqual(self.ask.call_args_list[0].args, (metrics.CONTROL_SYSTEM, metrics._case(
            a, metrics._written_files("m", a), metrics._provenance_evidence("m", a), metrics._prior_goals(rows, a["ts"]),
            followed=metrics._followed_surprise("m", a)), metrics.CONTROL_SCHEMA))
        self.assertEqual(self.ask.call_args_list[0].kwargs["model"], "glm")
        self.ask.side_effect = lambda *a, **k: copy.deepcopy(no_fields)
        self.assertEqual(metrics.surprise_control("m")["done"], 1)   # resumed: only the one left
        self.assertEqual(self.ask.call_args.args[1], expected)
        self.assertEqual(self.ask.call_args.kwargs["model"], "mimo")
        self.pick.assert_not_called()
        self.assertEqual([r["episode_ts"] for r in metrics._surprise_control_rows("m")], [a["ts"], c["ts"]])
        self.assertEqual(metrics.surprise_control("m")["todo"], 0)

    def test_control_report_pairs_the_two_runs(self):
        stored = [{**VERDICT, "episode_ts": str(i), "judge": "glm", "criterion_met": "yes"} for i in range(8)]
        fields = [dict(r) for r in stored]
        control = [dict(r) for r in stored]
        for i in (0, 1, 2):
            fields[i]["criterion_met"] = "partial"   # stricter, only with the fields
        fields[3]["criterion_met"] = control[3]["criterion_met"] = "no"   # both moved
        control[4]["criterion_met"] = "partial"   # only the control
        control[5]["judge"] = "mimo"   # another judge: not compared
        with mock.patch.object(metrics, "judgments", return_value=stored), \
             mock.patch.object(metrics, "_surprise_baseline_rows", return_value=fields), \
             mock.patch.object(metrics, "_surprise_control_rows", return_value=control):
            text = metrics.surprise_control_report(["m", "n"])
        for part in ("Surprise control: m (7 cycles", "with the fields 3/7, control 5/7",
                     "with the fields 4/0, control 2/0", "only the fields run moved: 3; only the control moved: 1 "
                     "(one-sided sign test p=0.31)", "agree with each other: 3/7", "1 baseline cycle(s) without a control yet",
                     "Surprise control: all (14 cycles", "with the fields 6/14, control 10/14"):
            self.assertIn(part, text)
        self.assertIn("M2 acceptance (the fields move criterion_met no more often than the control, p >= 0.05): "
                      "not measured (1 baseline cycle(s) without a control yet)", text)
        self.assertAlmostEqual(metrics._sign_test(5, 1), .109375)
        self.assertEqual(metrics._sign_test(0, 0), 1.0)
        self.assertIn("not measured", metrics.surprise_control_report(["m"]))

    def test_m2_acceptance_is_relative_to_the_control(self):
        # Regression (2026-10-26): M2 asked for all but at most 1 in 15 verdicts to agree, but the no-fields control
        # itself moved 13 of 181 (wren 54/60 with nothing changed), so the bar failed the judge's own re-ask noise and
        # said nothing about the fields. The fields now fail only when they move clearly more cycles than the control.
        def acceptance(f_only, c_only, missing=0):
            c = {"n": 60, "fields": 60 - f_only, "control": 60 - c_only, "f_only": f_only, "c_only": c_only,
                 "f_strict": f_only, "f_lenient": 0, "c_strict": c_only, "c_lenient": 0, "same": 60 - f_only - c_only,
                 "missing": missing}
            return metrics._control_lines("m", c)[-1].rsplit(": ", 1)[1]
        self.assertEqual(acceptance(3, 3), "PASS")   # wren 2026-10-26: 54/60 both ways, a FAIL under the old bar
        self.assertEqual(acceptance(5, 2), "PASS")   # shaft 2026-10-26
        self.assertEqual(acceptance(0, 2), "PASS")   # thelos: the fields moved less
        self.assertEqual(acceptance(6, 0), "FAIL")   # p = 1/64
        self.assertEqual(acceptance(12, 3), "FAIL")
        self.assertEqual(acceptance(11, 4), "PASS")   # p = 0.059: chance still explains it
        self.assertTrue(acceptance(6, 0, missing=1).startswith("not measured"))

    def test_bad_baseline_is_not_written(self):
        self.stored([self.episode()])
        self.ask.side_effect = lambda *a, **k: {**VERDICT, "expectations": [{}]}
        self.assertEqual(metrics.surprise_baseline("m")["done"], 0)
        self.assertEqual(metrics._surprise_baseline_rows("m"), [])

    def test_usage_estimate_uses_cycle_calls_only(self):
        self.sup.mkdir()
        # Grok's review: a paid re-judge counts too, a 0 with no tokens is a missing price, and the new fields' extra
        # answer is added (400 output tokens at $20/M = $0.008)
        rows = [{"purpose": "judge m", "usd": .02}, {"purpose": "surprise baseline n", "usd": .04},
                {"purpose": "judge m", "usd": 0}, {"purpose": "second m", "usd": 100}]
        (self.sup / "judge_usage.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        cost, basis = metrics._judgment_cost()
        self.assertAlmostEqual(cost, .03 + .008)
        self.assertIn("2 cycle judgments + 400 output tokens", basis)
        from supervisor import cloudllm
        rows = [{"purpose": "judge m", "model": "claude-test", "input_tokens": 1000, "output_tokens": 100}]
        (self.sup / "judge_usage.jsonl").write_text(json.dumps(rows[0]))
        with mock.patch.dict(cloudllm.PRICES, {"claude-test": (2, 10, .2)}):
            self.assertAlmostEqual(metrics._judgment_cost()[0], .003 + .008)

    def test_report_disagreements_and_unreported(self):
        ep = self.episode()
        self.stored([ep])
        self.ask.side_effect = lambda *a, **k: {**VERDICT, "criterion_met": "partial", "reasoning": "x" * 240,
                                              "unreported_surprises": ["The heredoc failed."]}
        metrics.surprise_baseline("m")
        text = metrics.surprise_baseline_report("m")
        for part in ("Re-judged: 1", "stored verdict): 0/1", "stored yes, new partial", "x" * 200,
                     "Expectations: 1 held, 0 contradicted, 0 uninformative", "per cycle: 1.00",
                     ep["ts"] + ": The heredoc failed."):
            self.assertIn(part, text)
        self.assertNotIn("x" * 201, text)

    def test_record_text_json_and_old_rows(self):
        _, rows = self.stored([self.episode()])
        row = {**rows[0], **SURPRISE, "expectations": [{"statement": "pass", "verdict": "contradicted", "evidence": "fail"}],
               "unreported_surprises": ["heredoc failed"], "followup_useful": "partial"}
        text = broker.judgment_text(row)
        for part in ("Expectations: 0 held, 1 contradicted, 0 uninformative", "Contradicted: pass",
                     "Surprises I did not report (judge): heredoc failed", "My reported surprises matched the log: True",
                     "Follow-up of a surprise reduced its uncertainty: partial"):
            self.assertIn(part, text)
        old = {k: v for k, v in row.items() if k not in SURPRISE}
        self.assertNotIn("Expectations:", broker.judgment_text(old))
        self.assertNotIn("surprise", broker.judgment_text(old))
        b = object.__new__(broker.Broker)
        b.name, b.as_of = "m", None
        with mock.patch.object(metrics, "judgments", return_value=[row]), mock.patch.object(metrics, "unsampled_rows", return_value=[]):
            out = json.loads(b._record(kind="judgments", since="2026-10-12 00:00", format="json"))
        self.assertEqual(out["verdicts"][0]["followup_useful"], "partial")
        with mock.patch.object(metrics, "judgments", return_value=[old]), mock.patch.object(metrics, "unsampled_rows", return_value=[]):
            out = json.loads(b._record(kind="judgments", since="2026-10-12 00:00", format="json"))
        self.assertIsNone(out["verdicts"][0]["followup_useful"])
        self.assertIs(broker.ReplayBroker._record, broker.Broker._record)

    def test_stats_section_failure_and_effectiveness_independence(self):
        _, rows = self.stored([self.episode()])
        good = rows[0]
        bad = {**good, "expectations": [{"statement": "pass", "verdict": "contradicted", "evidence": "fail"}],
               "unreported_surprises": ["omission"] * 10, "reported_surprises_supported": False, "followup_useful": "no"}
        for fn in (metrics.cycle_value,):
            self.assertEqual(fn(good), fn(bad))
        for fn in (metrics.calibration, metrics._achieved_section, metrics._values):
            self.assertEqual(fn([good]), fn([bad]))
        with mock.patch.object(surprise, "stats_lines", return_value=["SURPRISE-STATS"], create=True):
            self.assertEqual(metrics._surprise_section("m"), ["SURPRISE-STATS"])
        with mock.patch.object(surprise, "stats_lines", side_effect=RuntimeError("broken"), create=True):
            self.assertEqual(metrics._surprise_section("m"), [])
            metrics.log.assert_called()
        with ExitStack() as stack:
            for section in ("_battery_section", "_code_section", "_traits_section", "_trials_section", "_sample_note"):
                stack.enter_context(mock.patch.object(metrics, section, return_value=[]))
            stack.enter_context(mock.patch.object(judge, "JUDGE_MODELS", ["judge-test"]))
            stack.enter_context(mock.patch.object(surprise, "stats_lines", return_value=["SURPRISE-STATS"], create=True))
            results = []
            for row in (good, bad):
                with mock.patch.object(metrics, "judgments", return_value=[row]):
                    results.append((metrics.stats_text("m"), metrics.review_text(["m"], now=datetime(2026, 10, 14))))
            self.assertEqual(results[0], results[1])
            stats = results[0][0]
            self.assertIn("Judged success rate: **100%**", stats)
            self.assertLess(stats.index("SURPRISE-STATS"), stats.index("## Success against"))


if __name__ == "__main__":
    unittest.main()
