"""Reply expectations use supervisor receipts, never the instance's editable episode."""
from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import broker, core, judge, messages, tom


class TomTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-tom-")
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.sup = self.project / ".supervisor"
        self.roots = self.project / "instances"
        for name in ("a", "b"):
            (self.roots / name / "shaft").mkdir(parents=True)
            (self.roots / name / ".git").write_text("gitdir: test")
        for module in (core, messages, tom):
            for key, value in (("SUP_DIR", self.sup), ("INSTANCES", self.roots)):
                patch = mock.patch.object(module, key, value)
                patch.start()
                self.addCleanup(patch.stop)
        patch = mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox")
        patch.start()
        self.addCleanup(patch.stop)
        for target in ("supervisor.broker.notify", "supervisor.tom.log"):
            patch = mock.patch(target)
            patch.start()
            self.addCleanup(patch.stop)
        self.start = datetime(2026, 10, 26, 12)
        self.expect = {"will_reply": 80, "within_hours": 2, "gist": "Agrees."}

    def receipt(self, id="one", to="b", **kw):
        return {"id": id, "sent_at": self.start.isoformat(timespec="seconds"), "to": to,
                "message": "sent.md", "text": "Can you help?", **self.expect, **kw}

    def reply(self, at=None, sender="b", folder="inbox", text="Agreed."):
        at = at or self.start + timedelta(minutes=1)
        path = self.roots / "a" / folder / f"{at:%Y%m%d-%H%M%S-%f}-from-{sender}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"From: {sender}\n\n{text}\n")
        return path

    def make(self, replay=False):
        if replay:
            return broker.ReplayBroker("a", self.project, {}, self.project / "replay.log",
                                       state_root=self.roots / "a", start_commit="0", as_of=self.start,
                                       cache={}, cache_lock=threading.Lock(), billed="a@trial:x",
                                       tools_log=self.project / "tools.log")
        return broker.Broker("a", self.project, {}, self.project / "broker.log")

    def test_live_receipt_uses_supervisor_clock_and_delivered_filename(self):
        with mock.patch.object(broker, "datetime") as clock:
            clock.now.return_value = self.start
            result = self.make().call("send_message", {"to": "b", "text": "x" * 2500,
                                                       "expect_reply": self.expect})
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"], "delivered to b; your expectation of the reply is recorded")
        row, = tom.receipts("a")
        self.assertEqual(row["sent_at"], "2026-10-26T12:00:00")
        self.assertEqual(row["text"], "x" * 2000)
        self.assertEqual(row["will_reply"], 80)
        self.assertTrue((self.roots / "b" / "inbox" / row["message"]).is_file())

    def test_malformed_expectations_never_block_delivery(self):
        for expectation in ([], "bad", {}, {**self.expect, "will_reply": True},
                            {**self.expect, "will_reply": 101}, {**self.expect, "within_hours": 0},
                            {**self.expect, "within_hours": 169}, {**self.expect, "gist": 5}):
            with self.subTest(expectation=expectation):
                out = self.make().call("send_message", {"to": "b", "text": "hi", "expect_reply": expectation})
                self.assertTrue(out["ok"])
                self.assertIn("expectation not recorded:", out["result"])
        self.assertEqual(len(list((self.roots / "b" / "inbox").glob("*.md"))), 8)
        self.assertEqual(tom.receipts("a"), [])

    def test_failed_delivery_has_no_receipt(self):
        with mock.patch.object(broker.Broker, "_deliver", side_effect=OSError("failed")):
            out = self.make().call("send_message", {"to": "b", "text": "hi", "expect_reply": self.expect})
        self.assertFalse(out["ok"])
        self.assertEqual(tom.receipts("a"), [])

    def test_replay_has_live_result_but_no_delivery_or_receipt(self):
        args = {"to": "b", "text": "hi", "expect_reply": self.expect}
        replay = self.make(True)
        out = replay.call("send_message", args)
        self.assertFalse((self.roots / "b" / "inbox").exists())
        self.assertEqual(tom.receipts("a"), [])
        self.assertIn("simulated: not delivered", replay.trace[-1]["tags"])
        self.assertEqual(out, self.make().call("send_message", args))

    def test_operator_replay_has_no_receipt(self):
        out = self.make(True).call("send_message", {"to": "operator", "text": "hi", "expect_reply": self.expect})
        self.assertTrue(out["ok"])
        self.assertTrue(out["result"].endswith("; your expectation of the reply is recorded"))
        self.assertFalse((self.sup / "operator-inbox").exists())
        self.assertEqual(tom.receipts("a"), [])

    def test_reply_lookup_window_sender_and_archive(self):
        self.reply(self.start - timedelta(seconds=1))
        self.reply(self.start)
        self.reply(self.start + timedelta(hours=3))
        self.reply(self.start + timedelta(seconds=1), sender="other")
        wanted = self.reply(self.start + timedelta(minutes=1), folder="memory/messages")
        self.reply(self.start + timedelta(minutes=2))
        found = tom.reply_for("a", self.receipt())
        self.assertEqual(found, {"file": wanted.name, "at": "2026-10-26T12:01:00", "text": "Agreed."})
        self.assertIsNone(tom.reply_for("a", self.receipt(to="nobody")))

    def test_operator_reply_filename(self):
        with mock.patch.object(messages, "datetime") as clock, mock.patch.object(messages, "log"):
            clock.now.return_value = self.start + timedelta(minutes=2)
            messages.reply("a", "Yes, go ahead.")
        found = tom.reply_for("a", self.receipt(to="operator"))
        self.assertEqual(found["file"], "20261026-120200-000000-from-operator.md")
        self.assertEqual(found["text"], "Yes, go ahead.")

    def test_reply_never_follows_file_or_folder_links(self):
        path = self.reply()
        target = self.project / "plain.md"
        path.rename(target)
        path.symlink_to(target)
        self.assertIsNone(tom.reply_for("a", self.receipt()))
        path.unlink()
        path.parent.rmdir()
        external = self.project / "plain-folder"
        external.mkdir()
        target.rename(external / path.name)
        path.parent.symlink_to(external, target_is_directory=True)
        self.assertIsNone(tom.reply_for("a", self.receipt()))

    def test_score_brier_gist_and_resumability(self):
        tom.record("a", self.receipt())
        self.reply()
        with mock.patch.object(judge, "ask", return_value={"match": "partial", "reason": "Some overlap."}) as ask:
            self.assertEqual(tom.score("a", self.start + timedelta(hours=1)), 1)
            self.assertEqual(tom.score("a", self.start + timedelta(hours=1)), 0)
        row, = tom._rows("a", "-scored")
        self.assertAlmostEqual(row["brier"], .04)
        self.assertEqual((row["outcome"], row["match"]), (1, "partial"))
        self.assertEqual(ask.call_args.kwargs["purpose"], "tom a")
        self.assertEqual(ask.call_count, 1)

    def test_no_reply_waits_for_window_and_scores_without_judge(self):
        tom.record("a", self.receipt())
        with mock.patch.object(judge, "ask") as ask:
            self.assertEqual(tom.score("a", self.start + timedelta(hours=1)), 0)
            self.assertEqual(tom.score("a", self.start + timedelta(hours=2)), 1)
        ask.assert_not_called()
        row, = tom._rows("a", "-scored")
        self.assertEqual(row["outcome"], 0)
        self.assertAlmostEqual(row["brier"], .64)

    def test_judge_error_stops_and_preserves_prior_scores(self):
        for id in ("one", "two", "three"):
            tom.record("a", self.receipt(id))
        self.reply()
        with mock.patch.object(judge, "ask", side_effect=[{"match": "yes", "reason": "Agrees"},
                                                         judge.JudgeError("unavailable")]) as ask:
            self.assertEqual(tom.score("a", self.start + timedelta(hours=1)), 1)
        self.assertEqual(ask.call_count, 2)
        with mock.patch.object(judge, "ask", return_value={"match": "yes", "reason": "Agrees"}):
            self.assertEqual(tom.score("a", self.start + timedelta(hours=1)), 2)
        self.assertEqual(len(tom._rows("a", "-scored")), 3)

    def test_section_and_paired_report(self):
        self.assertEqual(len(tom.section("a")), 2)
        for id, outcome, brier in (("one", 1, .04), ("two", 0, .64)):
            tom._append("a", {**self.receipt(id), "outcome": outcome, "brier": brier,
                              **({"match": "yes"} if outcome else {})}, "-scored")
        section = tom.section("a")
        self.assertLessEqual(len(section), 6)
        self.assertIn("Predictions scored: 2.", section)
        self.assertIn("always saying the base rate (0.500): 0.250", section[2])
        self.assertIn("yes=1, partial=0, no=0", section[3])
        self.assertIn("b: n=2", section[4])
        for arm in ("a", "b"):
            tom._append("a", {"id": "one", "arm": arm, "brier": .25, "match": "partial"}, "-baselines")
        report = tom.report(["a"])
        self.assertIn("instance: n=1, Brier=0.040, gist=1.000", report)
        self.assertIn("accepted only if the instance beats both baselines: accepted (paired n=1)", report)

    def test_baselines_use_historical_people_and_resume_paid_prediction(self):
        receipt = self.receipt(to="operator")
        tom._append("a", {**receipt, "outcome": 1, "brier": .04,
                          "reply": {"text": "Yes"}}, "-scored")
        with mock.patch.object(tom, "instance_config", return_value={"OPERATOR_NAME": "Jane Doe"}), \
                mock.patch.object(tom.mind, "at", return_value="old-rev") as at, \
                mock.patch.object(tom.mind, "show", return_value="Historical people text") as show, \
                mock.patch.object(judge, "ask", side_effect=[{"will_reply": 50, "gist": "Yes"},
                    {"match": "yes", "reason": "agrees"}, {"will_reply": 75, "gist": "Yes"},
                    judge.JudgeError("offline")]) as ask:
            self.assertEqual(tom.baselines("a"), 1)
            show.assert_called_once_with("a", "old-rev", "memory/people/jane-doe.md")
            at.assert_called_once_with("a", receipt["sent_at"])
            self.assertNotIn("Historical people text", ask.call_args_list[0].args[1])
            self.assertIn("Historical people text", ask.call_args_list[2].args[1])
        with mock.patch.object(judge, "ask", return_value={"match": "partial", "reason": "some"}) as ask:
            self.assertEqual(tom.baselines("a"), 1)
            self.assertEqual(tom.baselines("a"), 0)
            self.assertEqual(ask.call_count, 1)
        rows = tom._baseline_rows("a")
        self.assertEqual(rows[("one", "a")]["brier"], .25)
        self.assertEqual(rows[("one", "b")]["brier"], .0625)

    def test_baseline_b_skipped_when_historical_people_unavailable(self):
        tom._append("a", {**self.receipt(), "reply": {"text": "yes"}}, "-scored")
        with mock.patch.object(tom.mind, "at", return_value="old"), \
                mock.patch.object(tom.mind, "show", return_value=None), \
                mock.patch.object(judge, "ask", side_effect=[{"will_reply": 50, "gist": "Yes"},
                    {"match": "no", "reason": "different"}]):
            self.assertEqual(tom.baselines("a"), 1)
        self.assertEqual(set(tom._baseline_rows("a")), {("one", "a")})
        self.assertIn("not accepted (paired n=0)", tom.report(["a"]))
