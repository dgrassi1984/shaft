"""Located errors (#98) and confidence arms (#137), measured against recorded outcomes."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import broker, judge, metacog, metrics
from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from supervisor.tests.test_calibration_feedback_template import WITH_DATA
from supervisor.tests.test_m2_value import VERDICT
from shaft import agent, base_rate, config, live


CLAUSES = [{"clause": "output exists", "confidence": 80}, {"clause": "output is correct", "confidence": 50}]


class ControlsTest(InstanceFixture, unittest.TestCase):
    def cycle(self, exercise):
        def run(client, messages, toolbox, *, controls, **kwargs):
            self.prompt = messages[1]["content"]
            exercise(controls)
            return agent.Outcome(None, "control", 2)
        with mock.patch.object(agent, "run", side_effect=run):
            live.run(self.cfg, self.state, mock.Mock())
        return self.state.last_episode()

    def test_doubts_stored_without_changing_claim(self):
        """#98: doubts accompany a yes claim without moving it."""
        doubts = [{"step": 2, "what": "the result may be stale"}]
        def exercise(controls):
            control = controls["finish_cycle"]
            schema = control.spec["function"]["parameters"]
            self.assertIn("doubts", schema["properties"])
            self.assertNotIn("doubts", schema["required"])
            self.assertEqual(control.handler({"criterion_met": "yes", "doubts": doubts}), "cycle finished")
        finish = self.cycle(exercise)["finish"]
        self.assertEqual((finish["criterion_met"], finish["doubts"]), ("yes", doubts))

    def test_clauses_validated_recorded_and_not_combined_by_template(self):
        """#137: arm A records 1-4 bounded probabilities with a null combined confidence."""
        self.cfg.CONFIDENCE_MODE = "clauses"
        def exercise(controls):
            self.assertNotIn("confirm_confidence", controls)
            control = controls["declare_intent"]
            schema = control.spec["function"]["parameters"]
            self.assertIn("clauses", schema["required"])
            self.assertNotIn("confidence", schema["properties"])
            args = {"goal": "output", "success_criterion": "exists and correct", "origin": "self"}
            bad = [[], CLAUSES * 3, [{"clause": "bad", "confidence": -1}],
                   [{"clause": "bad", "confidence": 101}], [{"clause": "bad", "confidence": True}],
                   [{"clause": "bad", "confidence": "80"}], [{"clause": "", "confidence": 80}]]
            for clauses in bad:
                self.assertTrue(control.handler({**args, "clauses": clauses}).startswith("ERROR"))
            self.assertTrue(control.handler({**args, "clauses": CLAUSES}).startswith("intent recorded"))
        ep = self.cycle(exercise)
        self.assertEqual(ep["intent"]["clauses"], CLAUSES)
        self.assertIsNone(ep["intent"]["confidence"])
        self.assertEqual(ep["intent"]["confidence_of"], metrics.CONFIDENCE_V4)
        self.assertEqual(live.CONFIDENCE_OF_CLAUSES, metrics.CONFIDENCE_V4)
        self.assertIn("Confidence mode: clauses", self.prompt)
        self.assertIn("Do not combine", self.prompt)
        self.assertNotIn("base_rate_shown", ep["intent"])

    def test_noconfirm_hides_rate_and_confirm_keeps_existing_flow(self):
        """#137: arm B hides a populated rate; the default arm still offers it."""
        (self.root / base_rate.FILE).write_text(json.dumps(WITH_DATA))
        for mode in ("noconfirm", "confirm"):
            self.cfg.CONFIDENCE_MODE = mode
            def exercise(controls):
                self.assertEqual("confirm_confidence" in controls, mode == "confirm")
                result = controls["declare_intent"].handler({"goal": "output", "success_criterion": "correct",
                                                            "confidence": 85, "origin": "self"})
                if mode == "confirm":
                    self.assertIn("63 of 100", result)
                else:
                    self.assertEqual(result, "intent recorded — now act")
            self.cfg.FORECAST = "off"
            ep = self.cycle(exercise)
            self.assertEqual("base_rate_shown" in ep["intent"], mode == "confirm")
            self.assertEqual(ep["intent"]["confidence"], 85)
            self.assertIn(f"Confidence mode: {mode}", self.prompt)
        self.assertEqual(config.FIELDS["CONFIDENCE_MODE"], (str, "confirm"))
        self.assertEqual(config.CHOICES["CONFIDENCE_MODE"], ("confirm", "clauses", "noconfirm"))

    def test_four_clauses_accept_zero_and_one_hundred(self):
        """#137: the four-clause limit and both probability endpoints are inclusive."""
        self.cfg.CONFIDENCE_MODE = "clauses"
        clauses = [{"clause": str(i), "confidence": c} for i, c in enumerate((0, 100, 50, 80))]
        def exercise(controls):
            result = controls["declare_intent"].handler({"goal": "output", "success_criterion": "correct",
                                                        "origin": "self", "clauses": clauses})
            self.assertTrue(result.startswith("intent recorded"), result)
        self.assertEqual(self.cycle(exercise)["intent"]["clauses"], clauses)


class JudgmentTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-x-metacog-")))
        self.enterContext(mock.patch.object(metrics, "SUP_DIR", self.root))
        self.enterContext(mock.patch.object(metrics, "log"))

    def test_fresh_fields_required_old_rows_load_and_record_exposes_error(self):
        """#98: fresh verdicts require both error fields; historical rows need no backfill."""
        metrics._check_ratings(VERDICT)
        for key in metrics.ERRORS:
            with self.subTest(key=key), self.assertRaises(judge.JudgeError):
                metrics._check_ratings({k: v for k, v in VERDICT.items() if k != key})
        metrics._check_ratings({**VERDICT, "first_error_step": 0})   # 0 = no step at fault
        for value in (True, "2", None, -1, 2.5):
            with self.subTest(value=value), self.assertRaises(judge.JudgeError):
                metrics._check_ratings({**VERDICT, "first_error_step": value})
        with self.assertRaises(judge.JudgeError):
            metrics._check_ratings({**VERDICT, "criterion_met": "yes", "first_error_step": 2})
        for schema in (metrics.JUDGE_SCHEMA, metrics.CONTROL_SCHEMA):
            self.assertTrue(set(metrics.ERRORS) <= set(schema["required"]))
        self.assertIn("First error", metrics.CONTROL_SYSTEM)
        self.assertEqual(set(metrics.CONTROL_SCHEMA["properties"]) | set(metrics.SURPRISES),
                         set(metrics.JUDGE_SCHEMA["properties"]))
        old = {k: v for k, v in VERDICT.items() if k not in metrics.ERRORS}
        old.update(episode_ts="2026-10-20T10:00:00", judged_at="2026-10-20T10:00:00")
        (self.root / "judgments").mkdir()
        (self.root / "judgments/m.jsonl").write_text(json.dumps(old) + "\n")
        self.assertEqual(metrics.judgments("m"), [old])
        self.assertEqual(metrics._missing(old), [])
        text = broker.judgment_text({**old, "first_error_step": 2, "first_error": "Used stale output."})
        self.assertIn("first_error_step: 2; first_error: Used stale output.", text)
        b = object.__new__(broker.Broker)
        b.name, b.as_of = "m", None
        fresh = {**old, "first_error_step": 2, "first_error": "Used stale output."}
        with mock.patch.object(metrics, "judgments", return_value=[fresh]), \
             mock.patch.object(metrics, "unsampled_rows", return_value=[]):
            page = json.loads(b._record("judgments", since="1970-01-01 00:00:00", format="json"))
        self.assertEqual(page["verdicts"][0]["first_error_step"], 2)
        self.assertEqual(page["verdicts"][0]["first_error"], "Used stale output.")

    def test_supervisor_combines_clause_product_in_persisted_row(self):
        """#137: the judgment writer computes 80% * 50% = 40%, ignoring an instance's aggregate."""
        ep = {"ts": "2026-10-28T10:00:00", "intent": {"goal": "output", "confidence": 99,
              "confidence_of": metrics.CONFIDENCE_V4, "clauses": CLAUSES}, "finish": {"criterion_met": "yes"}}
        for name, value in (("judgments", []), ("unsampled", set()), ("live_episodes", [ep]),
                            ("judge_sampled", True), ("second_sampled", False), ("_written_files", ""),
                            ("_provenance_evidence", ""), ("_prior_goals", ""), ("_followed_surprise", ""),
                            ("own_models", []), ("cycle_model", "m")):
            self.enterContext(mock.patch.object(metrics, name, return_value=value))
        self.enterContext(mock.patch.object(judge, "available", return_value=True))
        self.enterContext(mock.patch.object(judge, "pick", return_value=judge.JUDGE_MODELS[0]))
        self.enterContext(mock.patch.object(judge, "ask", return_value=dict(VERDICT)))
        self.assertEqual(metrics.judge_pending("m"), 1)
        rec = json.loads((self.root / "judgments/m.jsonl").read_text())
        self.assertEqual(rec["confidence"], 40)
        self.assertEqual(rec["clauses"], CLAUSES)
        self.assertEqual(rec["confidence_of"], metrics.CONFIDENCE_V4)
        self.assertEqual(ep["intent"]["confidence"], 99)

    def test_first_error_uses_original_log_indices_after_blinding(self):
        """#98: removing confirmation calls must not renumber an error or its matching doubt."""
        ep = {"intent": {"base_rate_shown": {}, "confidence_of": metrics.CONFIDENCE_V3}, "tools": [
            {"tool": "declare_intent", "result": "intent recorded"},
            {"tool": "confirm_confidence", "result": "recorded"},
            {"tool": "run", "result": "failed"}]}
        case = metrics._case(ep)
        self.assertIn("### 3. run(", case)
        self.assertNotIn("confirm_confidence", case)


class MeasurementsTest(unittest.TestCase):
    def test_matrix_and_doubt_alignment_counts(self):
        """#98: count the three claim labels and only cycles with both doubts and a located error."""
        rows = [{"episode_ts": str(i), "claimed": claim, "criterion_met": verdict, "first_error_step": error}
                for i, (claim, verdict, error) in enumerate([
                    ("yes", "no", 3), ("partial", "no", 2), ("no", "yes", None),
                    ("yes", "partial", 1), (None, "no", 2)])]
        eps = {str(i): {"finish": {"doubts": doubts}} for i, doubts in enumerate([
            [{"step": 3}, {"step": 5}], [{"step": 1}], [{"step": 1}], [{"step": 2}], []])}
        matrix = metacog.claim_matrix(rows)
        self.assertEqual(matrix, {"source": "all judged cycles", "n": 4, "excluded": 1, "counts": {
            "yes": {"yes": 0, "partial": 1, "no": 1}, "partial": {"yes": 0, "partial": 0, "no": 1},
            "no": {"yes": 1, "partial": 0, "no": 0}}})
        self.assertEqual(metacog.doubt_alignment(rows, eps), {"matched": 2, "n": 3, "rate": 2 / 3})
        self.assertEqual(metacog.doubt_alignment([], {}), {"matched": 0, "n": 0, "rate": None})

    def test_v4_calibration_base_rates_and_auroc_stay_apart(self):
        """#137: v4 probabilities and outcomes never enter v1-v3 scores or confirm base rates."""
        rows = [{"episode_ts": f"2026-10-28T10:0{i}:00", "judge": judge.JUDGE_MODELS[0],
                 "confidence": confidence, "criterion_met": verdict, "confidence_of": question}
                for i, (confidence, verdict, question) in enumerate([
                    (90, "yes", metrics.CONFIDENCE_V4), (10, "no", metrics.CONFIDENCE_V4),
                    (10, "yes", metrics.CONFIDENCE_V2), (90, "no", metrics.CONFIDENCE_V3),
                    (50, "partial", None)])]
        cal = metrics.calibration(rows)
        self.assertEqual([cal[v]["n"] for v in ("v1", "v2", "v3", "v4")], [1, 1, 1, 2])
        self.assertAlmostEqual(cal["v4"]["brier"], 0.01)
        rates = metrics.base_rates(rows)
        self.assertEqual([(b["met"], b["n"]) for b in rates["bands"]], [(1, 1), (0, 0), (0, 1)])
        self.assertEqual([(b["met"], b["n"]) for b in rates["clauses"]["bands"]], [(0, 1), (0, 0), (1, 1)])
        self.assertEqual(rates["clauses"]["question"], metrics.CONFIDENCE_V4)
        self.assertEqual(metacog.summary(rows)["auroc"], 0)
        self.assertEqual(metacog.summary(rows, versions=("v4",))["auroc"], 1)
        with mock.patch.object(metrics, "judgments", return_value=rows), \
             mock.patch.object(metrics, "live_episodes", return_value=[]):
            text = metacog.report(["m"])
        self.assertIn("v4 (clauses, apart): AUROC 1.00", text)
        self.assertIn("all judged cycles; n 0, excluded 5", text)
        self.assertIn("Doubted step equals or precedes first error: 0/0", text)
