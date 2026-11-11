"""Usage records count live calls and closed probation evidence without reading real instance state."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import core, usage

NOW = datetime(2026, 11, 11, 12)


class UsageTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-usage-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "agents" / "ember"
        self.root.mkdir(parents=True)
        self.sup = self.base / "records"
        self.sup.mkdir()
        self.enterContext(mock.patch.object(core, "INSTANCES", self.root.parent))
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.live = self.enterContext(mock.patch.object(usage.metrics, "live_episodes", return_value=[]))
        self.judgments = self.enterContext(mock.patch.object(usage.metrics, "judgments", return_value=[]))

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def plugin(self, folder, **spec):
        self.write(f"plugins/{folder}/plugin.json", json.dumps(spec))
        self.write(f"plugins/{folder}/main.py", "def run(): return 'ok'\n")

    def ledger(self, folder, rows):
        path = self.sup / folder / "ember.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def cycle(self, day, calls):
        return {"ts": f"2026-11-{day:02d}T10:00:00", "tools": calls}

    def test_unused_plugins_sort_by_first_seen_then_used_by_last_call(self):
        """Pruning candidates come first; manifest tool names and missing-name fallback both work."""
        for name in ("older", "newer", "used", "recent"):
            self.plugin(name, **({"name": "watch_feed"} if name == "used" else {}))
        self.ledger("code_changes", [
            {"ts": "2026-10-03T10:00:00", "files": ["plugins/older/main.py"]},
            {"ts": "2026-10-04T10:00:00", "files": ["plugins/newer/plugin.json"]},
            {"ts": "2026-10-01T10:00:00", "files": ["plugins/older-other/main.py"]}])
        self.live.return_value = [self.cycle(2, [{"tool": "watch_feed", "result": "ok"}]),
                                  self.cycle(3, [{"tool": "recent", "result": "ok"}])]
        rows = usage.plugins("ember", NOW)
        self.assertEqual([r["plugin"] for r in rows], ["older", "newer", "used", "recent"])
        self.assertEqual(rows[0]["first_seen"], "2026-10-03T10:00:00")
        self.assertIsNone(rows[0]["last_call"])
        self.assertIsNone(rows[0]["mean_value"])
        self.assertIn("older (tool older): built 2026-10-03, never called since", usage.record_text("ember", as_of=NOW))

    def test_calls_failures_cycles_values_and_schedule(self):
        """Failed calls count as plugin usage; values average cycles rather than calls."""
        self.plugin("watcher", name="watch_feed", schedule={"every_minutes": 30})
        self.live.return_value = [self.cycle(1, [{"tool": "watch_feed", "result": "ok"},
                                               {"tool": "watch_feed", "result": "ERROR: failed"}]),
                                  self.cycle(2, [{"tool": "watch_feed", "result": "ok"}]),
                                  self.cycle(3, [{"tool": "watch_feed", "result": "ok"}])]
        self.judgments.return_value = [{"episode_ts": "2026-11-01T10:00:00", "value": 1},
                                       {"episode_ts": "2026-11-02T10:00:00", "value": 4},
                                       {"episode_ts": "2026-11-03T10:00:00", "value": None}]
        self.assertEqual(usage.plugins("ember", NOW), [{"plugin": "watcher", "tool": "watch_feed",
                         "first_seen": None, "calls": 4, "failed": 1, "cycles": 3,
                         "last_call": "2026-11-03T10:00:00", "mean_value": 2.5, "scheduled": True}])
        text = usage.record_text("ember", as_of=NOW)
        self.assertIn("4 calls in 3 cycles, last 2026-11-03 10:00, mean value of those cycles 2.5", text)
        self.assertIn("runs as a scheduled job (job runs are not cycle calls)", text)

    def test_skill_reads_are_successful_exact_files_with_string_or_dict_args(self):
        """Only successful read_file calls under skills count, including nested files and normalized ./ paths."""
        self.write("skills/guide.md", "guide")
        self.write("skills/nested/unused.md", "unused")
        calls = [{"tool": "read_file", "args": args, "result": result} for args, result in [
            ('{"path": "skills/guide.md"}', "guide"), ({"path": "./skills/guide.md"}, "guide"),
            ({"path": "skills/guide.md"}, "ERROR: denied"), ({"path": "notes/guide.md"}, "guide"),
            ({"path": "/skills/guide.md"}, "guide"), ({"path": "skills/../skills/guide.md"}, "guide")]]
        calls.append({"tool": "write_file", "args": {"path": "skills/guide.md"}, "result": "ok"})
        self.live.return_value = [self.cycle(1, calls), self.cycle(2, calls[:1])]
        self.judgments.return_value = [{"episode_ts": "2026-11-01T10:00:00", "value": 0},
                                       {"episode_ts": "2026-11-02T10:00:00", "value": 4}]
        self.assertEqual(usage.skills("ember", NOW), [
            {"file": "skills/nested/unused.md", "reads": 0, "cycles": 0, "last_read": None, "mean_value": None},
            {"file": "skills/guide.md", "reads": 3, "cycles": 2,
             "last_read": "2026-11-02T10:00:00", "mean_value": 2.0}])

    def test_as_of_excludes_later_calls_judgments_and_ledger_rows(self):
        """A replay cannot use judgments made later, even for an earlier cycle."""
        self.plugin("watcher")
        self.write("skills/guide.md", "guide")
        calls = [{"tool": "watcher", "result": "ok"},
                 {"tool": "read_file", "args": {"path": "skills/guide.md"}, "result": "guide"}]
        self.live.return_value = [self.cycle(1, calls), self.cycle(2, calls), self.cycle(3, calls)]
        self.judgments.return_value = [{"episode_ts": "2026-11-01T10:00:00", "value": 5,
                                        "judged_at": "2026-11-02T10:00:00"},
                                       {"episode_ts": "2026-11-03T10:00:00", "value": 5}]
        self.ledger("code_changes", [{"ts": "2026-11-02T10:00:00", "files": ["plugins/watcher/main.py"]}])
        cut = datetime(2026, 11, 2, 10)
        row = usage.plugins("ember", cut)[0]
        self.assertEqual((row["calls"], row["cycles"], row["first_seen"], row["mean_value"]), (1, 1, None, None))
        self.assertEqual(usage.skills("ember", cut)[0]["reads"], 1)

    def test_kept_changes_line_counts_tested_and_each_probation_outcome(self):
        """The line counts the period's kept changes, the trial-tested ones and each probation outcome apart, and is
        absent when nothing was kept."""
        rows = [{"ts": f"2026-11-0{d}T10:00:00", "files": [], "commit": None, "outcome": o, "tested": t}
                for d, (o, t) in enumerate([("passed", False), ("passed", True), ("reverted", True),
                                            ("on probation", False), ("kept, not judged", None)], start=1)]
        rows.append({"ts": "2026-10-01T10:00:00", "files": [], "commit": None, "outcome": "passed", "tested": True})
        with mock.patch.object(usage.metrics, "change_outcomes", return_value=rows):
            self.assertEqual(usage.kept_changes_line("ember", now=NOW),
                             "Kept changes to my code in the last 14 days: 5, of which tested by a trial first: 2; "
                             "probation: passed 2, reverted 1, still running 1, too little use to judge 1")
        with mock.patch.object(usage.metrics, "change_outcomes", return_value=[]):
            self.assertIsNone(usage.kept_changes_line("ember", now=NOW))

    def test_empty_records_and_missing_ledgers(self):
        """An instance with no tooling or recorded code changes gets explicit empty text."""
        self.assertEqual(usage.record_text("ember", as_of=NOW),
                         "Calls are counted from my cycles' tool logs.\n\nMy plugins\n(no plugins)"
                         "\n\nMy skill files\n(no skill files)")
        self.assertIsNone(usage.kept_changes_line("ember", now=NOW))

    def test_links_and_invalid_manifests_are_not_reported(self):
        """Instance links cannot expose files or folders outside the instance."""
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "guide.md").write_text("outside")
        self.write("skills/local.md", "local")
        (self.root / "skills" / "linked").symlink_to(outside, target_is_directory=True)
        (self.root / "skills" / "linked.md").symlink_to(outside / "guide.md")
        self.write("plugins/broken/plugin.json", "{torn")
        self.write("plugins/array/plugin.json", "[]")
        (self.root / "plugins" / "linked").symlink_to(outside, target_is_directory=True)
        self.assertEqual(usage.plugins("ember", NOW), [])
        self.assertEqual([r["file"] for r in usage.skills("ember", NOW)], ["skills/local.md"])
