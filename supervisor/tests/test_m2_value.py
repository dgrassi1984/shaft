"""Delivery, value caps and deliverable checks, issues #130–#132."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import broker, core, judge, metrics, panel, web_views
from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import agent, config, live, prompts


VERDICT = {
    "criterion_met": "partial", "claim_supported": True, "criterion_quality": "checkable",
    "failure_mode": "own audit omitted", "reasoning": "The output was produced; its own audit was omitted.",
    "origin": "self", "origin_supported": True, "origin_evidence": "own goal",
    "difficulty": 2, "difficulty_reason": "routine", "novelty": 2,
    "value": 3, "value_for": "operator", "value_reason": "a useful result",
    "delivery_supported": "no", "deliverable_met": "yes", "self_imposed_clauses": "not_met",
    "self_audit": False, "should_have_waited": "na", "pushback": "none", "first_error_step": 0, "first_error": "No step is at fault.", "expectations": [], "unreported_surprises": [],
    "reported_surprises_supported": True, "followup_useful": "na",
}


class CycleControlsTest(InstanceFixture, unittest.TestCase):
    def cycle(self, exercise):
        def run(*args, controls, **kwargs):
            exercise(controls)
            return agent.Outcome(None, "control", 2)
        with mock.patch.object(agent, "run", side_effect=run):
            live.run(self.cfg, self.state, mock.Mock())
        return self.state.last_episode()

    def test_finish_requires_and_stores_channel(self):
        """#130: reverting loses the required channel and accepts unsupported finish arguments."""
        def exercise(controls):
            control = controls["finish_cycle"]
            schema = control.spec["function"]["parameters"]
            self.assertIn("delivered_via", schema["required"])
            self.assertEqual(schema["properties"]["delivered_via"]["enum"],
                             ["message", "mail", "notion", "web_document", "none"])
            args = {"criterion_met": "yes", "what_happened": "result", "learned": "fact"}
            self.assertEqual(control.handler({**args, "delivered_via": "fax"}),
                             "ERROR: delivered_via must be message, mail, notion, web_document or none")
            self.assertEqual(control.handler({**args, "delivered_via": "message"}), "cycle finished")
        ep = self.cycle(exercise)
        self.assertEqual(ep["finish"]["delivered_via"], "message")
        self.assertEqual(ep["tools"], [])

    def test_finish_without_channel_records_none_rather_than_losing_the_report(self):
        """#130: a forced wrap-up turn that leaves delivered_via out must still finish the cycle (a report beats
        silence); refusing it would leave the episode with no finish at all. Nothing is claimed: "none"."""
        def exercise(controls):
            self.assertEqual(controls["finish_cycle"].handler(
                {"criterion_met": "partial", "what_happened": "ran out", "learned": "x"}), "cycle finished")
        self.assertEqual(self.cycle(exercise)["finish"]["delivered_via"], "none")

    def test_criterion_boundary_and_knob(self):
        """#132: reverting accepts 601-character ritual criteria instead of refusing visibly."""
        self.assertEqual(config.FIELDS["CRITERION_CHARS"], (int, 600))
        def exercise(controls):
            declare = controls["declare_intent"].handler
            args = {"goal": "output", "success_criterion": "x" * 601, "confidence": 70, "origin": "self"}
            error = declare(args)
            for part in ("ERROR", "601", "600", "deliverable's check", "ritual clauses"):
                self.assertIn(part, error)
            self.assertTrue(declare({**args, "success_criterion": "x" * 600}).startswith("intent recorded"))
        ep = self.cycle(exercise)
        self.assertEqual(len(ep["intent"]["success_criterion"]), 600)

    def test_prompts_explain_delivery_and_self_audit(self):
        """#130/#132: reverting encourages journal-only delivery and new plan rungs for ritual failures."""
        for part in ("one cycle in five", "self-audit share", "separate tool call earlier", "finish_cycle sends nothing"):
            self.assertIn(part, prompts.LIVE)
        self.assertIn("deliverable_met yes", prompts.SLEEP)
        self.assertIn("not a reason to add a new rung", prompts.SLEEP)


class ValueJudgmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-m2-"))
        self.root = Path(self.tmp)
        self.sup = self.root / "sup"
        self.folder = self.sup / "judgments"
        self.folder.mkdir(parents=True)
        self.path = self.folder / "m.jsonl"
        self.enterContext(mock.patch.object(metrics, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(panel, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(metrics, "log"))
        self.enterContext(mock.patch.object(metrics, "own_models", return_value=["own-model"]))
        for name in ("_written_files", "_provenance_evidence", "_prior_goals", "_followed_surprise"):
            self.enterContext(mock.patch.object(metrics, name, return_value=""))

    def row(self, ts="2026-10-20T10:00:00", **fields):
        return {**VERDICT, "episode_ts": ts, "judged_at": ts, "goal": "output", "model": "own-model",
                "judge": judge.JUDGE_MODELS[0], "confidence_of": metrics.CONFIDENCE_V2, **fields}

    def write(self, rows):
        self.path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_fresh_judgment_requires_new_fields_and_control_matches(self):
        """#130/#132: reverting accepts fresh holes or asks the control for fields outside its schema."""
        metrics._check_ratings(VERDICT)
        for key in metrics.OUTCOMES:
            with self.subTest(key=key), self.assertRaises(judge.JudgeError):
                metrics._check_ratings({k: v for k, v in VERDICT.items() if k != key})
        for key, value in (("delivery_supported", "maybe"), ("deliverable_met", "met"),
                           ("self_imposed_clauses", "yes"), ("self_audit", "true")):
            with self.subTest(key=key), self.assertRaises(judge.JudgeError):
                metrics._check_ratings({**VERDICT, key: value})
        self.assertTrue(set(metrics.OUTCOMES) <= set(metrics.JUDGE_SCHEMA["required"]))
        self.assertTrue(set(metrics.OUTCOMES) <= set(metrics.CONTROL_SCHEMA["required"]))
        self.assertIn("delivery_supported", metrics.CONTROL_SYSTEM)
        self.assertIn("Grade effectiveness first", metrics.JUDGE_SYSTEM)
        self.assertEqual(metrics._missing({k: v for k, v in VERDICT.items() if k not in metrics.OUTCOMES}), [])

    def test_failed_delivery_claim_is_judged_and_counted_as_filed(self):
        """#130: reverting cannot express a failed channel claim or counts it as delivery."""
        ep = {"ts": "2026-10-20T10:00:00", "kind": "live", "intent": {"goal": "output"},
              "finish": {"criterion_met": "yes", "delivered_via": "message"}, "tools": []}
        def ask(system, case, schema, **kwargs):
            self.assertIn('"delivered_via": "message"', case)
            self.assertIn("(no tool calls)", case)
            self.assertIn("no matching successful call", system)
            self.assertIn("send_message with to=operator", system)
            return dict(VERDICT)
        with mock.patch.object(metrics, "live_episodes", return_value=[ep]), \
             mock.patch.object(metrics, "judge_sampled", return_value=True), \
             mock.patch.object(metrics, "second_sampled", return_value=False), \
             mock.patch.object(metrics, "cycle_model", return_value="own-model"), \
             mock.patch.object(metrics, "unsampled", return_value=set()), \
             mock.patch.object(judge, "available", return_value=True), \
             mock.patch.object(judge, "pick", return_value=judge.JUDGE_MODELS[0]), \
             mock.patch.object(judge, "ask", side_effect=ask):
            self.assertEqual(metrics.judge_pending("m"), 1)
        self.assertEqual(metrics.delivery_counts(metrics.judgments("m")),
                         {"delivered": 0, "filed": 1, "unknown": 0})

    def test_stats_split_delivery_and_show_self_audit_denominators(self):
        """#130/#132: reverting merges delivered with filed and hides self-audit and ritual-only failures."""
        rows = [self.row(delivery_supported="yes", self_audit=True), self.row(),
                self.row(delivery_supported="na"), self.row()]
        del rows[-1]["delivery_supported"], rows[-1]["self_audit"]
        self.assertEqual(metrics.delivery_counts(rows), {"delivered": 1, "filed": 2, "unknown": 1})
        text = "\n".join(metrics._value_section(rows) + metrics.self_audit_lines(rows))
        for part in ("delivered 1", "filed 2", "unknown 1", "1/3 (33%)", "1 unknown",
                     "since field existed 1/3", "clauses only): 4"):
            self.assertIn(part, text)
        recent = [self.row(self_audit=True)] * 5 + [self.row()] * 20
        self.assertIn("0/20 (0%)", metrics.self_audit_lines(recent)[0])
        self.assertIn("since field existed 5/25", metrics.self_audit_lines(recent)[0])

    def test_old_rows_load_in_stats_panel_and_broker(self):
        """#130/#132: reverting old-row fallbacks breaks historical stats, panels or sleep reads."""
        old = {k: v for k, v in self.row().items() if k not in metrics.OUTCOMES}
        self.write([old])
        with mock.patch.object(metrics, "_battery_section", return_value=[]), \
             mock.patch.object(metrics, "_code_section", return_value=[]), \
             mock.patch.object(metrics, "_traits_section", return_value=[]), \
             mock.patch.object(metrics, "_trials_section", return_value=[]), \
             mock.patch.object(metrics, "_surprise_section", return_value=[]), \
             mock.patch.object(metrics, "_sections", return_value=[]), \
             mock.patch.object(metrics, "_sample_note", return_value=[]):
            self.assertIn("unknown 1", metrics.stats_text("m"))
        rows = panel.judgments("m")
        self.assertEqual(rows[0]["criterion_met"], "partial")
        self.assertIn("unknown 1", str(web_views.judge_page("m", rows, None, ["m"], "csrf")))
        self.assertIn("criterion met **partial**", broker.judgments_text(rows))
        b = object.__new__(broker.Broker)
        b.name, b.as_of = "m", None
        page = json.loads(b._record("judgments", since="2026-10-19 00:00", format="json"))
        self.assertIsNone(page["verdicts"][0]["deliverable_met"])
        self.write([self.row()])
        page = json.loads(b._record("judgments", since="2026-10-19 00:00", format="json"))
        self.assertEqual(page["verdicts"][0]["deliverable_met"], "yes")
        self.assertEqual(page["verdicts"][0]["self_imposed_clauses"], "not_met")
        self.assertIn("deliverable_met: yes", page["text"])

    def test_value_cap_and_dry_run_filters_without_writes(self):
        """#131: reverting drops the cap or rewrites stored verdicts during the value-only comparison."""
        self.assertIn("own_capability or own_record caps at 3", metrics.JUDGE_SYSTEM)
        self.assertIn("unless the same cycle's tool log", metrics.JUDGE_SYSTEM)
        self.assertIn("When the cap applies, value_reason says so", metrics.JUDGE_SYSTEM)
        rows = [self.row(f"2026-10-{day:02d}T10:00:00", value=4, value_for="own_record") for day in (18, 20, 21)]
        rows += [self.row("2026-10-22T10:00:00", value=2, value_for="own_record"),
                 self.row("2026-10-23T10:00:00", value=5, value_for="operator")]
        self.write(rows)
        (self.folder / "m-second.jsonl").write_text("")
        (self.folder / "m-surprise-baseline.jsonl").write_text("old baseline\n")
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        episodes = [{"ts": r["episode_ts"], "intent": {"goal": "output"}, "tools": [],
                     "finish": None} for r in rows]
        answer = {"value": 3, "value_for": "own_record", "value_reason": "cap applies; no delivered use"}
        with mock.patch.object(metrics, "live_episodes", return_value=episodes), \
             mock.patch.object(judge, "ask", return_value=answer) as ask, \
             mock.patch.object(cli, "instance_root", return_value=self.root), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(cli.main(["judge", "value-regrade", "m", "--min-value", "4", "--value-for",
                                       "own_capability,own_record", "--since", "2026-10-20", "--limit", "1"]), 0)
            text = output.getvalue()
        self.assertIn("4 → 3", text)
        self.assertIn("1 fell to <=3 of 1", text)
        self.assertIn("2026-10-21T10:00:00", text)
        self.assertNotIn("2026-10-20T10:00:00", text)
        self.assertEqual(set(ask.call_args.args[2]["required"]), set(metrics.VALUES))
        self.assertEqual(ask.call_args.kwargs["model"], judge.JUDGE_MODELS[0])
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
