"""Idle exits preserve event wakes and never enter either judging ledger."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, commitments, consult, core, delete, idle, jobs, judge, metrics, trial

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
        for module in (core, metrics, commitments, autonomy, jobs, trial):
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
        b.run_started = (NOW - timedelta(minutes=10)).isoformat()
        return b


class IdleExitTest(IsolatedCase):
    def test_live_exit_records_one_row_and_schedule_without_failure(self):
        """An exit must retain the supervisor run stamp rather than just its recording time."""
        b = self.box()
        result = b.call("nothing_to_do", {"reason": "Inbox empty; no work due"})
        self.assertTrue(result["ok"], result)
        self.assertIn("not a failure", result["result"])
        row, = idle.rows("ember")
        self.assertEqual(set(row), {"at", "run", "reason"})
        self.assertEqual(row["run"], b.run_started)
        self.assertEqual(row["reason"], "Inbox empty; no work due")
        self.assertIn("ember", json.loads(autonomy.STATE.read_text())["idle_exits"])

    def test_nonlive_prior_work_and_empty_reason_never_record_exit(self):
        """Delegation, predictions and mutations must not escape judging as idle cycles."""
        cases = [("talk", {}, "nothing due", "not counted")]
        prior = ("send_message", "mail_draft", "mail_draft_update", "calendar_create", "calendar_move",
                 "calendar_respond", "delegate_coding", "set_config", "secret_set", "secret_delete",
                 "trial", "record_order", "predict")
        cases += [("live", {tool: 1}, "nothing due", "not counted") for tool in prior]
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

    def test_run_identity_replaces_windows_and_previous_episode_fallback(self):
        """An old exit cannot exempt a backdated window or an episode without its run id."""
        run = "2026-11-11T09:00:00"
        eps = [{"ts": "2026-11-11T10:00:00", "run_started": run},
               {"ts": "2026-11-11T11:00:00"},
               {"ts": "2026-11-11T12:00:00", "run_started": "2026-11-11T08:00:00"}]
        for at, expected in [("08:59:59", set()), ("09:00:00", {eps[0]["ts"]}),
                             ("10:00:00", {eps[0]["ts"]}), ("10:00:01", set()),
                             ("11:00:00", set()), ("11:00:01", set())]:
            with self.subTest(at=at):
                self.write(idle.path("ember"), [{"at": "2026-11-11T" + at, "run": run, "reason": "empty"}])
                self.assertEqual(idle.exits("ember", eps), expected)

    def test_duplicate_run_claim_only_exempts_earliest_episode(self):
        """Duplicating a run id must not hide a second episode from judging."""
        run = "2026-11-11T09:00:00"
        idle.record("ember", run, "empty", NOW - timedelta(hours=2))
        for later in ("2026-11-11T12:00:00", "2026-11-11 12:00:00"):
            with self.subTest(later=later):
                eps = [{"ts": later, "run_started": run},
                       {"ts": "2026-11-11T11:00:00", "run_started": run}]
                self.assertEqual(idle.exits("ember", eps), {eps[1]["ts"]})

    def test_unconsumed_exit_cannot_be_claimed_by_next_day_forged_episode(self):
        run = (NOW - timedelta(minutes=10)).isoformat()
        idle.record("ember", run, "empty", NOW)
        ep = {"ts": (NOW + timedelta(days=1)).isoformat(), "run_started": run}
        self.assertEqual(idle.exits("ember", [ep]), set())
        ep["ts"] = (NOW + timedelta(minutes=1)).isoformat()
        self.assertEqual(idle.exits("ember", [ep]), {ep["ts"]})

    def test_intervening_episode_end_prevents_claiming_unconsumed_exit(self):
        run = (NOW - timedelta(minutes=10)).isoformat()
        idle.record("ember", run, "empty", NOW)
        ep = {"ts": (NOW + timedelta(minutes=20)).isoformat(), "run_started": run}
        other = {"ts": (NOW + timedelta(minutes=10)).isoformat()}
        self.assertEqual(idle.exits("ember", [ep, other]), set())
        for at in (NOW, NOW + timedelta(minutes=20)):
            other["ts"] = at.isoformat()
            self.assertEqual(idle.exits("ember", [ep, other]), {ep["ts"]})

    def test_exit_times_parse_both_separators_and_kept_code_prevents_exemption(self):
        """String ordering must not lose exits or hide a cycle that kept code."""
        for sep in ("T", " "):
            with self.subTest(separator=sep):
                run = "2026-11-11" + sep + "09:00:00"
                ep = {"ts": "2026-11-11" + sep + "12:00:00", "run_started": run}
                self.write(idle.path("ember"), [{"at": "2026-11-11" + (" " if sep == "T" else "T")
                    + "10:00:00", "run": run, "reason": "empty"}])
                self.assertEqual(idle.exits("ember", [ep]), {ep["ts"]})
                self.assertEqual(idle.exits("ember", [ep], changed=["2026-11-11T11:00:00"]), set())

    def test_stats_counts_recent_window_and_empty_is_empty(self):
        """Withdrawn and old exits must not inflate the standing idle count."""
        self.assertEqual(idle.stats_lines("ember", NOW), [])
        for age in (idle.WINDOW_DAYS + 1, idle.WINDOW_DAYS, 1):
            idle.record("ember", str(age), "empty", NOW - timedelta(days=age))
        self.assertIn(f"days): 2.", idle.stats_lines("ember", NOW)[0])
        idle.revoke("ember", "1", "more work", NOW)
        self.assertIn("days): 1.", idle.stats_lines("ember", NOW)[0])

    def test_judging_skips_idle_with_or_without_intent_but_keeps_no_intent_rule(self):
        """Run-bound idle exits must bypass judging without exempting ordinary goalless cycles."""
        eps = [{"kind": "live", "ts": (NOW + timedelta(hours=i)).isoformat(),
                "run_started": (NOW + timedelta(hours=i, minutes=-10)).isoformat(),
                "intent": {"goal": "check work"} if i == 1 else None, "tools": []} for i in range(3)]
        self.write(self.root / "memory/episodes/day.jsonl", eps)
        for ep in eps[:2]:
            idle.record("ember", ep["run_started"], "empty", datetime.fromisoformat(ep["ts"]))
        with mock.patch.object(judge, "JUDGE_EVERY", 1), mock.patch.object(judge, "ask") as ask, \
             mock.patch.object(metrics, "cycle_model", return_value="example"):
            self.assertEqual(metrics.judge_pending("ember"), 1)
        ask.assert_not_called()
        row, = metrics.judgments("ember")
        self.assertEqual((row["episode_ts"], row["failure_mode"], row["judge"]),
                         (eps[2]["ts"], "never declared an intent", "rule"))
        self.assertEqual(metrics.unsampled_rows("ember"), [])

    def test_idle_episode_is_not_added_to_unsampled_even_when_sampling_rejects(self):
        """An idle run must not pollute the unsampled ledger when the sampling coin rejects it."""
        ep = {"kind": "live", "ts": NOW.isoformat(), "run_started": (NOW - timedelta(minutes=5)).isoformat()}
        self.write(self.root / "memory/episodes/day.jsonl", [ep])
        idle.record("ember", ep["run_started"], "empty", NOW)
        with mock.patch.object(judge, "JUDGE_EVERY", 100), mock.patch.object(metrics.secrets, "randbelow", return_value=1):
            self.assertEqual(metrics.judge_pending("ember"), 0)
        self.assertEqual(metrics.unsampled_rows("ember"), [])

    def test_delete_removes_only_named_idle_exit_schedule_entry(self):
        autonomy.STATE.write_text(json.dumps({"idle_exits": {"ember": "at", "example": "at"}}))
        with mock.patch.object(delete, "SUP_DIR", self.sup):
            edit = next(e for e in delete.shared_edits("ember") if e.path.name == "schedule.json")
        delete._edit(edit, apply=True)
        self.assertEqual(json.loads(autonomy.STATE.read_text()), {"idle_exits": {"example": "at"}})

    def test_looking_around_before_exit_is_allowed(self):
        """Local runs and HTTP reads before an exit must not count as prior outward work."""
        b = self.box()
        b.counts = {"run": 1, "http_request": 1}
        self.assertIn("recorded:", b.call("nothing_to_do", {"reason": "empty"})["result"])
        self.assertEqual(len(idle.rows("ember")), 1)

    def test_later_work_withdraws_once_still_dispatches_and_is_judged(self):
        """An idle declaration must not conceal later messaging or even a web search."""
        for tool in ("send_message", "web_search"):
            with self.subTest(tool=tool):
                idle.path("ember").unlink(missing_ok=True)
                (self.sup / "judgments/ember.jsonl").unlink(missing_ok=True)
                b = self.box()
                b.call("nothing_to_do", {"reason": "empty"})
                with mock.patch.object(b, "_" + tool, return_value="dispatched") as dispatch:
                    self.assertEqual(b.call(tool, {}), {"ok": True, "result": "dispatched"})
                    self.assertEqual(b.call(tool, {}), {"ok": True, "result": "dispatched"})
                self.assertEqual(dispatch.call_count, 2)
                opened, revoked = idle.rows("ember")
                self.assertEqual(opened["run"], b.run_started)
                self.assertEqual(revoked["run"], b.run_started)
                self.assertIn(tool, revoked["revoked"])
                self.assertFalse(b.idle_declared)
                self.assertNotIn("ember", json.loads(autonomy.STATE.read_text())["idle_exits"])
                ep = {"kind": "live", "ts": revoked["at"], "run_started": b.run_started, "tools": []}
                self.assertEqual(idle.exits("ember", [ep]), set())
                self.write(self.root / "memory/episodes/day.jsonl", [ep])
                with mock.patch.object(judge, "JUDGE_EVERY", 1), \
                     mock.patch.object(metrics, "cycle_model", return_value="example"):
                    self.assertEqual(metrics.judge_pending("ember"), 1)
                self.assertEqual(metrics.judgments("ember")[0]["episode_ts"], ep["ts"])

    def test_quiet_reads_after_exit_do_not_withdraw(self):
        """Reading record after an exit must preserve the scheduler backoff."""
        b = self.box()
        b.call("nothing_to_do", {"reason": "empty"})
        self.assertTrue(b.call("record", {"kind": "runs"})["ok"])
        self.assertTrue(b.idle_declared)
        self.assertEqual(len(idle.rows("ember")), 1)
        self.assertIn("ember", json.loads(autonomy.STATE.read_text())["idle_exits"])

    def test_failed_idle_withdrawal_retries_on_following_tool_call(self):
        b = self.box()
        self.assertTrue(b.call("nothing_to_do", {"reason": "empty"})["ok"])
        withdraw = b._idle_withdrawn
        with mock.patch.object(b, "_idle_withdrawn", side_effect=[OSError("disk error"), None]) as attempt, \
             mock.patch.object(b, "_send_message", return_value="dispatched"):
            self.assertEqual(b.call("send_message", {}), {"ok": True, "result": "dispatched"})
            self.assertTrue(b.idle_declared)
            self.assertEqual(len(idle.rows("ember")), 1)
            attempt.side_effect = withdraw
            self.assertEqual(b.call("send_message", {}), {"ok": True, "result": "dispatched"})
            self.assertEqual(attempt.call_count, 2)
        self.assertFalse(b.idle_declared)
        self.assertIn("send_message", idle.rows("ember")[1]["revoked"])

    def test_failed_idle_backoff_still_allows_later_message_to_revoke_exit(self):
        b = self.box()
        with mock.patch.object(autonomy, "record", side_effect=OSError("disk error")):
            result = b.call("nothing_to_do", {"reason": "empty"})
        self.assertTrue(result["ok"], result)
        self.assertTrue(b.idle_declared)
        self.assertEqual(len(idle.rows("ember")), 1)
        with mock.patch.object(b, "_send_message", return_value="dispatched"):
            self.assertEqual(b.call("send_message", {}), {"ok": True, "result": "dispatched"})
        self.assertFalse(b.idle_declared)
        self.assertIn("send_message", idle.rows("ember")[1]["revoked"])

    def test_unreadable_changes_disable_idle_exemption_for_judging(self):
        ep = {"kind": "live", "ts": NOW.isoformat(), "run_started": (NOW - timedelta(minutes=5)).isoformat()}
        self.write(self.root / "memory/episodes/day.jsonl", [ep])
        self.write(self.sup / "code_changes/ember.jsonl", [{"ts": NOW.isoformat(), "files": ["shaft/example.py"]}])
        idle.record("ember", ep["run_started"], "empty", NOW)
        with mock.patch.object(metrics, "change_outcomes", side_effect=OSError("unreadable changes")), \
             mock.patch.object(judge, "JUDGE_EVERY", 1), \
             mock.patch.object(metrics, "cycle_model", return_value="example"):
            self.assertEqual(metrics.judge_pending("ember"), 1)
        row, = metrics.judgments("ember")
        self.assertEqual((row["episode_ts"], row["failure_mode"]), (ep["ts"], "never declared an intent"))
        self.assertTrue(any("no idle exemption, cycles judged normally" in call.args[0]
                            for call in metrics.log.call_args_list))

    def test_replay_later_work_writes_neither_exit_revoke_nor_schedule(self):
        """Replay withdrawal must never create real exit or scheduling records."""
        b = self.box(replay=True)
        self.assertTrue(b.call("nothing_to_do", {"reason": "empty"})["ok"])
        self.assertFalse(b.idle_declared)
        with mock.patch.object(b, "_web_search", return_value="simulated search"):
            self.assertTrue(b.call("web_search", {})["ok"])
        self.assertFalse(b.idle_declared)
        self.assertFalse(idle.path("ember").exists())
        self.assertFalse(autonomy.STATE.exists())

    def test_commitment_citation_wins_over_idle_exit_and_sampling(self):
        """A kept promise must not escape evidence judging through an idle exit."""
        ep = {"kind": "live", "ts": NOW.isoformat(), "run_started": (NOW - timedelta(minutes=5)).isoformat()}
        self.write(self.root / "memory/episodes/day.jsonl", [ep])
        idle.record("ember", ep["run_started"], "empty", NOW)
        with mock.patch.object(commitments, "ledger", return_value={"example": {
                 "status": "kept", "evidence": "2026-11-11 12:00"}}), \
             mock.patch.object(judge, "JUDGE_EVERY", 100), \
             mock.patch.object(metrics.secrets, "randbelow", return_value=1), \
             mock.patch.object(metrics, "cycle_model", return_value="example"):
            self.assertEqual(metrics.judge_pending("ember"), 1)
        self.assertTrue(metrics.judgments("ember")[0]["cited"])
