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
           "value": 3, "value_for": "own_capability", "value_reason": "fixed parser", **SURPRISE}


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
                 "goal": "fix parser", "confidence": 70, "claimed": "yes", **VERDICT} for ep in episodes]
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
        self.assertAlmostEqual(dry["per_judgment_usd"], .054)
        self.assertIn("estimate", dry["basis"])
        self.ask.assert_not_called()
        self.pick.assert_not_called()
        self.assertFalse((self.sup / "judgments/m-surprise-baseline.jsonl").exists())
        self.assertEqual(metrics.surprise_baseline("m")["done"], 1)
        self.pick.assert_called_once_with(exclude=["own-model"])
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
        self.assertEqual(metrics.surprise_baseline("m", limit=1)["done"], 1)
        self.assertEqual(metrics.surprise_baseline("m")["done"], 1)

    def test_bad_baseline_is_not_written(self):
        self.stored([self.episode()])
        self.ask.side_effect = lambda *a, **k: {**VERDICT, "expectations": [{}]}
        self.assertEqual(metrics.surprise_baseline("m")["done"], 0)
        self.assertEqual(metrics._surprise_baseline_rows("m"), [])

    def test_usage_estimate_uses_cycle_calls_only(self):
        self.sup.mkdir()
        rows = [{"purpose": "judge m", "usd": .02}, {"purpose": "judge n", "usd": .04},
                {"purpose": "second m", "usd": 100}]
        (self.sup / "judge_usage.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        cost, basis = metrics._judgment_cost()
        self.assertAlmostEqual(cost, .03)
        self.assertIn("2 cycle judge calls", basis)
        from supervisor import cloudllm
        rows = [{"purpose": "judge m", "model": "claude-test", "input_tokens": 1000, "output_tokens": 100}]
        (self.sup / "judge_usage.jsonl").write_text(json.dumps(rows[0]))
        with mock.patch.dict(cloudllm.PRICES, {"claude-test": (2, 10, .2)}):
            self.assertAlmostEqual(metrics._judgment_cost()[0], .003)

    def test_report_disagreements_and_unreported(self):
        ep = self.episode()
        self.stored([ep])
        self.ask.side_effect = lambda *a, **k: {**VERDICT, "criterion_met": "partial", "reasoning": "x" * 240,
                                              "unreported_surprises": ["The heredoc failed."]}
        metrics.surprise_baseline("m")
        text = metrics.surprise_baseline_report("m")
        for part in ("Re-judged: 1", "agreement: 0/1", "FAIL", "stored yes, new partial", "x" * 200,
                     "Expectations: 1 held, 0 contradicted, 0 uninformative", "per cycle: 1.00",
                     ep["ts"] + ": The heredoc failed."):
            self.assertIn(part, text)
        self.assertNotIn("x" * 201, text)
        with mock.patch.object(metrics, "_surprise_baseline_rows", return_value=[{**VERDICT, "episode_ts": ep["ts"]}]):
            self.assertIn("PASS", metrics.surprise_baseline_report("m"))

    def test_report_acceptance_boundary_and_empty_measurement(self):
        self.assertIn("not measured", metrics.surprise_baseline_report("m"))
        rows = [{**VERDICT, "episode_ts": str(i)} for i in range(15)]
        new = [dict(r) for r in rows]
        new[0]["criterion_met"] = "no"
        with mock.patch.object(metrics, "judgments", return_value=rows), \
             mock.patch.object(metrics, "_surprise_baseline_rows", return_value=new):
            self.assertIn("PASS", metrics.surprise_baseline_report("m"))
            new.pop()
            self.assertIn("FAIL", metrics.surprise_baseline_report("m"))

    def test_record_text_json_and_old_rows(self):
        _, rows = self.stored([self.episode()])
        row = {**rows[0], "expectations": [{"statement": "pass", "verdict": "contradicted", "evidence": "fail"}],
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
