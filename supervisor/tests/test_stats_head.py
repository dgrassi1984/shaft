"""The stats head fits its prompt budget and reports evidence without exposing battery items."""
import json
import re
import signal
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import battery, core, metacog, metrics, panel, predictions, stats_head, surprise

from supervisor.tests.test_idle_exit import IsolatedCase

NOW = datetime(2026, 11, 11, 12)


class StatsHeadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-stats-head-")
        self.addCleanup(tmp.cleanup)
        self.sup = Path(tmp.name)
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(core, "INSTANCES", self.sup / "agents"))
        self.enterContext(mock.patch.object(metrics, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(metrics, "INSTANCES", self.sup / "agents"))
        self.enterContext(mock.patch.object(stats_head, "log"))
        self.js = self.enterContext(mock.patch.object(metrics, "judgments_for_stats", return_value=[]))
        self.due = self.enterContext(mock.patch.object(predictions, "due_lines", return_value=[]))
        self.since = self.enterContext(mock.patch.object(stats_head, "since_sleep", return_value=[]))
        self.misses = self.enterContext(mock.patch.object(battery, "self_miss_lines", return_value=[]))
        self.paired = self.enterContext(mock.patch.object(metacog, "paired_v3", return_value={"n": 0}))
        self.brier = self.enterContext(mock.patch.object(metrics, "paired_v3", return_value={"n": 0}))
        self.coin = self.enterContext(mock.patch.object(surprise, "coin_comparison", return_value={
            "drawn": {"n": 0, "value": None}, "undrawn": {"n": 0, "value": None}}))

    def test_stats_text_without_head_is_byte_identical_to_body(self):
        body = metrics.TITLE + "\n\n## Old tables\n\n  keep spacing  \n"
        with mock.patch.object(metrics, "_stats_text", return_value=body):
            self.assertEqual(stats_head.head("ember", NOW), [])
            self.assertEqual(metrics.stats_text("ember"), body)

    def test_stats_text_inserts_head_after_title_before_unchanged_body(self):
        body = metrics.TITLE + "\n\n## Old tables\n\n  keep spacing  \n"
        self.since.return_value = ["- Recent work"]
        head = stats_head.head("ember", NOW)
        with mock.patch.object(metrics, "_stats_text", return_value=body):
            text = metrics.stats_text("ember")
        self.assertEqual(text, metrics.TITLE + "\n\n" + "\n".join(head) + "\n" + body[len(metrics.TITLE) + 2:])
        self.assertLess(text.index("- Recent work"), text.index(stats_head.BODY_MARK))
        self.assertLess(text.index(stats_head.BODY_MARK), text.index("## Old tables"))

    def crowded_head(self, notes=20):
        self.due.return_value = ["- prediction " + str(i) + "p" * 400 for i in range(4)]
        self.since.return_value = ["- note " + str(i) + "n" * 400 for i in range(notes)]
        # A timeout reports a trimming loop as a failure instead of hanging the suite.
        def timed_out(signum, frame):
            raise AssertionError("stats_head.head did not finish trimming within 1 second")
        previous = signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, 1)
        try:
            return stats_head.head("ember", NOW)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def test_head_limits_include_body_marker_and_keep_predictions_before_last_notes(self):
        lines = self.crowded_head()
        for line in lines:
            self.assertLessEqual(len(line), stats_head.LINE_CHARS)
        for i in range(4):
            self.assertTrue(any(line.startswith(f"- prediction {i}") for line in lines))
        self.assertTrue(any(line.startswith("- note 0") for line in lines))
        self.assertFalse(any(line.startswith("- note 19") for line in lines))
        self.assertLessEqual(len("\n".join(lines)), stats_head.HEAD_CHARS)

    def test_head_limit_counts_marker_even_when_content_needs_no_trimming(self):
        self.since.return_value = ["- " + "n" * 238] * 10 + ["- " + "n" * 58]
        lines = stats_head.head("ember", NOW)
        self.assertLessEqual(len("\n".join(lines)), stats_head.HEAD_CHARS)

    def test_head_fits_whole_before_prompt_line_snapback_region(self):
        # Read agent code as text only; never import it into the supervisor.
        prompt = (core.PROJECT / "template" / "shaft" / "prompts.py").read_text()
        match = re.search(r'"stats"\s*:\s*\(\s*(\d+)\s*,\s*"head"', prompt)
        self.assertIsNotNone(match)
        budget = int(match[1])
        lines = self.crowded_head(notes=4)
        self.assertTrue(all(len(line) <= stats_head.LINE_CHARS for line in lines))
        head = "\n".join(lines)
        self.assertLess(len(metrics.TITLE + "\n\n" + head), .8 * budget)
        self.assertLess(stats_head.HEAD_CHARS, budget / 2)

    def test_model_change_requires_recent_transition_and_names_local_model(self):
        def row(days, model):
            return {"episode_ts": (NOW - timedelta(days=days)).isoformat(), "model": model}
        for rows, expected in [([], []), ([row(2, "cloud"), row(1, "cloud")], []),
                               ([row(10, None), row(stats_head.MODEL_NOTE_DAYS + 1, "cloud")], [])]:
            with self.subTest(rows=rows):
                self.js.return_value = rows
                self.assertEqual(stats_head.model_change("ember", NOW), expected)
        self.js.return_value = [row(9, None), row(2, "cloud"), row(1, "cloud")]
        lines = stats_head.model_change("ember", NOW)
        self.assertEqual(len(lines), 1)
        self.assertIn("since 2026-11-09", lines[0])
        self.assertIn("run on cloud (before: local)", lines[0])
        self.js.return_value = [row(9, "cloud"), row(stats_head.MODEL_NOTE_DAYS, None)]
        self.assertIn("run on local (before: cloud)", stats_head.model_change("ember", NOW)[0])

    def test_verdict_words_follow_interval_and_data_verdict(self):
        for ci, verdict, expected in [([.1, .4], "better", "gain on me"),
                                      ([0, .4], "same", "no gain shown on me"),
                                      ([-.1, .4], "same", "no gain shown on me"),
                                      (None, "not enough data", "not enough data yet on me")]:
            with self.subTest(ci=ci, verdict=verdict):
                self.paired.return_value = {"n": 10, "ci": ci, "diff": .2, "verdict": verdict}
                row, = stats_head.verdicts("ember")
                self.assertIn(expected, row["line"])
                self.assertEqual(row["alert"], "")

    def test_base_rate_alert_starts_at_threshold_and_brier_is_shown(self):
        self.brier.return_value = {"n": 5, "brier_first": .25, "brier_final": .2}
        for n, alert in [(stats_head.BASE_RATE_ALERT_N - 1, False), (stats_head.BASE_RATE_ALERT_N, True)]:
            with self.subTest(n=n):
                self.paired.return_value = {"n": n, "ci": [-.1, .1], "diff": 0, "verdict": "same"}
                row, = stats_head.verdicts("ember")
                self.assertEqual(bool(row["alert"]), alert)
                self.assertIn("Brier 0.25 before, 0.20 after", row["line"])
        self.paired.return_value = {"n": 1000, "ci": [.1, .3], "diff": .2, "verdict": "better"}
        self.assertEqual(stats_head.verdicts("ember")[0]["alert"], "")

    def test_coin_alert_needs_both_arms_at_threshold_and_lower_drawn_value(self):
        n = stats_head.COIN_ALERT_N
        for drawn, undrawn, dv, uv, expected in [(n - 1, n, 1, 2, False), (n, n - 1, 1, 2, False),
                                                (n, n, 1, 2, True), (n, n, 2, 2, False),
                                                (n, n, 3, 2, False)]:
            with self.subTest(drawn=drawn, undrawn=undrawn, dv=dv):
                self.coin.return_value = {"drawn": {"n": drawn, "value": dv}, "undrawn": {"n": undrawn, "value": uv}}
                row, = stats_head.verdicts("ember")
                self.assertEqual(row["key"], "coin")
                self.assertEqual(bool(row["alert"]), expected)

    def test_saved_verdict_alerts_have_stable_ids_and_unreadable_file_is_empty(self):
        self.paired.return_value = {"n": stats_head.BASE_RATE_ALERT_N, "ci": [-.1, .1], "diff": 0, "verdict": "same"}
        self.coin.return_value = {"drawn": {"n": stats_head.COIN_ALERT_N, "value": 1},
                                 "undrawn": {"n": stats_head.COIN_ALERT_N, "value": 2}}
        self.assertEqual(stats_head.alerts("ember"), [])
        stats_head.head("ember", NOW, save=True)
        path = self.sup / "verdicts/ember.json"
        saved = json.loads(path.read_text())
        self.assertEqual([row["key"] for row in saved], ["base_rate", "coin"])
        self.assertEqual(stats_head.alerts("ember"), [
            {"id": f"verdict:ember:{row['key']}", "text": row["alert"]} for row in saved])
        path.write_text("{")
        self.assertEqual(stats_head.alerts("ember"), [])
        with mock.patch.object(Path, "read_text", side_effect=OSError("unreadable")):
            self.assertEqual(stats_head.alerts("ember"), [])

    def test_panel_includes_saved_verdict_alerts_without_recomputing_comparisons(self):
        self.paired.return_value = {"n": stats_head.BASE_RATE_ALERT_N, "ci": [-.1, .1], "diff": 0, "verdict": "same"}
        stats_head.head("ember", NOW, save=True)
        expected = stats_head.alerts("ember")
        self.assertEqual(len(expected), 1)
        self.paired.reset_mock()
        self.coin.reset_mock()
        with mock.patch.object(panel, "instance_names", return_value=["ember"]), \
             mock.patch.object(panel, "_log_failures", return_value=[]), \
             mock.patch.object(core, "long_pauses", return_value=[]), \
             mock.patch.object(panel, "provider_alerts", return_value=[]), \
             mock.patch.object(panel.planchannel, "outage", return_value=None), \
             mock.patch.object(panel.corrections, "alerts", return_value=[]):
            found = panel.alerts(snap={"services": []}, now=NOW,
                                 cost={"caps": [], "judge_today_total": 0, "judge_alert": 1})
        self.assertEqual(found, expected)
        self.paired.assert_not_called()
        self.coin.assert_not_called()

    def test_a_raising_part_does_not_remove_other_head_parts(self):
        self.due.return_value = ["- prediction survives"]
        self.misses.side_effect = ValueError("broken battery topics")
        self.since.return_value = ["- note survives"]
        text = "\n".join(stats_head.head("ember", NOW))
        self.assertIn("- prediction survives", text)
        self.assertIn("- note survives", text)
        self.assertIn(stats_head.BODY_MARK, text)


class BatteryTopicsTest(unittest.TestCase):
    def test_self_misses_reveal_only_latest_missed_topics_in_current_era(self):
        marker = "SECRET-ITEM-CONTENT"
        items = [{"id": key, "category": "self", "truth": key, "prompt": marker, "answer": marker}
                 for key in ("model", "last_sleep", "code_changes")]
        def result(key, correct, **fields):
            return {"item": key, "status": "scored", "category": "self", "arm": "instance",
                    "answer": marker, "grade": {"correct": correct, "reasoning": marker}, "current": True, **fields}
        rows = [result("model", "no"), result("last_sleep", "no"), result("last_sleep", "yes"),
                result("code_changes", "no", current=False), result("code_changes", "no", arm="control")]
        with mock.patch.object(battery, "load_items", return_value=items), \
             mock.patch.object(battery, "results", return_value=rows), \
             mock.patch.object(battery, "era", return_value="example"), \
             mock.patch.object(battery, "_same_era", side_effect=lambda r, e: r["current"]):
            self.assertEqual(battery.self_miss_topics("ember"), [battery.SELF_TOPICS["model"]])
            lines = battery.self_miss_lines("ember")
            self.assertEqual(lines, ["- On the battery, what I said about myself did not match the supervisor's record on: "
                                     + battery.SELF_TOPICS["model"]])
            self.assertNotIn(marker, "\n".join(lines))

    def test_coin_comparison_uses_only_valued_self_cycles_on_latest_model_and_known_coin(self):
        rows, episodes = [], []
        for i, model, origin, value, drawn in [(1, "old", "self", 5, True), (2, "new", "operator", 5, True),
                                              (3, "new", "self", 2, True), (4, "new", "self", 4, True),
                                              (5, "new", "self", 1, False), (6, "new", "self", None, False),
                                              (7, "new", "self", 5, None)]:
            ts = f"2026-11-11T10:{i:02d}:00"
            rows.append({"episode_ts": ts, "model": model, "origin": origin, "value": value})
            episodes.append({"ts": ts, "curiosity": {"drawn": drawn}})
        with mock.patch.object(metrics, "judgments_for_stats", return_value=rows), \
             mock.patch.object(metrics, "live_episodes", return_value=episodes):
            self.assertEqual(surprise.coin_comparison("ember"),
                             {"drawn": {"n": 2, "value": 3}, "undrawn": {"n": 1, "value": 1}})
            self.assertEqual(surprise.coin_comparison("ember", datetime(2026, 11, 11, 10, 5)),
                             {"drawn": {"n": 0, "value": None}, "undrawn": {"n": 1, "value": 1}})


class StatsHeadRecordsTest(IsolatedCase):
    def test_unfinished_sleep_does_not_reset_summary_window(self):
        finished = (NOW - timedelta(hours=8)).isoformat()
        self.write(self.root / "memory/episodes/day.jsonl", [
            {"kind": "sleep", "ts": finished, "content": "Consolidated notes"},
            {"kind": "live", "ts": (NOW - timedelta(hours=4)).isoformat()},
            {"kind": "sleep", "ts": (NOW - timedelta(hours=1)).isoformat()}])
        with mock.patch.object(metrics, "judgments_for_stats", return_value=[]):
            self.assertEqual(stats_head.since_sleep("ember"), [
                "- Since my last sleep (2026-11-11 04:00): 1 cycles, 0 judged "
                "(yes 0, partial 0, no 0); code changes kept 0"])

    def test_since_sleep_parses_mixed_separators_for_cycles_judgments_and_changes(self):
        """A space-separated noon timestamp must sort after a T-separated morning sleep."""
        self.write(self.root / "memory/episodes/day.jsonl", [
            {"kind": "sleep", "ts": "2026-11-11T04:00:00", "content": "Consolidated notes"},
            {"kind": "live", "ts": "2026-11-11 12:00:00"}])
        self.write(self.sup / "code_changes/ember.jsonl", [{"ts": "2026-11-11 12:00:00"}])
        with mock.patch.object(metrics, "judgments_for_stats", return_value=[
                {"episode_ts": "2026-11-11 12:00:00", "criterion_met": "yes"}]):
            self.assertEqual(stats_head.since_sleep("ember"), [
                "- Since my last sleep (2026-11-11 04:00): 1 cycles, 1 judged "
                "(yes 1, partial 0, no 0); code changes kept 1"])

    def test_system_exit_in_one_head_part_preserves_stats_page_and_other_parts(self):
        """A part exiting for a missing instance must not drop the whole head or page."""
        body = metrics.TITLE + "\n\n## Tables\nExample body\n"
        with mock.patch.object(metrics, "_stats_text", return_value=body), \
             mock.patch.object(stats_head, "verdicts", return_value=[]), \
             mock.patch.object(stats_head, "since_sleep", return_value=["- Surviving note"]), \
             mock.patch.object(battery, "self_miss_lines", side_effect=SystemExit("missing instance")) as part, \
             mock.patch.object(stats_head, "log") as log:
            text = metrics.stats_text("ember")
        part.assert_called_once_with("ember")
        self.assertIn("- Surviving note", text)
        self.assertIn(stats_head.BODY_MARK, text)
        self.assertTrue(text.endswith("## Tables\nExample body\n"))
        self.assertIn("battery topics failed: SystemExit", log.call_args.args[0])



if __name__ == "__main__":
    unittest.main()
