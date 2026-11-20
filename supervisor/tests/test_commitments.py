"""Commitments tested against temporary records; both folds share the event fixtures below."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import commitments as agent
from supervisor import commitments as supervisor, metrics

NOW = datetime(2026, 10, 26, 14, 12)
PROMISE = {"promise": "Send the checked report", "due": "tomorrow", "criterion": "The operator has the report",
           "made_to": "operator"}
CID = "c1026-1412-1"
MADE = {"event": "made", "id": CID, **PROMISE, "due": "2026-10-27T23:59:59.999999",
        "source": "cycle", "at": NOW.isoformat(), "talk_id": "conversation-1"}
TALK = {"kind": "talk", "role": "user", "talk_id": "conversation-1", "ts": "2026-10-26T14:00:00"}
MSG = "20261026-140000-000000-from-x.md"   # a message x delivered to the operator (send_message's message_file)
FOLD_ROWS = [
    MADE, None, [], {}, {**MADE, "id": "bad"}, {**MADE, "id": "c1026-1412-2", "due": "bad"},
    {**MADE, "promise": "duplicate must not replace the first"},
    {"event": "settled", "id": CID, "status": "kept", "evidence": "2026-10-26T15:00:00#1", "at": "bad"},
    {"event": "settled", "id": CID, "status": "kept", "evidence": "", "at": "2026-10-26T16:00:00"},
    {"event": "settled", "id": CID, "status": "broken", "evidence": "Too early", "at": "2026-10-25T16:00:00"},
    {"event": "settled", "id": CID, "status": "kept", "evidence": "2026-10-26T15:00:00#1",
     "at": "2026-10-26T16:00:00"},
    {"event": "settled", "id": CID, "status": "broken", "evidence": "Already settled", "at": "2026-10-26T17:00:00"},
    {**MADE, "id": "c1026-1412-3", "due": "2026-10-26T16:00:00", "made_to": "self"},
    {**MADE, "id": "c1026-1412-4", "due": "2026-11-26T16:00:00", "source": "talk", "made_to": "wisp"},
    {**MADE, "id": "c1026-1412-5", "promise": []},
    {**MADE, "id": "c1026-1412-6", "made_to": "../outside"},
]


def episode(ts="2026-10-26T15:00:00", results=("done",), kind="live"):
    return {"ts": ts, "kind": kind, "intent": {}, "finish": {},
            "tools": [{"tool": "send_message", "args": {}, "result": r} for r in results]}


class CommitmentsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-commitments-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "x"
        (self.root / "memory/episodes").mkdir(parents=True)
        (self.root / "notes").mkdir()
        (self.root / "notes/operator-message.md").write_text("To: operator\n\nI will send the checked report.\n")
        (self.root / "memory/episodes/talk.jsonl").write_text(json.dumps(TALK) + "\n")   # the conversation of MADE
        self.sup = self.root.parent / "sup"
        (self.sup / "operator-inbox").mkdir(parents=True)
        (self.sup / "operator-inbox" / MSG).write_text(
            "From: x\nTo: operator\nKind: note\nDate: 2026-10-26 14:00\n\nI will send the checked report.\n")
        self.enterContext(mock.patch.object(supervisor, "SUP_DIR", self.sup))
        for module in (supervisor, metrics):
            patch = mock.patch.object(module, "INSTANCES", self.root.parent)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(metrics, "judgments", return_value=[])
        self.judgments = patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(supervisor, "datetime", wraps=datetime)
        patch.start().now.return_value = datetime(2026, 10, 28)
        self.addCleanup(patch.stop)

    def write_rows(self, rows):
        (self.root / agent.FILE).write_text("\n".join(json.dumps(r) for r in rows) + "\nnot JSON\n")

    def write_episodes(self, episodes):
        (self.root / "memory/episodes/day.jsonl").write_text("\n".join(json.dumps(ep) for ep in episodes) + "\n")

    def add(self, **changes):
        return agent.add(self.root, {**PROMISE, "talk_id": "conversation-1", **changes}, "cycle", NOW)

    def broker(self, replay_files=()):
        """The agent's broker.call answered by a real Broker's record (no socket); `replay_files`: a ReplayBroker's
        simulated sends."""
        from supervisor import broker
        box = broker.ReplayBroker.__new__(broker.ReplayBroker) if replay_files else broker.Broker.__new__(broker.Broker)
        box.name, box.as_of = "x", None
        if replay_files:
            box.operator_files = set(replay_files)
            box.as_of = datetime(2026, 10, 26, 13)
        return box, mock.patch.object(agent.broker, "call", side_effect=lambda tool, **args: box._record(**args))

    def test_due_parsing(self):
        end = datetime(2026, 10, 26, 23, 59, 59, 999999)
        self.assertEqual(agent.parse_due("today", NOW), end)
        self.assertEqual(agent.parse_due("tomorrow", NOW), end + timedelta(days=1))
        self.assertEqual(agent.parse_due("2026-10-26", NOW), end)
        self.assertEqual(agent.parse_due("2026-10-27 09:30", NOW), datetime(2026, 10, 27, 9, 30))
        self.assertEqual(agent.parse_due("tomorrow", datetime(2026, 12, 31)), datetime(2027, 1, 1, 23, 59, 59, 999999))
        for text in (None, "bad", "2026-02-30", "2026-10-26 25:00", "2026-10-26T14:00", "2026-1-1"):
            with self.subTest(text=text):
                self.assertIsNone(agent.parse_due(text, NOW))
        aware = NOW.replace(tzinfo=timezone.utc)
        local = aware.astimezone().replace(tzinfo=None)
        self.assertEqual(agent.parse_due("today", aware), local.replace(hour=23, minute=59, second=59, microsecond=999999))

    def test_I10_self_expires_at_due_plus_24_hours_in_both_folds(self):
        due = NOW + timedelta(hours=1)
        for recipient in ("self", "own", self.root.name):
            with self.subTest(recipient=recipient):
                self.write_rows([{**MADE, "made_to": recipient, "due": due.isoformat()}])
                for at, status in ((due, "open"), (due + timedelta(hours=24, microseconds=-1), "overdue"),
                                   (due + timedelta(hours=24), "expired"), (due + timedelta(days=7), "expired")):
                    item = agent.fold(self.root, at)[CID]
                    self.assertEqual(item["status"], status)
                    self.assertEqual(supervisor.ledger("x", at), agent.fold(self.root, at))
                self.assertEqual(item["settled_at"], (due + timedelta(hours=24)).isoformat())
                self.assertIn(CID, agent.fold(self.root, due + timedelta(days=7)))

    def test_I10_expiration_precedes_late_settlement_but_preserves_earlier_history(self):
        due = NOW + timedelta(hours=1)
        for at, status in ((due + timedelta(hours=23), "kept"), (due + timedelta(hours=24), "expired")):
            self.write_rows([{**MADE, "made_to": "self", "due": due.isoformat()},
                             {"event": "settled", "id": CID, "status": "kept", "evidence": "an episode",
                              "at": at.isoformat()}])
            self.assertEqual(agent.fold(self.root, due + timedelta(days=7))[CID]["status"], status)
            self.assertEqual(supervisor.ledger("x", due + timedelta(days=7)),
                             agent.fold(self.root, due + timedelta(days=7)))

    def test_I10_operator_requires_existing_source_and_refuses_without_one(self):
        # Grok's review: a note the instance wrote itself, headed "To: operator", is not a message to the operator.
        # #238: without a source the promise is refused at commit_to, never silently recorded as self.
        for refs in ({}, {"talk_id": "missing"}, {"message_file": "notes/missing.md"},
                     {"message_file": "notes/operator-message.md"},
                     {"message_file": "../outside.md"}, {"message_file": "/outside.md"},
                     {"inbox_file": "20261026-140000-000000-from-operator.md"},   # named, but never received
                     {"inbox_file": "../20261026-140000-000000-from-operator.md"}):
            with self.subTest(refs=refs):
                (self.root / agent.FILE).unlink(missing_ok=True)
                result = agent.add(self.root, {**PROMISE, **refs}, "cycle", NOW)
                self.assertTrue(result.startswith("ERROR: commitment not recorded: a promise to the operator needs "
                                                  "its source"), result)
                self.assertIn("send_message first", result)
                self.assertFalse((self.root / agent.FILE).exists())
        _, stub = self.broker()
        for refs in ({"talk_id": "conversation-1"}, {"message_file": MSG}):
            (self.root / agent.FILE).unlink(missing_ok=True)
            with stub:
                result = agent.add(self.root, {**PROMISE, **refs}, "talk", NOW)
            self.assertNotIn("recorded as self", result)
            self.assertEqual(agent.fold(self.root, NOW)[CID]["made_to"], "operator")
            self.assertEqual(supervisor.ledger("x", NOW), agent.fold(self.root, NOW))

    def test_I10_peer_promises_never_expire_and_invalid_sourced_operator_is_self(self):
        unsourced = {k: v for k, v in MADE.items() if k != "talk_id"}
        self.write_rows([{**MADE, "made_to": "wisp"},
                         {**unsourced, "id": "c1026-1412-2", "message_file": "missing.md", "sourced": True},
                         {**unsourced, "id": "c1026-1412-3", "sourced": True}])
        late = NOW + timedelta(days=7)
        items = agent.fold(self.root, late)
        self.assertEqual(items[CID]["status"], "overdue")
        for cid in ("c1026-1412-2", "c1026-1412-3"):
            self.assertEqual(items[cid]["made_to"], "self")
            self.assertEqual(items[cid]["status"], "expired")
        self.assertEqual(supervisor.ledger("x", late), items)

    def test_I10_operator_promise_made_before_the_source_rule_stays_the_operators(self):
        """18 open operator promises (shaft 8, wisp 6, tundra 4) predate the rule: reclassifying them as self would
        expire them unseen (integration review, 2026-11-03)."""
        legacy = {k: v for k, v in MADE.items() if k != "talk_id"}
        self.write_rows([legacy])
        late = NOW + timedelta(days=7)
        items = agent.fold(self.root, late)
        self.assertEqual(items[CID]["made_to"], "operator")
        self.assertEqual(items[CID]["status"], "overdue")
        self.assertEqual(supervisor.ledger("x", late), items)

    def test_I10_delivered_message_source_is_checked_by_broker_without_delivery(self):
        file, path = MSG, self.sup / "operator-inbox" / MSG
        box, stub = self.broker()
        with stub as call:
            result = agent.add(self.root, {**PROMISE, "message_file": file}, "cycle", NOW)
            self.assertNotIn("recorded as self", result)
            self.assertEqual(agent.fold(self.root, NOW)[CID]["made_to"], "operator")
            self.assertEqual(agent.fold(self.root, NOW), supervisor.ledger("x", NOW))
            call.assert_called_with("record", kind="commitment_source", cycle=file)
            box.as_of = datetime(2026, 10, 26, 13)
            self.assertEqual(box._record("commitment_source", cycle=file), "")
            box.as_of = None
            self.assertEqual(box._record("commitment_source", cycle=file.replace("from-x", "from-wisp")), "")
            (self.sup / "operator-inbox/read").mkdir()
            path.rename(self.sup / "operator-inbox/read" / file)   # messages._archive: the operator read or answered it
            self.assertEqual(box._record("commitment_source", cycle=file), "To: operator\n\n")
            self.assertEqual(supervisor.ledger("x", NOW)[CID]["made_to"], "operator")
            (self.sup / "operator-inbox/read" / file).unlink()
            self.assertEqual(box._record("commitment_source", cycle=file), "")
            self.assertEqual(supervisor.ledger("x", NOW)[CID]["made_to"], "self")

    def test_I10_archived_operator_message_still_sources_its_promise(self):
        """An operator promise must not turn into an expiring self record once the operator reads the message."""
        self.write_rows([{**{k: v for k, v in MADE.items() if k != "talk_id"}, "message_file": MSG}])
        (self.sup / "operator-inbox/read").mkdir()
        (self.sup / "operator-inbox" / MSG).rename(self.sup / "operator-inbox/read" / MSG)
        late = NOW + timedelta(days=7)
        self.assertEqual(supervisor.ledger("x", late)[CID]["made_to"], "operator")
        self.assertEqual(supervisor.ledger("x", late)[CID]["status"], "overdue")
        self.assertEqual(supervisor.ledger("x", late), agent.fold(self.root, late))

    def test_I10_broker_outage_is_an_error_not_a_downgrade(self):
        """Grok's review: a BrokerError read as "no source" wrote the promise as self."""
        with mock.patch.object(agent.broker, "call", side_effect=agent.broker.BrokerError("offline")):
            result = agent.add(self.root, {**PROMISE, "message_file": MSG}, "cycle", NOW)
        self.assertTrue(result.startswith("ERROR: commitment not recorded"), result)
        self.assertIn("offline", result)
        self.assertFalse((self.root / agent.FILE).exists())

    def test_I10_replay_send_is_a_source_inside_the_replay_only(self):
        """Grok's review: a trial replay's simulated send_message wrote no file, so its promise became self in the
        replay while the live cycle kept it as operator."""
        simulated = "20261026-140500-000000-from-x.md"   # after the replay's as_of: only the replay's own send counts
        box, stub = self.broker(replay_files=[simulated])
        with stub:
            self.assertNotIn("recorded as self", agent.add(self.root, {**PROMISE, "message_file": simulated}, "cycle", NOW))
        self.assertEqual(agent.fold(self.root, NOW)[CID]["made_to"], "operator")
        self.assertFalse((self.sup / "operator-inbox" / simulated).exists())
        self.assertEqual(box._record("commitment_source", cycle=MSG), "")   # a real message newer than the replay
        box.operator_files = set()
        self.assertEqual(box._record("commitment_source", cycle=simulated), "")

    def test_I10_model_cannot_cite_a_past_conversation_or_file_in_finish_cycle(self):
        """Grok's review: finish_cycle took a model-supplied talk_id, so any past conversation sourced a promise."""
        self.assertEqual(agent.clean([{**PROMISE, "talk_id": "conversation-1", "message_file": MSG}]), [PROMISE])

    def test_I10_tools_attach_current_talk_or_delivered_message_source(self):
        from shaft.tools import Toolbox
        self.write_episodes([{"kind": "talk", "role": "user", "talk_id": "current-talk", "ts": NOW.isoformat()}])
        box = Toolbox(self.root)
        box.commit_source, box.commit_talk_id = "talk", "current-talk"
        with mock.patch("shaft.clock.now", return_value=NOW):
            self.assertNotIn("recorded as self", box.run("commit_to", json.dumps(PROMISE)))
        self.assertEqual(json.loads((self.root / agent.FILE).read_text())["talk_id"], "current-talk")
        file = MSG
        log = [{"tool": "send_message", "result": f"delivered to the operator (note); message_file {file}"}]
        self.assertEqual(agent.message_source(log), {"message_file": file})
        self.assertEqual(agent.message_source([{"tool": "send_message", "result": "ERROR: refused"}]), {})

    def test_I10_send_message_result_supplies_the_existing_commitment_source_path(self):
        from supervisor import broker
        box = broker.Broker.__new__(broker.Broker)
        box.name, box.project, box.counts = "x", self.root.parent, {"send_message": 1}
        box.as_of, box.root = None, self.root             # set by Broker.__init__; __new__ skips it
        with mock.patch.object(broker, "datetime", wraps=datetime) as clock, \
                mock.patch.object(box, "_read_attachments", return_value=[]), \
                mock.patch.object(box, "_operator_sent_today", return_value=0), \
                mock.patch.object(box, "_store", return_value=[]), \
                mock.patch.object(box, "_deliver") as deliver, \
                mock.patch.object(box, "_push_usage", return_value=None):
            clock.now.return_value = NOW
            result = box._send_message("operator", "I will send the checked report.")
        file = "20261026-141200-000000-from-x.md"
        self.assertEqual(agent.message_source([{"tool": "send_message", "result": result}]), {"message_file": file})
        self.assertEqual(deliver.call_args.args[:2], ("operator", file))

    def test_I10_wrong_recipient_speaker_and_link_are_not_operator_sources(self):
        (self.root / "notes/peer.md").write_text("To: wisp\n\nTo: operator\n")
        (self.root / "notes/link.md").symlink_to(self.root / "notes/operator-message.md")
        forged = "20261026-140000-000000-from-x.md"
        (self.root / forged).write_text("To: operator\n\nA local file is not a delivered message.\n")
        self.write_episodes([{"kind": "talk", "role": "user", "talk_id": "coder", "speaker": "Claude",
                              "ts": NOW.isoformat()}])
        (self.sup / "operator-inbox" / MSG).unlink()   # only the local copy exists: never delivered
        _, stub = self.broker()
        for refs in ({"message_file": "notes/peer.md"}, {"message_file": "notes/link.md"}, {"talk_id": "coder"},
                     {"message_file": forged}, {"message_file": forged.replace("from-x", "from-wisp")}):
            with self.subTest(refs=refs), stub:
                (self.root / agent.FILE).unlink(missing_ok=True)
                self.assertIn("needs its source", agent.add(self.root, {**PROMISE, **refs}, "talk", NOW))
                self.assertFalse((self.root / agent.FILE).exists())

    def operator_inbox_message(self, file, sender="operator", logged=True):
        """A message the supervisor delivered to x's inbox (and logged), as messages._deliver or watch._deliver do."""
        (self.root / "inbox").mkdir(exist_ok=True)
        (self.root / "inbox" / file).write_text(f"From: {sender}\nTo: x\nKind: note\nDate: 2026-10-26 14:00\n\nDo it.\n")
        if logged:
            log = self.sup / "messages.jsonl"
            with log.open("a") as out:
                out.write(json.dumps({"ts": "2026-10-26T14:00:00", "from": sender, "to": "x", "kind": "note",
                                      "file": file, "text": "Do it."}) + "\n")

    def test_238_cycle_promise_answering_operator_inbox_message_stays_the_operators(self):
        """#238: a promise made in a cycle that an operator message, chat or mail woke was relabelled as made to
        itself and expired 24 h after due, unseen ("overdue 0" while it lapsed). Reverting the inbox source makes
        these rows self again, and refused at commit_to."""
        from supervisor import messages
        self.enterContext(mock.patch.object(messages, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(messages, "INSTANCES", self.root.parent))
        self.enterContext(mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"))
        self.enterContext(mock.patch.object(messages, "_cache", {"key": None, "rows": []}))
        for n, (file, sender) in enumerate((("20261026-140000-000001-from-operator.md", "operator"),
                                            ("20261026-140000-000002-from-supervisor-gchat-watch.md",
                                             "supervisor (gchat watch)"),
                                            ("20261026-140000-000003-from-supervisor-mail-watch.md",
                                             "supervisor (mail watch)")), 1):
            with self.subTest(sender=sender):
                (self.root / agent.FILE).unlink(missing_ok=True)
                self.operator_inbox_message(file, sender)
                self.assertEqual(agent.cycle_source([], ["20261026-130000-000000-from-job-x.md", file]),
                                 {"inbox_file": file})
                result = agent.add(self.root, {**PROMISE, "inbox_file": file}, "cycle", NOW)
                self.assertEqual(result, "commitment c1026-1412-1 recorded, due tomorrow")
                late = NOW + timedelta(days=7)
                item = supervisor.ledger("x", late)[CID]
                self.assertEqual((item["made_to"], item["status"]), ("operator", "overdue"))
                self.assertEqual(supervisor.ledger("x", late), agent.fold(self.root, late))
                # archived after the cycle (live.archive_inbox): still the operator's in both folds
                (self.root / "memory/messages").mkdir(exist_ok=True)
                (self.root / "inbox" / file).rename(self.root / "memory/messages" / file)
                self.assertEqual(agent.fold(self.root, late)[CID]["made_to"], "operator")
                self.assertEqual(supervisor.ledger("x", late), agent.fold(self.root, late))
        # neither a calendar watch, nor a job, nor a file the supervisor never delivered sources a promise
        for file, sender, logged in (("20261026-140000-000004-from-supervisor-calendar-watch.md",
                                      "supervisor (calendar watch)", True),
                                     ("20261026-140000-000005-from-job-x.md", "job x", True),
                                     ("20261026-140000-000006-from-operator.md", "operator", False)):
            with self.subTest(file=file):
                (self.root / agent.FILE).unlink(missing_ok=True)
                self.operator_inbox_message(file, sender, logged)
                self.assertEqual(agent.cycle_source([], [file]), {} if "operator" not in file else {"inbox_file": file})
                if "operator" in file:   # the agent sees its own inbox: the supervisor's log is what counts
                    self.write_rows([{**MADE, "talk_id": None, "inbox_file": file, "sourced": True}])
                    self.assertEqual(supervisor.ledger("x", NOW)[CID]["made_to"], "self")

    def test_238_rows_recorded_as_self_before_the_fix_return_to_the_operator(self):
        """#238: six promises this week were made in cycles that answered the operator (his chat in the inbox, or a
        message delivered to him after commit_to) and recorded as self. The fold gives them back to the operator."""
        legacy = {k: v for k, v in MADE.items() if k != "talk_id"} | {"sourced": True}
        at = datetime.fromisoformat(MADE["at"])
        cycles = [{**episode((at + timedelta(minutes=5)).isoformat(), ()),
                   "inbox": ["20261026-140000-000000-from-supervisor-gchat-watch.md"]},
                  {**episode((at + timedelta(hours=1, minutes=5)).isoformat(),
                             ("delivered to the operator (note); message_file " + MSG,)), "inbox": []},
                  {**episode((at + timedelta(hours=2, minutes=5)).isoformat(), ("ERROR: refused",)),
                   "inbox": ["20261026-150000-000000-from-job-x.md"]}]
        self.write_episodes(cycles)
        # what commit_to wrote then: the relabel in the row itself
        legacy = {**legacy, "made_to": "self", "note": "operator source missing or invalid; recorded as self"}
        self.write_rows([legacy, {**legacy, "id": "c1026-1412-2", "at": (at + timedelta(hours=1)).isoformat()},
                         {**legacy, "id": "c1026-1412-3", "at": (at + timedelta(hours=2)).isoformat()},
                         {**legacy, "id": "c1026-1412-4", "at": (at + timedelta(hours=9)).isoformat(),
                          "due": "2026-10-27T23:59:59.999999"}])   # no cycle episode within CYCLE_SPAN
        late = NOW + timedelta(days=7)
        items = agent.fold(self.root, late)
        self.assertEqual({cid: it["made_to"] for cid, it in items.items()},
                         {CID: "operator", "c1026-1412-2": "operator", "c1026-1412-3": "self", "c1026-1412-4": "self"})
        self.assertEqual(items[CID]["status"], "overdue")
        self.assertEqual(supervisor.ledger("x", late), items)

    def test_238_commit_to_tool_and_finish_cycle_attach_the_cycle_inbox_source(self):
        from shaft.tools import Toolbox
        file = "20261026-140000-000001-from-operator.md"
        self.operator_inbox_message(file)
        box = Toolbox(self.root)
        box.commit_source, box.commit_inbox = "cycle", ["20261026-130000-000000-from-job-x.md", file]
        with mock.patch("shaft.clock.now", return_value=NOW):
            self.assertEqual(box.run("commit_to", json.dumps(PROMISE)), "commitment c1026-1412-1 recorded, due tomorrow")
        self.assertEqual(json.loads((self.root / agent.FILE).read_text())["inbox_file"], file)
        box.commit_inbox = []
        with mock.patch("shaft.clock.now", return_value=NOW):
            self.assertIn("needs its source", box.run("commit_to", json.dumps(PROMISE)))

    def test_add_and_ids_and_append(self):
        self.assertEqual(self.add(), "commitment c1026-1412-1 recorded, due tomorrow")
        before = (self.root / agent.FILE).read_text()
        self.assertEqual(self.add(promise="  Send\n report  "), "commitment c1026-1412-2 recorded, due tomorrow")
        self.assertTrue((self.root / agent.FILE).read_text().startswith(before))
        items = agent.fold(self.root, NOW)
        self.assertEqual(list(items), [CID, "c1026-1412-2"])
        self.assertEqual(items["c1026-1412-2"]["promise"], "Send report")
        self.assertEqual(items[CID]["due"], "2026-10-27T23:59:59.999999")
        self.assertEqual(items[CID]["status"], "open")
        with mock.patch("shaft.broker.call", return_value="instances: wisp, a_2, 0-peer"):
            for recipient in ("wisp", "a_2", "0-peer"):
                self.assertFalse(self.add(made_to=recipient).startswith("ERROR:"))

    def test_self_audit_loop_ledger_keeps_only_promises_to_others(self):
        """Commitments to self fed the self-audit loop; the ledger keeps only promises to others."""
        from shaft import broker
        with mock.patch.object(broker, "call", return_value="instances: wisp, a_2, 0-peer") as peers:
            for recipient in ("self", self.root.name, "unknown"):
                with self.subTest(recipient=recipient):
                    result = self.add(made_to=recipient)
                    self.assertTrue(result.startswith("ERROR:"), result)
                    self.assertIn("goals/notes", result)
                    self.assertFalse((self.root / agent.FILE).exists())
            self.assertFalse(self.add().startswith("ERROR:"))
            for recipient in ("wisp", "a_2", "0-peer"):
                self.assertFalse(self.add(made_to=recipient).startswith("ERROR:"))
            peers.assert_called_with("list_instances")
        with mock.patch.object(broker, "call", side_effect=broker.BrokerError("offline")):
            self.assertIn("ERROR:", self.add(made_to="wisp"))
            self.assertFalse(self.add().startswith("ERROR:"))   # a conversation source needs no broker
            self.assertIn("ERROR:", self.add(talk_id="", message_file=MSG))

    def test_commit_to_tool_refuses_self_and_accepts_operator_and_peer(self):
        """Commitments to self fed the self-audit loop; the ledger keeps only promises to others."""
        from shaft.tools import Toolbox
        box = Toolbox(self.root)
        box.commit_source, box.commit_talk_id = "talk", "conversation-1"
        with mock.patch("shaft.clock.now", return_value=NOW), \
                mock.patch("shaft.broker.call", return_value="instances: wisp"):
            self.assertIn("goals/notes", box.run("commit_to", json.dumps({**PROMISE, "made_to": "self"})))
            self.assertFalse((self.root / agent.FILE).exists())
            for recipient in ("operator", "wisp"):
                result = box.run("commit_to", json.dumps({**PROMISE, "made_to": recipient}))
                self.assertTrue(result.startswith("commitment c"), result)
        self.assertEqual([it["made_to"] for it in agent.fold(self.root, NOW).values()], ["operator", "wisp"])

    def test_self_audit_loop_legacy_self_hidden_from_prompt_sleep_and_recall(self):
        """Old self promises still fold, but only promises to others reach the agent."""
        from shaft import prompts, sleep, workspace
        rows = [{**MADE, "promise": "operator report"},
                {**MADE, "id": "c1026-1412-2", "made_to": "self", "promise": "self audit"},
                {**MADE, "id": "c1026-1412-3", "made_to": self.root.name, "promise": "own name audit"}]
        self.write_rows(rows)
        before = (self.root / agent.FILE).read_text()
        late = NOW + timedelta(days=3)
        cfg = type("Config", (), {"COMMITMENTS": "on"})()
        with mock.patch.object(prompts, "now", return_value=late):
            shown = prompts._promises(self.root, cfg)
        with mock.patch("shaft.clock.now", return_value=late):
            sleep_shown = sleep._overdue(type("State", (), {"root": self.root})())
        for text in (shown, agent.sleep_text(self.root, late),
                     sleep_shown,
                     "\n".join(it.text for it in workspace._commitments(self.root, late))):
            self.assertIn("operator report", text)
            self.assertNotIn("self audit", text)
            self.assertNotIn("own name audit", text)
        self.assertEqual(len(agent.fold(self.root, late)), 3)
        self.assertEqual([it["id"] for it in agent.overdue(self.root, late)], [CID])
        self.assertIn("ERROR:", agent.settle(self.root, {"id": "c1026-1412-2", "status": "void",
                                                        "evidence": "obsolete"}, late))
        self.assertEqual((self.root / agent.FILE).read_text(), before)
        self.assertIn("to self (no longer kept): 2", supervisor.report(["x"]))
        self.assertNotIn("self", "\n".join(supervisor.section("x")))   # stats.md is in my prompt: no reminder of them

    def test_invalid_add_never_writes(self):
        for changes in ({"promise": " "}, {"criterion": ""}, {"promise": "x" * 301}, {"due": "yesterday"},
                        {"due": "2026-10-25"}, {"due": "2026-10-26 14:11"}, {"made_to": "Wisp"},
                        {"made_to": "../wisp"}, {"criterion": []}):
            with self.subTest(changes=changes):
                self.assertTrue(self.add(**changes).startswith("ERROR:"))
        self.assertTrue(agent.add(self.root, PROMISE, "sleep", NOW).startswith("ERROR:"))
        self.assertFalse((self.root / agent.FILE).exists())
        self.assertFalse(self.add(due="today").startswith("ERROR:"))
        self.assertFalse(self.add(due="2026-10-26 14:12").startswith("ERROR:"))

    def test_clean(self):
        values = [PROMISE, None, [], {}, {**PROMISE, "due": "bad"}, {**PROMISE, "made_to": "A"},
                  {**PROMISE, "promise": " "}, {**PROMISE, "criterion": 3}, {**PROMISE, "promise": "x" * 301},
                  {**PROMISE, "promise": "  Send\n report  ", "extra": True}]
        self.assertEqual(agent.clean(values), [PROMISE, {**PROMISE, "promise": "Send report"}])
        self.assertEqual(agent.clean(PROMISE), [PROMISE])
        self.assertEqual(agent.clean("bad"), [])

    def test_shared_fold_fixture(self):
        original = copy.deepcopy(FOLD_ROWS)
        self.write_rows(FOLD_ROWS)
        now = datetime(2026, 10, 28)
        actual = agent.fold(self.root, now)
        self.assertEqual(supervisor.ledger("x", now), actual)
        self.assertEqual({cid: it["status"] for cid, it in actual.items()},
                         {CID: "kept", "c1026-1412-3": "expired", "c1026-1412-4": "open"})
        self.assertEqual(actual[CID], {"id": CID, **PROMISE, "due": MADE["due"], "source": "cycle",
                                      "made_at": NOW.isoformat(), "status": "kept", "settled_at": "2026-10-26T16:00:00",
                                      "evidence": "2026-10-26T15:00:00#1", "talk_id": "conversation-1"})
        self.assertEqual(FOLD_ROWS, original)

    def test_overdue_and_prompt_limit(self):
        self.assertEqual(agent.block(self.root, NOW), "(none)")
        self.assertEqual(agent.sleep_text(self.root, NOW), "(none overdue)")
        for n in range(8):
            self.add(due=f"2026-10-{27 + n:02d}" if n < 5 else f"2026-11-{n - 4:02d}")
        late = datetime(2026, 11, 6)
        self.assertEqual(len(agent.overdue(self.root, late)), 8)
        self.assertEqual(len(agent.block(self.root, late).splitlines()), 6)
        self.assertEqual(len(agent.sleep_text(self.root, late).splitlines()), 8)
        self.assertEqual(agent.block(self.root, datetime(2026, 10, 28)).splitlines()[0],
                         "- c1026-1412-1 (to operator, due 2026-10-27 — overdue by 1 day): "
                         "Send the checked report — check: The operator has the report")
        boundary = datetime(2026, 10, 27, 23, 59, 59, 999999)
        self.assertEqual(agent.overdue(self.root, boundary), [])
        self.assertEqual([it["id"] for it in agent.overdue(self.root, boundary + timedelta(microseconds=1))], [CID])

    def test_settle_rejects_bad_evidence(self):
        self.add()
        cases = [([], "2026-10-26T15:00:00", "no such episode"),
                 ([episode(ts="2026-10-26T14:11:00")], "2026-10-26T14:11:00", "did not end after"),
                 ([episode(ts=NOW.isoformat())], NOW.isoformat(), "did not end after"),
                 ([episode(results=("ERROR: failed",))], "2026-10-26T15:00:00#1", "no successful"),
                 ([episode(results=("(held, not run yet) cards",))], "2026-10-26T15:00:00#1", "no successful"),
                 ([episode(results=("(repeat: not run)",))], "2026-10-26T15:00:00", "no successful"),
                 ([episode()], "2026-10-26T15:00:00#2", "no such tool-log step"),
                 ([episode()], "2026-10-26T15:00:00#0", "cite an episode"),
                 ([episode(results=(None,))], "2026-10-26T15:00:00", "no successful"),
                 ([episode(), episode(ts="2026-10-26T15:00:30")], "2026-10-26 15:00", "ambiguous")]
        before = (self.root / agent.FILE).read_text()
        for episodes, evidence, reason in cases:
            with self.subTest(evidence=evidence, reason=reason):
                self.write_episodes(episodes)
                result = agent.settle(self.root, {"id": CID, "status": "kept", "evidence": evidence}, NOW + timedelta(hours=2))
                self.assertTrue(result.startswith("ERROR:"), result)
                self.assertIn(reason, result)
                item = {**agent.fold(self.root, NOW)[CID], "status": "kept", "evidence": evidence}
                self.assertIn(reason, supervisor.verify("x", item))
                self.assertEqual((self.root / agent.FILE).read_text(), before)

    def test_kept_with_step_or_any_good_call(self):
        for evidence in ("2026-10-26T15:00:00#2", "2026-10-26T15:00:00", "2026-10-26 15:00", "2026-10-26T15:00#2"):
            with self.subTest(evidence=evidence):
                (self.root / agent.FILE).unlink(missing_ok=True)
                self.add()
                self.write_episodes([episode(results=("ERROR: bad", "delivered"))])
                result = agent.settle(self.root, {"id": CID, "status": "kept", "evidence": evidence}, NOW + timedelta(hours=2))
                self.assertEqual(result, f"settled {CID} as kept (evidence {evidence})")
                item = agent.fold(self.root, NOW + timedelta(days=3))[CID]
                self.assertEqual(item["status"], "kept")
                self.assertEqual(supervisor.verify("x", item), "cited cycle not judged")
                self.judgments.return_value = [{"episode_ts": "2026-10-26T15:00:00", "criterion_met": "yes"}]
                self.assertEqual(supervisor.verify("x", item), "verified")
                self.judgments.return_value = []

    def test_the_promise_itself_is_no_evidence(self):
        """Regression (Grok's review): commit_to's own "recorded" made the committing cycle evidence it was kept."""
        self.add()
        self.write_episodes([{"ts": "2026-10-26T15:00:00", "kind": "live", "intent": {}, "finish": {},
                              "tools": [{"tool": "commit_to", "args": {}, "result": f"commitment {CID} recorded"},
                                        {"tool": "finish_cycle", "args": {}, "result": "cycle finished"}]}])
        result = agent.settle(self.root, {"id": CID, "status": "kept", "evidence": "2026-10-26T15:00:00"},
                              NOW + timedelta(hours=2))
        self.assertEqual(result, "ERROR: cited evidence has no successful tool call")

    def test_other_settlements_and_validation(self):
        self.add()
        for args in ({"id": "missing", "status": "broken", "evidence": "why"},
                     {"id": CID, "status": "open", "evidence": "why"},
                     {"id": CID, "status": "broken", "evidence": " "}):
            self.assertTrue(agent.settle(self.root, args, NOW).startswith("ERROR:"))
        for status in ("broken", "superseded", "void"):
            (self.root / agent.FILE).unlink()
            self.add()
            args = {"id": CID, "status": status, "evidence": "The task changed."}
            self.assertTrue(agent.settle(self.root, args, NOW - timedelta(seconds=1)).startswith("ERROR:"))
            self.assertTrue(agent.settle(self.root, args, NOW + timedelta(days=3)).startswith(
                f"settled {CID} as {status} (evidence The task changed.)"))
            self.assertTrue(agent.settle(self.root, args, NOW + timedelta(days=3)).startswith("ERROR:"))
            self.assertEqual(agent.fold(self.root, NOW + timedelta(days=4))[CID]["status"], status)
            self.assertEqual(supervisor.ledger("x", NOW + timedelta(days=4)),
                             agent.fold(self.root, NOW + timedelta(days=4)))

    def test_verify_judge_and_talk_sleep(self):
        self.add()
        self.write_episodes([episode()])
        agent.settle(self.root, {"id": CID, "status": "kept", "evidence": "2026-10-26T15:00:00#1"}, NOW + timedelta(hours=2))
        item = agent.fold(self.root, NOW)[CID]
        for verdict, expected in (("no", "cited cycle judged no"), ("yes", "verified"), ("partial", "verified")):
            self.judgments.return_value = [{"episode_ts": "2026-10-26T15:00:00", "criterion_met": verdict}]
            self.assertEqual(supervisor.verify("x", item), expected)
        for kind in ("talk", "sleep"):   # no judge grades these: kept, not verified
            self.write_episodes([episode(kind=kind)])
            self.assertEqual(supervisor.verify("x", item), "cited a conversation or a sleep: no judge graded it")
        self.write_episodes([])
        self.assertEqual(supervisor.verify("x", item), "no such episode")

    def test_stats_head_cut_prioritizes_commitments_value_and_failure_modes(self):
        from supervisor.tests.test_measurement import judgment

        self.write_rows([MADE])
        self.judgments.return_value = [
            {**judgment("2026-10-26T15:00:00", "no", 3), "failure_mode": "Report was not sent"}]
        # Render the real commitment section; isolate unrelated sections from runtime data.
        sections = metrics._sections
        with mock.patch.object(metrics, "_sections", side_effect=lambda name, which:
                               sections(name, tuple(m for m in which if m == "commitments"))), \
             mock.patch.object(metrics, "SUP_DIR", self.sup):
            text = metrics.stats_text("x")
        titles = ("## Promises I made and kept", "## What my cycles were worth",
                  "## Recent failure modes", "## What I achieved", "## Success against my own criteria (raw)")
        for title in titles:
            self.assertEqual(text.count(title), 1)
        for before, after in zip(titles, titles[1:]):
            self.assertLess(text.index(before), text.index(after))
        self.assertIn("Report was not sent", text)

    def test_supervisor_safe_reads_and_reports(self):
        self.assertEqual(supervisor.section("x"), [])
        self.assertEqual(supervisor.report(["x"]), "x: commitments\n  (none)")
        self.write_rows(FOLD_ROWS)
        self.write_episodes([episode()])
        self.judgments.return_value = [{"episode_ts": "2026-10-26T15:00:00", "criterion_met": "yes", "difficulty": 3}]
        lines = supervisor.section("x")
        self.assertLessEqual(len(lines), 8)
        self.assertEqual(lines[0], "## Promises I made and kept")
        self.assertIn("made 1; kept (verified) 1; kept (not verified) 0", lines[2])
        self.assertIn("made 1; kept (verified) 0", lines[3])
        self.assertEqual(lines[-1], "- Mean judged difficulty of cited cycles: 3.00/5 (n=1).")
        text = supervisor.report(["x"])
        self.assertIn(f"{CID}: kept; verified; to operator", text)
        self.assertIn("to self (no longer kept): 1", text)
        self.assertNotIn("c1026-1412-3: overdue", text)
        self.judgments.return_value[0]["criterion_met"] = "no"
        self.assertIn("kept (not verified) 1", supervisor.section("x")[2])
        path = self.root / agent.FILE
        target = self.root.parent / "ledger.jsonl"
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(supervisor.ledger("x", NOW), {})
        path.unlink()
        (self.root / "memory/episodes/day.jsonl").unlink()
        (self.root / "memory/episodes/day.jsonl").symlink_to(target)
        self.assertEqual(supervisor.verify("x", {"status": "kept", "made_at": NOW.isoformat(),
                                                "evidence": "2026-10-26T15:00:00"}), "no such episode")
        self.assertEqual(supervisor.ledger("../x"), {})


    # -- #239: a promise to the operator settled as broken reached him only when he asked, the next day; the
    # instance's next message, five minutes after the settlement, was about something else.
    def notices(self):
        from supervisor import messages
        self.messages = messages
        self.enterContext(mock.patch.object(messages, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(messages, "INSTANCES", self.root.parent))
        self.enterContext(mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"))
        self.enterContext(mock.patch.object(messages, "_cache", {"key": None, "rows": []}))
        self.sent = []
        return self

    def said(self, at, text):
        with (self.sup / "messages.jsonl").open("a") as out:
            out.write(json.dumps({"ts": at, "from": "x", "to": "operator", "kind": "note",
                                  "file": f"{at.replace(':', '').replace('-', '')}-from-x.md", "text": text}) + "\n")

    def tell(self, now):
        return supervisor.tell_broken("x", self.sent.append, now)

    def broken(self, cid=CID, made_to="operator", at="2026-10-26T18:16:00"):
        return [{**MADE, "id": cid, "made_to": made_to},
                {"event": "settled", "id": cid, "status": "broken", "evidence": "The change is held by the gate.",
                 "at": at}]

    def test_239_broken_operator_promise_with_no_message_naming_it_is_told_once(self):
        self.notices()
        self.assertEqual(self.tell(datetime(2026, 10, 26, 15)), [])            # the first run marks where it starts
        self.write_rows(self.broken())
        self.said("2026-10-26T18:21:00", "Here is the recap you asked for.")    # after it, but about something else
        self.assertEqual(self.tell(datetime(2026, 10, 26, 18, 30)), [CID])
        self.assertEqual(len(self.sent), 1)
        self.assertIn(f"Promise {CID} to you is broken: Send the checked report", self.sent[0])
        self.assertIn("What blocks it: The change is held by the gate.", self.sent[0])
        self.assertIn("New estimate: none given", self.sent[0])
        self.assertEqual(self.tell(datetime(2026, 10, 27, 6, 15)), [])          # never twice
        self.assertEqual(len(self.sent), 1)

    def test_239_instance_message_naming_the_promise_is_enough_and_old_or_other_breaks_are_not_told(self):
        self.notices()
        self.tell(datetime(2026, 10, 26, 15))
        self.write_rows(self.broken() + self.broken("c1026-1412-2", "wisp")
                        + self.broken("c1026-1412-3", at="2026-10-26T14:00:00")       # before the record began
                        + self.broken("c1026-1412-4"))
        self.said("2026-10-26T18:20:00", f"{CID} is late: the gate held it; new estimate tomorrow 12:00.")
        self.said("2026-10-26T18:00:00", "c1026-1412-4 will be late.")                # before it was settled
        self.assertEqual(self.tell(datetime(2026, 10, 26, 18, 30)), ["c1026-1412-4"])
        state = json.loads((self.sup / supervisor.TOLD_DIR / "x.json").read_text())
        self.assertEqual(state["told"], {CID: "instance", "c1026-1412-4": "supervisor"})

    def test_239_runner_delivers_urgent_from_the_instance_and_tells_the_instance(self):
        self.notices()
        from supervisor import runner
        broker = mock.Mock()
        with mock.patch.object(supervisor, "tell_broken", side_effect=lambda name, deliver: deliver("Promise c1 broke.")
                               or ["c1"]), mock.patch.object(runner, "INSTANCES", self.root.parent), \
                mock.patch.object(runner.core, "SUP_DIR", self.sup), \
                mock.patch.object(self.messages, "record") as record:
            (self.root / "inbox").mkdir(exist_ok=True)
            runner._tell_broken("x", broker)
        to, file, content, kind, _, text = broker._deliver.call_args.args
        self.assertEqual((to, kind, text, broker._deliver.call_args.kwargs), ("operator", "note", "Promise c1 broke.",
                                                                               {"urgent": True}))
        self.assertTrue(file.endswith("-from-x.md") and content.startswith("From: x\nTo: operator\n"))
        note, = (self.root / "inbox").glob("*-from-supervisor-promises.md")
        self.assertIn("I told the operator: Promise c1 broke.", note.read_text())
        self.assertEqual(record.call_args.args[2:4], ("supervisor (promises)", "x"))

    def test_239_settle_broken_asks_for_a_message_naming_the_promise(self):
        self.write_rows([MADE])
        result = agent.settle(self.root, {"id": CID, "status": "broken", "evidence": "Held by the gate"}, NOW)
        self.assertIn(f"Tell the operator now with send_message, in one line naming {CID}", result)
        self.write_rows([MADE])
        result = agent.settle(self.root, {"id": CID, "status": "broken", "evidence": "Held"}, NOW, can_send=False)
        self.assertIn("The supervisor tells the operator after this sleep", result)

if __name__ == "__main__":
    unittest.main()
