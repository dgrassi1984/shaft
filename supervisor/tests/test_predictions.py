"""Guard prediction timestamps, replay isolation and scoring from independently recorded tool evidence."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import core, judge, predictions

NOW = datetime(2026, 1, 2, 12)
DUE = NOW + timedelta(hours=2)
CITATION = "2026-01-02T15:00:00#1"


class PredictionsTest(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-predictions-"))
        self.root = Path(tmp)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.root))
        self.log = self.enterContext(mock.patch.object(core, "log"))
        self.available = self.enterContext(mock.patch.object(judge, "available", return_value=True))
        self.pick = self.enterContext(mock.patch.object(judge, "pick", return_value="openrouter/example/judge"))
        self.ask = self.enterContext(mock.patch.object(judge, "ask", return_value={"result": "held", "reason": "42"}))
        self.own = self.enterContext(mock.patch.object(predictions.metrics, "own_models", return_value=["example/own"]))
        self.episodes = self.enterContext(mock.patch.object(predictions.commitments, "_episodes", return_value=[{
            "ts": "2026-01-02T15:00:00", "tools": [
                {"tool": "fetch_page", "args": {"url": "https://example.com"}, "result": "Reading: 42"}]}]))

    def add(self, **kwargs):
        return predictions.add("ember", **{"text": "The reading will be 42", "due": DUE.isoformat(),
                                            "how_to_check": "Read the source", "now": NOW, **kwargs})

    def report(self, **kwargs):
        return predictions.report("ember", **{"id": "p1", "outcome": "failed", "evidence": CITATION,
                                               "now": DUE + timedelta(hours=1), **kwargs})

    def rows(self):
        return [json.loads(line) for line in core.jsonl_lines((self.root / "predictions/ember.jsonl").read_text())]

    def test_supervisor_clock_and_due_validation(self):
        """Creation uses the supervisor clock; due is a deadline, never a caller's creation timestamp."""
        with mock.patch.object(predictions, "datetime") as clock:
            clock.now.return_value = NOW
            event = self.add(now=None, due="2026-01-02 14:00")
        self.assertEqual(event["at"], NOW.isoformat())
        self.assertEqual(event["due"], DUE.isoformat())
        self.assertEqual(self.rows(), [event])
        with self.assertRaises(TypeError):
            self.add(at="2020-01-01T00:00:00")
        for due in (NOW + timedelta(minutes=59), NOW + timedelta(days=90, seconds=1)):
            with self.subTest(due=due), self.assertRaisesRegex(ValueError, "1 hour and 90 days"):
                self.add(due=due.isoformat())
        for due in ("tomorrow", "2026-01-02", "2026-02-30 14:00", "20260102T140000", None):
            with self.subTest(due=due), self.assertRaisesRegex(ValueError, "ISO time YYYY-MM-DD HH:MM"):
                self.add(due=due)
        for due in (NOW + predictions.MIN_AHEAD, NOW + predictions.MAX_AHEAD):
            self.assertEqual(self.add(due=due.isoformat())["due"], due.isoformat())

    def test_fields_reject_invalid_values_without_appending(self):
        """Refuse multiline, overlong and incorrectly typed fields rather than silently truncating them."""
        for field, value in (("text", ""), ("text", "x\ny"), ("text", "x" * 501),
                             ("how_to_check", " "), ("how_to_check", "x" * 501),
                             ("subject", "x" * 61), ("subject", "x\ry"), ("subject", None),
                             ("confidence", True), ("confidence", 1.5), ("confidence", -1), ("confidence", 101)):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "pass"):
                self.add(**{field: value})
        self.assertEqual(self.rows(), [])
        for confidence in (None, 0, 100):
            self.assertEqual(self.add(confidence=confidence)["confidence"], confidence)

    def test_cap_counts_unscored_and_ids_are_never_reused(self):
        """Reported predictions occupy a slot until scored; scoring frees a slot without reusing its id."""
        for _ in range(predictions.OPEN_MAX):
            self.add()
        self.report()
        with self.assertRaisesRegex(ValueError, "20 unscored"):
            self.add()
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 1)
        self.assertEqual(self.add()["id"], "p21")

    def test_report_requires_known_unreported_due_prediction_and_step(self):
        """Reports cannot be submitted early, replaced, or detached from a specific tool-log step."""
        self.add()
        for kwargs, message in (({"now": DUE - timedelta(seconds=1)}, "not due until.*checked at its due time"),
                                ({"id": "p9"}, "unknown prediction"), ({"outcome": "unclear"}, "pass outcome"),
                                ({"evidence": "2026-01-02 15:00"}, "#<step>"),
                                ({"evidence": "2026-01-02 15:00#0"}, "#<step>"),
                                ({"evidence": None}, "#<step>")):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, message):
                self.report(**kwargs)
        event = self.report(now=DUE)
        self.assertEqual(event["at"], DUE.isoformat())
        with self.assertRaisesRegex(ValueError, "already reported"):
            self.report()

    def test_concurrent_adds_reports_and_scores_are_serialized(self):
        """Concurrent writers cannot reuse ids, exceed the cap, double-report or append duplicate scores."""
        def attempt(function):
            try:
                return function()
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: attempt(self.add), range(25)))
            accepted = [event for event in results if event is not None]
            self.assertEqual(len(accepted), 20)
            self.assertEqual({event["id"] for event in accepted}, {f"p{i}" for i in range(1, 21)})
            reports = list(pool.map(lambda _: attempt(self.report), range(8)))
            self.assertEqual(sum(event is not None for event in reports), 1)
            scores = list(pool.map(lambda _: predictions.score("ember", DUE + timedelta(hours=2)), range(8)))
            self.assertEqual(sum(scores), 1)
        self.assertEqual(sum(r.get("event") == "scored" for r in self.rows()), 1)

    def test_f1_judge_wait_does_not_lock_writers_and_scores_once(self):
        """F1: blocked judges leave predict writes free and overlapping scorers append one verdict."""
        self.add()
        self.report()
        entered = threading.Barrier(3)
        release = threading.Event()

        def ask(*args, **kwargs):
            entered.wait(timeout=3)
            if not release.wait(3):
                raise AssertionError("judge was not released")
            return {"result": "held", "reason": "42"}

        self.ask.side_effect = ask
        with ThreadPoolExecutor(max_workers=3) as pool:
            scores = [pool.submit(predictions.score, "ember", DUE + timedelta(hours=2)) for _ in range(2)]
            try:
                entered.wait(timeout=3)
                made = pool.submit(predictions.add_built, "ember", lambda every, now: predictions.made(
                    "ember", every, "Another reading will hold", DUE.isoformat(), "Read source", now=NOW))
                self.assertEqual(made.result(timeout=1)["id"], "p2")
            finally:
                release.set()
            self.assertEqual(sum(f.result(timeout=3) for f in scores), 1)
        self.assertEqual(sum(r.get("event") == "scored" for r in self.rows()), 1)

    def test_f2_only_external_supervisor_reads_can_be_evidence(self):
        """F2: an authored run result cannot establish a prediction; check rules are judge data."""
        from supervisor import broker
        self.assertEqual(set(predictions.EVIDENCE_TOOLS) - broker.SPECS.keys(), set())
        self.add()
        self.report()
        self.episodes.return_value[0]["tools"][0]["tool"] = "run"
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 1)
        scored = predictions.fold("ember")["p1"]["score"]
        self.assertEqual(scored["result"], "not checked")
        self.assertIn("supervisor tool reading an external outcome", scored["reason"])
        self.ask.assert_not_called()
        self.add()
        self.report(id="p2")
        self.episodes.return_value[0]["tools"][0]["tool"] = "fetch_page"
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 1)
        self.ask.assert_called_once()
        self.assertIn("how_to_check is the predictor's own description", self.ask.call_args.args[0])
        self.assertIn("shown as data", self.ask.call_args.args[0])

    def test_unresolvable_evidence_never_calls_judge(self):
        """Missing, ambiguous, old, failed and bookkeeping evidence cannot establish an outcome."""
        original = self.episodes.return_value
        cases = [([], "no such episode"), (original * 2, "ambiguous"),
                 ([{"ts": NOW.isoformat(), "tools": original[0]["tools"]}], "did not end after"),
                 ([{"ts": "2026-01-02T15:00:00", "tools": []}], "no such tool-log step"),
                 ([{"ts": "2026-01-02T15:00:00", "tools": [{"tool": "fetch_page", "result": "ERROR timeout"}]}],
                  "no successful tool call"),
                 ([{"ts": "2026-01-02T15:00:00", "tools": [{"tool": "finish_cycle", "result": "done"}]}],
                  "no successful tool call")]
        for episodes, reason in cases:
            with self.subTest(reason=reason):
                event = self.add()
                citation = NOW.isoformat() + "#1" if "did not end" in reason else CITATION
                self.report(id=event["id"], evidence=citation)
                self.episodes.return_value = episodes
                self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 1)
                score = predictions.fold("ember")[event["id"]]["score"]
                self.assertEqual(score["result"], "not checked")
                self.assertIn(reason, score["reason"])
                self.assertIsNone(score["judge"])
        self.ask.assert_not_called()
        self.available.assert_not_called()

    def test_valid_citation_judges_only_cited_result_and_is_idempotent(self):
        """A claimed failure is hidden from the judge, which sees exactly the selected step and cannot score twice."""
        self.add()
        self.report()
        self.episodes.return_value[0]["tools"].append({"tool": "fetch_page", "result": "unrelated secret word"})
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 1)
        self.ask.assert_called_once()
        system, prompt, schema = self.ask.call_args.args
        self.assertIn("Reading: 42", prompt)
        self.assertNotIn("failed", system + prompt)
        self.assertNotIn("outcome", prompt)
        self.assertNotIn("unrelated secret word", prompt)
        self.assertEqual(json.loads(prompt)["at"], NOW.isoformat())
        self.assertEqual(json.loads(prompt)["due"], DUE.isoformat())
        self.assertEqual(schema["properties"]["result"]["enum"], ["held", "failed", "unclear"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(self.ask.call_args.kwargs, {"purpose": "predict ember", "effort": "low",
                                                   "exclude": ["example/own"], "model": "openrouter/example/judge"})
        self.pick.assert_called_once_with(["example/own"])
        score = predictions.fold("ember")["p1"]["score"]
        self.assertEqual((score["result"], score["judge"]), ("held", "openrouter/example/judge"))
        before = self.rows()
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=3)), 0)
        self.assertEqual(self.rows(), before)
        with self.assertRaisesRegex(ValueError, "already reported or scored"):
            self.report()

    def test_judge_unclear_and_failed_and_result_truncation(self):
        """Unclear evidence is not a failure, and long results have a fixed prompt bound."""
        self.episodes.return_value[0]["tools"][0]["result"] = "x" * 4100
        for result, expected in (("unclear", "not checked"), ("failed", "failed")):
            event = self.add()
            self.report(id=event["id"])
            self.ask.return_value = {"result": result, "reason": "source insufficient"}
            self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 1)
            self.assertEqual(predictions.fold("ember")[event["id"]]["score"]["result"], expected)
            self.assertEqual(json.loads(self.ask.call_args.args[1])["tool_call"]["result"], "x" * 4000)

    def test_judge_error_unavailable_and_invalid_verdict_remain_retryable(self):
        """A failed judge leaves no score row, allowing a later run to produce the real verdict."""
        self.add()
        self.report()
        self.ask.side_effect = judge.JudgeError("offline")
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 0)
        self.ask.side_effect = None
        self.available.return_value = False
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 0)
        self.assertEqual(self.ask.call_count, 1)
        self.available.return_value = True
        self.ask.return_value = {"result": "invented", "reason": "bad"}
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=2)), 0)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.log.call_count, 3)
        self.ask.return_value = {"result": "held", "reason": "Reading: 42"}
        self.assertEqual(predictions.score("ember", DUE + timedelta(hours=3)), 1)

    def test_no_report_grace_boundary_and_idempotence(self):
        """No report is penalized only after the full 48-hour reporting window, without buying a judgment."""
        self.add()
        deadline = DUE + predictions.REPORT_GRACE
        self.assertEqual(predictions.score("ember", deadline - timedelta(microseconds=1)), 0)
        self.assertEqual(predictions.score("ember", deadline), 1)
        self.assertEqual(predictions.score("ember", deadline), 0)
        score = predictions.fold("ember")["p1"]["score"]
        self.assertEqual((score["result"], score["reason"], score["judge"]),
                         ("not checked", "no outcome reported within 48 h of its due time", None))
        self.ask.assert_not_called()
        self.episodes.assert_not_called()

    def test_fold_as_of_replay_extra_and_read_helpers_never_write(self):
        """Historical reads hide future reports and scores, while replay-only events remain off disk."""
        self.assertEqual(predictions.record_text("ember"), "(no predictions yet)")
        self.assertEqual(predictions.stats_lines("ember"), [])
        self.assertIsNone(predictions.last_made("ember"))
        self.add()
        self.report()
        predictions.score("ember", DUE + timedelta(hours=2))
        self.assertEqual(predictions.fold("ember", NOW - timedelta(seconds=1)), {})
        self.assertEqual(predictions.fold("ember", NOW)["p1"]["status"], "open")
        self.assertEqual(predictions.fold("ember", DUE)["p1"]["status"], "due")
        self.assertEqual(predictions.fold("ember", DUE + timedelta(hours=1))["p1"]["status"], "reported")
        self.assertEqual(predictions.fold("ember", DUE + timedelta(hours=2))["p1"]["status"], "scored")
        before = self.rows()
        event = predictions.made("ember", predictions.fold("ember", NOW), "Replay prediction", DUE.isoformat(),
                                 "Read source", now=NOW)
        replay = predictions.fold("ember", NOW, extra=[event])
        self.assertEqual(replay["p2"]["text"], "Replay prediction")
        report = predictions.reported("ember", replay, "p2", "held", CITATION, now=DUE)
        self.assertEqual(predictions.fold("ember", DUE, [event, report])["p2"]["status"], "reported")
        self.assertIn("p2: open", predictions.record_text("ember", as_of=NOW, extra=[event]))
        self.assertEqual(len(predictions.due_lines("ember", as_of=DUE, extra=[event])), 2)
        self.assertEqual(self.rows(), before)

    def test_torn_lines_are_skipped_and_next_append_survives(self):
        """A writer killed mid-line cannot prevent later events from being read."""
        self.add()
        with (self.root / "predictions/ember.jsonl").open("a") as file:
            file.write('null\n{"event":')
        self.assertEqual(list(predictions.fold("ember")), ["p1"])
        self.assertEqual(self.add()["id"], "p2")
        self.assertEqual(list(predictions.fold("ember")), ["p1", "p2"])

    def test_subject_counts_brier_and_inactivity(self):
        """Brier uses only scored binary outcomes with confidence, independently within each subject."""
        for confidence, result in ((80, "held"), (40, "failed"), (100, "unclear"), (None, "held")):
            event = self.add(subject="source", confidence=confidence)
            self.report(id=event["id"])
            self.ask.return_value = {"result": result, "reason": "checked"}
            predictions.score("ember", DUE + timedelta(hours=2))
        self.add()
        with mock.patch.object(predictions, "datetime") as clock:
            clock.now.return_value = NOW + timedelta(days=30)
            lines = predictions.stats_lines("ember")
        self.assertEqual(lines[:3], ["## My predictions, scored by the supervisor",
                         "- (no subject): made 1; held 0; failed 0; not checked 0; open 1; Brier n/a (n=0).",
                         "- source: made 4; held 2; failed 1; not checked 1; open 0; Brier 0.100 (n=2)."])
        self.assertIn("last made 2026-01-02T12:00:00", lines[3])
        self.assertEqual(predictions.last_made("ember"), NOW)
        with mock.patch.object(predictions, "datetime") as clock:
            clock.now.return_value = NOW + timedelta(days=29)
            self.assertEqual(len(predictions.stats_lines("ember")), 3)

    def test_due_cap_order_and_record_order_limit(self):
        """Reminders prioritize the oldest due times, while record limits retain the newest predictions last."""
        for hours in (5, 2, 4, 3, 6):
            self.add(due=(NOW + timedelta(hours=hours)).isoformat())
        lines = predictions.due_lines("ember", now=NOW + timedelta(hours=6))
        self.assertEqual([line.split()[2] for line in lines], ["p2", "p4", "p3"])
        self.assertIn('predict(id="p2", outcome="held"|"failed", evidence="<episode time>#<step>")', lines[0])
        self.assertEqual(predictions.due_lines("ember", now=NOW), [])
        record = predictions.record_text("ember", limit=2, as_of=NOW)
        self.assertEqual([line.split(":")[0] for line in record.splitlines()], ["p4", "p5"])
        self.assertIn("subject (no subject); confidence None", record)
        self.report(id="p2")
        self.ask.return_value = {"result": "held", "reason": "42\nwas read"}
        predictions.score("ember", DUE + timedelta(hours=2))
        record = predictions.record_text("ember")
        self.assertIn("held: 42 was read", record)
        self.assertEqual(len(record.splitlines()), 5)


if __name__ == "__main__":
    unittest.main()
