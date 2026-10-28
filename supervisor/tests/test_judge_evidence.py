"""The judge must see what the agent saw.

Regression (2026-10-13): the agent's tool log cut every argument and result at 2,000 characters
while the agent saw 6,000, and _written_files showed the judge only files written with write_file.
Shaft's 06:53 and 14:40 cycles rewrote and read back self/self.md (4.7K): both were judged
'partial', claim unsupported, because the read-back "is cut off" in the log.

The end-to-end test runs the real agent code ($SHAFT_UNDER_TEST, else template/) in its sandbox,
so an instance that cuts its own log again fails this invariant.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import metrics
from supervisor.tests.test_invariants import InstanceCase

LATE_SENTINEL = "The section the judge never saw: goals and constraints kept verbatim."
NEW_SELF = ("# Self-Model\n\n## Identity\n" + "I am shaft, a local agent on Bonsai. " * 120
            + "\n\n## Goals & constraints\n" + LATE_SENTINEL + "\n")


class FullEvidenceReachesTheJudge(InstanceCase):
    def test_read_back_longer_than_2000_chars_reaches_the_judge_whole(self):
        self.assertGreater(NEW_SELF.index(LATE_SENTINEL), 3000)   # well past the old 2,000 cut
        replies = [
            {"tool_calls": [("declare_intent", {"goal": "rewrite self/self.md so it is no longer false",
                                                "success_criterion": "read-back shows the Goals & constraints "
                                                                     "section kept", "confidence": 80,
                                                "origin": "self"})]},
            {"tool_calls": [("update_self_model", {"text": NEW_SELF})]},
            {"tool_calls": [("read_file", {"path": "self/self.md"})]},
            {"tool_calls": [("finish_cycle", {"delivered_via": "none", "what_happened": "rewrote self.md; the read-back shows Goals & "
                                                              "constraints kept verbatim",
                                              "criterion_met": "yes", "learned": "read back what I write"})]},
        ]
        r = self.run_agent(replies, "live")
        self.assertEqual(r.returncode, 0, r.stderr)
        episodes = [json.loads(l) for p in (self.root / "memory" / "episodes").glob("*.jsonl")
                    for l in p.read_text().splitlines()]
        ep = [e for e in episodes if e["kind"] == "live"][-1]

        with mock.patch.object(metrics, "INSTANCES", self.tmp):
            case = metrics._case(ep, metrics._written_files(self.root.name, ep))
        log, _, files = case.partition("## Files written in this cycle")
        read_back = next(t for t in ep["tools"] if t["tool"] == "read_file")
        self.assertEqual(read_back["result"], NEW_SELF)                  # the log kept the whole read-back
        self.assertIn(LATE_SENTINEL, read_back["result"])
        self.assertEqual(log.count(LATE_SENTINEL), 2)                    # in the update's text and the read-back
        self.assertNotIn("the log stops here", case)
        self.assertNotIn("cut in the log", case)
        self.assertIn("### self/self.md (now,", files)                   # update_self_model's file is shown
        self.assertIn(LATE_SENTINEL, files)


class JudgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-judge-")).resolve()
        self.root = self.tmp / "x"
        for rel, text in {"self/self.md": "# Self\nme\n", "self/personality.md": "## Core\n- calm\n",
                          "memory/facts.md": "# Facts\n- [2026-10-13 10:00] a fact\n",
                          "memory/digests.md": "old digest\n" * 1000 + "THE NEW DIGEST\n",
                          "notes/log.md": "old line\n" * 1000 + "THE APPENDED ENTRY\n",
                          "notes/big.md": "THE HEAD\n" + "body\n" * 2000}.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)

    def files(self, tools: list[dict]) -> str:
        with mock.patch.object(metrics, "INSTANCES", self.tmp):
            return metrics._written_files("x", {"tools": tools})

    def test_every_file_writing_tool_shows_its_file(self):
        out = self.files([{"tool": "update_self_model", "args": "{}", "result": "self-model updated"},
                          {"tool": "update_personality", "args": "{}", "result": "core personality updated (1 traits)"},
                          {"tool": "rewrite_facts", "args": "{}", "result": "facts consolidated: 1 -> 1"},
                          {"tool": "write_digest", "args": "{}", "result": "digest written"},
                          {"tool": "append_file", "args": '{"path": "notes/log.md", "content": "x"}',
                           "result": "appended 1 chars to notes/log.md"}])
        for rel in ("self/self.md", "self/personality.md", "memory/facts.md", "memory/digests.md", "notes/log.md"):
            self.assertIn(f"### {rel} (now,", out)
        self.assertIn("THE NEW DIGEST", out)        # appended files show their end, where the new text is
        self.assertIn("THE APPENDED ENTRY", out)
        self.assertIn("the last 6000 shown", out)

    # docs/plans/PLAN-value V0: a judge grading what a cycle added to a running record (shaft's watch appends one entry per
    # cycle to the same notes file) must see where the addition starts, or old entries read as new findings.
    ENTRY = "## 2026-10-15 09:40 watch\nNo new primary source since the 09:30 entry.\n"

    def test_an_append_is_marked_where_it_starts(self):
        (self.root / "notes/log.md").write_text("old line\n" * 1000 + self.ENTRY)
        out = self.files([{"tool": "append_file", "args": json.dumps({"path": "notes/log.md", "content": self.ENTRY}),
                           "result": f"appended {len(self.ENTRY)} chars to notes/log.md"}])
        at = 9000
        self.assertIn(f"this cycle appended from character {at} on — the {at} before it were already there", out)
        before, _, after = out.partition(metrics.ADDED_MARK)
        self.assertIn("old line", before)
        self.assertTrue(after.lstrip("\n").startswith("## 2026-10-15 09:40 watch"))
        self.assertNotIn("old line", after)

    def test_an_append_later_cycles_buried_stays_in_view(self):
        # The backfill judges old cycles against files as they are now: later entries come after this one.
        later = "## later cycle\n" + "a later finding\n" * 600
        (self.root / "notes/log.md").write_text("old line\n" * 1000 + self.ENTRY + later)
        out = self.files([{"tool": "append_file", "args": json.dumps({"path": "notes/log.md", "content": self.ENTRY}),
                           "result": "appended"}])
        self.assertIn(metrics.ADDED_MARK + "\n" + self.ENTRY, out)     # the addition is shown, not only later text
        self.assertIn("characters 7500–13500 shown", out)
        self.assertIn("old line", out.partition(metrics.ADDED_MARK)[0])

    def test_an_append_whose_logged_args_were_cut_is_still_found(self):
        (self.root / "memory/facts.md").write_text("# Facts\n- [2026-10-13 10:00] a fact\n- [2026-10-15 09:41] "
                                                   "The \"watch\" found no new source since 09:30 and the next run is due at 10:00\n")
        cut = '{"fact": "The \\"watch\\" found no new source since 09:30 and the next ru'   # cut mid-argument
        out = self.files([{"tool": "remember", "args": cut, "result": "remembered"}])
        self.assertIn("appended from character", out)
        self.assertIn(metrics.ADDED_MARK + "\nThe \"watch\" found", out)

    def test_an_append_too_short_to_place_is_shown_as_before(self):
        out = self.files([{"tool": "append_file", "args": '{"path": "notes/log.md", "content": "ok"}',
                           "result": "appended 2 chars"}])
        self.assertNotIn(metrics.ADDED_MARK, out)
        self.assertIn("the last 6000 shown", out)

    def test_overwritten_file_shows_its_head_and_says_it_was_cut(self):
        out = self.files([{"tool": "write_file", "args": '{"path": "notes/big.md", "content": "THE HEAD\\nbo',
                           "result": "wrote 10009 chars to notes/big.md"}])   # args cut in the log
        self.assertIn("THE HEAD", out)
        self.assertIn("the first 6000 shown", out)

    def test_failed_writes_show_nothing(self):
        self.assertEqual(self.files([{"tool": "update_self_model", "args": "{}",
                                      "result": "ERROR: self-model not changed: too short"}]), "")

    def test_an_entry_cut_by_the_old_log_limit_says_so(self):
        cut = "x" * metrics.LEGACY_LOG_CAP
        case = metrics._case({"tools": [{"tool": "read_file", "args": '{"path": "self/self.md"}', "result": cut}]})
        self.assertIn(cut + "\n[... the log stops here, at 2000 chars", case)
        whole = "y" * 2500                                   # new logs keep what the agent saw: shown whole
        case = metrics._case({"tools": [{"tool": "read_file", "args": "{}", "result": whole}]})
        self.assertIn(whole, case)
        self.assertNotIn("the log stops here", case)


if __name__ == "__main__":
    unittest.main()
