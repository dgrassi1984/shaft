"""Surprise measurement (docs/plans/PLAN-surprise M0): the rule flags, recurrence and the per-model baseline.

The result strings are sanitized from real episodes. The template's in-the-moment rule (M1) must
flag these same strings the same way: surprise_fixtures.py is the contract between the two copies.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import metrics, surprise
from supervisor.tests.surprise_fixtures import FLAG_FIXTURES

SHARED_FIXTURES = FLAG_FIXTURES   # the contract, in surprise_fixtures.py


class FlagTest(unittest.TestCase):
    def test_shared_fixtures(self):
        for result, expected in SHARED_FIXTURES:
            self.assertEqual(surprise.flag(result), expected, result[:80])

    def test_permission_errors_on_any_write_are_one_class(self):
        # One lesson ("this file has its own tool") whichever protected file it hit (shaft 06:53 and 13:44).
        a = surprise.kind_of("write_file", "error", "PermissionError")
        b = surprise.kind_of("append_file", "error", "PermissionError")
        self.assertEqual(a, b)
        self.assertNotEqual(a, surprise.kind_of("read_file", "error", "PermissionError"))


def _tool(tool, result, path=""):
    return {"tool": tool, "args": json.dumps({"path": path}) if path else "{}", "result": result}


class BaselineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-surprise-")).resolve()
        self.root = self.tmp / "instances" / "x"
        (self.root / "memory" / "episodes").mkdir(parents=True)
        (self.root / "logs").mkdir()
        (self.tmp / "sup" / "judgments").mkdir(parents=True)
        self.patch = mock.patch.object(metrics, "SUP_DIR", self.tmp / "sup")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, episodes, usage, judgments):
        (self.root / "memory" / "episodes" / "2026-10-13.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in episodes))
        (self.root / "logs" / "llm_usage.jsonl").write_text("".join(json.dumps(u) + "\n" for u in usage))
        (self.tmp / "sup" / "judgments" / "x.jsonl").write_text("".join(json.dumps(j) + "\n" for j in judgments))

    def test_recurrence_counts_episodes_not_retries(self):
        denied = "ERROR: PermissionError: self/self.md: use update_self_model"
        self.write([
            # the same error twice in one episode is one surprise (sanitized from real episodes)
            {"ts": "2026-10-13T01:00:00", "kind": "talk", "tools": [_tool("write_file", denied, "self/self.md"),
                                                                    _tool("write_file", denied, "self/self.md")]},
            {"ts": "2026-10-13T02:00:00", "kind": "live", "steps": 3, "tools": [_tool("list_files", "(empty)", "notes")]},
            {"ts": "2026-10-13T03:00:00", "kind": "live", "steps": 4,
             "tools": [_tool("append_file", "ERROR: PermissionError: memory/journal.md: append-only", "memory/journal.md")]},
        ], [], [])
        rec = surprise.recurrence(surprise.events("x", self.root))
        self.assertEqual(rec["classes"], 2)
        self.assertEqual(rec["recurred"], 1)
        self.assertEqual(rec["repeat_episodes"], 1)
        self.assertEqual(list(rec["recurring"]), ["error:PermissionError:write"])

    def test_cost_and_success_are_per_model(self):
        # shaft's first cycle ran on Haiku (seconds per turn), the rest on Bonsai (a minute or more): mixing them
        # put the "median turn" at 48 s when Bonsai's is 73 s.
        self.write(
            [{"ts": "2026-10-12T20:35:00", "kind": "live", "steps": 2, "tools": []},
             {"ts": "2026-10-13T02:00:00", "kind": "live", "steps": 2, "tools": []},
             {"ts": "2026-10-13T03:00:00", "kind": "talk", "tools": []}],
            [{"ts": "2026-10-12T20:34:00", "purpose": "live", "model": "haiku", "duration_s": 2, "prompt_tokens": 100,
              "completion_tokens": 50},
             {"ts": "2026-10-12T20:34:30", "purpose": "live", "model": "haiku", "duration_s": 4, "prompt_tokens": 100,
              "completion_tokens": 50},
             {"ts": "2026-10-13T01:58:00", "purpose": "live", "model": "bonsai", "duration_s": 60, "prompt_tokens": 100,
              "completion_tokens": 900},
             {"ts": "2026-10-13T01:59:00", "purpose": "live", "model": "bonsai", "duration_s": 80, "prompt_tokens": 100,
              "completion_tokens": 700},
             {"ts": "2026-10-13T01:59:30", "purpose": "live", "model": "bonsai", "duration_s": 999, "error": "HTTP 503"},
             {"ts": "2026-10-13T02:30:00", "purpose": "talk", "model": "bonsai", "duration_s": 500},
             # a cycle still running: no episode yet, so it belongs to no cycle
             {"ts": "2026-10-13T04:00:00", "purpose": "live", "model": "bonsai", "duration_s": 400}],
            [{"episode_ts": "2026-10-12T20:35:00", "confidence": 75, "criterion_met": "yes", "claim_supported": True},
             {"episode_ts": "2026-10-13T02:00:00", "confidence": 90, "criterion_met": "no", "claim_supported": False}])
        b = surprise.baseline("x", self.root)
        self.assertEqual(list(b["by_model"]), ["haiku", "bonsai"])
        self.assertEqual(b["current_model"], "bonsai")
        bonsai = b["by_model"]["bonsai"]
        self.assertEqual(bonsai["turns"], 2)                       # the 503 and the unfinished cycle's turn are out
        self.assertEqual(bonsai["turn_seconds_median"], 70)
        self.assertEqual(bonsai["cycle_seconds_median"], 140)
        self.assertEqual(bonsai["cycle_completion_tokens_median"], 1600)
        self.assertEqual(bonsai["judged"]["success"], 0.0)
        self.assertEqual(bonsai["judged"]["brier"], 0.81)
        self.assertEqual(b["by_model"]["haiku"]["judged"]["success"], 1.0)
        self.assertEqual(b["judged_all_models"]["n"], 2)
        self.assertIn("| bonsai (", surprise.report(b))

    def test_freeze_writes_the_baseline(self):
        self.write([{"ts": "2026-10-13T02:00:00", "kind": "live", "steps": 1, "tools": [_tool("list_files", "(empty)")]}],
                   [], [])
        target = self.tmp / "sup" / "evals" / "surprise-baseline.json"
        with mock.patch.object(surprise, "INSTANCES", self.tmp / "instances"), \
                mock.patch.object(surprise, "BASELINE", target), mock.patch("builtins.print"):
            self.assertEqual(surprise.command(["x"], freeze=True), 0)
        data = json.loads(target.read_text())
        self.assertEqual(data["instances"]["x"]["flagged"]["live"], 1)
        self.assertFalse(target.with_suffix(".tmp").exists())


if __name__ == "__main__":
    unittest.main()
