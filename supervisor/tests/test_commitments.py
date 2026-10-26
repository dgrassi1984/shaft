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
        "source": "cycle", "at": NOW.isoformat()}
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
    {**MADE, "id": "c1026-1412-4", "due": "2026-11-26T16:00:00", "source": "talk", "made_to": "wren"},
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
        return agent.add(self.root, {**PROMISE, **changes}, "cycle", NOW)

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
        for recipient in ("self", "wren", "a_2", "0-peer"):
            self.assertFalse(self.add(made_to=recipient).startswith("ERROR:"))

    def test_invalid_add_never_writes(self):
        for changes in ({"promise": " "}, {"criterion": ""}, {"promise": "x" * 301}, {"due": "yesterday"},
                        {"due": "2026-10-25"}, {"due": "2026-10-26 14:11"}, {"made_to": "Wren"},
                        {"made_to": "../wren"}, {"criterion": []}):
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
                         {CID: "kept", "c1026-1412-3": "overdue", "c1026-1412-4": "open"})
        self.assertEqual(actual[CID], {"id": CID, **PROMISE, "due": MADE["due"], "source": "cycle",
                                      "made_at": NOW.isoformat(), "status": "kept", "settled_at": "2026-10-26T16:00:00",
                                      "evidence": "2026-10-26T15:00:00#1"})
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
                self.assertEqual(supervisor.verify("x", item), "verified")

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
            self.assertEqual(agent.settle(self.root, args, NOW + timedelta(days=3)),
                             f"settled {CID} as {status} (evidence The task changed.)")
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
        for kind in ("talk", "sleep"):
            self.write_episodes([episode(kind=kind)])
            self.assertEqual(supervisor.verify("x", item), "verified")
        self.write_episodes([])
        self.assertEqual(supervisor.verify("x", item), "no such episode")

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
        self.assertIn("made 2; kept (verified) 0", lines[3])
        self.assertEqual(lines[-1], "- Mean judged difficulty of cited cycles: 3.00/5 (n=1).")
        text = supervisor.report(["x"])
        self.assertIn(f"{CID}: kept; verified; to operator", text)
        self.assertIn("c1026-1412-3: overdue", text)
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


if __name__ == "__main__":
    unittest.main()
