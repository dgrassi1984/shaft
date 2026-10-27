"""Synthetic visibility experiments: no instance files or paid judge calls."""
from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from supervisor import anchoring, judge, metrics


def episode(ts="2026-10-25T10:00:00", claim="yes"):
    finish = {"criterion_met": claim, "what_happened": "report detail", "learned": "lesson detail",
              "surprises": ["surprise detail"]}
    return {"ts": ts, "kind": "live", "intent": {"goal": "test", "criterion": "passes",
            "confidence_of": metrics.CONFIDENCE_V3, "confidence_first": 80, "base_rate_shown": {}},
            "finish": finish, "tools": [{"tool": "finish_cycle", "args": json.dumps(finish), "result": "recorded"}]}


def judgment(ep, by="judge-a"):
    return {"episode_ts": ep["ts"], "judge": by, "criterion_met": "yes"}


ANSWER = {"criterion_met": "yes", "criterion_quality": "checkable", "failure_mode": "", "reasoning": "log"}


class AnchoringTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(anchoring, "SUP_DIR", self.root))
        self.stack.enter_context(mock.patch.object(judge, "JUDGE_MODELS", ["judge-a", "judge-b"]))
        self.eps = [episode()]
        self.js = [judgment(self.eps[0])]
        self.stack.enter_context(mock.patch.object(metrics, "judgments", side_effect=lambda name: self.js))
        self.stack.enter_context(mock.patch.object(metrics, "live_episodes", side_effect=lambda name: self.eps))
        self.stack.enter_context(mock.patch.object(metrics, "own_models", return_value=[]))
        for method, value in (("_written_files", "written evidence"), ("_provenance_evidence", "provenance"),
                              ("_followed_surprise", ""), ("_judgment_cost", (.1, "synthetic"))):
            self.stack.enter_context(mock.patch.object(metrics, method, return_value=value))
        self.ask = self.stack.enter_context(mock.patch.object(judge, "ask", return_value=ANSWER))
        self.log = self.stack.enter_context(mock.patch.object(anchoring, "log"))

    def test_blind_hides_each_claim_in_both_places_even_cut_args(self):
        for claim in ("yes", "partial", "no"):
            for ending in ("valid", "cut_after_value", "cut_in_value"):
                with self.subTest(claim=claim, ending=ending):
                    ep = episode(claim=claim)
                    if ending == "cut_after_value":
                        ep["tools"][0]["args"] = '{"criterion_met" : "' + claim + '", "what_happened": "cut'
                    elif ending == "cut_in_value":
                        ep["tools"][0]["args"] = '{"criterion_met": "' + claim
                    before = copy.deepcopy(ep)
                    text = anchoring.case("x", ep, "blind", [])
                    self.assertNotIn('"' + claim + '"', text)
                    self.assertEqual(text.count(anchoring.WITHHELD), 2)
                    self.assertIn("report detail", text)
                    self.assertEqual(ep, before)

    def test_sighted_and_reask_match_live_case_exactly(self):
        ep = self.eps[0]
        expected = metrics._case(ep, "written evidence", "provenance", metrics._prior_goals(self.js, ep["ts"]),
                                 followed="")
        for arm in ("sighted", "reask"):
            self.assertEqual(anchoring.case("x", ep, arm, self.js), expected)
        paragraph = metrics.JUDGE_SYSTEM.split("\n\nThen, separately")[0]
        self.assertEqual(anchoring.ANCHOR_SYSTEM, paragraph.replace(
            "Flag claims that the tool log does not support. ", "") +
            " Parts of the agent's own report may be withheld for this grading: grade from the tool log.")
        self.assertEqual(tuple(anchoring.ANCHOR_SCHEMA["properties"]),
                         ("criterion_met", "criterion_quality", "failure_mode", "reasoning"))

    def test_bare_hides_entire_report_and_keeps_written_evidence(self):
        ep = self.eps[0]
        before = copy.deepcopy(ep)
        metrics._written_files.return_value = "criterion met: yes"
        text = anchoring.case("x", ep, "bare", self.js)
        for field in ("what_happened", "learned", "surprises", "report detail", "lesson detail", "surprise detail"):
            self.assertNotIn(field, text)
        self.assertIn("criterion met: yes", text)
        self.assertEqual(text.count(anchoring.WITHHELD), 2)
        self.assertEqual(ep, before)
        stripped = anchoring.strip_claim(ep, bare=True)
        stripped["intent"]["goal"] = "changed"
        self.assertEqual(ep, before)

    def test_candidates_filter_and_seeded_order(self):
        self.eps = [episode(f"2026-10-25T{i:02}:00:00") for i in range(12)]
        self.js = [judgment(ep) for ep in self.eps]
        self.js[0]["judge"] = "rule"
        self.js[1]["judge"] = "retired"
        self.js[2]["judge"] = "judge-b"
        metrics.own_models.return_value = ["openrouter/judge-b"]
        self.eps[3].pop("finish")
        self.eps[4].pop("intent")
        self.eps[5]["finish"]["criterion_met"] = "unknown"
        self.js.append({"episode_ts": "missing", "judge": "judge-a"})
        expected = self.eps[6:12].copy()
        random.Random("x|anchoring").shuffle(expected)
        self.assertEqual(anchoring.candidates("x"), expected)
        self.assertEqual(anchoring.candidates("x"), expected)

    def test_run_flushes_each_answer_and_resumes_missing_arms(self):
        seen = []

        def ask(system, prompt, schema, *, purpose, model):
            self.assertEqual(model, "judge-a")
            self.assertEqual(system, anchoring.ANCHOR_SYSTEM)
            self.assertEqual(schema, anchoring.ANCHOR_SCHEMA)
            self.assertEqual(len(anchoring.rows("x")), len(seen))
            seen.append(purpose)
            return ANSWER

        self.ask.side_effect = ask
        result = anchoring.run("x", n=1)
        self.assertEqual((result["todo"], result["done"], result["cycles"]), (3, 3, 1))
        self.assertEqual(seen, [f"anchoring x {arm}" for arm in anchoring.ARMS[:3]])
        self.assertEqual(anchoring.run("x")["done"], 0)
        self.assertEqual(anchoring.run("x", bare=True)["done"], 1)
        records = anchoring.rows("x")
        self.assertEqual([r["arm"] for r in records], list(anchoring.ARMS))
        for r in records:
            self.assertEqual((r["judge"], r["claimed"], r["stored"]), ("judge-a", "yes", "yes"))
            self.assertIn("judged_at", r)
            self.assertEqual({k: r[k] for k in ANSWER}, ANSWER)

    def test_error_stops_and_preserves_paid_answers(self):
        self.ask.side_effect = [ANSWER, judge.JudgeError("failed"), ANSWER]
        r = anchoring.run("x")
        self.assertEqual(r["done"], 1)
        # Regression (2026-10-26): with no judge key the verb printed "0 done" and nothing else; the reason was only
        # in the supervisor's log. It is in the result, for the verb to say.
        self.assertIn("failed", r["stopped"])
        self.assertEqual(self.ask.call_count, 2)
        self.assertEqual([r["arm"] for r in anchoring.rows("x")], ["sighted"])
        self.log.assert_called_once()
        self.ask.side_effect = None
        self.assertEqual(anchoring.run("x")["done"], 2)

    def test_invalid_answers_stop_without_writing(self):
        for bad in ({}, {**ANSWER, "criterion_met": "maybe"}, {**ANSWER, "reasoning": None},
                    {k: v for k, v in ANSWER.items() if k != "failure_mode"}):
            self.ask.return_value = bad
            self.assertEqual(anchoring.run("x")["done"], 0)
            self.assertFalse(anchoring.path("x").exists())

    def test_dry_run_does_not_ask_or_write(self):
        result = anchoring.run("x", bare=True, dry_run=True)
        self.assertEqual(result["todo"], 4)
        self.assertEqual(result["estimated_usd"], .4)
        self.assertEqual(result["done"], 0)
        self.ask.assert_not_called()
        self.assertFalse(anchoring.path("x").parent.exists())

    def test_report_hand_computed_pairs(self):
        # Claim ranks 2,1,0,2. Sighted hits have ranks 2,2; reask hits 2,1;
        # blind hits 1,0; bare hits 0,2. AUROCs: 1, .625, 0, .375 (including tied ranks).
        claims = ["yes", "partial", "no", "yes"]
        verdicts = {"sighted": ["yes", "partial", "no", "yes"], "reask": ["yes", "yes", "no", "no"],
                    "blind": ["no", "yes", "yes", "no"], "bare": ["no", "partial", "yes", "yes"]}
        records = [{"episode_ts": f"2026-10-{20+i}T10:00:00", "claimed": claims[i], "stored": "yes",
                    "arm": arm, "criterion_met": values[i]} for arm, values in verdicts.items() for i in range(4)]
        with mock.patch.object(anchoring, "rows", return_value=records):
            text = anchoring.report(["x", "y"])
        for expected in ("Anchoring: x: n=4", "Anchoring: all: n=8", "noise (sighted != reask): 2/4",
                         "shift (sighted != blind): 4/4", "sighted 4/4, blind 0/4; a=4, b=0",
                         "p=0.0625", "shift (sighted != bare): 2/4", "sighted 4/4, bare 2/4",
                         "against sighted: 1.000", "against reask: 0.625", "against blind: 0.000",
                         "against bare: 0.375", "sighted agreement with stored verdict: 2/4"):
            self.assertIn(expected, text)
        # four one-cycle days: too many resamples have one class, so no interval — the value is still shown
        self.assertIn("(no interval: fewer than 5 days", text)

    def test_auroc_ties_and_cluster_bootstrap(self):
        auc = lambda pairs: anchoring._auc([{"score": sc, "hit": h} for sc, h in pairs])
        self.assertEqual(auc([(2, True), (0, False)]), 1)
        self.assertEqual(auc([(0, True), (2, False)]), 0)
        self.assertEqual(auc([(1, True), (1, False)]), .5)
        self.assertEqual(auc([(2, True), (2, False), (0, False)]), .75)
        self.assertIsNone(auc([(2, True)]))
        # Each day has both classes. Resampling whole days always keeps a perfect ranking.
        data = [(day, score, hit) for day in "abcde" for score, hit in ((2, True), (0, False))]
        self.assertEqual(anchoring._ci(data), (1, 1))
        self.assertEqual(anchoring._ci(data), anchoring._ci(data))
        self.assertIsNone(anchoring._ci([("a", 2, True)]))
        # Regression (Grok's review): one day with both classes gave a zero-width interval; now no interval
        self.assertIsNone(anchoring._ci([("a", 2, True), ("a", 0, False)]))

    def test_report_reverse_pairs_and_partial_bare(self):
        records = []
        for i, (sighted, blind) in enumerate((("yes", "no"), ("no", "yes"), ("no", "yes"))):
            for arm, verdict in (("sighted", sighted), ("reask", sighted), ("blind", blind)):
                records.append({"episode_ts": f"2026-10-{20+i}T10:00:00", "claimed": "yes", "stored": "no",
                                "arm": arm, "criterion_met": verdict})
        records.append({**records[0], "arm": "bare", "criterion_met": "no"})
        with mock.patch.object(anchoring, "rows", return_value=records):
            text = anchoring.report(["x"])
        self.assertIn("sighted 1/3, blind 2/3; a=1, b=2, one-sided sign test p=0.8750", text)
        self.assertIn("shift (sighted != bare): 1/1", text)
        self.assertIn("sighted 1/1, bare 0/1; a=1, b=0", text)

    def test_report_incomplete_cycles_and_no_bare(self):
        with mock.patch.object(anchoring, "rows", return_value=[{
                "episode_ts": "t", "arm": "sighted", "criterion_met": "yes"}]):
            self.assertIn("not measured (n=0)", anchoring.report(["x"]))
        self.assertEqual(anchoring.run("x")["done"], 3)
        text = anchoring.report(["x"])
        self.assertIn("n=1", text)
        self.assertIn("against sighted: not measured", text)
        self.assertIn("against bare: not measured (one class only), n=0", text)


if __name__ == "__main__":
    unittest.main()
