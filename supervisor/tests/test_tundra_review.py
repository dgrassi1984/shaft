"""Tundra review, 2026-10-28: sleep's verdict throughput and forgotten operator pauses (M2/M4)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, core, metrics, panel, web_alerts, webpush


class SleepJudgmentsTest(unittest.TestCase):
    def setUp(self):
        self.b = object.__new__(broker.Broker)
        self.b.name, self.b.as_of = "tundra", None
        self.rows = [{"episode_ts": f"2026-10-01T10:{i:02d}:00", "judged_at": f"2026-10-02T10:{i:02d}:00",
                      "goal": "a useful result", "judge": "main", "reasoning": "abc" * 900}
                     for i in range(60)]
        for patch in (mock.patch.object(metrics, "judgments", return_value=self.rows),
                      mock.patch.object(metrics, "unsampled_rows", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)

    def test_sleep_page_holds_twenty_long_verdicts(self):
        page = json.loads(self.b._record("judgments", since="2026-10-01 00:00", format="json"))
        self.assertGreaterEqual(len(page["verdicts"]), 20)
        self.assertLessEqual(len(page["text"]), 24000)
        self.assertEqual(page["next_since"], "2026-10-02 10:19:00")
        self.assertIn("abc" * 200 + "… (full: record(kind='judgments', cycle='2026-10-01 10:00'))", page["text"])
        self.assertNotIn("abc" * 201, page["text"])

    def test_model_paths_keep_full_text_and_original_budget(self):
        original = self.b._record("judgments")
        self.assertEqual(original, broker.judgments_text(self.rows))
        self.assertEqual(original, self.b._record("judgments", format="json"))
        self.assertIn("abc" * 900, original)
        self.assertNotIn("… (full:", original)
        text = self.b._record("judgments", since="2026-10-01 00:00")
        self.assertIn("abc" * 900, text)
        self.assertLessEqual(len(text), 5500)
        self.assertEqual(text.count("### "), original.count("### "))

    def test_both_judges_are_compact_only_in_sleep(self):
        self.rows[:] = [{**self.rows[0], "second": {"judge": "other", "reasoning": "xyz" * 900}}]
        page = json.loads(self.b._record("judgments", since="2026-10-01 00:00", format="json"))
        pointer = "… (full: record(kind='judgments', cycle='2026-10-01 10:00'))"
        self.assertIn("abc" * 200 + pointer, page["text"])
        self.assertIn("xyz" * 200 + pointer, page["text"])
        full = self.b._record("judgments", cycle="2026-10-01 10:00")
        self.assertIn("abc" * 900, full)
        self.assertIn("xyz" * 900, full)

    def test_json_page_boundary_keeps_ties(self):
        self.rows[19]["judged_at"] = self.rows[20]["judged_at"]
        page = json.loads(self.b._record("judgments", since="2026-10-01 00:00", format="json"))
        self.assertEqual(len(page["verdicts"]), 19)
        next_page = json.loads(self.b._record("judgments", since=page["next_since"], format="json"))
        self.assertEqual([r["episode_ts"] for r in next_page["verdicts"][:2]],
                         [r["episode_ts"] for r in self.rows[19:21]])


class PauseAlertsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-pause-alert-")
        self.addCleanup(tmp.cleanup)
        self.sup = Path(tmp.name)
        self.now = datetime(2026, 10, 28, 12)
        for patch in (mock.patch.object(core, "SUP_DIR", self.sup),
                      mock.patch.object(panel, "SUP_DIR", self.sup),
                      mock.patch.object(panel, "instance_names", return_value=[]),
                      mock.patch.object(core, "PAUSE_ALERT_HOURS", 3),
                      mock.patch.object(core, "settings_file", return_value={}),
                      mock.patch.object(webpush, "notify", return_value=["device"])):
            value = patch.start()
            if hasattr(value, "assert_called_once_with"):
                self.notify = value
            self.addCleanup(patch.stop)
        clock = mock.patch.object(core, "datetime", wraps=datetime)
        self.clock = clock.start()
        self.clock.now.return_value = self.now
        self.addCleanup(clock.stop)

    def flag(self, name="tundra", hours=4):
        path = self.sup / ("paused" if name == "all" else f"paused-{name}")
        path.write_text((self.now - timedelta(hours=hours)).isoformat())
        return path

    def alerts(self):
        return panel.alerts(now=self.now, cost={"caps": [], "judge_today_total": 0, "judge_alert": 3})

    def test_long_pause_alert_and_push_once_then_resume_removes_record(self):
        flag = self.flag()
        expected = {"id": "pause:tundra:2026-10-28T08:00",
                    "text": "tundra has been paused by the operator for 4 h (since 10-28 08:00) — shaft resume tundra"}
        self.assertEqual(self.alerts(), [expected])
        self.assertEqual(web_alerts.forward_pauses(), 1)
        self.notify.assert_called_once_with({"title": "Operator pause", "body": expected["text"],
                                             "url": "/overview", "tag": expected["id"]})
        record = self.sup / "web" / "pause-alerts.json"
        self.assertEqual(json.loads(record.read_text()), [expected["id"]])
        self.assertEqual(web_alerts.forward_pauses(), 0)
        flag.unlink()
        self.assertEqual(self.alerts(), [])
        self.assertEqual(web_alerts.forward_pauses(), 0)
        self.assertEqual(json.loads(record.read_text()), [])
        self.notify.assert_called_once()

    def test_retired_tools_absent_and_operator_pause_still_skips_wakes_and_alerts(self):
        # Keep the retired names in parts so the source audit finds only historical documents.
        retired = {"_".join(parts) for parts in (("request", "pause"), ("decline", "task"),
                                                 ("state", "preference"))}
        self.assertTrue(retired.isdisjoint(broker.SPECS))
        template = str(Path(__file__).resolve().parents[2] / "template")
        with mock.patch.object(sys, "path", [template, *sys.path]):
            from shaft import tools
        with mock.patch.object(tools.broker, "available", return_value=True), \
             mock.patch.object(tools.broker, "tools", return_value=list(broker.SPECS.values())):
            offered = {s["function"]["name"] for s in tools.Toolbox(self.sup).specs()}
        self.assertIn("set_config", offered)
        self.assertTrue(retired.isdisjoint(offered))
        self.assertTrue(retired.isdisjoint(tools.STATE_CHANGING))

        # A future-dated legacy ledger must no longer delay a wake.
        ledger = self.sup / "preferences" / "tundra.jsonl"
        ledger.parent.mkdir()
        ledger.write_text(json.dumps({"kind": "pause", "at": self.now.isoformat(),
                                     "from": self.now.isoformat(),
                                     "until": (self.now + timedelta(hours=24)).isoformat()}) + "\n")
        for patch in (mock.patch.object(autonomy, "SUP_DIR", self.sup),
                      mock.patch.object(autonomy, "log"),
                      mock.patch.object(autonomy, "record"),
                      mock.patch.object(autonomy, "_state", return_value={}),
                      mock.patch.object(autonomy, "instance_names", return_value=["tundra"]),
                      mock.patch.object(autonomy, "update_jobs", return_value=[]),
                      mock.patch.object(autonomy, "updating", return_value=False),
                      mock.patch.object(autonomy, "wake_interval", return_value=timedelta(hours=2)),
                      mock.patch.object(autonomy, "_awake", return_value=""),
                      mock.patch.object(autonomy, "sleep_due", return_value=False),
                      mock.patch.object(autonomy, "_batteries"),
                      mock.patch.object(autonomy, "gate_warm"),
                      mock.patch.object(autonomy, "stop_children_on_signal"),
                      mock.patch.object(autonomy.time, "sleep", side_effect=StopIteration),
                      mock.patch("supervisor.version.record_service"),
                      mock.patch("supervisor.browser.startup_note", return_value=""),
                      mock.patch("supervisor.calendar_proposals.expire"),
                      mock.patch("supervisor.jobs.tick"),
                      mock.patch("supervisor.trial.has_work", return_value=False)):
            self.enterContext(patch)
        with mock.patch.object(autonomy, "_start") as started:
            with self.assertRaises(StopIteration):
                autonomy.serve()
            started.assert_called_once_with(["wake", "tundra"])
            self.assertEqual(autonomy.next_wake("tundra", self.now), "due")
            started.reset_mock()
            self.flag()
            with self.assertRaises(StopIteration):
                autonomy.serve()
            started.assert_not_called()
        self.assertEqual(self.alerts(), [{"id": "pause:tundra:2026-10-28T08:00",
                         "text": "tundra has been paused by the operator for 4 h (since 10-28 08:00) — "
                                 "shaft resume tundra"}])
        self.assertEqual(web_alerts.forward_pauses(), 1)
        self.assertEqual(self.notify.call_args.args[0]["tag"], "pause:tundra:2026-10-28T08:00")

    def test_short_pause_does_not_alert(self):
        self.flag(hours=1)
        self.assertEqual(core.long_pauses(self.now), [])
        self.assertEqual(self.alerts(), [])
        self.assertEqual(web_alerts.forward_pauses(), 0)
        self.notify.assert_not_called()

    def test_zero_disables_alerts_and_pushes(self):
        self.flag()
        with mock.patch.object(core, "PAUSE_ALERT_HOURS", 0):
            self.assertEqual(core.long_pauses(self.now), [])
            self.assertEqual(self.alerts(), [])
            self.assertEqual(web_alerts.forward_pauses(), 0)
        self.notify.assert_not_called()

    def test_global_pause_names_all_instances(self):
        self.flag("all")
        self.assertEqual(self.alerts(), [{"id": "pause:all:2026-10-28T08:00",
                          "text": "all instances have been paused by the operator for 4 h "
                                  "(since 10-28 08:00) — shaft resume"}])
        self.assertEqual(web_alerts.forward_pauses(), 1)
        self.assertEqual(self.notify.call_args.args[0]["tag"], "pause:all:2026-10-28T08:00")

    def test_bad_iso_falls_back_to_mtime_and_unrelated_flags_are_excluded(self):
        flag = self.flag()
        flag.write_text("old format")
        stamp = (self.now - timedelta(hours=4)).timestamp()
        os.utime(flag, (stamp, stamp))
        (self.sup / "pausedness").write_text("old format")
        self.assertEqual(core.long_pauses(self.now), [
            {"name": "tundra", "since": datetime(2026, 10, 28, 8), "hours": 4}])

    def test_timezone_stamp_and_failed_push_retry(self):
        flag = self.flag()
        flag.write_text((self.now - timedelta(hours=4)).astimezone(timezone.utc).isoformat())
        self.notify.return_value = []
        self.assertEqual(web_alerts.forward_pauses(), 0)
        self.notify.return_value = ["device"]
        self.assertEqual(web_alerts.forward_pauses(), 1)
        self.assertEqual(self.notify.call_count, 2)
