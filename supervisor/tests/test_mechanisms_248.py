"""Issue #248 mechanisms: unrecorded orders reach the instance's inbox (excluding own-message quotes),
and consecutive identical job messages are not delivered twice."""
from __future__ import annotations

import json
import unittest
from datetime import timedelta
from unittest import mock

from supervisor import browser, core, jobs, sandbox, unrecorded
from supervisor.tests import test_jobs, test_unrecorded

NOW = test_unrecorded.NOW
WORDS = test_unrecorded.WORDS


class UnrecordedOrdersReachInstanceTest(unittest.TestCase):
    def setUp(self):
        self.fx = test_unrecorded.UnrecordedTest()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)

    def test_248_unrecorded_order_reaches_the_instance(self):
        self.fx.talk(tid="talk-248")
        self.assertEqual(unrecorded.run("ember", NOW), 1)
        notes = sorted((self.fx.root / "inbox").glob("*-from-supervisor-orders.md"))
        self.assertEqual(len(notes), 1)
        content = notes[0].read_text()
        self.assertIn("From: supervisor (unrecorded orders)\nTo: ember\nKind: note\nDate: ", content)
        self.assertIn("talk-248", content)
        self.assertIn(json.dumps(WORDS, ensure_ascii=False), content)
        self.assertIn("record_commitment", content)
        self.assertIn("record_order", content)
        row, = unrecorded.rows("ember")
        self.assertIs(row.get("told"), True)
        self.assertNotIn("own_quotes", row)
        logged = [json.loads(l) for l in (self.fx.sup / "messages.jsonl").read_text().splitlines() if l.strip()]
        self.assertEqual(len(logged), 1)
        self.assertEqual((logged[0]["from"], logged[0]["to"], logged[0]["kind"], logged[0]["file"]),
                         ("supervisor (unrecorded orders)", "ember", "note", notes[0].name))
        # A second run delivers nothing more.
        self.assertEqual(unrecorded.run("ember", NOW + timedelta(minutes=5)), 0)
        self.assertEqual(sorted((self.fx.root / "inbox").glob("*-from-supervisor-orders.md")), notes)

    def test_248_a_short_answer_found_in_the_instance_turn_is_still_an_order(self):
        """A rule matching the order against the instance's previous turn dropped the operator's own "Yes" (a live
        ledger, 11-20): a short answer is in most turns. Only quote-reply lines are the instance's words."""
        at = NOW - timedelta(hours=1)
        self.fx.episodes([
            {"ts": (at - timedelta(minutes=3)).isoformat(), "kind": "talk", "talk_id": "talk-yes",
             "role": "assistant", "content": "If yes, I will move the review to Monday and tell the team."},
            {"ts": (at - timedelta(minutes=2)).isoformat(), "kind": "talk", "talk_id": "talk-yes",
             "role": "user", "content": "Yes"},
            {"ts": at.isoformat(), "kind": "talk_recovered", "talk_id": "talk-yes", "content": "Review moved."},
        ])
        self.fx.ask.return_value = {"orders": [{"words": "Yes", "recorded": False}]}
        self.assertEqual(unrecorded.run("ember", NOW), 1)
        row, = unrecorded.rows("ember")
        self.assertEqual(row["orders"], [{"words": "Yes", "recorded": False}])
        self.assertNotIn("own_quotes", row)
        self.assertIs(row["told"], True)

    def test_248_quote_reply_of_a_sent_message_is_not_an_order(self):
        """The real case (one instance's ledger, 11-13..11-20: 6 of 13 unrecorded "orders"): the operator quote-replies a message the
        instance sent with send_message ("> …" lines), so the suggestion is in no talk turn. His own line after the
        quote is still his order."""
        at = NOW - timedelta(hours=1)
        self.fx.episodes([
            {"ts": (at - timedelta(minutes=2)).isoformat(), "kind": "talk", "talk_id": "talk-qr",
             "role": "user", "content": "> Vuoi che prepari il recap di oggi,\n> senza attribuire le voci incerte?\n\n"
                                         "Sì, e guarda anche la questione Daikin"},
            {"ts": at.isoformat(), "kind": "talk_recovered", "talk_id": "talk-qr", "content": "Recap agreed."},
        ])
        self.fx.ask.return_value = {"orders": [
            {"words": "Vuoi che prepari il recap di oggi, senza attribuire le voci incerte?", "recorded": False},
            {"words": "guarda anche la questione Daikin", "recorded": False}]}
        self.assertEqual(unrecorded.run("ember", NOW), 1)
        row, = unrecorded.rows("ember")
        self.assertEqual(row["orders"], [{"words": "guarda anche la questione Daikin", "recorded": False}])
        self.assertEqual(row["own_quotes"], 1)
        note, = (self.fx.root / "inbox").glob("*-from-supervisor-orders.md")
        self.assertIn("Daikin", note.read_text())
        self.assertNotIn("Vuoi che prepari", note.read_text())

    def test_248_his_own_line_under_a_quote_is_an_order_even_if_the_quote_has_the_words(self):
        """Another instance's ledger, 11-20: he quote-replied a message containing "yes" and answered "Yes" under it. Words in his
        own lines are his, whatever the quote above them says."""
        at = NOW - timedelta(hours=1)
        self.fx.episodes([
            {"ts": (at - timedelta(minutes=2)).isoformat(), "kind": "talk", "talk_id": "talk-qyes",
             "role": "user", "content": "> I checked my send log: yes, it was sent. Shall I send the next part?\n\nYes"},
            {"ts": at.isoformat(), "kind": "talk_recovered", "talk_id": "talk-qyes", "content": "Next part agreed."},
        ])
        self.fx.ask.return_value = {"orders": [{"words": "Yes", "recorded": False}]}
        self.assertEqual(unrecorded.run("ember", NOW), 1)
        row, = unrecorded.rows("ember")
        self.assertEqual(row["orders"], [{"words": "Yes", "recorded": False}])
        self.assertNotIn("own_quotes", row)

    def test_248_quoting_another_speaker_is_still_an_order(self):
        """Only the instance's own preceding turn makes an operator quote not an order: a colleague's words in a group
        talk, repeated by the operator, are his order (the lane first counted any non-operator turn)."""
        at = NOW - timedelta(hours=1)
        self.fx.episodes([
            {"ts": (at - timedelta(minutes=3)).isoformat(), "kind": "talk", "talk_id": "talk-peer",
             "role": "user", "speaker": "Anna", "content": "Someone should send the report tomorrow."},
            {"ts": (at - timedelta(minutes=2)).isoformat(), "kind": "talk", "talk_id": "talk-peer",
             "role": "user", "content": "Yes: send the report tomorrow."},
            {"ts": at.isoformat(), "kind": "talk_recovered", "talk_id": "talk-peer", "content": "Report agreed."},
        ])
        self.fx.ask.return_value = {"orders": [{"words": "send the report tomorrow", "recorded": False}]}
        self.assertEqual(unrecorded.run("ember", NOW), 1)
        row, = unrecorded.rows("ember")
        self.assertEqual(row["orders"], [{"words": "send the report tomorrow", "recorded": False}])
        self.assertNotIn("own_quotes", row)
        self.assertIs(row["told"], True)

    def test_248_delivery_failure_is_logged_and_keeps_ledger_row_with_told_false(self):
        self.fx.talk(tid="talk-fail")
        with mock.patch.object(core, "safe_write", side_effect=OSError("disk full")):
            self.assertEqual(unrecorded.run("ember", NOW), 1)
        row, = unrecorded.rows("ember")
        self.assertIs(row.get("told"), False)
        self.assertTrue(any("talk-fail" in call.args[0] for call in self.fx.log.call_args_list))
        self.assertEqual(unrecorded.run("ember", NOW + timedelta(minutes=5)), 0)


class IdenticalJobMessagesDroppedTest(test_jobs.JobCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(browser, "sweep"))
        self.enterContext(mock.patch.object(sandbox, "wrap", side_effect=lambda prof, cmd: [cmd[0], "-B", *cmd[1:]]))

    def test_248_consecutive_identical_job_messages_are_not_delivered_twice(self):
        state = self.root / ".jobs" / "watch"
        state.mkdir(parents=True)
        (state / "msg.txt").write_text("one-line status body")
        self.plugin("watch", """
            import os, pathlib
            def run():
                state = pathlib.Path(os.environ["SHAFT_JOB_STATE"])
                return {"message": (state / "msg.txt").read_text(), "wake": True}
            """, {"every_minutes": 10, "can_wake": True})

        self.assertEqual(jobs.run("wisp", "watch"), 0)
        self.assertEqual(len(self.inbox()), 1)
        self.assertIn("one-line status body", self.inbox()[0])
        self.assertTrue(self.rows()[-1]["delivered"])
        self.assertNotIn("duplicate", self.rows()[-1])
        self.assertEqual(jobs.pending_wakes()["wisp"]["by"], ["watch"])
        jobs.take_wake("wisp")

        with mock.patch.object(jobs, "log") as log:
            self.assertEqual(jobs.run("wisp", "watch"), 0)
        self.assertEqual(len(self.inbox()), 1)
        second = self.rows()[-1]
        self.assertIs(second.get("duplicate"), True)
        self.assertIs(second.get("delivered"), False)
        self.assertEqual(jobs.entry("wisp", "watch").get("duplicates"), 1)
        self.assertEqual(jobs.pending_wakes(), {})
        self.assertTrue(any("same message as last time, not delivered" in c.args[0] for c in log.call_args_list))
        self.assertIn("1 duplicate(s) not delivered", jobs.describe("wisp"))

        (state / "msg.txt").write_text("different status body")
        self.assertEqual(jobs.run("wisp", "watch"), 0)
        self.assertEqual(len(self.inbox()), 2)
        self.assertIn("different status body", self.inbox()[-1])
        self.assertTrue(self.rows()[-1]["delivered"])
        self.assertNotIn("duplicate", self.rows()[-1])
        self.assertEqual(jobs.pending_wakes()["wisp"]["by"], ["watch"])
        jobs.take_wake("wisp")

        # Handed-over file bytes are part of the digest, and an empty-message wake keeps today's behaviour.
        (state / " attachment.txt".strip()).write_bytes(b"v1")
        self.assertIs(jobs._deliver_job("wisp", "watch", "with file", "", ["attachment.txt"]), True)
        self.assertEqual(jobs._deliver_job("wisp", "watch", "with file", "", ["attachment.txt"]), "duplicate")
        (state / "attachment.txt").write_bytes(b"v2")
        self.assertIs(jobs._deliver_job("wisp", "watch", "with file", "", ["attachment.txt"]), True)
        (state / "msg.txt").write_text("")
        self.assertEqual(jobs.run("wisp", "watch"), 0)
        self.assertIs(self.rows()[-1]["delivered"], False)
        self.assertNotIn("duplicate", self.rows()[-1])
        self.assertEqual(jobs.pending_wakes()["wisp"]["by"], ["watch"])


if __name__ == "__main__":
    unittest.main()
