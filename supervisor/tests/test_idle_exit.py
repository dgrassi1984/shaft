"""Idle exits preserve event wakes and never enter either judging ledger."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, commitments, consult, core, delete, idle, jobs, judge, metrics, tom, trial

NOW = datetime(2026, 11, 11, 12)


class IsolatedCase(unittest.TestCase):
    """All instance and supervisor paths point at invented temporary records."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-ledgers-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "agents/ember"
        (self.root / "memory/episodes").mkdir(parents=True)
        (self.root / "inbox").mkdir()
        (self.root / "shaft").mkdir()
        (self.root / ".git").write_text("fixture")
        (self.root / "config.env").write_text("")
        self.sup = self.base / "records"
        self.sup.mkdir()
        for module in (core, metrics, commitments, autonomy, jobs, tom, trial):
            if hasattr(module, "INSTANCES"):
                self.enterContext(mock.patch.object(module, "INSTANCES", self.root.parent))
            if hasattr(module, "SUP_DIR"):
                self.enterContext(mock.patch.object(module, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(consult, "USAGE", self.sup / "consult_usage.jsonl"))
        self.enterContext(mock.patch.object(autonomy, "STATE", self.sup / "schedule.json"))
        for module in (core, metrics, autonomy):
            self.enterContext(mock.patch.object(module, "log"))
        self.enterContext(mock.patch.object(core, "disabled_tools", return_value=set()))
        self.enterContext(mock.patch.object(broker.Broker, "_shared_folder", return_value=None))
        self.enterContext(mock.patch.object(broker.Broker, "_make_egress", return_value=None))

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def box(self, replay=False):
        if replay:
            b = broker.ReplayBroker("ember", self.base, {}, self.sup / "broker.log",
                state_root=self.root, start_commit="a" * 40, as_of=NOW, cache={},
                cache_lock=threading.Lock(), billed="ember@trial:example", tools_log=self.sup / "tools.log")
        else:
            b = broker.Broker("ember", self.base, {}, self.sup / "broker.log", root=self.root, network=False)
        b.run_kind = "live"
        return b


class IdleExitTest(IsolatedCase):
    def test_live_exit_records_one_row_and_schedule_without_failure(self):
        result = self.box().call("nothing_to_do", {"reason": "Inbox empty; no work due"})
        self.assertTrue(result["ok"], result)
        self.assertIn("not a failure", result["result"])
        row, = idle.rows("ember")
        self.assertEqual(row["reason"], "Inbox empty; no work due")
        self.assertIn("ember", json.loads(autonomy.STATE.read_text())["idle_exits"])

    def test_nonlive_outward_and_empty_reason_never_record_exit(self):
        cases = [("talk", {}, "nothing due", "not counted")]
        cases += [("live", {tool: 1}, "nothing due", "not counted") for tool in idle.OUTWARD]
        cases += [("live", {}, "  ", "error")]
        for kind, counts, reason, expected in cases:
            with self.subTest(kind=kind, counts=counts, reason=reason):
                b = self.box()
                b.run_kind, b.counts = kind, counts
                result = b.call("nothing_to_do", {"reason": reason})
                self.assertEqual(result["ok"], expected != "error", result)
                if expected != "error":
                    self.assertIn(expected, result["result"])
                self.assertEqual(idle.rows("ember"), [])
                self.assertFalse(autonomy.STATE.exists())

    def test_replay_exit_answers_live_but_only_tags_simulation(self):
        b = self.box(replay=True)
        result = b.call("nothing_to_do", {"reason": "Inbox empty"})
        self.assertTrue(result["ok"], result)
        self.assertIn("not a failure", result["result"])
        self.assertIn("simulated: not recorded", b.trace[-1]["tags"])
        self.assertFalse(idle.path("ember").exists())
        self.assertFalse(autonomy.STATE.exists())

    def test_backoff_is_one_doubling_unread_bypasses_and_cycle_clears(self):
        interval, idle_time = timedelta(minutes=5), timedelta(minutes=30)
        st = {"cycles": {"ember": NOW.isoformat()}}
        def starts(at, facts=autonomy.WakeFacts()):
            return autonomy.decide_wakes(at, st, ["ember"], lambda _: interval, lambda _: None,
                lambda *args: facts, idle_time)[0]
        for exit_at in (NOW, NOW + timedelta(minutes=10)):
            st["idle_exits"] = {"ember": exit_at.isoformat()}
            self.assertEqual(starts(NOW + idle_time), {})
            self.assertIn("ember", starts(NOW + idle_time * autonomy.IDLE_BACKOFF))
            self.assertIn("ember", starts(NOW + interval, autonomy.WakeFacts(unread=1)))
        autonomy.STATE.write_text(json.dumps(st))
        with mock.patch.object(autonomy, "datetime", mock.Mock(now=lambda: NOW)):
            autonomy.record("cycle", "ember")
        st = json.loads(autonomy.STATE.read_text())
        self.assertNotIn("ember", st["idle_exits"])
        self.assertIn("ember", starts(NOW + idle_time))

    def test_idle_windows_are_open_at_start_closed_at_end_with_fallback(self):
        eps = [{"ts": "2026-11-11T10:00:00", "run_started": "2026-11-11T09:00:00"},
               {"ts": "2026-11-11T11:00:00"}]
        for at, expected in [("08:59:59", set()), ("09:00:00", set()),
                             ("10:00:00", {eps[0]["ts"]}), ("10:00:01", {eps[1]["ts"]}),
                             ("11:00:00", {eps[1]["ts"]}), ("11:00:01", set())]:
            with self.subTest(at=at):
                self.write(idle.path("ember"), [{"at": "2026-11-11T" + at, "reason": "empty"}])
                self.assertEqual(idle.exits("ember", eps), expected)

    def test_stats_counts_recent_window_and_empty_is_empty(self):
        self.assertEqual(idle.stats_lines("ember", NOW), [])
        for age in (idle.WINDOW_DAYS + 1, idle.WINDOW_DAYS, 1):
            idle.record("ember", "empty", NOW - timedelta(days=age))
        self.assertIn(f"days): 2.", idle.stats_lines("ember", NOW)[0])

    def test_judging_skips_idle_with_or_without_intent_but_keeps_no_intent_rule(self):
        eps = [{"kind": "live", "ts": (NOW + timedelta(hours=i)).isoformat(),
                "run_started": (NOW + timedelta(hours=i, minutes=-10)).isoformat(),
                "intent": {"goal": "check work"} if i == 1 else None, "tools": []} for i in range(3)]
        self.write(self.root / "memory/episodes/day.jsonl", eps)
        for ep in eps[:2]:
            idle.record("ember", "empty", datetime.fromisoformat(ep["ts"]))
        with mock.patch.object(judge, "JUDGE_EVERY", 1), mock.patch.object(judge, "ask") as ask, \
             mock.patch.object(metrics, "cycle_model", return_value="example"):
            self.assertEqual(metrics.judge_pending("ember"), 1)
        ask.assert_not_called()
        row, = metrics.judgments("ember")
        self.assertEqual((row["episode_ts"], row["failure_mode"], row["judge"]),
                         (eps[2]["ts"], "never declared an intent", "rule"))
        self.assertEqual(metrics.unsampled_rows("ember"), [])

    def test_idle_episode_is_not_added_to_unsampled_even_when_sampling_rejects(self):
        ep = {"kind": "live", "ts": NOW.isoformat(), "run_started": (NOW - timedelta(minutes=5)).isoformat()}
        self.write(self.root / "memory/episodes/day.jsonl", [ep])
        idle.record("ember", "empty", NOW)
        with mock.patch.object(judge, "JUDGE_EVERY", 100), mock.patch.object(metrics.secrets, "randbelow", return_value=1):
            self.assertEqual(metrics.judge_pending("ember"), 0)
        self.assertEqual(metrics.unsampled_rows("ember"), [])

    def test_delete_removes_only_named_idle_exit_schedule_entry(self):
        autonomy.STATE.write_text(json.dumps({"idle_exits": {"ember": "at", "example": "at"}}))
        with mock.patch.object(delete, "SUP_DIR", self.sup):
            edit = next(e for e in delete.shared_edits("ember") if e.path.name == "schedule.json")
        delete._edit(edit, apply=True)
        self.assertEqual(json.loads(autonomy.STATE.read_text()), {"idle_exits": {"example": "at"}})
