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
from supervisor import broker, claimlint, core, factledger, families, metrics, orders, provenance, web_config
from supervisor.tests import test_factledger, test_unrecorded, test_usage


class ReviewModulesTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="shaft-review-")))
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.base / "sup"))
        self.enterContext(mock.patch.object(core, "INSTANCES", self.base / "agents"))
        self.now = datetime(2026, 11, 11, 12)

    def test_f3_operator_takes_over_same_id_and_instance_cannot_change_it(self):
        """F3: operator adoption changes ownership even when the text differs only in case."""
        for by in ("self", "instance"):
            with self.subTest(by=by):
                original, _ = orders.add("ember", f"Keep {by} notes short.", by, "cycle", self.now)
                text = original["text"].upper()
                output = io.StringIO()
                with mock.patch.object(cli, "instance_root", return_value=self.base), \
                        mock.patch.object(cli, "log"), redirect_stdout(output):
                    self.assertEqual(cli.cmd_orders("ember", "add", [text]), 0)
                self.assertIn("took over", output.getvalue())
                adopted = next(o for o in orders.listing("ember") if o["id"] == original["id"])
                self.assertEqual((adopted["by"], adopted["source"]), ("operator", "cli"))
                with self.assertRaisesRegex(ValueError, "only the operator"):
                    orders.drop_own("ember", original["id"])
                for caller in ("self", "instance"):
                    same, new = orders.add("ember", text, caller, "other", self.now)
                    self.assertFalse(new)
                    self.assertEqual(same, adopted)
                    self.assertIn("from the operator", orders.line(same))
                b = object.__new__(broker.Broker)
                b.name = "ember"
                with mock.patch.object(b, "_order_source", return_value="cycle"):
                    result = b._record_order(text, "cycle", own=True)
                self.assertIn(f"already standing order {original['id']}", result)
                self.assertIn("only the operator drops it", result)

    def test_f3_self_duplicate_and_ui_name_ownership(self):
        """F3: duplicate instance records keep their author; the UI names self rules truthfully."""
        original, _ = orders.add("ember", "Keep notes short.", "self", "cycle", self.now)
        for by in ("self", "instance"):
            self.assertEqual(orders.add("ember", "KEEP NOTES SHORT.", by, "other", self.now), (original, False))
        with mock.patch.object(orders, "corrections", return_value=[]):
            text = str(web_config.orders_panel("ember"))
        self.assertIn("ember&#x27;s own rule", text)
        self.assertNotIn("from what you said", text)
        b = object.__new__(broker.Broker)
        b.name = "ember"
        with mock.patch.object(b, "_order_source", return_value="cycle"):
            result = b._record_order("KEEP NOTES SHORT.", "cycle", own=True)
        self.assertIn("already standing order o1", result)
        self.assertIn("your own rule", result)

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

    def test_f7_mixed_separators_advance_cursor_and_filter_findings(self):
        """F7: space and T timestamps compare chronologically, including after a sleep cutoff."""
        first = {"ts": "2026-11-11T10:00:00", "kind": "live"}
        second = {"ts": "2026-11-11 11:00:00", "kind": "live"}
        def lint(name, ep):
            return [{"episode_ts": ep["ts"], "what": "example finding"}]
        with mock.patch.object(metrics, "live_episodes", return_value=[first]) as live, \
                mock.patch.object(claimlint, "lint", side_effect=lint):
            self.assertEqual(claimlint.run("ember"), 1)
            live.return_value = [first, second]
            self.assertEqual(claimlint.run("ember"), 1)
        self.assertEqual(claimlint.head_lines("ember", since="2026-11-11 10:30:00"),
                         ["- 2026-11-11 11:00: example finding"])
        self.assertEqual(claimlint.head_lines("ember", since="2026-11-11T11:00:00"), [])
        self.assertFalse(claimlint._candidate({"tool": "retire_fact"}, "notion"))

    def test_f11_torn_lines_do_not_block_add_or_drop(self):
        """F11: malformed ledger lines cannot prevent or swallow subsequent add/drop events."""
        original, _ = orders.add("ember", "Keep notes short.", "operator", "cli", self.now)
        with orders.path("ember").open("a") as file:
            file.write('null\n{"id":')
        added, new = orders.add("ember", "Check sources.", "self", "cycle", self.now)
        self.assertTrue(new)
        self.assertEqual(added["id"], "o2")
        with orders.path("ember").open("a") as file:
            file.write('{"id":')
        orders.drop("ember", original["id"], self.now)
        self.assertEqual([o["id"] for o in orders.listing("ember")], ["o2"])


class FactRegressions(unittest.TestCase):
    def setUp(self):
        self.f = test_factledger.FactLedgerTest()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_f8_archive_facts_are_never_classified(self):
        """F8: source classification and episode parsing happen once for current facts only."""
        current = self.f.fact(src="log:2026-10-01T09:00:00")
        self.f.write_facts([current])
        self.f.write_facts([self.f.fact("Archived orchard survey found damaged trees.")], archive=True)
        real_check = provenance._Evidence.check
        seen = []
        def check(evidence, fact):
            seen.append(fact)
            return real_check(evidence, fact)
        with mock.patch.object(provenance._Evidence, "check", new=check), \
                mock.patch.object(provenance, "_episodes", wraps=provenance._episodes) as parsed:
            self.assertEqual(len(factledger.rows("ember")), 1)
        self.assertEqual(seen, [current])
        parsed.assert_called_once_with("ember")

    def test_f8_one_shared_shingle_is_insufficient(self):
        """F8: one chance window cannot match a long fact, while sufficient overlap can."""
        text = " ".join(f"word{i}" for i in range(30))
        row = {**self.f.fact(text), "status": "unmatched"}
        quote = " ".join(f"word{i}" for i in range(8))
        self.assertIsNone(factledger.match_claim("ember", quote, rows=[row]))
        self.assertEqual(factledger.match_claim("ember", text, rows=[row]), row)

    def test_f8_invalid_and_date_only_fact_dates(self):
        """F8: invalid dates have unknown uses; date-only facts exclude their entire creation day."""
        self.f.write_facts([self.f.fact(date="broken"), self.f.fact(date="2026-10-02")])
        self.f.write_episodes([self.f.episode("2026-10-02T23:00:00", finish={"learned": test_factledger.TEXT})])
        self.assertEqual([r["uses"] for r in factledger.rows("ember")], [None, 0])
        self.f.write_episodes([self.f.episode("2026-10-03T00:00:00", finish={"learned": test_factledger.TEXT})])
        self.assertEqual([r["uses"] for r in factledger.rows("ember")], [None, 1])
        self.assertIn("invalid fact date", factledger.record_text("ember"))


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


class UsageRegression(unittest.TestCase):
    def test_f10_nonstring_results_and_null_schedule(self):
        """F10: malformed results count as failed calls, not reads; null schedules are not jobs."""
        f = test_usage.UsageTest()
        try:
            f.setUp()
            f.plugin("watcher", name="watch_feed", schedule=None)
            f.write("skills/guide.md", "guide")
            f.live.return_value = [f.cycle(1, [
                {"tool": tool, "args": {"path": "skills/guide.md"}, "result": result}
                for result in (None, {}, [], 3) for tool in ("watch_feed", "read_file")])]
            plugin, = test_usage.usage.plugins("ember", test_usage.NOW)
            self.assertEqual((plugin["calls"], plugin["failed"], plugin["scheduled"]), (4, 4, False))
            self.assertEqual(test_usage.usage.skills("ember", test_usage.NOW)[0]["reads"], 0)
        finally:
            f.doCleanups()
