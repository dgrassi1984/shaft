"""The scheduler wakes an instance for something, not for the clock (review C15/G1/G4), and stays alive while it does
(review B13, B14, B16).

C15/G1/G4: every instance was woken every WAKE_INTERVAL_MINUTES (5-10 min live) whatever it had to do, and a message or a
commitment falling due waited for that interval. Now a timed wake needs an unread message, a commitment coming due, a job
result since the last cycle, or SHAFT_IDLE_WAKE_MINUTES without a cycle; a message or a due commitment wakes it at once.
B13: a run that never acknowledged its pause held the reply, and its budget, forever. B14: a scheduler child's stderr was
read only after it ended (a child that wrote more than a pipe holds never ended; a grandchild holding the pipe blocked the
scheduler's loop). B16: schedule.json and battery/schedule.json were rewritten without a lock (a lost update; a half-written
file read as empty made every wake and the night's sleep due)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, battery, broker, commitments, core, delete, jobs, messages, runner
from supervisor.autonomy import WakeFacts, decide_wakes, wake_gate

D = datetime.fromisoformat
NOW = D("2026-11-03T12:00:00")
TEN = timedelta(minutes=10)
IDLE = timedelta(minutes=30)


def nothing() -> WakeFacts:
    return WakeFacts()


class GateTest(unittest.TestCase):
    """The pure rule (C15/G1/G4)."""

    def gate(self, since, facts=nothing):
        return wake_gate(NOW, since and D(since), NOW + TEN, IDLE, facts)

    def test_nothing_to_do_is_skipped_with_the_reason(self):
        go, why = self.gate("2026-11-03T11:50:00")
        self.assertFalse(go)                       # the old scheduler woke it: its interval had passed
        self.assertTrue(why.startswith(autonomy.NOTHING_TO_DO))
        self.assertIn("no commitment due before 12:10", why)
        self.assertIn("last cycle at 11:50", why)

    def test_each_reason_wakes(self):
        for facts, said in ((WakeFacts(unread=2), "2 unread message(s) in its inbox"),
                            (WakeFacts(due="c1103-0001-1 due 12:05"), "commitment c1103-0001-1 due 12:05"),
                            (WakeFacts(results=1), "1 job result(s) since its last cycle"),
                            (WakeFacts(error="OSError: boom"), "what it has to do could not be read (OSError: boom)")):
            self.assertEqual(self.gate("2026-11-03T11:50:00", lambda f=facts: f), (True, said))

    def test_idle_rule_decides_before_the_facts_are_read(self):
        def never():
            raise AssertionError("facts read although the idle rule decided")
        self.assertEqual(self.gate(None, never), (True, "no cycle on record"))
        self.assertEqual(self.gate("2026-11-03T11:25:00", never), (True, "no cycle for 35 min"))

    def test_decide_wakes_applies_the_gate_after_the_awake_check(self):
        st = {"wakes": {"a": "2026-11-03T11:45:00", "b": "2026-11-03T11:45:00", "c": "2026-11-03T11:45:00"},
              "cycles": {"a": "2026-11-03T11:45:00", "b": "2026-11-03T11:45:00", "c": "2026-11-03T11:45:00"}}
        facts = {"a": WakeFacts(), "b": WakeFacts(unread=1), "c": WakeFacts()}
        seen = []

        def facts_of(name, since, until):
            seen.append((name, since, until))
            return facts[name]
        start, skip = decide_wakes(NOW, st, ["a", "b", "c"], lambda n: TEN, {"c": "talk since 11:58"}.get,
                                   facts_of, IDLE)
        self.assertEqual(start, {"b": "1 unread message(s) in its inbox"})
        self.assertTrue(skip["a"].startswith(autonomy.NOTHING_TO_DO))
        self.assertEqual(skip["c"], "talk since 11:58")                    # awake: said as before, facts not read
        self.assertEqual(seen, [("a", D("2026-11-03T11:45:00"), NOW + TEN), ("b", D("2026-11-03T11:45:00"), NOW + TEN)])


class FactsTest(unittest.TestCase):
    """wake_facts reads the real records: the inbox listing, commitments.jsonl, the jobs' run log."""

    def setUp(self):
        self.name = "gatex"
        self.root = autonomy.INSTANCES / self.name
        (self.root / "inbox" / "x.files").mkdir(parents=True)          # a job hand-off's folder is not a message
        (self.root / "memory").mkdir()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup((jobs._dir() / f"{self.name}.jsonl").unlink, missing_ok=True)

    def commit(self, cid: str, made: str, due: str) -> None:
        with (self.root / commitments.FILE).open("a") as f:
            f.write(json.dumps({"event": "made", "id": cid, "at": made, "due": due, "promise": "send the report",
                                "criterion": "sent", "made_to": "operator", "source": "cycle"}) + "\n")

    def test_unread_commitments_and_results(self):
        since = D("2026-11-03T11:00:00")
        self.assertEqual(autonomy.wake_facts(self.name, since, NOW + TEN), WakeFacts())
        (self.root / "inbox" / "20261103-115900-000001-from-operator.md").write_text("hi")
        self.commit("c1103-0001-1", "2026-11-03T09:00:00", "2026-11-03T10:30:00")   # seen by the last cycle
        self.commit("c1103-0001-2", "2026-11-03T09:00:00", "2026-11-03T13:00:00")   # after the next check
        self.commit("c1103-0001-3", "2026-11-03T09:00:00", "2026-11-03T12:05:00")
        jobs._dir().mkdir(parents=True, exist_ok=True)
        rows = [{"ts": "2026-11-03T10:00:00", "plugin": "p", "delivered": True, "done": "2026-11-03T10:00:05"},
                {"ts": "2026-11-03T11:30:00", "plugin": "p", "delivered": False, "done": "2026-11-03T11:30:05"},
                {"ts": "2026-11-03T11:40:00", "plugin": "p", "published": ["notes/b.md"], "done": "2026-11-03T11:40:05"}]
        (jobs._dir() / f"{self.name}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        self.assertEqual(autonomy.wake_facts(self.name, since, NOW + TEN),
                         WakeFacts(unread=1, due="c1103-0001-3 due 12:05", results=1))

    def test_an_unreadable_record_wakes_saying_why(self):
        with mock.patch.object(commitments, "ledger", side_effect=OSError("disk")):
            facts = autonomy.wake_facts(self.name, None, NOW)
        self.assertEqual(facts.error, "OSError: disk")
        self.assertEqual(wake_gate(NOW, NOW - TEN, NOW + TEN, IDLE, lambda: facts)[0], True)


class StateCase(unittest.TestCase):
    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-sched-"))
        self.addCleanup(shutil.rmtree, self.sup, True)
        p = mock.patch.object(autonomy, "STATE", self.sup / "schedule.json")
        p.start()
        self.addCleanup(p.stop)

    def st(self) -> dict:
        return json.loads((self.sup / "schedule.json").read_text())


class RecordTest(StateCase):
    def test_a_skip_is_kept_until_the_next_cycle(self):
        autonomy.record("skip", "wren", "nothing to do: inbox empty")
        self.assertTrue(autonomy.last_skip("wren").startswith("nothing to do: inbox empty (at "))
        self.assertEqual(self.st()["wakes"]["wren"], self.st()["skipped"]["wren"]["at"])   # its interval restarts
        autonomy.record("cycle", "wren")
        self.assertEqual(autonomy.last_skip("wren"), "")
        self.assertEqual(self.st()["cycles"]["wren"], self.st()["wakes"]["wren"])

    def test_b16_concurrent_records_lose_nothing(self):
        # B16: an unlocked read-modify-write. With every reader slowed past the others' writes, the old record kept
        # one instance's stamp out of eight.
        real = autonomy._state

        def slow():
            st = real()
            time.sleep(0.05)
            return st
        with mock.patch.object(autonomy, "_state", slow):
            threads = [threading.Thread(target=autonomy.record, args=("wake", f"i{k}")) for k in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(self.st()["wakes"]), [f"i{k}" for k in range(8)])
        self.assertEqual(list(self.sup.glob("*.tmp")), [])

    def test_b16_the_file_is_replaced_whole_never_truncated_in_place(self):
        autonomy.record("sleep")
        before = (self.sup / "schedule.json").stat().st_ino
        autonomy.record("wake", "wren")   # a reader between truncate and write saw {}: every wake and the sleep due
        self.assertNotEqual((self.sup / "schedule.json").stat().st_ino, before)

    def test_b16_battery_records_lose_nothing(self):
        real = json.loads

        def slow(text):
            time.sleep(0.05)
            return real(text)
        fake_json = mock.Mock(loads=slow, dumps=json.dumps)
        (self.sup / "battery").mkdir()
        (self.sup / "battery" / "schedule.json").write_text("{}")
        with mock.patch.object(battery, "SUP_DIR", self.sup), mock.patch.object(battery, "json", fake_json):
            threads = [threading.Thread(target=battery._record_run, args=(f"i{k}",)) for k in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(sorted(json.loads((self.sup / "battery" / "schedule.json").read_text())),
                         [f"i{k}" for k in range(6)])

    def test_b16_shaft_delete_takes_the_new_keys_under_the_lock(self):
        (self.sup / "schedule.json").write_text(json.dumps({
            "wakes": {"kdx": "t", "k2": "t"}, "cycles": {"kdx": "t", "k2": "t"},
            "skipped": {"kdx": {"at": "t", "why": "w"}}, "sleep": "t"}))
        with mock.patch.object(delete, "SUP_DIR", self.sup):
            edit = next(e for e in delete.shared_edits("kdx") if e.path.name == "schedule.json")
        self.assertIs(edit.lock, autonomy.editing_lock)
        delete._edit(edit, apply=True)
        self.assertEqual(self.st(), {"wakes": {"k2": "t"}, "cycles": {"k2": "t"}, "skipped": {}, "sleep": "t"})


class EventWakeTest(StateCase):
    """C15/G1: a message or a commitment falling due asks for a wake now, through the jobs' wake path."""

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: [jobs.take_wake(n) for n in list(jobs.pending_wakes())])
        for p in (mock.patch.object(autonomy, "paused", return_value=False),
                  mock.patch.object(autonomy, "wake_interval", return_value=TEN),
                  mock.patch.object(autonomy, "wake_floor", return_value=10),
                  mock.patch.object(autonomy, "log")):
            p.start()
            self.addCleanup(p.stop)

    def by(self, name: str) -> list[str]:
        return (jobs.pending_wakes().get(name) or {}).get("by", [])

    def test_an_operator_message_asks_for_a_wake(self):
        root = autonomy.INSTANCES / "evx"
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root, True)
        with mock.patch.object(messages, "instance_root", return_value=root):
            messages.deliver_background("evx", "rework section 2", shown="rework section 2")
        self.assertEqual(self.by("evx"), ["message from the operator"])   # before: it waited for the next timed wake

    def test_another_instance_wakes_it_at_most_once_per_floor(self):
        from supervisor.broker import Broker
        project = Path(tempfile.mkdtemp(prefix="shaft-proj-"))
        self.addCleanup(shutil.rmtree, project, True)
        (project / "instances" / "evy").mkdir(parents=True)
        b = object.__new__(Broker)
        b.name, b.project = "wren", project
        st = {"cycles": {"evy": (datetime.now() - timedelta(minutes=11)).isoformat(timespec="seconds")}}
        (self.sup / "schedule.json").write_text(json.dumps(st))
        b._deliver("evy", "inbox/m1.md", "hi", "message", NOW, "hi")
        self.assertTrue((project / "instances" / "evy" / "inbox" / "m1.md").exists())
        self.assertEqual(self.by("evy"), ["message from wren"])            # past the floor: due now
        self.assertNotIn("not_before", jobs.pending_wakes()["evy"])

    def test_a_message_inside_the_floor_is_paced_not_dropped(self):
        # Codex on the C15 lane: inside the recipient's floor the request was dropped (it waited for the timed wake,
        # up to a full interval). Now it is queued for the floor's end and the jobs' tick holds it until then.
        from supervisor.broker import Broker
        project = Path(tempfile.mkdtemp(prefix="shaft-proj-"))
        self.addCleanup(shutil.rmtree, project, True)
        (project / "instances" / "evp" / "shaft").mkdir(parents=True)
        b = object.__new__(Broker)
        b.name, b.project = "wren", project
        began = datetime.now().replace(microsecond=0) - timedelta(minutes=2)   # a cycle that has ended since
        (self.sup / "schedule.json").write_text(json.dumps({"cycles": {"evp": began.isoformat()}}))
        b._deliver("evp", "inbox/m1.md", "hi", "message", NOW, "hi")
        req = jobs.pending_wakes()["evp"]
        self.assertEqual(req["by"], ["message from wren"])                  # before: [] (dropped)
        self.assertEqual(req["not_before"], (began + timedelta(minutes=10)).isoformat())
        started = []
        tick = lambda now: jobs.tick({}, now, lambda a: started.append(a) or object(), ["evp"], lambda n: "")
        with mock.patch.object(jobs, "log"), mock.patch("supervisor.core.paused", return_value=False), \
                mock.patch.object(jobs, "declared", return_value=([], [])):
            tick(began + timedelta(minutes=9))
            self.assertEqual(started, [])                                  # inside the floor: kept
            self.assertIn("evp", jobs.pending_wakes())
            tick(began + timedelta(minutes=10))
        self.assertEqual(started, [["wake", "evp"]])

    def test_an_operator_message_lifts_the_pacing(self):
        jobs.request_wake("evq", "message from wren", datetime.now() + timedelta(minutes=8))
        jobs.request_wake("evq", "message from the operator")
        self.assertNotIn("not_before", jobs.pending_wakes()["evq"])
        jobs.take_wake("evq")
        jobs.request_wake("evq", "message from the operator")               # due now: a paced one does not delay it
        jobs.request_wake("evq", "message from wren", datetime.now() + timedelta(minutes=8))
        self.assertNotIn("not_before", jobs.pending_wakes()["evq"])

    def test_a_cycle_after_the_request_drops_it(self):
        # A deferred request, or one kept while a timed wake's cycle ran, would wake the instance again for what that
        # cycle already saw.
        (autonomy.INSTANCES / "evd" / "shaft").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, autonomy.INSTANCES / "evd", True)
        jobs.request_wake("evd", "message from wren", datetime.now() + timedelta(minutes=5))
        later = (datetime.now() + timedelta(seconds=2)).isoformat(timespec="seconds")
        (self.sup / "schedule.json").write_text(json.dumps({"cycles": {"evd": later}}))
        started = []
        with mock.patch.object(jobs, "log") as log, mock.patch("supervisor.core.paused", return_value=False), \
                mock.patch.object(jobs, "INSTANCES", autonomy.INSTANCES):
            jobs.tick({}, datetime.now() + timedelta(minutes=6), lambda a: started.append(a) or object(), [],
                      lambda n: "")
        self.assertEqual(started, [])
        self.assertNotIn("evd", jobs.pending_wakes())
        self.assertIn("came after", log.call_args.args[0])
        jobs.request_wake("evd", "message from wren")                      # one after the last cycle began: kept
        st = self.st()
        st["cycles"]["evd"] = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
        (self.sup / "schedule.json").write_text(json.dumps(st))
        with mock.patch.object(jobs, "log"), mock.patch("supervisor.core.paused", return_value=False), \
                mock.patch.object(jobs, "INSTANCES", autonomy.INSTANCES), mock.patch("supervisor.autonomy.record"):
            jobs.tick({}, datetime.now(), lambda a: started.append(a) or object(), [], lambda n: "")
        self.assertEqual(started, [["wake", "evd"]])

    def test_an_event_wake_never_fails_the_delivery(self):
        # Found by the suite: instance_root raises SystemExit for a name with no folder, which `except Exception`
        # let through — the message was delivered and its sender's call failed.
        with mock.patch.object(autonomy, "wake_interval", side_effect=SystemExit("no instance named 'x'")):
            autonomy.event_wake("x", "message from the operator")
        self.assertEqual(self.by("x"), [])

    def test_no_event_wake_when_paused_or_never_woken(self):
        with mock.patch.object(autonomy, "paused", return_value=True):
            autonomy.event_wake("evz", "message from the operator")
        with mock.patch.object(autonomy, "wake_interval", return_value=None):
            autonomy.event_wake("evz", "message from the operator")
        self.assertEqual(self.by("evz"), [])

    def test_a_commitment_falling_due_asks_once(self):
        st = {"cycles": {"evc": "2026-11-03T11:00:00"}}
        due = [{"id": "c1103-0001-1", "due_at": D("2026-11-03T11:30:00")}]
        with mock.patch.object(commitments, "coming_due", return_value=due) as coming:
            autonomy.commitment_wakes(NOW, st, ["evc"], {}, {})
            autonomy.commitment_wakes(NOW, st, ["evc"], {}, {})
            coming.assert_called_with("evc", D("2026-11-03T11:00:00"), NOW)
            self.assertEqual(self.by("evc"), ["commitment c1103-0001-1 due"])
            jobs.take_wake("evc")
            autonomy.commitment_wakes(NOW, st, ["evc"], {"wake evc": None}, {})   # its wake is starting
            self.assertEqual(self.by("evc"), [])
            # Codex on the C15 lane: an instance with no cycle on record (just migrated, or only ever run by hand
            # before cycles were stamped) was skipped, its due commitment left to the next timed wake.
            autonomy.commitment_wakes(NOW, {}, ["evc"], {}, {})
            coming.assert_called_with("evc", None, NOW)
        self.assertEqual(self.by("evc"), ["commitment c1103-0001-1 due"])

    def test_the_jobs_tick_names_who_asked(self):
        jobs.request_wake("evj", "watcher")
        jobs.request_wake("evj", "message from the operator")
        started = []
        (autonomy.INSTANCES / "evj" / "shaft").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, autonomy.INSTANCES / "evj", True)
        with mock.patch.object(jobs, "log") as log, mock.patch("supervisor.autonomy.record"), \
                mock.patch.object(jobs, "INSTANCES", autonomy.INSTANCES), \
                mock.patch("supervisor.core.paused", return_value=False):
            jobs.tick({}, NOW, lambda a: started.append(a) or object(), [], lambda n: "")
        self.assertEqual(started, [["wake", "evj"]])
        self.assertIn("woken by its job(s) watcher; message from the operator", log.call_args.args[0])


class CycleClockTest(StateCase):
    """The cycle stamp (wake_gate's `since`, the commitment wakes' cutoff) is written by the live cycle itself, under its
    run lock (Codex on the C15 lane)."""

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.old = "2026-01-02T11:00:00"
        (self.sup / "schedule.json").write_text(json.dumps({
            "wakes": {"wren": self.old}, "cycles": {"wren": self.old},
            "skipped": {"wren": {"at": self.old, "why": "nothing to do: inbox empty"}}}))
        for p in (mock.patch.object(runner, "instance_root", return_value=self.root),
                  mock.patch.object(runner, "log"), mock.patch.object(autonomy, "log"),
                  mock.patch.object(runner, "_second_judge_later")):
            p.start()
            self.addCleanup(p.stop)

    def test_a_busy_wake_keeps_the_last_cycle_and_its_skip(self):
        # It stamped the cycle before run_agent took the lock: a wake that lost the lock to another run moved the
        # commitment cutoff past commitments no cycle saw, and cleared the shown skip.
        with mock.patch.object(runner._Held, "acquire", side_effect=runner.Busy("wren")), \
                mock.patch.object(autonomy, "_awake", return_value="talk since 10:58"):
            self.assertEqual(autonomy.wake("wren"), 0)
        st = self.st()
        self.assertEqual(st["cycles"]["wren"], self.old)
        self.assertEqual(st["skipped"]["wren"]["why"], "nothing to do: inbox empty")
        self.assertNotEqual(st["wakes"]["wren"], self.old)                  # its interval restarts, as before

    def test_a_refused_run_records_no_cycle(self):
        with mock.patch.object(runner._Held, "acquire"), mock.patch.object(runner._Held, "release"), \
                mock.patch.object(runner, "_clean_tmp"), \
                mock.patch.object(runner, "_reconcile", side_effect=runner.RunRefused("a git merge is in progress")), \
                mock.patch.object(runner, "_refused", return_value=1):
            self.assertEqual(runner.run_agent("wren", "live", ["live"], interactive=False, wait_lock=False), 1)
        self.assertEqual(self.st()["cycles"]["wren"], self.old)

    def launched(self, argv: list[str]) -> None:
        """`shaft <argv>` through the real dispatch, stopped as the agent would start (after the stamp)."""
        from supervisor import __main__ as cli

        class Started(Exception):
            pass
        with mock.patch.object(runner._Held, "acquire"), mock.patch.object(runner._Held, "release"), \
                mock.patch.object(runner, "_clean_tmp"), mock.patch.object(runner, "_reconcile"), \
                mock.patch.object(runner, "_snapshot", return_value={}), \
                mock.patch.object(runner, "_launch", return_value=(mock.Mock(), ["agent"], {})), \
                mock.patch.object(runner, "_trap_stop", side_effect=Started), \
                mock.patch.object(cli, "paused", return_value=False):
            with self.assertRaises(Started):
                cli.main(argv)

    def test_shaft_live_by_hand_stamps_the_cycle(self):
        # Before: only wake() stamped it, so a manual `shaft live` left the gate and the commitment cutoff on the older
        # scheduled cycle.
        self.launched(["live", "wren"])
        st = self.st()
        self.assertGreater(st["cycles"]["wren"], self.old)
        self.assertEqual(st["cycles"]["wren"], st["wakes"]["wren"])
        self.assertNotIn("wren", st["skipped"])

    def test_other_runs_stamp_no_cycle(self):
        self.launched(["reflect", "wren"])
        self.assertEqual(self.st()["cycles"]["wren"], self.old)


class ServeTest(StateCase):
    """The gate and the event wakes are wired into the scheduler's loop."""

    def tick(self, facts: WakeFacts, polls: int = 1) -> tuple[list, list[str]]:
        class Stop(Exception):
            pass
        started, logged = [], []
        def log(message):
            logged.append(message)
            core.log(message)

        patches = [mock.patch.object(autonomy, "SUP_DIR", self.sup),
                   mock.patch.object(core, "SUP_DIR", self.sup),
                   mock.patch.object(autonomy, "log", log),
                   mock.patch.object(autonomy, "stop_children_on_signal"),
                   mock.patch("supervisor.version.record_service"),
                   mock.patch("supervisor.browser.startup_note", return_value=""),
                   mock.patch("supervisor.calendar_proposals.expire"),
                   mock.patch.object(autonomy, "instance_names", return_value=["wren"]),
                   mock.patch.object(autonomy, "paused", return_value=False),
                   mock.patch.object(autonomy, "update_jobs", return_value=[]),
                   mock.patch.object(autonomy, "updating", return_value=False),
                   mock.patch.object(autonomy, "wake_interval", return_value=TEN),
                   mock.patch.object(autonomy, "_awake", return_value=""),
                   mock.patch.object(autonomy, "sleep_due", return_value=False),
                   mock.patch.object(autonomy, "_batteries"), mock.patch.object(autonomy, "gate_warm"),
                   mock.patch.object(autonomy, "wake_facts", return_value=facts),
                   mock.patch("supervisor.jobs.tick"), mock.patch("supervisor.watch.tick"),
                   mock.patch("supervisor.trial.has_work", return_value=False),
                   mock.patch.object(autonomy, "_start", lambda a: started.append(a) or object()),
                   mock.patch.object(autonomy.time, "sleep", side_effect=[None] * (polls - 1) + [Stop])]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            cw = stack.enter_context(mock.patch.object(autonomy, "commitment_wakes"))
            with self.assertRaises(Stop):
                autonomy.serve(poll_s=0)
        self.assertEqual(cw.call_count, polls)
        return started, logged

    def test_m3_skips_are_visible_only_in_own_record_and_throttled(self):
        times = [NOW, NOW + timedelta(minutes=5), NOW + timedelta(hours=1), NOW + timedelta(hours=1, minutes=5)]
        reasons = [f"nothing to do: no commitment due before {at + TEN:%H:%M}" for at in times[:3]]
        reasons.append("already busy")
        with mock.patch.object(autonomy, "datetime", wraps=datetime) as clock, \
                mock.patch.object(autonomy, "decide_wakes", side_effect=[({}, {"wren": r}) for r in reasons]):
            clock.now.side_effect = [at for at in times for _ in range(2)]   # scheduler and record("skip")
            _, logged = self.tick(WakeFacts(), polls=4)
        skips = [line for line in logged if "wake skipped" in line]
        self.assertEqual(len(skips), 3, skips)
        with mock.patch.object(core, "SUP_DIR", self.sup):
            b = broker.Broker("wren", self.sup, {}, self.sup / "broker.log", root=self.sup, share=False)
            answer = b.call("record", {"kind": "runs"})
            self.assertTrue(answer["ok"], answer)
            self.assertIn("wren: wake skipped — nothing to do", answer["result"])
            self.assertIn("wren: wake skipped — already awake (already busy)", answer["result"])
            b.name = "kant"
            self.assertNotIn("wake skipped", b.call("record", {"kind": "runs"})["result"])

    def test_nothing_to_do_skips_then_a_message_wakes(self):
        recent = (datetime.now() - timedelta(minutes=12)).isoformat(timespec="seconds")
        (self.sup / "schedule.json").write_text(json.dumps({"wakes": {"wren": recent}, "cycles": {"wren": recent}}))
        started, logged = self.tick(WakeFacts())
        self.assertEqual(started, [])                                      # the old loop started ["wake", "wren"]
        self.assertTrue(any("wren: wake skipped — nothing to do" in l for l in logged), logged)
        self.assertTrue(autonomy.last_skip("wren").startswith(autonomy.NOTHING_TO_DO))
        st = self.st()
        st["wakes"]["wren"] = recent                                       # its interval passed again
        (self.sup / "schedule.json").write_text(json.dumps(st))
        started, logged = self.tick(WakeFacts(unread=1))
        self.assertEqual(started, [["wake", "wren"]])
        self.assertIn("scheduler: waking wren — 1 unread message(s) in its inbox", logged)

    def test_status_shows_the_skip(self):
        autonomy.record("skip", "wren", "nothing to do: inbox empty")
        out = []
        with mock.patch.object(autonomy, "service"), mock.patch.object(autonomy, "instance_names", return_value=["wren"]), \
                mock.patch.object(autonomy, "wake_interval", return_value=TEN), \
                mock.patch.object(autonomy, "_launchctl", return_value=mock.Mock(returncode=1)), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            autonomy.autonomy("status")
        self.assertTrue(any("last wake skipped: nothing to do: inbox empty" in l for l in out), out)


class PauseDeadlineTest(unittest.TestCase):
    """B13: the wait for a paused run's acknowledgement had no deadline."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / ".tmp").mkdir()
        self.proc = mock.Mock(pid=999999)
        self.proc.poll.return_value = None                     # still running, never acknowledging
        (self.root / ".tmp" / f"pausable-{self.proc.pid}").write_text("x")

    def pause(self, run: dict, until=None) -> float | None:
        out = []
        t = threading.Thread(target=lambda: out.append(runner._pause("shaft", self.root, "live", self.proc, mock.Mock(),
                                                                     run, mock.Mock(), until=until)), daemon=True)
        t.start()
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "the pause wait never gave up")
        return out[0]

    def test_an_unacknowledged_pause_is_given_up_for_a_wrap_up(self):
        run = {}
        with mock.patch.object(runner, "PAUSE_ACK_S", 0.3), mock.patch.object(runner, "request_yield") as yielded, \
                mock.patch("os.kill") as kill, mock.patch.object(runner, "log") as log:
            self.assertEqual(self.pause(run), 0.0)
            self.assertEqual(self.pause(run), 0.0)                 # the next poll: not signalled again
        self.assertEqual(kill.call_count, 1)
        yielded.assert_called_once_with(["shaft"])
        self.assertFalse((self.root / ".tmp" / f"pause-{self.proc.pid}").exists())   # a late ack resumes at once
        self.assertIn("did not acknowledge its pause", log.call_args.args[0])

    def test_the_run_budget_ends_the_wait(self):
        with mock.patch.object(runner, "request_yield"), mock.patch("os.kill"), mock.patch.object(runner, "log"):
            self.assertEqual(self.pause({}, until=time.monotonic() - 1), 0.0)


class StderrTest(unittest.TestCase):
    """B14: a scheduler child's stderr is read while it runs."""

    def child(self, code: str) -> subprocess.Popen:
        real = subprocess.Popen

        def popen(cmd, **kw):                              # _start's own options, another program
            return real([sys.executable, "-c", code], **{**kw, "cwd": None})
        with mock.patch.object(autonomy.subprocess, "Popen", popen), mock.patch.object(autonomy, "log"):
            proc = autonomy._start(["wake", "x"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def test_a_child_writing_more_than_a_pipe_holds_ends(self):
        proc = self.child("import sys; sys.stderr.write('x' * 400000 + 'the end'); sys.exit(2)")
        self.assertEqual(proc.wait(timeout=20), 2)         # read after it ended, it blocked on its write for good
        self.assertTrue(autonomy.stderr_tail(proc).endswith("the end"))

    def test_a_grandchild_holding_the_pipe_never_blocks_the_loop(self):
        proc = self.child("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; "
                          "time.sleep(30)']); sys.stderr.write('boom'); sys.stderr.flush()")
        proc.wait(timeout=20)
        t0 = time.monotonic()
        self.assertEqual(autonomy.stderr_tail(proc), "boom")
        self.assertLess(time.monotonic() - t0, autonomy.STDERR_JOIN_S + 1)

    def test_a_child_without_the_reader_says_nothing(self):
        self.assertEqual(autonomy.stderr_tail(mock.Mock(spec=["poll", "returncode"])), "")


if __name__ == "__main__":
    unittest.main()
