"""Calibration feedback at declare time (docs/plans/PLAN-calibration-feedback.md): version-safe scoring, the v3
question, and the paired before/after score. Each test fails if its bug returns."""
from __future__ import annotations

import unittest
import json
import tempfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import judge, metrics, panel, surprise, web_views


def load_top_views():
    """supervisor.top_views, which needs rich (installed in .venv-ui only). Without rich its detail text, which uses
    none of it, is still tested: the file is loaded under another name with stand-ins for rich, so the real
    supervisor.top_views stays unimported and the tests that skip without rich still skip."""
    try:
        from supervisor import top_views
        return top_views
    except ModuleNotFoundError:
        import importlib.util
        import sys
        path = Path(__file__).resolve().parent.parent / "top_views.py"
        spec = importlib.util.spec_from_file_location("supervisor._top_views_without_rich", path)
        module = importlib.util.module_from_spec(spec)
        stubs = {name: mock.MagicMock() for name in ("rich", "rich.console", "rich.table", "rich.text")}
        with mock.patch.dict(sys.modules, {**stubs, spec.name: module}):   # dataclasses finds its module here
            spec.loader.exec_module(module)
        return module


def row(confidence, criterion_met="partial", **extra):
    return {"episode_ts": "2026-10-25T10:00:00", "confidence": confidence, "criterion_met": criterion_met, **extra}


def v3(confidence, first, criterion_met):
    return row(confidence, criterion_met, confidence_of=metrics.CONFIDENCE_V3, confidence_first=first)


class CalibrationFeedbackTest(unittest.TestCase):
    def test_unknown_question_is_excluded_not_v1(self):
        # Bug: any confidence_of other than v2 was pooled into v1, so a future question corrupted the v1 score.
        rows = [row(50, confidence_of="something-new"), row(50)]
        cal = metrics.calibration(rows)
        self.assertEqual(cal["v1"]["n"], 1)
        self.assertEqual(cal["excluded"], 1)
        self.assertEqual((cal["v2"]["n"], cal["v3"]["n"], cal["v3_first"]["n"]), (0, 0, 0))

    def test_v3_scored_binary_on_final_and_first_separately(self):
        cal = metrics.calibration([v3(60, 90, "partial")])
        self.assertAlmostEqual(cal["v3"]["brier"], 0.36)           # (0.6 - 0)^2: partial is not met
        self.assertAlmostEqual(cal["v3_first"]["brier"], 0.81)     # (0.9 - 0)^2
        self.assertEqual(cal["v3"]["n"], 1)
        self.assertEqual(cal["v2"]["n"], 0)                        # v3 never pools into v2
        self.assertEqual(cal["excluded"], 0)

    def test_v3_first_needs_confidence_first(self):
        cal = metrics.calibration([row(60, confidence_of=metrics.CONFIDENCE_V3)])
        self.assertEqual((cal["v3"]["n"], cal["v3_first"]["n"]), (1, 0))

    def test_paired_v3_counts_closer_and_further(self):
        paired = metrics.paired_v3([v3(60, 90, "partial"), v3(80, 50, "yes"), v3(70, 70, "no"),
                                    row(50), row(50, confidence_of=metrics.CONFIDENCE_V2)])
        self.assertEqual((paired["n"], paired["closer"], paired["further"], paired["changed"]), (3, 2, 0, 2))
        self.assertAlmostEqual(paired["mean_change"], 0.0)         # (-30 + 30 + 0) / 3
        self.assertAlmostEqual(paired["brier_first"], (0.81 + 0.25 + 0.49) / 3)
        self.assertAlmostEqual(paired["brier_final"], (0.36 + 0.04 + 0.49) / 3)

    def test_paired_v3_counts_further(self):
        paired = metrics.paired_v3([v3(90, 60, "no")])
        self.assertEqual((paired["closer"], paired["further"], paired["changed"]), (0, 1, 1))
        self.assertAlmostEqual(paired["mean_change"], 30.0)

    def test_paired_v3_empty(self):
        paired = metrics.paired_v3([row(50)])
        self.assertEqual(paired["n"], 0)
        self.assertIsNone(paired["brier_first"])
        self.assertIsNone(paired["brier_final"])
        self.assertIsNone(paired["mean_change"])

    def test_bands_constant_drives_calibration_bands(self):
        bands = metrics.calibration([row(50)])["v1"]["bands"]
        self.assertEqual([(b["lo"], b["hi"]) for b in bands],
                         [(lo, min(hi, 100)) for lo, hi in metrics.BANDS])

    def test_review_survives_v3_and_excluded(self):
        # Bug guard: review iterated calibration().items() and indexed c["n"]; "excluded" is an int, v3 a new key.
        rows = [{**r, "episode_ts": "2026-10-24T10:00:00", "judged_at": "2026-10-24T10:00:00"}
                for r in (row(50), v3(60, 90, "partial"), row(50, confidence_of="something-new"))]
        with mock.patch.object(metrics, "judgments", return_value=rows):
            text = metrics.review_text(["x"], now=datetime(2026, 10, 24, 11))
        self.assertIn("confidence v1 (probability of success, yes=1/partial=0.5) beforehand 50% vs judged 50%", text)


def judged(confidence, criterion_met, confidence_of=None, by=None, **extra):
    """A verdict row by a judge in today's rotation (by default), with the question it was asked under."""
    return {"episode_ts": "2026-10-25T10:00:00", "judged_at": "2026-10-25T10:05:00", "goal": "g",
            "confidence": confidence, "confidence_of": confidence_of, "criterion_met": criterion_met,
            "claimed": criterion_met, "claim_supported": True, "criterion_quality": "checkable", "reasoning": "r",
            "judge": by or judge.JUDGE_MODELS[0], **extra}


VERDICT = {"criterion_met": "partial", "claim_supported": True, "criterion_quality": "checkable",
           "failure_mode": "", "reasoning": "r", "difficulty": 2, "difficulty_reason": "r", "novelty": 2,
           "origin": "self", "origin_supported": True, "origin_evidence": "r", "value": 2,
           "value_for": "own_capability", "value_reason": "r"}
SHOWN = {"lo": 70, "hi": 100, "met": 3, "n": 4}


class DeclareStepTest(unittest.TestCase):
    """Base rates for the declare step (self/calibration.json) and v3 in every reader. Each test fails if its bug returns."""

    def setUp(self):
        metrics._QUESTIONS.clear()
        self.addCleanup(metrics._QUESTIONS.clear)

    def stats(self, rows):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(metrics, "judgments", return_value=rows))
            for name in ("_sample_note", "_battery_section", "_code_section", "_traits_section", "_trials_section"):
                stack.enter_context(mock.patch.object(metrics, name, return_value=[]))
            return metrics.stats_text("x")

    def test_base_rates_current_judges_only_and_questions_apart(self):
        v2, v3q = metrics.CONFIDENCE_V2, metrics.CONFIDENCE_V3
        rows = [judged(30, "yes", v2), judged(35, "partial", v3q), judged(50, "yes", v3q),
                judged(95, "yes", v3q), judged(100, "no", v2),
                judged(30, "yes"), judged(30, "no"), judged(75, "yes"),     # v1: asked another question
                judged(30, "yes", v2, by="retired-model"),                  # a judge no longer in the rotation
                judged(None, "yes", v2),                                    # no confidence declared
                judged(30, "yes", "something-new")]                         # a question this supervisor does not know
        got = metrics.base_rates(rows)
        self.assertEqual(got["question"], "criterion_met=yes")
        self.assertEqual([(b["lo"], b["hi"]) for b in got["bands"]], [(0, 40), (40, 70), (70, 100)])
        self.assertEqual([(b["met"], b["n"]) for b in got["bands"]], [(1, 2), (1, 1), (1, 2)])
        self.assertEqual([(b["older_met"], b["older_n"]) for b in got["bands"]], [(1, 2), (0, 0), (1, 1)])
        self.assertEqual(datetime.fromisoformat(got["written"]).microsecond, 0)

    def test_write_stats_writes_calibration_json_beside_stats(self):
        rows = [judged(30, "yes", metrics.CONFIDENCE_V2), judged(80, "no")]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(metrics, "INSTANCES", Path(tmp)):
            (Path(tmp) / "x" / "shaft").mkdir(parents=True)
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(metrics, "judgments", return_value=rows))
                for name in ("_sample_note", "_battery_section", "_code_section", "_traits_section", "_trials_section"):
                    stack.enter_context(mock.patch.object(metrics, name, return_value=[]))
                metrics.write_stats("x")
            self.assertTrue((Path(tmp) / "x" / "self" / "stats.md").is_file())
            data = json.loads((Path(tmp) / "x" / metrics.CALIBRATION_FILE).read_text())
            self.assertEqual(metrics.CALIBRATION_FILE, "self/calibration.json")
            self.assertEqual(len(data["bands"]), 3)
            self.assertEqual((data["bands"][0]["met"], data["bands"][0]["n"]), (1, 1))
            self.assertEqual((data["bands"][2]["older_met"], data["bands"][2]["older_n"]), (0, 1))

    def test_write_stats_writes_nothing_for_a_deleted_instance(self):
        # Bug guard: the calibration file was written without the deleted-instance check stats.md has, so
        # `shaft delete` left a half folder holding only self/calibration.json.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(metrics, "INSTANCES", Path(tmp)), \
                mock.patch.object(metrics, "judgments", return_value=[]):
            metrics.write_stats("gone")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_stats_text_shows_v3_and_paired_line(self):
        text = self.stats([judged(60, "partial", metrics.CONFIDENCE_V3, confidence_first=90)])
        self.assertIn("Calibration v3 — P(criterion met = yes) after seeing my base rate, Brier on met/not met: "
                      "Brier 0.36 (n=1; 0 = perfect, 0.25 = coin flip)", text)
        self.assertIn("- Base rate shown when I declared (v3): Brier before I saw it 0.81, after 0.36 on the same "
                      "1 cycles; I changed my number in 1 of 1 (mean change -30 points; closer to the outcome 1, "
                      "further 0)", text)

    def test_stats_text_has_no_paired_line_without_first_confidences(self):
        # A v3 row that lost confidence_first scores as v3 but pairs with nothing: no line of made-up zeros.
        text = self.stats([judged(60, "partial", metrics.CONFIDENCE_V3)])
        self.assertIn("Calibration v3", text)
        self.assertNotIn("Base rate shown when I declared", text)

    def test_stats_text_shows_the_excluded_count_only_above_zero(self):
        line = "- Confidences left out of calibration: 2 (asked under a question this supervisor does not know)"
        text = self.stats([judged(50, "yes", "something-new"), judged(50, "no", "another"), judged(50, "yes")])
        self.assertIn(line, text)
        self.assertLess(text.index("## Success against my own criteria (raw)"), text.index(line))
        self.assertNotIn("left out of calibration", self.stats([judged(50, "yes")]))

    def test_review_labels_v3_as_criterion_met(self):
        # Bug guard: the label was picked by `== "v2"`, so v3 (and any later version) read "probability of success".
        rows = [judged(60, "partial", metrics.CONFIDENCE_V3, confidence_first=90, episode_ts="2026-10-24T10:00:00")]
        with mock.patch.object(metrics, "judgments", return_value=rows):
            text = metrics.review_text(["x"], now=datetime(2026, 10, 24, 11))
        line = next(l for l in text.splitlines() if "confidence v3" in l)
        self.assertIn("criterion met = yes", line)
        self.assertNotIn("probability of success", line)
        self.assertIn("beforehand 60% vs judged 0% (n=1, Brier 0.36", line)
        self.assertIn("Brier before the base rate 0.81, after 0.36 on the same 1 cycles", text)

    def test_judgment_row_copies_first_and_shown(self):
        # Bug guard: the judgment row kept confidence but not confidence_first, so v3_first and the paired
        # score had nothing to read.
        ep = {"ts": "2026-10-25T10:00:00", "tools": [], "intent": {
            "goal": "g", "confidence": 60, "confidence_first": 90, "base_rate_shown": SHOWN,
            "confidence_of": metrics.CONFIDENCE_V3}}
        with tempfile.TemporaryDirectory(prefix="shaft-confidence-") as tmp, ExitStack() as stack:
            path = Path(tmp) / "judgments.jsonl"
            for name, value in (("judgments", []), ("unsampled", set()), ("live_episodes", [ep]),
                                ("judge_sampled", True), ("second_sampled", False), ("_written_files", ""),
                                ("_provenance_evidence", ""), ("own_models", []), ("cycle_model", "m"),
                                ("_judgments_path", path)):
                stack.enter_context(mock.patch.object(metrics, name, return_value=value))
            stack.enter_context(mock.patch.object(judge, "available", return_value=True))
            stack.enter_context(mock.patch.object(judge, "pick", return_value=judge.JUDGE_MODELS[0]))
            stack.enter_context(mock.patch.object(judge, "ask", return_value=dict(VERDICT)))
            self.assertEqual(metrics.judge_pending("x"), 1)
            row = json.loads(path.read_text())
        self.assertEqual((row["confidence"], row["confidence_first"]), (60, 90))
        self.assertEqual(row["base_rate_shown"], SHOWN)
        self.assertEqual(row["confidence_of"], metrics.CONFIDENCE_V3)

    def test_backfill_copies_first_from_episode(self):
        # Bug guard: the backfill copied confidence_of only, so a v3 row judged before the supervisor learned
        # the field scored as v3 final but never paired.
        row = {"episode_ts": "2026-10-25T10:00:00", "judged_at": "2026-10-25T10:05:00", "goal": "g",
               "confidence": 60, "criterion_met": "partial"}
        ep = {"ts": row["episode_ts"], "intent": {"confidence_of": metrics.CONFIDENCE_V3, "confidence": 60,
                                                  "confidence_first": 90, "base_rate_shown": SHOWN}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "judgments").mkdir()
            path = root / "judgments" / "x.jsonl"
            path.write_text(json.dumps(row) + "\n")
            with mock.patch.object(metrics, "SUP_DIR", root), mock.patch.object(metrics, "live_episodes",
                                                                                 return_value=[ep]):
                got = metrics.judgments("x", root)[0]
                other = metrics.judgments("x", Path(tmp) / "elsewhere")
            self.assertEqual((got["confidence_of"], got["confidence_first"]), (metrics.CONFIDENCE_V3, 90))
            self.assertEqual(got["base_rate_shown"], SHOWN)
            self.assertEqual(metrics.paired_v3([got])["n"], 1)
            self.assertEqual(other, [])
            self.assertNotIn("confidence_first", json.loads(path.read_text()))   # the file itself is never rewritten

    def test_scoring_never_reads_calibration_json(self):
        # The instance can edit its own self/calibration.json: nothing that scores may read it back.
        rows = [judged(60, "partial", metrics.CONFIDENCE_V3, confidence_first=90), judged(50, "yes")]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(metrics, "INSTANCES", Path(tmp)):
            (Path(tmp) / "x" / "self").mkdir(parents=True)
            before = (self.stats(rows), metrics.calibration(rows), metrics.paired_v3(rows))
            (Path(tmp) / "x" / metrics.CALIBRATION_FILE).write_text("{{ not json \x00 garbage")
            after = (self.stats(rows), metrics.calibration(rows), metrics.paired_v3(rows))
        strip = lambda text: "\n".join(l for l in text.splitlines() if "Updated" not in l)
        self.assertEqual((strip(before[0]), before[1:]), (strip(after[0]), after[1:]))

    def test_surprise_reports_v3_beside_v2(self):
        rows = [judged(60, "partial", metrics.CONFIDENCE_V3, confidence_first=90), judged(50, "yes", metrics.CONFIDENCE_V2)]
        got = surprise._judged(rows)
        self.assertEqual((got["brier_v3"], got["n_v3"]), (0.36, 1))
        self.assertEqual((got["brier_v2"], got["n_v2"]), (0.25, 1))
        self.assertIsNone(surprise._judged(rows[1:])["brier_v3"])
        self.assertEqual(surprise._judged(rows[1:])["n_v3"], 0)
        b = {"instance": "x", "window": [None, None], "live_cycles": 0, "current_model": "m", "events": [],
             "tool_calls": {"live": 0, "sleep": 0, "talk": 0}, "flagged": {"live": 0, "sleep": 0, "talk": 0},
             "recurrence": {"classes": 0, "recurred": 0, "repeat_episodes": 0, "recurring": {}},
             "flagged_by_class": {}, "judged_all_models": got,
             "by_model": {"m": {"cycles": 1, "first": "a", "last": "b", "steps_per_cycle_median": 1,
                                "turn_seconds_median": 1, "turn_prompt_tokens_median": 1,
                                "turn_completion_tokens_median": 1, "cycle_seconds_median": 1,
                                "cycle_completion_tokens_median": 1, "turns": 1, "judged": got}}}
        text = surprise.report(b)
        self.assertIn("| Brier v3 (n) |", text)
        self.assertIn("| 0.36 (1) |", text)
        self.assertIn("Brier v3: P(criterion met = yes) after the base rate was shown", text)


class ConfidenceShownTest(unittest.TestCase):
    """Per-cycle confidence reads `90 → 60` where the base rate moved it, and the plain number otherwise."""
    MOVED = {"episode_ts": "2026-10-25T10:00:00", "confidence": 60, "confidence_first": 90, "goal": "g"}
    SAME = {**MOVED, "confidence": 90}
    OLD = {"episode_ts": "2026-10-25T10:00:00", "confidence": 60, "goal": "g"}

    def test_helper(self):
        self.assertEqual(metrics.confidence_shown(self.MOVED), "90 → 60")
        self.assertEqual(metrics.confidence_shown(self.SAME), 90)
        self.assertEqual(metrics.confidence_shown(self.OLD), 60)
        self.assertIsNone(metrics.confidence_shown({"goal": "g"}))

    def test_web_verdict_page(self):
        render = lambda j: str(web_views.judge_detail_page("x", j, ["x"], "csrf"))
        self.assertIn("90 → 60", render(self.MOVED))
        self.assertNotIn("→ 90", render(self.SAME))
        self.assertNotIn("90 →", render(self.SAME))
        self.assertIn(">90<", render(self.SAME))
        self.assertIn(">60<", render(self.OLD))

    def test_top_verdict_detail(self):
        rows = lambda j: [l for l in load_top_views().judge_detail(j, 80) if l.startswith("confidence")]
        self.assertEqual([l.split(None, 1)[1] for l in rows(self.MOVED)], ["90 → 60"])
        self.assertEqual([l.split(None, 1)[1] for l in rows(self.SAME)], ["90"])
        self.assertEqual([l.split(None, 1)[1] for l in rows(self.OLD)], ["60"])

    def test_cycle_transcript(self):
        def confidence(intent):
            e = {"kind": "live", "ts": "2026-10-25T10:00:00", "where": "w",
                 "record": {"intent": {"goal": "g", **intent}, "tools": []}}
            lines = [t for t, _ in panel.transcript_lines("x", e)]
            return lines[lines.index("Confidence") + 1]
        self.assertEqual(confidence({"confidence": 60, "confidence_first": 90}), "90 → 60")
        self.assertEqual(confidence({"confidence": 90, "confidence_first": 90}), "90")
        self.assertEqual(confidence({"confidence": 60}), "60")


if __name__ == "__main__":
    unittest.main()
