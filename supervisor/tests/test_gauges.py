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
        self.assertIn("promises and corrections from the rows of your ledgers as the supervisor copied them", text)
        self.assertIn("editing those files does not change the reading", text)
        self.assertIn("plan from your own account of it", text)
        self.assertIn("My plan (my own account)", text)
        self.assertNotIn("Neither you nor a file of yours can change a reading", text)

    # -- #7: promises and corrections are counted from the supervisor's own copy of the ledgers ------------------
    def ledgers(self):
        made = [{"event": "made", "id": f"c1111-1200-{i}", "at": (NOW - timedelta(days=1)).isoformat(),
                 "source": "cycle", "promise": f"Send report {i}", "criterion": "Report delivered",
                 "made_to": "operator", "due": (NOW + timedelta(days=1)).isoformat()} for i in (1, 2)]
        recorded = [{"event": "recorded", "id": f"k1111-1200-{i}", "what_was_wrong": f"Wrong fact {i}",
                     "what_operator_said": "Use the source", "when": (NOW - timedelta(days=i)).isoformat(),
                     "conversation": "example"} for i in (1, 2, 3)]
        return made, recorded

    def test_rows_removed_or_blanked_after_the_supervisor_copied_them_are_still_counted_and_named(self):
        from supervisor import ledger_mirror
        made, recorded = self.ledgers()
        self.write(commitments.FILE, made)
        self.write("memory/corrections.jsonl", recorded)
        self.assertEqual(ledger_mirror.sync("ember", self.root), 5)
        self.assertEqual(ledger_mirror.sync("ember", self.root), 0)        # add-only, never twice
        before = (gauges._promises("ember", NOW), gauges._corrections("ember", NOW))
        self.assertNotIn("ledger", before[0].text + before[1].text)
        # The instance rewrites its files: one promise and one correction removed, another correction blanked.
        self.write(commitments.FILE, made[1:])
        self.write("memory/corrections.jsonl", [recorded[0], {**recorded[1], "what_was_wrong": ""}])
        promises, corrections = gauges._promises("ember", NOW), gauges._corrections("ember", NOW)
        self.assertEqual(promises.gap, before[0].gap)
        self.assertTrue(promises.text.startswith("Promised to the operator and still open: 2 (overdue 0)"))
        self.assertIn("; 1 promise removed from the ledger since it was recorded (still counted)", promises.text)
        self.assertEqual(corrections.gap, before[1].gap)
        self.assertTrue(corrections.text.startswith("Corrections from the operator still open: 3 of 3"))
        self.assertIn("; 1 correction removed from the ledger since it was recorded; 1 correction altered in the "
                      "ledger since it was recorded (still counted)", corrections.text)
        # Emptying the ledger leaves the gauge reading what was recorded, not "none recorded".
        self.write("memory/corrections.jsonl", [])
        self.assertIn("3 corrections removed from the ledger since they were recorded",
                      gauges._corrections("ember", NOW).text)
        # A row the copy does not hold yet still counts, and closing a correction still closes it.
        self.write("memory/corrections.jsonl", recorded + [
            {"event": "closed", "id": "k1111-1200-1", "closed_by": "fact", "evidence": "Source checked"}])
        self.assertEqual(gauges._corrections("ember", NOW).text,
                         "Corrections from the operator still open: 2 of 3, oldest 3 days old")
        self.assertEqual(len(ledger_mirror.rows("ember", "corrections")), 3)      # read, never written by a gauge

    def test_first_copy_takes_the_mind_stores_history_and_commit_state_keeps_it(self):
        from supervisor import ledger_mirror, mind
        made, recorded = self.ledgers()
        mind.init("ember")
        self.write("memory/corrections.jsonl", recorded)
        self.write(commitments.FILE, made)
        self.assertTrue(mind.commit("ember", "ember: live", self.root))
        self.write("memory/corrections.jsonl", recorded[2:])      # rewritten before the copy was ever made
        self.write(commitments.FILE, [])
        with mock.patch.object(core, "git_wt", return_value=mock.Mock(stdout="")):
            self.assertTrue(core.commit_state("ember", "live", [], root=self.root))
        self.assertEqual(len(ledger_mirror.rows("ember", "corrections")), 3)
        self.assertEqual(len(ledger_mirror.rows("ember", "commitments")), 2)
        self.assertIn("2 corrections removed from the ledger", gauges._corrections("ember", NOW).text)
        self.assertIn("2 promises removed from the ledger", gauges._promises("ember", NOW).text)

    def test_a_malformed_row_costs_only_itself(self):
        """Regression (#7 review): one row with a list id made the promises and corrections gauges "could not be
        read", and one non-JSON line in the corrections ledger did the same: every recorded row dropped out."""
        from supervisor import ledger_mirror
        made, recorded = self.ledgers()
        self.write(commitments.FILE, made)
        self.write("memory/corrections.jsonl", recorded)
        ledger_mirror.sync("ember", self.root)
        self.write(commitments.FILE, made + [{"event": "made", "id": []}])
        self.write("memory/corrections.jsonl", recorded + [{"id": {"a": 1}}])
        with (self.root / "memory/corrections.jsonl").open("a") as f:
            f.write("not json\n[1]\n")
        promises, corrections = gauges._promises("ember", NOW), gauges._corrections("ember", NOW)
        self.assertTrue(promises.text.startswith("Promised to the operator and still open: 2"), promises.text)
        self.assertEqual(corrections.text, "Corrections from the operator still open: 3 of 3, oldest 3 days old; "
                                           "2 unreadable lines in the ledger, skipped")

    def test_a_recorded_promise_keeps_the_source_it_had_when_copied(self):
        """Regression (#7 review): a mirrored operator promise was re-checked against the current episodes on every
        read, so removing the conversation it was made in turned it into a promise to itself."""
        from supervisor import ledger_mirror
        made = {"event": "made", "id": "c1111-1200-1", "at": (NOW - timedelta(days=1)).isoformat(), "source": "talk",
                "promise": "Send report", "criterion": "Report delivered", "made_to": "operator", "sourced": True,
                "talk_id": "t1", "due": (NOW + timedelta(days=1)).isoformat()}
        talk = {"ts": (NOW - timedelta(days=1, minutes=1)).isoformat(), "kind": "talk", "role": "user",
                "talk_id": "t1", "content": "please"}
        self.write("memory/episodes/day.jsonl", [talk])
        self.write(commitments.FILE, [made])
        ledger_mirror.sync("ember", self.root)
        self.write("memory/episodes/day.jsonl", [])          # the conversation is gone from the instance's files
        self.assertTrue(gauges._promises("ember", NOW).text.startswith("Promised to the operator and still open: 1"))
        # A promise whose source never held is copied as the instance's own, and stays so.
        self.write(commitments.FILE, [made, {**made, "id": "c1111-1200-2", "talk_id": "never"}])
        ledger_mirror.sync("ember", self.root)
        self.write("memory/episodes/day.jsonl", [{**talk, "talk_id": "never"}])
        self.assertTrue(gauges._promises("ember", NOW).text.startswith("Promised to the operator and still open: 1"))

    def test_a_replay_reads_the_copy_as_it_stood(self):
        """Regression (#7 review): a replay's gauges counted ledger rows the supervisor copied after its moment."""
        from supervisor import ledger_mirror
        made, recorded = self.ledgers()
        self.write("memory/corrections.jsonl", recorded[:1])
        ledger_mirror.sync("ember", self.root, now=NOW - timedelta(hours=2))
        self.write("memory/corrections.jsonl", recorded)
        ledger_mirror.sync("ember", self.root, now=NOW + timedelta(hours=2))
        self.write("memory/corrections.jsonl", [])
        self.assertIn("still open: 3 of 3", gauges._corrections("ember", NOW).text)
        self.assertIn("still open: 1 of 1", gauges._corrections("ember", NOW, as_of=NOW).text)
        self.assertIn("still open: 1 of 1", gauges.menu_text("ember", as_of=NOW))
        # A current row stamped after the replay's moment is not counted either.
        late = {**recorded[0], "id": "k1111-1200-9", "when": (NOW + timedelta(hours=1)).isoformat()}
        self.write("memory/corrections.jsonl", [late])
        self.assertIn("still open: 1 of 1", gauges._corrections("ember", NOW, as_of=NOW).text)

    def test_backfill_is_marked_done_and_retried_if_torn(self):
        """Regression (#7 review): backfill was judged done by the copy being non-empty: an empty ledger walked git
        at every commit, and a torn first copy never walked it again."""
        from supervisor import ledger_mirror
        made, recorded = self.ledgers()
        with mock.patch.object(ledger_mirror, "_history", return_value=[]) as history:
            ledger_mirror.sync("ember", self.root)
            ledger_mirror.sync("ember", self.root)
        self.assertEqual(history.call_count, 2)                     # once per ledger, never again
        path = ledger_mirror.path("ember", "corrections")
        path.write_text('{"seen": "2026-11-11T10:00:00", "row": {"event": "recor')   # an interrupted first copy
        path.with_name("corrections.backfilled").unlink()
        text = "".join(json.dumps(r) + "\n" for r in recorded)
        with mock.patch.object(ledger_mirror, "_history", side_effect=lambda name, rel, project: [text]
                               if rel.endswith("corrections.jsonl") else []):
            self.assertEqual(ledger_mirror.sync("ember", self.root), 3)
        self.assertEqual(len(ledger_mirror.rows("ember", "corrections")), 3)

    def test_the_stats_section_and_claim_lint_still_see_a_removed_settle(self):
        """Regression (#7 review): the promises section and claim lint read the instance's file, so deleting a forged
        "kept" settle (and its promise) took it out of both."""
        from supervisor import claimlint, ledger_mirror
        made, _ = self.ledgers()
        settled = {"event": "settled", "id": made[0]["id"], "at": (NOW - timedelta(hours=1)).isoformat(),
                   "status": "kept", "evidence": (NOW - timedelta(minutes=30)).isoformat() + "#1"}
        ep = {"ts": (NOW - timedelta(minutes=30)).isoformat(), "run_started": (NOW - timedelta(hours=2)).isoformat(),
              "kind": "live", "finish": {}, "tools": [{"tool": "fetch_page", "args": {}, "result": "x"}]}
        self.write("memory/episodes/day.jsonl", [ep])
        self.write(commitments.FILE, made + [settled])
        ledger_mirror.sync("ember", self.root)
        self.write(commitments.FILE, made[1:])
        with mock.patch.object(metrics, "judgments", return_value=[]):
            section = "\n".join(commitments.section("ember"))
            report = commitments.report(["ember"])
        self.assertIn("made 2; kept (verified) 0; kept (verified, evidence not checkable) 0; kept (not verified) 1",
                      section)
        self.assertIn(f"{made[0]['id']}: kept; cited cycle not judged", report)
        self.assertIn("removed from its ledger since recorded", report)
        self.assertEqual([f["ref"] for f in claimlint.lint("ember", ep)], [])   # no record: nothing to flag
        from supervisor import served
        served.begin(NOW - timedelta(days=2))
        served.run_event("ember", ep["run_started"], "live", "start", at=NOW - timedelta(hours=2))
        self.assertEqual([f["ref"] for f in claimlint.lint("ember", ep)], [made[0]["id"]])

    def test_a_row_cannot_claim_it_was_checked_when_copied(self):
        """Regression (#7 re-review): a ledger row carrying "_checked": true skipped the source check."""
        from supervisor import ledger_mirror
        made = {"event": "made", "id": "c1111-1200-1", "at": (NOW - timedelta(days=1)).isoformat(), "source": "talk",
                "promise": "Send report", "criterion": "Report delivered", "made_to": "operator", "sourced": True,
                "talk_id": "never", "due": (NOW + timedelta(days=1)).isoformat(), "_checked": True}
        self.write(commitments.FILE, [made])
        self.assertEqual(gauges._promises("ember", NOW).text, "Promises to the operator: none recorded")
        ledger_mirror.sync("ember", self.root)
        self.assertEqual(gauges._promises("ember", NOW).text, "Promises to the operator: none recorded")

    def test_a_replay_does_not_take_a_later_closure_or_a_later_copy_from_the_file(self):
        """Regression (#7 re-review): a correction's `closed` event (stamped `at`, not `when`) and a row copied after
        the replay's moment but stamped before it reached the replay through the current file."""
        from supervisor import ledger_mirror
        _, recorded = self.ledgers()
        closed = {"event": "closed", "id": recorded[0]["id"], "closed_by": "fact", "evidence": "Checked",
                  "at": (NOW + timedelta(hours=1)).isoformat()}
        self.write("memory/corrections.jsonl", recorded[:1])
        ledger_mirror.sync("ember", self.root, now=NOW - timedelta(hours=2))
        self.write("memory/corrections.jsonl", recorded[:1] + [closed])
        self.assertIn("still open: 1 of 1", gauges._corrections("ember", NOW, as_of=NOW).text)
        self.write("memory/corrections.jsonl", recorded)
        ledger_mirror.sync("ember", self.root, now=NOW + timedelta(hours=2))
        self.assertIn("still open: 1 of 1", gauges._corrections("ember", NOW, as_of=NOW).text)
        aware = (NOW).astimezone()
        self.assertIn("still open: 1 of 1", gauges._corrections("ember", NOW, as_of=aware).text)

    def test_a_lone_surrogate_does_not_stop_the_copy(self):
        """Regression (#7 re-review): a row with an escaped lone surrogate could not be written, and the whole batch
        (and the corrections after it) went uncopied."""
        from supervisor import ledger_mirror
        made, recorded = self.ledgers()
        path = self.root / commitments.FILE
        path.write_text(json.dumps({**made[0], "promise": "bad \ud800 text"}) + "\n" + json.dumps(made[1]) + "\n")
        self.write("memory/corrections.jsonl", recorded)
        self.assertEqual(ledger_mirror.sync("ember", self.root), 5)
        self.assertEqual(ledger_mirror.rows("ember", "commitments")[0]["promise"], "bad \ud800 text")

    def test_claim_lint_skips_a_settle_with_a_bad_id(self):
        """Regression (#7 re-review): a mirrored kept settle with a list id raised TypeError in claim lint."""
        from supervisor import claimlint
        ep = {"ts": (NOW - timedelta(minutes=30)).isoformat(), "run_started": (NOW - timedelta(hours=2)).isoformat(),
              "kind": "live", "finish": {}, "tools": []}
        self.write(commitments.FILE, [{"event": "settled", "id": [], "status": "kept", "evidence": "x",
                                       "at": (NOW - timedelta(hours=1)).isoformat()},
                                      {"event": "settled", "status": "kept", "evidence": "x",
                                       "at": (NOW - timedelta(hours=1)).isoformat()}])
        self.assertEqual(claimlint.lint("ember", ep), [])


if __name__ == "__main__":
    unittest.main()
