"""Gauge choices are append-only; readings use their recorded sources and replay clock."""
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import __main__ as cli
from supervisor import battery, broker, commitments, core, gauges, jobs, metrics, surprise

NOW = datetime(2026, 11, 11, 12)


class GaugesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-gauges-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "agents" / "ember"
        (self.root / "memory/episodes").mkdir(parents=True)
        (self.root / "shaft").mkdir()
        (self.root / ".git").write_text("fixture")
        (self.root / "config.env").write_text("")
        self.sup = self.base / "records"
        self.sup.mkdir()
        for module in (core, metrics, commitments, battery, surprise):
            self.enterContext(mock.patch.object(module, "INSTANCES", self.root.parent))
            self.enterContext(mock.patch.object(module, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(core, "log"))
        self.enterContext(mock.patch.object(cli, "log"))
        self.judgments = self.enterContext(mock.patch.object(metrics, "judgments_for_stats", return_value=[]))

    def write(self, rel, rows):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_checked_rejects_unknown_repeat_excess_and_nonlist_and_accepts_clear(self):
        with self.assertRaises(ValueError) as caught:
            gauges.checked(["unknown"])
        for key in gauges.MENU:
            self.assertIn(key, str(caught.exception))
        for names in (["claims", "claims"], list(gauges.MENU)[:gauges.MAX_CHOSEN + 1],
                      "claims", None, {}, [1]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                gauges.checked(names)
        self.assertEqual(gauges.checked([]), [])

    def test_choice_history_clear_torn_lines_and_as_of(self):
        self.assertEqual(gauges.head_lines("ember", NOW), [])
        first = gauges.choose("ember", ["claims"], "instance", NOW)
        self.assertEqual(set(first), {"ts", "by", "chosen"})
        with gauges.path("ember").open("a") as out:
            out.write('garbage\n{"ts":\n[]\n')
        second = gauges.choose("ember", ["value"], "operator", NOW + timedelta(hours=1))
        self.assertEqual(gauges.choice("ember"), second)
        self.assertEqual(gauges.history("ember"), [first, second])
        self.assertEqual(gauges.choice("ember", NOW), first)
        self.assertEqual(gauges.history("ember", NOW - timedelta(seconds=1)), [])
        cleared = gauges.choose("ember", [], "instance", NOW + timedelta(hours=2))
        self.assertIsNone(gauges.choice("ember"))
        self.assertEqual(gauges.head_lines("ember", NOW), [])
        self.assertEqual(gauges.history("ember"), [first, second, cleared])

    def test_replay_extra_is_visible_without_writing(self):
        extra = [gauges.row(["claims"], "instance", NOW)]
        self.assertEqual(gauges.history("ember", extra=extra), extra)
        self.assertEqual(gauges.choice("ember", extra=extra), extra[0])
        self.assertTrue(gauges.head_lines("ember", NOW, extra=extra))
        self.assertFalse(gauges.path("ember").exists())

    def test_head_orders_gap_shares_with_unmeasurable_last_and_ignores_forged_values(self):
        menu = {k: (k, mock.Mock(return_value=gauges.Reading(text, gap)))
                for k, text, gap in [("a", "small", .1), ("b", "unknown", None),
                                     ("c", "largest", .8), ("d", "tie", .1)]}
        with mock.patch.object(gauges, "MENU", menu):
            row = gauges.choose("ember", list(menu), "instance", NOW)
            row.update(text="forged reading", gap=999)
            gauges.path("ember").write_text(json.dumps(row) + "\n")
            self.assertEqual(gauges.head_lines("ember", NOW)[1:-1],
                             ["- largest", "- small", "- tie", "- unknown"])

    def test_unreadable_source_costs_only_its_own_reading(self):
        menu = {"bad": ("broken", mock.Mock(side_effect=OSError("unreadable"))),
                "good": ("working", mock.Mock(return_value=gauges.Reading("still here", .2)))}
        with mock.patch.object(gauges, "MENU", menu):
            gauges.choose("ember", list(menu), "instance", NOW)
            lines = gauges.head_lines("ember", NOW)
        self.assertEqual(lines[1], "- still here")
        self.assertIn("could not be read", lines[2])

    def test_promises_count_operator_open_overdue_recent_settled_and_old_pending(self):
        rows = []
        for i, days, due, who in [(1, 20, -1, "operator"), (2, 1, 1, "operator"),
                                   (3, 2, 1, "operator"), (4, 1, 1, "peer")]:
            rows.append({"event": "made", "id": f"c1111-1200-{i}",
                         "at": (NOW - timedelta(days=days)).isoformat(), "source": "cycle",
                         "promise": "Send a report", "criterion": "Report delivered", "made_to": who,
                         "due": (NOW + timedelta(days=due)).isoformat()})
        rows.append({"event": "settled", "id": "c1111-1200-3", "at": NOW.isoformat(),
                     "status": "kept", "evidence": "example delivery"})
        self.write(commitments.FILE, rows)
        self.assertEqual(gauges._promises("ember", NOW), gauges.Reading(
            "Promised to the operator and still open: 2 (overdue 1), oldest made 20 days ago", 2 / 3))

    def test_claims_and_unasked_use_their_own_eligible_recent_denominators(self):
        self.judgments.return_value = [
            {"episode_ts": (NOW - timedelta(seconds=1)).isoformat(), "judge": "current", "claim_supported": False,
             "origin": "self", "anticipated": False},
            {"episode_ts": (NOW - timedelta(seconds=1)).isoformat(), "judge": "current", "claim_supported": True,
             "origin": "self", "anticipated": True},
            {"episode_ts": (NOW - timedelta(seconds=1)).isoformat(), "judge": "retired", "claim_supported": False},
            {"episode_ts": (NOW - timedelta(seconds=1)).isoformat(), "judge": "rule", "claim_supported": False},
            {"episode_ts": (NOW - timedelta(days=15)).isoformat(), "judge": "current",
             "origin": "self", "anticipated": False, "claim_supported": False},
            {"episode_ts": (NOW - timedelta(seconds=1)).isoformat(), "origin": "operator", "anticipated": False}]
        with mock.patch.object(metrics, "by_current_judge", side_effect=lambda j: j.get("judge") in ("current", "rule")):
            self.assertEqual(gauges._claims("ember", NOW), gauges.Reading(
                "Claims the tool log did not support: 1 of 2 judged cycles (14 days)", .5))
        self.assertEqual(gauges._unasked("ember", NOW), gauges.Reading(
            "Work I chose myself that the judge found anticipated a need: 1 of 2 judged self-chosen cycles (14 days)", .5))

    def test_value_uses_last_twenty_integer_grades_and_delivery_count(self):
        """Dated grades must retain the last-twenty limit and exclude noninteger values."""
        self.judgments.return_value = [{"value": 1}] * 3 + [
            {"value": v, "delivery_supported": "yes" if v == 5 else "no"} for v in [3, 5] * 10
        ] + [{"value": None}, {"value": True}]
        for row in self.judgments.return_value:
            row["episode_ts"] = NOW.isoformat()
        self.assertEqual(gauges._value("ember", NOW), gauges.Reading(
            "Judged value per cycle, last 20: 4.0 of 5 (1 = nobody would miss it); delivered to the operator in 10", .2))

    def test_corrections_count_open_and_oldest(self):
        rows = [{"event": "recorded", "id": f"k1111-1200-{i}", "what_was_wrong": "Wrong fact",
                 "what_operator_said": "Use the source", "when": (NOW - timedelta(days=i)).isoformat(),
                 "conversation": "example"} for i in (1, 2)]
        rows.append({"event": "closed", "id": "k1111-1200-1", "closed_by": "fact", "evidence": "Source checked"})
        self.write("memory/corrections.jsonl", rows)
        self.assertEqual(gauges._corrections("ember", NOW), gauges.Reading(
            "Corrections from the operator still open: 1 of 2, oldest 2 days old", .5))

    def test_empty_real_readings_have_no_gap_and_say_why(self):
        for build, text in [(gauges._promises, "none recorded"), (gauges._claims, "no judged cycle"),
                            (gauges._unasked, "no judged self-chosen cycle"),
                            (gauges._value, "none graded yet"), (gauges._corrections, "none recorded")]:
            with self.subTest(reading=build.__name__):
                reading = build("ember", NOW)
                self.assertIsNone(reading.gap)
                self.assertIn(text, reading.text)

    def make_broker(self, replay=False):
        self.enterContext(mock.patch.object(core, "disabled_tools", return_value=set()))
        self.enterContext(mock.patch.object(broker.Broker, "_shared_folder", return_value=None))
        if replay:
            return broker.ReplayBroker("ember", self.base, {}, self.sup / "broker.log",
                state_root=self.root, start_commit="a" * 40, as_of=NOW, cache={},
                cache_lock=threading.Lock(), billed="ember@trial:example", tools_log=self.sup / "tools.log")
        return broker.Broker("ember", self.base, {}, self.sup / "broker.log", root=self.root, network=False)

    def test_broker_menu_invalid_choice_and_instance_choice(self):
        box = self.make_broker()
        menu = box.call("gauges", {})
        self.assertTrue(menu["ok"], menu)
        for key in gauges.MENU:
            self.assertIn(key, menu["result"])
        invalid = box.call("gauges", {"choose": ["unknown"]})
        self.assertFalse(invalid["ok"], invalid)
        self.assertIn("The menu:", invalid["error"])
        self.assertFalse(gauges.path("ember").exists())
        self.assertTrue(box.call("gauges", {"choose": ["claims"]})["ok"])
        self.assertEqual(gauges.choice("ember")["by"], "instance")
        self.assertEqual(gauges.choice("ember")["chosen"], ["claims"])

    def test_replay_broker_choice_is_visible_only_in_that_replay(self):
        box = self.make_broker(replay=True)
        self.assertTrue(box.call("gauges", {"choose": ["claims"]})["ok"])
        menu = box.call("gauges", {})
        self.assertTrue(menu["ok"], menu)
        self.assertIn("claims [chosen]", menu["result"])
        self.assertEqual(box.sim_gauges[0]["chosen"], ["claims"])
        self.assertFalse((self.sup / "gauges").exists())
        self.assertNotIn("[chosen]", self.make_broker(replay=True).call("gauges", {})["result"])

    def test_gauges_are_disabled_in_exams_and_jobs(self):
        self.assertIn("gauges", battery.EXAM_OFF)
        self.assertIn("gauges", jobs.OFF_IN_JOBS)
        self.assertIn("gauges", broker.SPECS)

    def test_cli_set_clear_list_and_invalid_name_without_write(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "instance_root", return_value=self.root), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.cmd_gauges("ember", "set", ["unknown"]), 1)
            self.assertFalse(gauges.path("ember").exists())
            self.assertIn("The menu:", err.getvalue())
            self.assertEqual(cli.cmd_gauges("ember", "set", ["claims,value"]), 0)
            self.assertEqual(gauges.choice("ember")["chosen"], ["claims", "value"])
            self.assertEqual(gauges.choice("ember")["by"], "operator")
            self.assertEqual(cli.cmd_gauges("ember", "clear", []), 0)
            self.assertIsNone(gauges.choice("ember"))
            self.assertEqual(cli.cmd_gauges("ember", None, []), 0)
        self.assertEqual([r["chosen"] for r in gauges.history("ember")], [["claims", "value"], []])
        self.assertIn("Choices made:", out.getvalue())
        self.assertIn("by operator: claims, value", out.getvalue())
        self.assertIn("by operator: (none)", out.getvalue())

    def test_replay_head_and_menu_ignore_future_value_battery_and_recurrence(self):
        """Replay readings must not leak future grades, battery scores or repeated failures."""
        past = NOW - timedelta(hours=1)
        future = NOW + timedelta(hours=1)
        self.judgments.return_value = [
            {"episode_ts": past.isoformat(), "value": 2, "delivery_supported": "no"},
            {"episode_ts": future.isoformat(), "value": 5, "delivery_supported": "yes"}]
        rows = [{"at": at.isoformat(), "status": "scored", "category": "self", "arm": "instance",
                 "item": "example", "score": score} for at, score in [(past, 2), (future, 10)]]
        events = [{"episode": at.isoformat(), "episode_kind": "live", "flag": "error",
                   "tool": "read_file", "target": "notes/example.md"} for at in (past, future)]
        gauges.choose("ember", ["value", "battery", "recurrence"], "instance", past)
        with mock.patch.object(battery, "era", return_value={}), \
             mock.patch.object(battery, "results", return_value=rows), \
             mock.patch.object(surprise, "events", return_value=events):
            for text in ("\n".join(gauges.head_lines("ember", future, as_of=NOW)),
                         gauges.menu_text("ember", future, as_of=NOW)):
                self.assertIn("last 1: 2.0 of 5", text)
                self.assertIn("self 2.0 of 10", text)
                self.assertIn("came back in a later cycle: 0 of 1", text)
                self.assertNotIn("could not be read", text)
            self.assertEqual(gauges._value("ember", NOW).gap, .6)
            self.assertEqual(gauges._battery("ember", NOW).gap, .8)
            self.assertEqual(gauges._recurrence("ember", NOW).gap, 0)
            self.assertIn("last 2: 3.5 of 5", gauges._value("ember", future).text)
            self.assertIn("self 6.0 of 10", gauges._battery("ember", future).text)
            self.assertIn("1 of 1", gauges._recurrence("ember", future).text)

    def test_system_exit_source_costs_only_its_gauge_in_head_and_menu(self):
        """A deleted-instance SystemExit must not abort the remaining gauge readings."""
        menu = {"bad": ("broken", mock.Mock(side_effect=SystemExit("missing instance"))),
                "good": ("working", mock.Mock(return_value=gauges.Reading("still here", .2)))}
        with mock.patch.object(gauges, "MENU", menu):
            gauges.choose("ember", list(menu), "instance", NOW)
            for text in ("\n".join(gauges.head_lines("ember", NOW)), gauges.menu_text("ember", NOW)):
                self.assertIn("could not be read (SystemExit: missing instance)", text)
                self.assertIn("still here", text)

    def test_menu_discloses_which_readings_come_from_instance_ledgers(self):
        """The menu must not claim an instance's own files cannot affect any reading."""
        text = gauges.menu_text("ember", NOW)
        self.assertIn("promises, corrections and plan from your own ledgers as the supervisor re-checks them", text)
        self.assertNotIn("Neither you nor a file of yours can change a reading", text)


if __name__ == "__main__":
    unittest.main()
