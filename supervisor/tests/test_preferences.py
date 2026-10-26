"""E1: supervisor-enforced pauses and the append-only preference record."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, battery, broker, core, preferences

NOW = datetime(2026, 10, 26, 1, 0)
PAUSED = ("paused until 2026-10-26 03:00: the scheduler will not wake me for cycles or run my battery until then; "
          "my sleep still runs; the operator can lift it.")
DECLINED = "declined: recorded and sent to the operator. It still counts as work asked of me."
PREFERRED = "recorded in my preference ledger (the operator reads it)."


class LedgerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-prefs-")
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.sup = self.project / ".supervisor"
        for p in (mock.patch.object(preferences, "SUP_DIR", self.sup),
                  mock.patch.object(core, "SUP_DIR", self.sup),
                  mock.patch.object(battery, "SUP_DIR", self.sup)):
            p.start()
            self.addCleanup(p.stop)

    def ledger(self, rows):
        path = self.sup / "preferences" / "wren.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path


class PreferenceTest(LedgerCase):
    def test_a_bad_line_never_stops_the_scheduler(self):
        """Regression (Grok's review): self_paused runs before every wake and battery night; one torn line, or an event
        missing a field, raised and would have stopped the scheduler."""
        path = self.ledger([{"at": "2026-10-26T00:30:00", "kind": "pause", "from": "2026-10-26T00:30:00",
                             "until": "2026-10-26T03:00:00", "reason": "r"},
                            {"at": "2026-10-26T00:40:00", "kind": "pause"},            # no from/until
                            {"at": "not a time", "kind": "lift"}, ["not", "an", "event"]])
        with path.open("a") as f:
            f.write('{"at": "2026-10-26T00:50:00", "kind": "li')                       # torn mid-write
        self.assertEqual(preferences.self_paused("wren", NOW), datetime(2026, 10, 26, 3, 0))
        self.assertEqual(len(preferences.events("wren")), 1)

    def test_pause_text_and_exact_event(self):
        self.assertEqual(preferences.pause("wren", 2, "time to pause", NOW), PAUSED)
        self.assertEqual(preferences.events("wren"), [{"at": "2026-10-26T01:00:00", "kind": "pause",
                         "from": "2026-10-26T01:00:00", "until": "2026-10-26T03:00:00", "reason": "time to pause"}])
        self.assertEqual(preferences.self_paused("wren", NOW), datetime(2026, 10, 26, 3))
        self.assertIsNone(preferences.self_paused("wren", datetime(2026, 10, 26, 3)))
        self.assertIsNone(preferences.self_paused("wren", NOW - timedelta(seconds=1)))
        self.assertIsNone(preferences.self_paused("other", NOW))

    def test_hours_validation(self):
        for hours in (0, 25, -1, True, False, 2.0, "2", None):
            with self.subTest(hours=hours):
                self.assertEqual(preferences.pause("wren", hours, "reason", NOW),
                                 "ERROR: hours must be an integer from 1 to 24")
        self.assertEqual(preferences.events("wren"), [])
        for hours in (1, 24):
            self.assertTrue(preferences.pause("wren", hours, "reason", NOW).startswith("paused until"))
            preferences.lift("wren", NOW)

    def test_required_strings_and_no_events_on_refusal(self):
        for value in ("", "  \n", None, 3, [], {}):
            for action, args, field in ((preferences.pause, (2, value), "reason"),
                                        (preferences.decline, (value, "why"), "ref"),
                                        (preferences.decline, ("task", value), "reason"),
                                        (preferences.prefer, (value, "quiet"), "about"),
                                        (preferences.prefer, ("schedule", value), "preference")):
                with self.subTest(action=action.__name__, field=field, value=value):
                    self.assertEqual(action("wren", *args, NOW), f"ERROR: {field} must be a non-empty string")
        self.assertEqual(preferences.events("wren"), [])

    def test_decline_and_preference_texts(self):
        self.assertEqual(preferences.decline("wren", " inbox/request.md ", " later ", NOW), DECLINED)
        self.assertEqual(preferences.prefer("wren", " schedule ", " fewer wakes ", NOW), PREFERRED)
        self.assertEqual(preferences.events("wren"), [
            {"at": NOW.isoformat(), "kind": "decline", "ref": "inbox/request.md", "reason": "later"},
            {"at": NOW.isoformat(), "kind": "preference", "about": "schedule", "preference": "fewer wakes"}])

    def test_active_pause_refused_and_lift_does_not_rewrite(self):
        self.assertEqual(preferences.lift("wren", NOW), "no active pause")
        preferences.pause("wren", 2, "first", NOW)
        path = self.sup / "preferences" / "wren.jsonl"
        first = path.read_bytes()
        self.assertEqual(preferences.pause("wren", 1, "second", NOW),
                         "ERROR: already paused until 2026-10-26 03:00")
        self.assertEqual(path.read_bytes(), first)
        lifted = NOW + timedelta(minutes=1)
        self.assertEqual(preferences.lift("wren", lifted), "lifted pause until 2026-10-26 03:00")
        self.assertTrue(path.read_bytes().startswith(first))
        self.assertEqual(preferences.events("wren")[-1], {"at": lifted.isoformat(), "kind": "lift"})
        self.assertEqual(preferences.self_paused("wren", NOW), datetime(2026, 10, 26, 3))
        self.assertIsNone(preferences.self_paused("wren", lifted))
        self.assertEqual(preferences.lift("wren", lifted), "no active pause")
        preferences.pause("wren", 1, "third", lifted)
        self.assertEqual(preferences.self_paused("wren", lifted), datetime(2026, 10, 26, 2, 1))

    def test_three_starts_in_seven_days_even_when_lifted(self):
        for day in range(3):
            at = NOW + timedelta(days=day)
            preferences.pause("wren", 1, "reason", at)
            preferences.lift("wren", at)
        self.assertEqual(preferences.pause("wren", 1, "fourth", NOW + timedelta(days=6)),
                         "ERROR: already started 3 pauses in the last 7 days; next allowed at 2026-11-02 01:00:00")
        self.assertEqual(len(preferences.events("wren")), 6)
        self.assertTrue(preferences.pause("wren", 1, "allowed", NOW + timedelta(days=7)).startswith("paused until"))

    def test_concurrent_pause_starts_record_only_one(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: preferences.pause("wren", 2, "pause", NOW), range(2)))
        self.assertEqual(sorted(replies), ["ERROR: already paused until 2026-10-26 03:00", PAUSED])
        self.assertEqual(len(preferences.events("wren")), 1)

    def test_reports_from_hand_written_ledger(self):
        rows = [
            {"at": "2026-09-01T01:00:00", "kind": "preference", "about": "old", "preference": "old preference"},
            {"at": "2026-10-25T01:00:00", "kind": "decline", "ref": "task", "reason": "later"},
            {"at": "2026-10-26T00:00:00", "kind": "pause", "from": "2026-10-26T00:00:00",
             "until": "2026-10-26T04:00:00", "reason": "pause"},
            {"at": "2026-10-26T00:01:00", "kind": "lift"},
            *[{"at": "2026-10-26T00:02:00", "kind": "preference", "about": str(i), "preference": "x\n" * 100}
              for i in range(4)],
            {"at": "2026-10-26T00:03:00", "kind": "pause", "from": "2026-10-26T00:03:00",
             "until": "2026-10-26T04:03:00", "reason": "again"}]
        self.ledger(rows)
        with mock.patch.object(preferences, "datetime", wraps=datetime) as dt:
            dt.now.return_value = NOW
            lines = preferences.section("wren")
            self.assertEqual(lines[:3], ["## My pauses, declines and stated preferences",
                             "Paused at my request until 2026-10-26 04:03.",
                             "Last 30 days: 2 pauses, 1 declines, 4 stated preferences."])
            self.assertEqual(len(lines), 6)
            self.assertTrue(all(len(line) <= 120 for line in lines[3:]))
            self.assertEqual([line[:4] for line in lines[3:]], ["- 1:", "- 2:", "- 3:"])
            report = preferences.report("wren").splitlines()   # text, one line per event: never raw JSON
            self.assertEqual(report[0], "wren: paused until 2026-10-26 04:03")
            self.assertEqual(len(report), 1 + len(rows))
            self.assertEqual(report[1], "2026-09-01 01:00 preference about old: old preference")
            self.assertIn("2026-10-26 00:01 pause lifted by the operator", report)
            self.assertFalse(any(line.startswith("{") for line in report))
            self.assertEqual(preferences.report("absent"), "absent: no active pause")
        self.assertEqual(preferences.review_lines(["wren", "absent"], datetime(2026, 10, 26)),
                         [f"wren: {preferences.describe(r)}" for r in rows[2:]])
        self.assertEqual(preferences.section("absent"), [])   # an empty ledger adds nothing to stats.md


class BrokerPreferenceTest(LedgerCase):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(broker, "datetime", wraps=datetime),
                  mock.patch.object(broker, "notify"),
                  mock.patch("supervisor.channel.route", return_value=("web", "test")),
                  mock.patch.object(broker.Broker, "_answer_comments")):
            started = p.start()
            self.addCleanup(p.stop)
            if p.attribute == "datetime":
                started.now.return_value = NOW
        self.live = broker.Broker("wren", self.project, {}, self.sup / "broker.log", root=self.project)

    def replay(self):
        return broker.ReplayBroker("wren", self.project, {}, self.sup / "replay.log", state_root=self.project,
                                   start_commit="unused", as_of=NOW, cache={}, cache_lock=threading.Lock(),
                                   billed="trial", tools_log=self.sup / "tools.log")

    def test_live_delivers_operator_notes_without_using_daily_quota(self):
        inbox = self.sup / "operator-inbox"
        inbox.mkdir(parents=True)
        (inbox / "20261026-000000-000000-from-wren.md").write_text("existing")
        with mock.patch.object(broker, "OPERATOR_DAILY_LIMIT", 1):
            self.assertEqual(self.live.call("request_pause", {"hours": 2, "reason": "rest"}),
                             {"ok": True, "result": PAUSED})
            self.assertEqual(self.live.call("decline_task", {"ref": "goals.md: task", "reason": "later"}),
                             {"ok": True, "result": DECLINED})
            self.assertEqual(self.live.call("state_preference", {"about": "wakes", "preference": "fewer"}),
                             {"ok": True, "result": PREFERRED})
            self.assertEqual(self.live._operator_sent_today(NOW), 1)
            self.assertFalse(self.live.call("send_message", {"to": "operator", "text": "over quota"})["ok"])
        # The usual inbox name (readers of the inbox and `shaft delete` match it), marked as a notice in its header
        notes = [p.read_text() for p in inbox.glob("*-from-wren.md") if broker.NOTICE in p.read_text()]
        self.assertEqual(len(notes), 2)
        self.assertEqual(len(list(inbox.glob("*.md"))), 3)   # nothing named otherwise
        self.assertTrue(all(n.startswith("From: wren\nTo: operator\nKind: note\n") for n in notes))
        self.assertTrue(any("I asked for a pause until 2026-10-26 03:00: rest. `shaft prefs wren lift` ends it." in n
                            for n in notes))
        self.assertTrue(any("I declined goals.md: task: later." in n for n in notes))
        recorded = [json.loads(l) for l in (self.sup / "messages.jsonl").read_text().splitlines()]
        self.assertEqual([(r["from"], r["to"], r["kind"]) for r in recorded],
                         [("wren", "operator", "note"), ("wren", "operator", "note")])
        self.assertEqual(broker.notify.call_count, 2)

    def test_replay_matches_live_with_no_delivery_or_ledger_write(self):
        replay = self.replay()
        calls = [("request_pause", {"hours": h, "reason": "why"}) for h in (0, 25, True, "2")]
        calls += [("request_pause", {"hours": 2, "reason": ""}),
                  ("decline_task", {"ref": "", "reason": "why"}),
                  ("decline_task", {"ref": "task", "reason": ""}),
                  ("state_preference", {"about": "", "preference": "quiet"}),
                  ("state_preference", {"about": "schedule", "preference": ""}),
                  ("request_pause", {"hours": 2, "reason": "why"}),
                  ("request_pause", {"hours": 1, "reason": "again"}),
                  ("decline_task", {"ref": "task", "reason": "why"}),
                  ("state_preference", {"about": "schedule", "preference": "quiet"})]
        with mock.patch.object(replay, "_deliver", side_effect=AssertionError("delivered")):
            replay_results = [replay.call(tool, args) for tool, args in calls]
        self.assertEqual(preferences.events("wren"), [])
        self.assertFalse((self.sup / "preferences").exists())
        self.assertFalse((self.sup / "operator-inbox").exists())
        self.assertFalse((self.sup / "messages.jsonl").exists())
        broker.notify.assert_not_called()
        self.assertEqual(replay.operator_sent, 0)
        self.assertEqual(len(replay.trace), len(calls))
        self.assertEqual([self.live.call(tool, args) for tool, args in calls], replay_results)
        self.assertEqual(len(preferences.events("wren")), 3)

    def test_replay_uses_history_at_start_and_enforces_weekly_cap(self):
        rows = [{"at": (NOW - timedelta(days=d)).isoformat(), "kind": "pause",
                 "from": (NOW - timedelta(days=d)).isoformat(),
                 "until": (NOW - timedelta(days=d) + timedelta(hours=1)).isoformat(), "reason": "why"}
                for d in (3, 2, 1)]
        rows += [{"at": (NOW + timedelta(days=1)).isoformat(), "kind": "lift"}]
        path = self.ledger(rows)
        before = path.read_bytes()
        replay = self.replay()
        self.assertEqual(replay.call("request_pause", {"hours": 1, "reason": "again"}),
                         {"ok": True, "result": "ERROR: already started 3 pauses in the last 7 days; "
                          "next allowed at 2026-10-30 01:00:00"})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.sup / "operator-inbox").exists())

    def test_tools_default_on_and_switchable(self):
        with mock.patch("supervisor.telegram.reachable", return_value=""):
            offered = {s["function"]["name"] for s in self.live.tool_specs()}
            self.assertTrue({"request_pause", "decline_task", "state_preference"} <= offered)
            for tool in ("request_pause", "decline_task", "state_preference"):
                self.live.disabled.add(tool)
                self.assertNotIn(tool, {s["function"]["name"] for s in self.live.tool_specs()})
                self.assertFalse(self.live.call(tool, {})["ok"])


class SchedulingTest(LedgerCase):
    def test_decide_wakes_remains_pure_and_default_is_unchanged(self):
        args = (NOW, {}, ["wren", "other"], lambda n: timedelta(hours=2), lambda n: "")
        until = datetime(2026, 10, 26, 3)
        with mock.patch.object(preferences, "self_paused", side_effect=AssertionError("read from pure function")):
            self.assertEqual(autonomy.decide_wakes(*args), (["wren", "other"], {}))
            self.assertEqual(autonomy.decide_wakes(*args, self_paused_of=lambda n, now: until if n == "wren" else None),
                             (["other"], {"wren": "paused at its own request until 03:00"}))
            self.assertEqual(autonomy.decide_wakes(*args, self_paused_of=lambda n, now: now),
                             (["wren", "other"], {}))

    def test_next_cycle_and_next_wake_text(self):
        preferences.pause("wren", 2, "why", NOW)
        with mock.patch.object(autonomy, "paused", return_value=False), \
             mock.patch.object(preferences, "datetime", wraps=datetime) as dt:
            dt.now.return_value = NOW
            self.assertEqual(autonomy.next_cycle("wren"),
                             "wren asked for a pause until 03:00: it reads this in its first cycle after that")
            with mock.patch.object(autonomy, "_state", return_value={}):
                self.assertEqual(autonomy.next_wake("wren", NOW, timedelta(hours=2)), "03:00")

    def test_wake_child_rechecks_pause_and_manual_start_explains_lift(self):
        preferences.pause("wren", 2, "why", NOW)
        with mock.patch.object(preferences, "datetime", wraps=datetime) as dt, \
             mock.patch.object(autonomy, "paused", return_value=False), \
             mock.patch.object(autonomy, "log") as logged, \
             mock.patch.object(autonomy, "record") as recorded, \
             mock.patch.object(autonomy, "run_agent") as run:
            dt.now.return_value = NOW
            self.assertEqual(autonomy.wake("wren"), 0)
            run.assert_not_called()
            recorded.assert_not_called()
            logged.assert_called_once_with("wren: wake skipped — paused at its own request until 03:00")
            with self.assertRaisesRegex(RuntimeError, "paused at its own request until 03:00 .*shaft prefs wren lift"):
                autonomy.start_wake("wren")

    def test_schedule_mentions_pause_and_daily_sleep(self):
        with mock.patch.object(autonomy.Path, "exists", return_value=True):
            text = autonomy.describe_schedule("wren", {"WAKE_INTERVAL_MINUTES": "120"})
        self.assertIn("runs your sleep cycle every day at 04:00", text)
        self.assertIn("ask for a pause of up to 24 hours with request_pause", text)
        preferences.pause("wren", 24, "why", NOW)
        self.assertTrue(autonomy.sleep_due(datetime(2026, 10, 26, 4), {}))

    def test_serve_logs_own_pause_and_starts_nightly_sleep(self):
        preferences.pause("wren", 24, "why", NOW)
        with mock.patch.object(autonomy, "SUP_DIR", self.sup), \
             mock.patch.object(autonomy, "datetime", wraps=datetime) as dt, \
             mock.patch.object(autonomy, "log") as logged, \
             mock.patch.object(autonomy, "record"), \
             mock.patch.object(autonomy, "_state", return_value={}), \
             mock.patch.object(autonomy, "instance_names", return_value=["wren"]), \
             mock.patch.object(autonomy, "paused", return_value=False), \
             mock.patch.object(autonomy, "updating", return_value=False), \
             mock.patch.object(autonomy, "update_jobs", return_value=[]), \
             mock.patch.object(autonomy, "wake_interval", return_value=timedelta(hours=2)), \
             mock.patch.object(autonomy, "_awake", return_value=""), \
             mock.patch.object(autonomy, "_batteries"), \
             mock.patch.object(autonomy, "_start") as started, \
             mock.patch.object(autonomy, "stop_children_on_signal"), \
             mock.patch.object(autonomy.time, "sleep", side_effect=StopIteration), \
             mock.patch("supervisor.version.record_service"), \
             mock.patch("supervisor.browser.startup_note", return_value=""), \
             mock.patch("supervisor.jobs.tick") as jobs, \
             mock.patch("supervisor.trial.has_work", return_value=False):
            dt.now.return_value = datetime(2026, 10, 26, 4, 5)
            with self.assertRaises(StopIteration):
                autonomy.serve()
            started.assert_called_once_with(["sleep", "--all"])
            # Regression (Grok's review): at 04:05 "until 01:00" read as already over; a pause ending another day says it
            logged.assert_any_call("scheduler: wren's wake skipped — paused at its own request until 2026-10-27 01:00")
            self.assertEqual(jobs.call_args.args[-1]("wren"), "paused at its own request")

    def test_battery_due_skips_pause_and_resumes_after_lift(self):
        with mock.patch.object(battery, "EVERY_DAYS", 5):
            (self.sup / "battery" / "items" / battery.VERSION).mkdir(parents=True)
            self.assertTrue(battery.due("wren", NOW))
            preferences.pause("wren", 2, "why", NOW)
            self.assertFalse(battery.due("wren", NOW))
            self.assertTrue(battery.due("other", NOW))
            preferences.lift("wren", NOW + timedelta(minutes=1))
            self.assertTrue(battery.due("wren", NOW + timedelta(minutes=1)))


if __name__ == "__main__":
    unittest.main()
