"""Regressions for live controls, exam isolation, and historical withdrawals."""
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import attention, battery, broker, core, gate_held, surprise


class ReviewFollowupsTest(unittest.TestCase):
    def test_attention_does_not_count_live_settlement_as_off_target(self):
        ep = {"intent": {"attention": [{"source": "mail"}]},
              "tools": [{"tool": "mail_search", "args": "{}"},
                        {"tool": "settle_commitment", "args": "{}"}]}
        score = attention.score(ep)
        self.assertEqual((score["calls"], score["on_named"], score["share"]), (1, 1, 1.0))

    def test_exam_disables_withdrawal_and_standing_orders_on_live_instance(self):
        self.assertTrue({"withdraw_held", "record_order"} <= battery.EXAM_OFF)

    def test_withdrawal_is_inward_in_supervisor_mirror(self):
        self.assertTrue(surprise.inward({"observed": "withdraw_held accepted the request"}))

    def test_replay_withdrawal_uses_historical_gate_record_and_never_current_pending(self):
        rows = [
            {"ts": "2026-10-01T09:00:00", "outcome": "held", "held": "old", "files": ["shaft/old.py", "config.env"]},
            {"ts": "2026-10-01T09:01:00", "outcome": "held", "held": "gone", "files": ["shaft/gone.py"]},
            {"ts": "2026-10-01T09:02:00", "outcome": "re-gated", "held": "gone", "result": "withdrawn"},
            {"ts": "2026-10-01T09:03:00", "outcome": "held", "held": "kept", "files": ["shaft/kept.py", "config.env"]},
            # Successful re-gates currently log their files without a held id.
            {"ts": "2026-10-01T09:04:00", "outcome": "re-gated", "result": "kept", "files": ["shaft/kept.py"]},
            {"ts": "2026-10-01T09:05:00", "outcome": "re-gated", "held": "old", "result": "held",
             "files": ["shaft/old.py"]},
            {"ts": "2026-10-01T11:00:00", "outcome": "re-gated", "held": "old", "result": "withdrawn"},
            {"ts": "2026-10-01T11:01:00", "outcome": "held", "held": "future", "files": ["shaft/future.py"]},
        ]
        with tempfile.TemporaryDirectory(prefix="shaft-replay-withdraw-") as tmp:
            root = Path(tmp)
            path = root / gate_held.RUNS / "kant.jsonl"
            path.parent.mkdir()
            text = "".join(json.dumps(row) + "\n" for row in rows)
            path.write_text(text)
            with mock.patch.object(core, "SUP_DIR", root), \
                 mock.patch.object(gate_held, "pending", side_effect=AssertionError("current state read")):
                replay = broker.ReplayBroker.__new__(broker.ReplayBroker)
                replay.name = "kant"
                replay.as_of = datetime(2026, 10, 1, 10)
                replay.sim_withdrawn = set()
                replay._tags = threading.local()
                self.assertEqual(replay._withdraw_held("old", "obsolete"),
                                 "withdrawn held change old: shaft/old.py; obsolete")
                for hid in ("old", "gone", "kept", "future"):
                    with self.subTest(hid=hid), self.assertRaisesRegex(ValueError, "no held change"):
                        replay._withdraw_held(hid, "obsolete")
                self.assertEqual(path.read_text(), text)
                self.assertFalse((root / gate_held.HELD).exists())
