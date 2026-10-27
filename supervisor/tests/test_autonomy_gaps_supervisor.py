"""Regression tests for the supervisor side of REVIEW-autonomy-2026-10-24, gaps A2, A4, A5 and A8 (the agent side:
test_autonomy_gaps_template.py). Each class fails if its gap returns."""
from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, cloudllm, coder, consult, core, judge, metrics, runner, surprise, trial


def judgment(goal, judged_at, episode_ts="2026-10-01T10:00:00", **extra):
    return {"episode_ts": episode_ts, "judged_at": judged_at, "goal": goal, "confidence": 50,
            "criterion_met": "partial", "claimed": "partial", "claim_supported": True,
            "criterion_quality": "checkable", "reasoning": "evidence", "judge": judge.JUDGE_MODELS[0],
            "difficulty": 2, "model": "model-a", **extra}


class A2Test(unittest.TestCase):
    """A2 (REVIEW-autonomy-2026-10-24): review verdicts by judgment time without skipping pages."""

    def setUp(self):
        self.b = object.__new__(broker.Broker)
        self.b.name, self.b.as_of = "x", None
        clock = mock.patch.object(broker, "datetime", wraps=datetime)
        clock.start().now.return_value = datetime(2026, 10, 25)
        self.addCleanup(clock.stop)
        self.rows = [judgment("before", "2026-10-24T09:59:59"),
                     judgment("equal", "2026-10-24 10:00"),
                     judgment("old cycle newly judged", "2026-10-24T10:00:30"),
                     judgment("later", "2026-10-24 10:01:00", "2026-10-02T10:00:00"),
                     judgment("future", "2026-10-24T10:02:00", "2026-10-03T10:00:00")]
        for patch in (mock.patch.object(metrics, "judgments", return_value=self.rows),
                      mock.patch.object(metrics, "unsampled_rows", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def test_judged_at_since_and_replay_cut(self):
        self.b.as_of = datetime(2026, 10, 24, 10, 2)
        text = self.b._record("judgments", since="2026-10-24T10:00")
        self.assertEqual(text, "\n\n".join(broker.judgment_text(r) for r in self.rows[2:4]) + "\n\n"
                         "(2 new verdicts, oldest first. Next: record(kind='judgments', since='2026-10-24 10:01:00').)")
        self.assertIn("since", broker.SPECS["record"]["function"]["parameters"]["properties"])

    def test_limit_continuation_then_exactly_remaining(self):
        text = self.b._record("judgments", since="2026-10-24 10:00", limit=2)
        tail = "(2 of 3 new verdicts, oldest first. The rest: record(kind='judgments', since='2026-10-24 10:01:00').)"
        self.assertEqual(text, "\n\n".join(broker.judgment_text(r) for r in self.rows[2:4]) + "\n\n" + tail)
        cursor = re.search(r"record\(kind='judgments', since='([^']+)'\)", text)[1]
        self.assertEqual(self.b._record("judgments", since=cursor), broker.judgment_text(self.rows[4]) + "\n\n"
                         "(1 new verdict, oldest first. Next: record(kind='judgments', since='2026-10-24 10:02:00').)")

    def test_budget_continuation_and_judgment_order(self):
        self.rows[2:5] = [self.rows[4], self.rows[2], self.rows[3]]
        with mock.patch.object(broker, "JUDGMENTS_BUDGET", 1):
            text = self.b._record("judgments", since="2026-10-24 10:00")
        self.assertEqual(text, broker.judgment_text(self.rows[3]) + "\n\n"
                         "(1 of 3 new verdicts, oldest first. The rest: record(kind='judgments', since='2026-10-24 10:00:30').)")
        self.assertEqual(self.b._record("judgments", since="2026-10-24 10:00:30"),
                         "\n\n".join(broker.judgment_text(r) for r in (self.rows[4], self.rows[2])) + "\n\n"
                         "(2 new verdicts, oldest first. Next: record(kind='judgments', since='2026-10-24 10:02:00').)")

    def test_a_tie_is_never_split_across_the_continuation(self):
        # Found by Codex while writing these tests: two verdicts judged in the same second, cut between them, and
        # the cursor (strictly after the last shown) skipped the second one for good.
        tie = "2026-10-24T10:05:00"
        self.rows[:] = [judgment("a", "2026-10-24T10:04:00"), judgment("tie 1", tie), judgment("tie 2", tie),
                        judgment("after", "2026-10-24T10:06:00")]
        seen, cursor = [], "2026-10-24 10:00"
        for _ in range(6):
            text = self.b._record("judgments", since=cursor, limit=2)
            seen += [r["goal"] for r in self.rows if broker.judgment_text(r) in text]
            more = re.search(r"record\(kind='judgments', since='([^']+)'\)", text)
            if not more:
                break
            cursor = more[1]
        self.assertEqual(seen, ["a", "tie 1", "tie 2", "after"])
        self.rows[:] = [judgment("tie 1", tie), judgment("tie 2", tie), judgment("after", "2026-10-24T10:06:00")]
        first = self.b._record("judgments", since="2026-10-24 10:00", limit=1)   # all shown share it: show the tie
        self.assertIn(broker.judgment_text(self.rows[1]), first)
        self.assertIn("since='2026-10-24 10:05:00'", first)

    def test_a_late_second_opinion_comes_back(self):
        # Grok's review: the second judge runs detached, minutes later; since filtered on the main verdict's time,
        # so a second opinion arriving after the sleep had read the cycle was never shown.
        self.rows[:] = [judgment("seen", "2026-10-24T09:00:00",
                                 second={"judge": "j2", "criterion_met": "no", "judged_at": "2026-10-24T10:30:00"})]
        self.assertIn("Second judge", self.b._record("judgments", since="2026-10-24 10:00"))
        self.assertEqual(self.b._record("judgments", since="2026-10-24 10:30:00"), "(nothing recorded yet)")
        self.b.as_of = datetime(2026, 10, 24, 10, 15)   # a replay before the second opinion existed sees neither
        self.assertEqual(self.b._record("judgments", since="2026-10-24 10:00"), "(nothing recorded yet)")

    def test_no_since_keeps_newest_first_and_original_continuation(self):
        self.assertEqual(self.b._record("judgments", limit=2),
                         "\n\n".join(broker.judgment_text(r) for r in self.rows[:2:-1]) + "\n\n"
                         "(2 of 5 verdicts, newest first. Older: record(kind='judgments', before='2026-10-02 10:00'). "
                         "One in full: record(kind='judgments', cycle='YYYY-MM-DD HH:MM').)")

    def test_since_combines_with_before_and_cycle(self):
        self.assertEqual(self.b._record("judgments", since="2026-10-24 10:00", before="2026-10-02 10:00"),
                         broker.judgment_text(self.rows[2]) + "\n\n"
                         "(1 new verdict, oldest first. Next: record(kind='judgments', since='2026-10-24 10:00:30').)")
        self.assertEqual(self.b._record("judgments", since="2026-10-24 10:00", cycle="2026-10-02 10:00"),
                         broker.judgment_text(self.rows[3]) + "\n\n"
                         "(1 new verdict, oldest first. Next: record(kind='judgments', since='2026-10-24 10:01:00').)")

    def test_invalid_since_and_ignored_for_other_kinds(self):
        for value in ("yesterday", "2026-02-30 10:00", "2026-10-24", "2026-10-24 1:00", 42):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "since must be"):
                self.b._record("judgments", since=value)
        with mock.patch.object(trial, "describe_all", return_value="trials"):
            self.assertEqual(self.b._record("trials", since="invalid"), "trials")

    def test_first_fetch_json_pages_all_25_verdicts(self):
        self.rows[:] = [judgment(str(i), f"2026-10-24T10:{i:02d}:00", f"episode-{i}") for i in range(25)]
        cursor, seen = "1970-01-01 00:00:00", []
        for count in (20, 5, 0):
            page = json.loads(self.b._record("judgments", since=cursor, format="json"))
            self.assertEqual(page["text"], self.b._record("judgments", since=cursor))
            self.assertEqual(len(page["verdicts"]), count)
            self.assertEqual(set(page), {"text", "verdicts", "left_out", "next_since"})
            for verdict in page["verdicts"]:
                self.assertEqual(set(verdict), {"episode_ts", "judged_at", "reviewed_at", "criterion_met",
                                               "claim_supported", "failure_mode", "difficulty", "second_criterion_met",
                                               "followup_useful"})
                row = next(r for r in self.rows if r["episode_ts"] == verdict["episode_ts"])
                self.assertIn(broker.judgment_text(row), page["text"])
            seen.extend(v["episode_ts"] for v in page["verdicts"])
            cursor = page["next_since"]
        self.assertIsNone(cursor)
        self.assertEqual(seen, [f"episode-{i}" for i in range(25)])

    def test_json_second_opinion_and_tied_page_match_text(self):
        self.rows[:] = [judgment("one", "2026-10-24T10:00:00", "one", failure_mode="unsupported",
                                second={"judge": "j2", "criterion_met": "no", "judged_at": "2026-10-24T10:01:00"}),
                        judgment("two", "2026-10-24T10:01:00", "two")]
        page = json.loads(self.b._record("judgments", since="1970-01-01 00:00:00", format="json", limit=1))
        self.assertEqual(page["verdicts"], [
            {"episode_ts": "one", "judged_at": "2026-10-24T10:00:00", "reviewed_at": "2026-10-24 10:01:00",
             "criterion_met": "partial", "claim_supported": True, "failure_mode": "unsupported", "difficulty": 2,
             "second_criterion_met": "no", "followup_useful": None},
            {"episode_ts": "two", "judged_at": "2026-10-24T10:01:00", "reviewed_at": "2026-10-24 10:01:00",
             "criterion_met": "partial", "claim_supported": True, "failure_mode": None, "difficulty": 2,
             "second_criterion_met": None, "followup_useful": None}])
        self.assertEqual(page["text"], self.b._record("judgments", since="1970-01-01 00:00:00", limit=1))
        self.assertEqual(page["next_since"], "2026-10-24 10:01:00")

    def test_settled_window_and_left_out(self):
        self.rows[:] = [judgment("settled", "2026-10-24T10:00:07", "old"),
                        judgment("writing", "2026-10-24T10:00:08", "new")]
        omitted = [{"episode_ts": label, "decided_at": stamp} for label, stamp in
                   (("before", "2026-10-24T09:59:59"), ("left", "2026-10-24T10:00:07"),
                    ("writing", "2026-10-24T10:00:08"), ("future", "2026-10-24T11:00:00"))]
        with mock.patch.object(broker, "datetime", wraps=datetime) as clock, \
                mock.patch.object(metrics, "unsampled_rows", return_value=omitted):
            clock.now.return_value = datetime(2026, 10, 24, 10, 0, 10)
            page = json.loads(self.b._record("judgments", since="2026-10-24 10:00", format="json"))
            self.assertEqual([v["episode_ts"] for v in page["verdicts"]], ["old"])
            self.assertEqual(page["next_since"], "2026-10-24 10:00:07")
            self.assertEqual(page["left_out"], ["left"])
            clock.now.return_value = datetime(2026, 10, 24, 10, 0, 11)
            later = json.loads(self.b._record("judgments", since=page["next_since"], format="json"))
            self.assertEqual([v["episode_ts"] for v in later["verdicts"]], ["new"])
            self.assertEqual(later["left_out"], ["writing"])
            self.b.as_of = datetime(2026, 10, 24, 10, 0, 8)
            clock.now.return_value = self.b.as_of
            replay = json.loads(self.b._record("judgments", since="2026-10-24 10:00", format="json"))
            self.assertEqual(replay, page)



class A4Test(unittest.TestCase):
    """A4 (REVIEW-autonomy-2026-10-24): calibration scores the stated question, separately by version."""

    def setUp(self):
        self.rows = [judgment("old", "2026-10-24T10:00:00"),
                     judgment("new partial", "2026-10-24T10:01:00", confidence_of=metrics.CONFIDENCE_V2),
                     judgment("new yes", "2026-10-24T10:02:00", confidence_of=metrics.CONFIDENCE_V2,
                              criterion_met="yes")]

    def test_mixed_stats_binary_v2_and_separate_v1(self):
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(metrics, "judgments", return_value=self.rows))
            for name in ("_sample_note", "_battery_section", "_code_section", "_traits_section", "_trials_section"):
                stack.enter_context(mock.patch.object(metrics, name, return_value=[]))
            text = metrics.stats_text("x")
        self.assertIn("Calibration v2 — P(criterion met = yes), Brier on met/not met: Brier 0.25 (n=2", text)
        self.assertIn("when I said 40–70%: criterion met (yes) 50% of 2", text)
        self.assertIn("Calibration v1, older question 'probability of success', scored yes=1/partial=0.5; "
                      "not pooled with v2 or v3: Brier 0.00 (n=1", text)   # final fix F8
        self.assertIn("average confidence beforehand 50% vs succeeded 50%", text)   # v1 keeps its detail lines
        cal = metrics.calibration(self.rows)
        self.assertEqual((cal["v1"]["brier"], cal["v2"]["brier"]), (0.0, 0.25))
        self.assertEqual(metrics.calibration(self.rows[1:2])["v2"]["brier"], 0.25)

    def test_surprise_preserves_v1_baseline_and_adds_v2(self):
        result = surprise._judged(self.rows)
        self.assertEqual(result["brier"], 0.0)
        self.assertEqual(result["brier_v2"], 0.25)
        self.assertEqual(result["n_v2"], 2)
        self.assertIsNone(surprise._judged(self.rows[1:])["brier"])
        self.assertIsNone(surprise._judged(self.rows[:1])["brier_v2"])

    def test_judgment_preserves_intent_question(self):
        ep = {"ts": "2026-10-24T10:00:00", "intent": {"goal": "g", "confidence": 50,
              "confidence_of": metrics.CONFIDENCE_V2}, "tools": []}
        verdict = {"criterion_met": "partial", "claim_supported": True, "criterion_quality": "checkable",
                   "failure_mode": "", "reasoning": "r", "difficulty": 2, "difficulty_reason": "r", "novelty": 2,
                   "origin": "self", "origin_supported": True, "origin_evidence": "r", "value": 2,
                   "value_for": "own_capability", "value_reason": "r",
                   "expectations": [], "unreported_surprises": [], "reported_surprises_supported": True,
                   "followup_useful": "na"}
        with tempfile.TemporaryDirectory(prefix="shaft-confidence-") as tmp, ExitStack() as stack:
            path = Path(tmp) / "judgments.jsonl"
            for name, value in (("judgments", []), ("unsampled", set()), ("live_episodes", [ep]),
                                ("judge_sampled", True), ("second_sampled", False), ("_written_files", ""),
                                ("_provenance_evidence", ""), ("own_models", []), ("cycle_model", "m"),
                                ("_judgments_path", path)):
                stack.enter_context(mock.patch.object(metrics, name, return_value=value))
            stack.enter_context(mock.patch.object(judge, "available", return_value=True))
            stack.enter_context(mock.patch.object(judge, "pick", return_value=judge.JUDGE_MODELS[0]))
            stack.enter_context(mock.patch.object(judge, "ask", return_value=verdict))
            self.assertEqual(metrics.judge_pending("x"), 1)
            row = json.loads(path.read_text())
        self.assertEqual(row["confidence_of"], "criterion_met=yes")
        self.assertEqual(row["confidence"], 50)

    def test_bands_and_empty_versions(self):
        rows = [judgment("g", "2026-10-24T10:00:00", confidence=c, confidence_of=metrics.CONFIDENCE_V2)
                for c in (0, 39, 40, 69, 70, 100)]
        cal = metrics.calibration(rows + [{**rows[0], "confidence": None}])
        self.assertEqual([b["n"] for b in cal["v2"]["bands"]], [2, 2, 2])
        self.assertEqual(cal["v2"]["mean_outcome"], 0.0)
        self.assertEqual(cal["v1"]["n"], 0)
        self.assertIsNone(cal["v1"]["brier"])

    def test_review_reports_each_question(self):
        rows = [{**r, "episode_ts": "2026-10-24T10:00:00"} for r in self.rows]
        with mock.patch.object(metrics, "judgments", return_value=rows):
            text = metrics.review_text(["x"], now=datetime(2026, 10, 24, 11))
        self.assertIn("confidence v1 (probability of success, yes=1/partial=0.5) beforehand 50% vs judged 50%", text)
        self.assertIn("confidence v2 (criterion met = yes) beforehand 50% vs judged 50%", text)


class A5Test(unittest.TestCase):
    """A5 (REVIEW-autonomy-2026-10-24): authenticated, unmetered and secret-free operational envelope."""

    def setUp(self):
        self.real_probation_status = metrics.probation_status
        self.real_next_sleep = autonomy.next_sleep
        self.tmp = tempfile.TemporaryDirectory(prefix="shaft-envelope-")
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        for patch in (mock.patch.object(core, "disabled_tools", return_value=set()),
                      mock.patch.object(broker.Broker, "_shared_folder", return_value=None),
                      mock.patch.object(cloudllm, "spent_today", return_value=1.25),
                      mock.patch.object(cloudllm, "daily_cap", return_value=10.0),
                      mock.patch.object(coder, "spent_today", return_value=2.0),
                      mock.patch.object(coder, "DAILY_USD", 12.0),
                      mock.patch.object(trial, "usage_today", return_value={"started": 2, "usd": 3.0}),
                      mock.patch.object(trial, "TRIALS_PER_DAY", 4), mock.patch.object(trial, "DAILY_USD", 15.0),
                      mock.patch.object(metrics, "probation_status", return_value=[]),
                      mock.patch.object(autonomy, "next_sleep", return_value="2026-10-25 04:00"),
                      mock.patch.object(autonomy, "next_wake", return_value="14:00"),
                      mock.patch.object(broker.Broker, "ENVELOPE_TTL_S", 0)):   # each call recomputes, but in the cache test
            patch.start()
            self.addCleanup(patch.stop)
        self.b = broker.Broker("x", self.project, {"FAKE_KEY": "private-secret-value"}, self.project / "tools.log",
                               root=self.project / "copy", share=False, usage_name="billed-x")
        self.b.calls = 7
        self.expected = {"calls_used": 7, "calls_max": 60, "cloud": {"spent_usd": 1.25, "cap_usd": 10.0},
                         "consult": None, "coder": {"spent_usd": 2.0, "cap_usd": 12.0},
                         "trials": {"started": 2, "per_day": 4, "spent_usd": 3.0, "cap_usd": 15.0},
                         "probation": [], "next_sleep": "2026-10-25 04:00", "next_wake": "14:00"}

    def test_live_envelope_allowances_and_no_secrets(self):
        result = self.b.envelope()
        self.assertEqual(result, self.expected)
        cloudllm.spent_today.assert_called_once_with("billed-x")
        cloudllm.daily_cap.assert_called_once_with("x")
        coder.spent_today.assert_called_once_with("x")
        trial.usage_today.assert_called_once_with("x")
        encoded = json.dumps(result)
        for forbidden in ("/Users", str(self.project), "private-secret-value", self.b.token):
            self.assertNotIn(forbidden, encoded)
        self.assertNotIn("envelope", broker.SPECS)
        self.assertEqual(self.b.calls, 7)
        self.assertFalse(self.b.log_path.exists())

    def test_ledgers_are_read_once_a_minute_while_calls_stay_live(self):
        # Grok's review: the template asks every step, and each ask re-read the cloud ledger (megabytes) and, with
        # a probation open, every episode.
        with mock.patch.object(broker.Broker, "ENVELOPE_TTL_S", 60):
            first = self.b.envelope()
            self.b.calls = 9
            second = self.b.envelope()
        self.assertEqual(second, {**first, "calls_used": 9})
        cloudllm.spent_today.assert_called_once()
        trial.usage_today.assert_called_once()

    def test_http_envelope_token_and_no_call_or_tool_log(self):
        url = self.b.start()
        self.addCleanup(self.b.stop)
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        request = urllib.request.Request(url + "/envelope", headers={"X-Shaft-Token": self.b.token})
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(json.load(response), {"ok": True, "envelope": self.expected})
        self.assertEqual(self.b.calls, 7)
        self.assertFalse(self.b.log_path.exists())
        request = urllib.request.Request(url + "/envelope", headers={"X-Shaft-Token": "wrong"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.b.calls, 7)

    def test_disabled_unavailable_and_independent_failures(self):
        self.b.disabled = {"delegate_coding"}
        self.b.unavailable = {"trial": "not available"}
        with mock.patch.object(cloudllm, "spent_today", side_effect=OSError("no ledger")), \
                mock.patch.object(core, "log") as log:
            result = self.b.envelope()
        self.assertIsNone(result["cloud"])
        self.assertIsNone(result["coder"])
        self.assertIsNone(result["trials"])
        self.assertEqual(result["next_wake"], "14:00")
        coder.spent_today.assert_not_called()
        trial.usage_today.assert_not_called()
        log.assert_called_once()
        self.b.disabled, self.b.unavailable = {"trial"}, {"delegate_coding": "not available"}
        self.assertIsNone(self.b.envelope()["trials"])
        self.assertIsNone(self.b.envelope()["coder"])

    def test_replay_reconstructs_allowances(self):
        at = datetime(2026, 10, 20)
        replay = broker.ReplayBroker("x", self.project, {}, self.project / "replay.log",
                                     state_root=self.project / "copy", start_commit="a" * 40,
                                     as_of=at, cache={}, cache_lock=threading.Lock(),
                                     billed="trial-x", tools_log=self.project / "tools.log")
        replay.calls = 3
        replay.unavailable = {"delegate_coding": "replay", "trial": "replay"}
        with mock.patch.object(autonomy, "replay_next_sleep", return_value="2026-10-25 04:00"), \
                mock.patch.object(autonomy, "replay_next_wake", return_value="14:00"):
            self.assertEqual(replay.envelope(), {**self.expected, "calls_used": 3})
        for helper in (cloudllm.spent_today, coder.spent_today, trial.usage_today):
            helper.assert_called_once_with("x", until=at)
        metrics.probation_status.assert_called_once_with("x", as_of=at)
        replay.disabled = {"delegate_coding", "trial"}
        with mock.patch.object(autonomy, "replay_next_sleep"), mock.patch.object(autonomy, "replay_next_wake"):
            result = replay.envelope()
        self.assertIsNone(result["coder"])
        self.assertIsNone(result["trials"])

    def test_each_helper_failure_is_confined_to_its_field(self):
        for module, helper, field in ((cloudllm, "daily_cap", "cloud"), (coder, "spent_today", "coder"),
                                      (trial, "usage_today", "trials"), (metrics, "probation_status", "probation"),
                                      (autonomy, "next_sleep", "next_sleep"), (autonomy, "next_wake", "next_wake")):
            with self.subTest(field=field), mock.patch.object(module, helper, side_effect=ValueError("bad state")), \
                    mock.patch.object(core, "log") as log:
                result = self.b.envelope()
                self.assertEqual(result, {**self.expected, field: None})
                log.assert_called_once()
        with mock.patch.object(cloudllm, "daily_cap", side_effect=OSError("state unavailable")), \
                mock.patch.object(core, "log", side_effect=OSError("disk unavailable")):
            self.assertIsNone(self.b.envelope()["cloud"])

    def test_probation_status_matches_verdict_used_cycles(self):
        entry = {"commit": "abcdef01" * 5, "files": ["plugins/watch/main.py"], "judged_before": 3}
        rows = [judgment(f"g{i}", "2026-10-24T10:00:00", f"2026-10-24T10:{i:02d}:00") for i in range(9)]
        rows[4]["model"] = "other-model"
        rows[5]["judge"] = "retired-judge"
        rows[6]["difficulty"] = None
        tools = {r["episode_ts"]: {"watch"} for r in rows}
        tools[rows[7]["episode_ts"]] = {"other-plugin"}
        path = self.project / "probation.json"
        path.write_text(json.dumps([entry]))
        with mock.patch.object(metrics, "_probation_path", return_value=path), \
                mock.patch.object(metrics, "judgments", return_value=rows), \
                mock.patch.object(metrics, "_tools_by_cycle", return_value=tools), \
                mock.patch.object(metrics, "PROBATION_CYCLES", 3):
            status = self.real_probation_status("x")
            self.assertEqual(status, [{"commit": "abcdef01", "files": ["plugins/watch/main.py"],
                                       "cycles_used": 2, "cycles_needed": 3}])
            self.assertIsNone(metrics._verdict("x", entry, rows, tools))
            tools[rows[7]["episode_ts"]] = {"watch"}
            status = self.real_probation_status("x")
            verdict = metrics._verdict("x", entry, rows, tools)
            self.assertEqual(status[0]["cycles_used"], 3)
            self.assertEqual(verdict["n_after"], status[0]["cycles_used"])
            entry["files"] += [str(self.project / "host.py"), "../outside.py"]
            path.write_text(json.dumps([entry]))
            self.assertEqual(self.real_probation_status("x")[0]["files"], ["plugins/watch/main.py"])
            rows += [judgment(f"h{i}", "2026-10-24T11:00:00", f"2026-10-24T11:{i:02d}:00") for i in range(4)]
            tools.update({r["episode_ts"]: {"watch"} for r in rows})   # a window still open past its size
            self.assertEqual(self.real_probation_status("x")[0]["cycles_used"], 3)

    def test_next_sleep_off_due_and_next_day(self):
        with mock.patch.object(autonomy, "AGENTS", self.project), mock.patch.object(autonomy, "SLEEP_AT", (4, 0)), \
                mock.patch.object(autonomy, "_state", return_value={}) as state:
            self.assertIsNone(self.real_next_sleep(datetime(2026, 10, 24, 3)))
            (self.project / "com.shaft.scheduler.plist").touch()
            self.assertEqual(self.real_next_sleep(datetime(2026, 10, 24, 3)), "2026-10-24 04:00")
            self.assertEqual(self.real_next_sleep(datetime(2026, 10, 24, 4)), "due")
            self.assertEqual(self.real_next_sleep(datetime(2026, 10, 24, 8)), "due")
            state.return_value = {"sleep": "2026-10-24T04:00:00"}
            self.assertEqual(self.real_next_sleep(datetime(2026, 10, 24, 8)), "2026-10-25 04:00")


class ReplayLedgerTest(unittest.TestCase):
    def test_envelope_matches_live_as_of_synthetic_ledgers(self):
        at = datetime(2026, 10, 20, 4)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            ledger = root / "usage.jsonl"
            records = [{"instance": who, "ts": stamp, "usd": usd, "by": by} for who, stamp, usd, by in
                       (("x", "2026-10-20T03:00:00", 1.25, "instance"),
                        ("x", "2026-10-20T04:00:00", 20, "instance"),
                        ("x", "2026-10-20T05:00:00", 30, "instance"),
                        ("x", "2026-10-19T03:00:00", 40, "instance"),
                        ("trial-x", "2026-10-20T03:00:00", 50, "instance"))]
            ledger.write_text("".join(json.dumps(r) + "\n" for r in records))
            trials = [{"created": r["ts"], "by": r["by"]} for r in records if r["instance"] == "x"]
            trials.append({"created": "2026-10-20T02:00:00", "by": "operator"})
            probation = root / "probation.json"
            entries = [{"commit": "abcdefgh123", "files": ["shaft/live.py"], "since": "2026-10-20T02:00:00",
                        "judged_before": 0}]
            probation.write_text(json.dumps(entries + [{**entries[0], "since": at.isoformat()}]))
            rows = [judgment("before", "2026-10-20T03:00:00", "old"),
                    judgment("at", at.isoformat(), "new")]
            for module, attr, value in ((cloudllm, "USAGE", ledger), (coder, "USAGE", ledger),
                                        (trial, "LEDGER", ledger), (autonomy, "AGENTS", root),
                                        (autonomy, "SLEEP_AT", (4, 0)), (coder, "DAILY_USD", 12),
                                        (trial, "DAILY_USD", 15), (trial, "TRIALS_PER_DAY", 4)):
                stack.enter_context(mock.patch.object(module, attr, value))
            for module, attr, value in ((trial, "all_trials", trials), (metrics, "_probation_path", probation),
                                        (metrics, "judgments", rows), (metrics, "_tools_by_cycle", {}),
                                        (cloudllm, "daily_cap", 10), (autonomy, "wake_interval", timedelta(hours=1))):
                stack.enter_context(mock.patch.object(module, attr, return_value=value))
            stack.enter_context(mock.patch.object(metrics, "_used", return_value=True))
            (root / "com.shaft.scheduler.plist").touch()
            replay = object.__new__(broker.ReplayBroker)
            replay.name, replay.usage_name, replay.as_of = "x", "trial-x", at
            replay.calls, replay.max_calls, replay.disabled = 3, 60, set()
            replay.unavailable = {"trial": "replay", "delegate_coding": "replay"}
            expected = {"calls_used": 3, "calls_max": 60, "cloud": {"spent_usd": 1.25, "cap_usd": 10.0},
                        "consult": None, "coder": {"spent_usd": 1.25, "cap_usd": 12.0},
                        "trials": {"started": 1, "per_day": 4, "spent_usd": 1.25, "cap_usd": 15.0},
                        "probation": [{"commit": "abcdefgh", "files": ["shaft/live.py"],
                                       "cycles_used": 1, "cycles_needed": metrics.PROBATION_CYCLES}],
                        "next_sleep": "2026-10-21 04:00", "next_wake": "05:00"}
            historical = replay.envelope()
            self.assertEqual(historical, expected)
            # Live at that instant sees only the prefix of each ledger and the then-open probation entry.
            ledger.write_text(json.dumps(records[0]) + "\n")
            trials[:] = trials[:1]
            rows[:] = rows[:1]
            probation.write_text(json.dumps(entries))
            live = object.__new__(broker.Broker)
            live.name = live.usage_name = "x"
            live.calls, live.max_calls, live.as_of = 3, 60, None
            live.disabled, live.unavailable = set(), {}
            live.model_allowed = True
            stack.enter_context(mock.patch.object(consult, "envelope", return_value=None))
            for module in (cloudllm, coder):
                date = stack.enter_context(mock.patch.object(module, "date"))
                date.today.return_value = at.date()
            clock = stack.enter_context(mock.patch.object(trial, "datetime", wraps=datetime))
            clock.now.return_value = at
            clock = stack.enter_context(mock.patch.object(autonomy, "datetime", wraps=datetime))
            clock.now.return_value = at
            stack.enter_context(mock.patch.object(autonomy, "_state", return_value={
                "sleep": at.isoformat(), "wakes": {"x": at.isoformat()}}))
            self.assertEqual(live.envelope(), historical)
            self.assertEqual(set(historical), set(expected))
            self.assertNotIn("replay", json.dumps(historical))
            self.assertEqual(autonomy.replay_next_sleep(at - timedelta(seconds=1)), "2026-10-20 04:00")
            with mock.patch.object(autonomy, "wake_interval", return_value=None):
                self.assertEqual(autonomy.replay_next_wake("x", at), "never")
            (root / "com.shaft.scheduler.plist").unlink()
            self.assertIsNone(autonomy.replay_next_sleep(at))


class ConfidenceBackfillTest(unittest.TestCase):
    """A4 (REVIEW-autonomy-2026-10-24, Codex's review): the template may be deployed before the supervisor."""

    def setUp(self):
        metrics._QUESTIONS.clear()
        self.addCleanup(metrics._QUESTIONS.clear)

    def test_backfill_date_missing_key_and_single_episode_read(self):
        rows = [judgment("old", "2026-10-23T23:59:59", "old"),
                judgment("boundary", "2026-10-24T00:00:00", "new"),
                judgment("explicit", "2026-10-24T00:00:01", "explicit", confidence_of=None),
                judgment("missing episode", "2026-10-24T00:00:02", "missing")]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "judgments").mkdir()
            path = root / "judgments" / "x.jsonl"
            original = "".join(json.dumps(r) + "\n" for r in rows)
            path.write_text(original)
            episodes = [{"ts": r["episode_ts"], "intent": {"confidence_of": metrics.CONFIDENCE_V2}} for r in rows[:3]]
            sup = mock.patch.object(metrics, "SUP_DIR", root)
            sup.start()
            self.addCleanup(sup.stop)
            with mock.patch.object(metrics, "live_episodes", return_value=episodes) as read:
                result = metrics.judgments("x", root)
                read.assert_called_once_with("x")
            self.assertNotIn("confidence_of", result[0])
            self.assertEqual(result[1]["confidence_of"], metrics.CONFIDENCE_V2)
            self.assertIsNone(result[2]["confidence_of"])
            self.assertNotIn("confidence_of", result[3])
            self.assertEqual(path.read_text(), original)
            with mock.patch.object(metrics, "live_episodes") as read:   # Grok's review: not re-read every call
                self.assertEqual(metrics.judgments("x", root)[1]["confidence_of"], metrics.CONFIDENCE_V2)
                read.assert_not_called()
            path.write_text(json.dumps(rows[0]) + "\n" + json.dumps(rows[2]))
            metrics._QUESTIONS.clear()
            with mock.patch.object(metrics, "live_episodes") as read:
                metrics.judgments("x", root)
                read.assert_not_called()

    def test_no_backfill_from_live_episodes_for_another_record(self):
        # Grok's review: a record in another directory must not take the live instance's intents.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "judgments").mkdir()
            (root / "judgments" / "x.jsonl").write_text(json.dumps(judgment("g", "2026-10-24T01:00:00", "e")) + "\n")
            with mock.patch.object(metrics, "live_episodes") as read:
                self.assertNotIn("confidence_of", metrics.judgments("x", root)[0])
                read.assert_not_called()


class OldTemplateCompatibilityTest(unittest.TestCase):
    def test_old_http_client_never_requests_envelope(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(core, "disabled_tools", return_value=set()), \
                mock.patch.object(broker.Broker, "_shared_folder", return_value=None), \
                mock.patch.object(broker.Broker, "envelope", side_effect=AssertionError("unused")) as envelope:
            root = Path(tmp)
            b = broker.Broker("x", root, {}, root / "tools.log", root=root, share=False)
            url = b.start()
            try:
                request = urllib.request.Request(url + "/tools", headers={"X-Shaft-Token": b.token})
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(json.load(response), {"tools": b.tool_specs()})
                self.assertEqual(b.calls, 0)
                self.assertFalse(b.log_path.exists())
                envelope.assert_not_called()
            finally:
                b.stop()

    def test_old_episode_and_judgment_stay_v1_even_after_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "judgments").mkdir()
            rows = [judgment("old", "2026-10-23T23:00:00", "old"),
                    judgment("new", "2026-10-24T00:00:00", "new")]
            (root / "judgments" / "x.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            metrics._QUESTIONS.clear()
            with mock.patch.object(metrics, "SUP_DIR", root), mock.patch.object(metrics, "live_episodes", return_value=[
                    {"ts": "old", "intent": {"confidence": 50}}, {"ts": "new", "intent": {"confidence": 50}}]):
                loaded = metrics.judgments("x", root)
            self.assertEqual(loaded, rows)
            calibration = metrics.calibration(loaded)
            self.assertEqual(calibration["v1"]["n"], 2)
            self.assertEqual(calibration["v1"]["brier"], 0.0)
            self.assertEqual(calibration["v2"]["n"], 0)
            b = object.__new__(broker.Broker)
            b.name, b.as_of = "x", None
            with mock.patch.object(metrics, "judgments", return_value=loaded), \
                    mock.patch.object(metrics, "unsampled_rows", return_value=[]), \
                    mock.patch.object(broker.Broker, "envelope", side_effect=AssertionError("unused")) as envelope:
                self.assertEqual(b._record("judgments"), "\n\n".join(broker.judgment_text(r) for r in rows[::-1]))
                envelope.assert_not_called()



class A8Test(unittest.TestCase):
    """A8 (REVIEW-autonomy-2026-10-24): replayed agents receive the original cycle's start time."""

    def test_replay_passes_original_start_to_agent_env(self):
        with tempfile.TemporaryDirectory(prefix="shaft-replay-clock-") as tmp, ExitStack() as stack:
            project = Path(tmp)
            dest = project / "copy"
            dest.mkdir()
            state = {"t0": "2026-10-10T10:00:00", "start_commit": "a" * 40}
            for name, value in (("reconstruct", dest), ("link_env", None), ("secrets_env", {}),
                                ("_files_now", {}), ("_episode_lines", set()), ("_talk_ids", []),
                                ("_config", {}), ("_new_episodes", []), ("_llm_usage", {}), ("_cloud_usd", 0.0)):
                stack.enter_context(mock.patch.object(trial, name, return_value=value))
            stack.enter_context(mock.patch.object(autonomy, "describe_schedule", return_value="schedule"))
            fake_broker = stack.enter_context(mock.patch.object(broker, "ReplayBroker"))
            fake_broker.return_value.trace = []
            command = stack.enter_context(mock.patch.object(runner, "agent_command",
                side_effect=lambda root, b, args, profile, ports, extra: (["agent"], dict(extra))))
            popen = stack.enter_context(mock.patch.object(trial.subprocess, "Popen"))
            popen.return_value.wait.return_value = 0
            trial._replay(project, "x", state, {}, project, model="cloud-model", cache={},
                          cache_lock=threading.Lock(), billed="trial-x", should_stop=lambda: False, keep=None)
            self.assertEqual(command.call_args.args[5]["SHAFT_NOW"], state["t0"])
            self.assertEqual(popen.call_args.kwargs["env"]["SHAFT_NOW"], state["t0"])
            self.assertEqual(fake_broker.call_args.kwargs["as_of"], datetime(2026, 10, 10, 10))


if __name__ == "__main__":
    unittest.main()
