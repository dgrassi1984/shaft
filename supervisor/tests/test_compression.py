"""#113: offline compression, reconstruction, recall and CLI on synthetic records only."""
from __future__ import annotations

import contextlib
import io
import json
import random
import string
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import compression, core, metrics, mind
from supervisor.__main__ import main
from supervisor.tests.mindfix import commit_at


def noise(seed, size):
    rng = random.Random(seed)
    return "".join(rng.choices(string.ascii_letters + string.digits, k=size))


class CompressionTest(unittest.TestCase):
    """#113 uses real temporary mind histories and the real CLI dispatcher."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="shaft-compression-")
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.root = self.project / "birds" / "bird"
        (self.root / "memory/episodes").mkdir(parents=True)
        (self.root / "shaft").mkdir()
        (self.root / ".git").mkdir()
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.project / "record"))
        self.enterContext(mock.patch.object(core, "INSTANCES", self.root.parent))
        self.enterContext(mock.patch.object(metrics, "INSTANCES", self.root.parent))
        self.enterContext(mock.patch.object(mind, "minds_dir", return_value=self.project / "stores"))

    def commit(self, when, facts, digests="", archive="", kind="sleep"):
        for path, text in (("facts.md", facts), ("digests.md", digests), ("facts-archive.md", archive)):
            (self.root / "memory" / path).write_text(text)
        return commit_at("bird", self.project, self.root, when,
                         f"bird: {kind} {when[:10]} {when[11:16]}")

    def logs(self, text, start="2026-01-02T13:00:00+00:00", end="2026-01-02T14:00:00+00:00"):
        ep = {"kind": "live", "run_started": start, "ts": end,
              "tools": [{"tool": "fetch_page", "args": '{"url":"https://example.org"}', "result": text}]}
        (self.root / "memory/episodes/day.jsonl").write_text(json.dumps(ep) + "\n")

    def history(self, before, after, archive="", digests=""):
        self.commit("2026-01-02T10:00:00+00:00", before, kind="live")
        self.commit("2026-01-02T12:00:00+00:00", after, digests, archive)

    def test_recurring_dictionary_improves_next_day_compression(self):
        """#113: equal-sized memories isolate the recurring next-day strings."""
        recurring = noise(1, 12000)
        self.history("- durable fact\n" + noise(2, 12000), "- durable fact\n" + recurring)
        self.logs(recurring * 3)
        row = compression.assay("bird")[0]
        self.assertGreater(row["conditional_gain_bits"], 0)
        self.assertGreater(row["delta_bits"], 0)
        self.assertEqual(row["recall"], 1)
        self.assertEqual(row["verdict"], "gain")

    def test_huge_padding_does_not_win_two_part_code(self):
        """#113 charges the entire dictionary even beyond zlib's 32 KiB tail."""
        self.history("- durable fact", "- durable fact\n" + noise(3, 200000))
        self.logs("unrelated tool result " * 100)
        row = compression.assay("bird")[0]
        self.assertLess(row["delta_bits"], 0)
        self.assertEqual(row["verdict"], "no gain")

    def test_dropped_facts_flag_forgetting(self):
        """#113: shortening a memory by losing facts must not count as progress."""
        self.history("- durable fact\n- " + noise(4, 12000), "- durable fact")
        self.logs("unrelated result")
        row = compression.assay("bird")[0]
        self.assertGreater(row["delta_bits"], 0)
        self.assertEqual(row["recall"], 0.5)
        self.assertEqual(row["verdict"], "forgetting, not progress")

    def test_archived_facts_remain_recalled(self):
        """#113 checks the archive without charging it as the preset dictionary."""
        self.history("- [2026-01-01 10:00] old fact (src: operator)", "- new fact",
                     archive="- [2026-01-01 10:00] old fact (src: operator)")
        self.logs("result")
        self.assertEqual(compression.assay("bird")[0]["recall"], 1)

    def test_command_writes_nothing_and_uses_historical_memory(self):
        """#113: CLI leaves instance and store bytes unchanged despite later rewrites."""
        self.history("- old fact", "- old fact\n- next fact", digests="first digest")
        self.logs("next fact " * 100)
        expected = compression.assay("bird")
        self.commit("2026-01-04T12:00:00+00:00", "- future fact", "future digest", kind="live")
        snapshot = lambda: {p.relative_to(self.project): p.read_bytes()
                            for p in self.project.rglob("*") if p.is_file()}
        before = snapshot()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["compression", "bird", "--since", "2026-01-02"]), 0)
        self.assertIn("median Δbits two-part", output.getvalue())
        self.assertEqual(compression.assay("bird"), expected)
        self.assertEqual(snapshot(), before)
        self.assertEqual(compression.assay("bird", "2026-01-03"), [])

    def test_no_sleeps_prints_clear_line(self):
        """#113: an empty sleep history is explicit in command output."""
        self.commit("2026-01-01T12:00:00+00:00", "- fact", kind="live")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["compression", "bird"]), 0)
        self.assertIn("no sleeps", output.getvalue())
        self.assertNotIn("recall kept", output.getvalue())

    def test_overlapping_legacy_and_outside_runs_are_excluded(self):
        """#113: both snapshots must precede every held-out live tool log."""
        self.history("- fact", "- fact")
        for start, end in [("2026-01-02T11:00:00+00:00", "2026-01-02T13:00:00+00:00"),
                           ("2026-01-03T13:00:00+00:00", "2026-01-03T14:00:00+00:00"),
                           ("", "2026-01-02T14:00:00+00:00")]:
            with self.subTest(start=start):
                self.logs("leaked", start, end)
                row = compression.assay("bird")[0]
                self.assertEqual(row["verdict"], "no held-out tool logs")
                self.assertNotIn("delta_bits", row)

    def test_missing_parent_and_store_are_explicit(self):
        """#113 refuses to infer unseen historical facts from current memory."""
        with self.assertRaisesRegex(mind.MindError, "no mind history"):
            compression.assay("bird")
        self.commit("2026-01-02T12:00:00+00:00", "- fact")
        self.logs("result")
        self.assertEqual(compression.assay("bird")[0]["verdict"], "missing pre-sleep history")
