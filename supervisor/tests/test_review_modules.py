"""Regression checks for the review's ownership, evidence, chronology and malformed-record defects."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import broker, core, families, metrics, orders, web_config
from supervisor.tests import test_unrecorded


class ReviewModulesTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-review-")))
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.base / "sup"))
        self.enterContext(mock.patch.object(core, "INSTANCES", self.base / "agents"))
        self.now = datetime(2026, 11, 11, 12)

    def test_f3_operator_takes_over_same_id_and_instance_cannot_change_it(self):
        """F3: operator adoption changes ownership even when the text differs only in case."""
        original, _ = orders.add("ember", "Keep notes short.", "instance", "conversation", self.now)
        text = original["text"].upper()
        output = io.StringIO()
        with mock.patch.object(cli, "instance_root", return_value=self.base), \
                mock.patch.object(cli, "log"), redirect_stdout(output):
            self.assertEqual(cli.cmd_orders("ember", "add", [text]), 0)
        self.assertIn("took over", output.getvalue())
        adopted, = orders.listing("ember")
        self.assertEqual((adopted["id"], adopted["by"], adopted["source"]), (original["id"], "operator", "cli"))
        same, new = orders.add("ember", text, "instance", "other", self.now)
        self.assertFalse(new)
        self.assertEqual(same, adopted)
        self.assertIn("from the operator", orders.line(same))

    def test_f3_instance_duplicate_and_ui_name_ownership(self):
        original, _ = orders.add("ember", "Keep notes short.", "instance", "conversation", self.now)
        self.assertEqual(orders.add("ember", "KEEP NOTES SHORT.", "instance", "other", self.now), (original, False))
        with mock.patch.object(orders, "corrections", return_value=[]):
            self.assertIn("recorded by ember from what you said", str(web_config.orders_panel("ember")))
        b = object.__new__(broker.Broker)
        b.name = "ember"
        with mock.patch.object(b, "_order_source", return_value="conversation"):
            result = b._record_order("KEEP NOTES SHORT.", "conversation")
        self.assertIn("already standing order o1", result)
        self.assertIn("only the operator drops it", result)

    def test_f5_message_success_survives_check_log_error(self):
        """F5: both delivered message routes retain their normal answer after logging fails."""
        b = object.__new__(broker.Broker)
        b.name, b.project, b.root = "ember", self.base, self.base / "ember"
        b.counts, b.as_of, b._envelope = Counter(), None, None
        (self.base / "instances/ember-peer/shaft").mkdir(parents=True)
        with mock.patch.object(b, "_message_stamp", return_value=self.now), \
                mock.patch.object(b, "_read_attachments", return_value=[]), \
                mock.patch.object(b, "_operator_sent_today", return_value=0), \
                mock.patch.object(b, "_conversation_target", return_value=(None, None, "")), \
                mock.patch.object(b, "_check_orders", return_value=({"violates": False}, "; checked")), \
                mock.patch.object(b, "_store", return_value=[]), \
                mock.patch.object(b, "_deliver", return_value=None) as deliver, \
                mock.patch.object(b, "_log_check", side_effect=OSError("disk error")), \
                mock.patch.object(b, "_push_usage", return_value=None), \
                mock.patch.object(b, "_waited_share", return_value=None), mock.patch.object(core, "log") as log:
            for recipient in ("operator", "ember-peer"):
                self.assertIn("delivered to", b._send_message(recipient, "Report ready."))
            self.assertEqual(deliver.call_count, 2)
            self.assertEqual(log.call_count, 2)
            log.side_effect = OSError("disk full")
            self.assertIn("delivered to", b._send_message("operator", "Report ready."))

    def test_f6_exact_links_and_ambiguous_minutes(self):
        """F6: seconds select the right cycle; ambiguous minute links cannot select the first cycle."""
        a, b, c, d = [f"2026-11-11T10:{minute}:{second}" for minute, second in
                      (("00", "10"), ("00", "40"), ("01", "00"), ("02", "00"))]
        rows = [{"episode_ts": a}, {"episode_ts": b}, {"episode_ts": c, "resembles": a[:16]},
                {"episode_ts": d, "resembles": b.replace("T", " ")}]
        with mock.patch.object(metrics, "judgments", return_value=rows), \
                mock.patch.object(metrics, "change_outcomes", return_value=[]):
            groups = families.families("ember")
        self.assertEqual({tuple(g["members"]) for g in groups}, {(a,), (b, d), (c,)})
        rows[0]["episode_ts"] = a[:16]
        with mock.patch.object(metrics, "judgments", return_value=rows), \
                mock.patch.object(metrics, "change_outcomes", return_value=[]):
            groups = families.families("ember")
        self.assertEqual({tuple(g["members"]) for g in groups}, {(a[:16],), (b, d), (c,)})

    def test_f6_malformed_times_do_not_stop_family_report(self):
        """F6: invalid episode, run-start, change and judgment dates do not crash the report."""
        stamp = self.now.isoformat()
        rows = [{"episode_ts": stamp}, {"episode_ts": "broken"},
                {"episode_ts": "2026-11-11T11:00:00", "judged_at": "broken"}]
        episodes = [{"ts": "broken"}, {"ts": stamp, "run_started": "broken"}]
        with mock.patch.object(metrics, "judgments", return_value=rows), \
                mock.patch.object(metrics, "live_episodes", return_value=episodes), \
                mock.patch.object(metrics, "change_outcomes", return_value=[{"ts": "broken"}]):
            self.assertEqual([g["members"] for g in families.families("ember")], [[stamp]])

    def test_f11_torn_lines_do_not_block_add_or_drop(self):
        """F11: malformed ledger lines cannot prevent or swallow subsequent add/drop events."""
        original, _ = orders.add("ember", "Keep notes short.", "operator", "cli", self.now)
        with orders.path("ember").open("a") as file:
            file.write(json.dumps({**original, "id": "o2", "by": "self", "text": "Check sources."}) + "\n")
            file.write('null\n{"id":')
        added, new = orders.add("ember", "Check sources.", "instance", "conversation", self.now)
        self.assertTrue(new)
        self.assertEqual(added["id"], "o3")
        with orders.path("ember").open("a") as file:
            file.write('{"id":')
        orders.drop("ember", original["id"], self.now)
        self.assertEqual([o["id"] for o in orders.listing("ember", dropped=True)], ["o1", "o3"])
        self.assertEqual([o["id"] for o in orders.listing("ember")], ["o3"])


class UnrecordedRegression(unittest.TestCase):
    def test_f9_first_run_marker_survives_outage_and_empty_runs(self):
        """F9: a conversation after the first check does not age out during a judge outage."""
        f = test_unrecorded.UnrecordedTest()
        try:
            f.setUp()
            for mode in ("down", "empty"):
                with self.subTest(mode=mode):
                    target = test_unrecorded.unrecorded.path("ember")
                    if target.exists():
                        target.unlink()
                    f.available.return_value = mode != "down"
                    self.assertEqual(test_unrecorded.unrecorded.run("ember", test_unrecorded.NOW), 0)
                    marker, = [json.loads(line) for line in target.read_text().splitlines() if line]
                    self.assertEqual(marker, {"at": test_unrecorded.NOW.isoformat(), "started": True})
                    f.talk(tid=mode, at=test_unrecorded.NOW + timedelta(minutes=1))
                    f.available.return_value = True
                    self.assertEqual(test_unrecorded.unrecorded.run(
                        "ember", test_unrecorded.NOW + timedelta(hours=30)), 1)
                    row, = test_unrecorded.unrecorded.rows("ember")
                    self.assertNotIn("skipped", row)
                    self.assertEqual(row["talk_id"], mode)
                    self.assertEqual(row["orders"], [{"words": test_unrecorded.WORDS, "recorded": False}])
                    (f.root / "memory/episodes/2026-11-11.jsonl").unlink()
        finally:
            f.doCleanups()
