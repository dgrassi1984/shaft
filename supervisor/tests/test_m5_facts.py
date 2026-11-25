"""#143: reverting leaves contradicted facts current and loses dated retirement evidence."""
from __future__ import annotations

import copy
import json
import unittest
from datetime import datetime
from unittest import mock

from supervisor.tests.test_autonomy_gaps_template import InstanceFixture
from shaft import clock, memory, prompts, sleep
from shaft.llm import Reply
from shaft.tools import BUILTINS, SOURCE, Toolbox

NOW = datetime(2026, 10, 28, 14, 5)
OLD = "- [2026-10-24 09:12] Today is Sat 24 Oct (src: operator)"
NEW = "- [2026-10-26 09:12] Today is Mon 26 Oct (src: operator)"


class FactsTest(InstanceFixture, unittest.TestCase):
    """#143: facts have valid_from dates; replacements must preserve searchable history."""

    def setUp(self):
        super().setUp()
        self.path = self.root / "memory/facts.md"
        self.archive = self.root / "memory/facts-archive.md"
        self.path.write_text("# Facts I have learned\n\n" + OLD + "\n")
        self.box = Toolbox(self.root)
        self.enterContext(mock.patch.object(clock, "now", return_value=NOW))
        # these sleeps are about facts, not the #13 entry check (self-model line, improvement ids)
        self.enterContext(mock.patch.object(sleep, "entry_problems", return_value=[], create=True))

    def remember(self, supersedes):
        return self.box.run("remember", json.dumps({"fact": "Today is Wed 28 Oct", "source": "operator",
                                                    "supersedes": supersedes}))

    def test_supersession_archives_both_dates_and_marks_replacement(self):
        for selector in ("2026-10-24 09:12", "2026-10-24 09:12 Today is Sat"):
            with self.subTest(selector=selector):
                self.path.write_text(OLD + "\n")
                result = self.remember(selector)
                self.assertFalse(result.startswith("ERROR"), result)
                self.assertEqual(self.path.read_text(), "- [2026-10-28 14:05] Today is Wed 28 Oct "
                                 "(supersedes: [2026-10-24 09:12]) (src: operator)\n")
                archived = self.archive.read_text()
                self.assertIn("## Retired (superseded) 2026-10-28 14:05", archived)
                self.assertIn(OLD + " (superseded by [2026-10-28 14:05]: operator: Today is Wed 28 Oct)", archived)
                self.assertIn("retired " + OLD, self.box.log[-1]["result"])
                self.assertEqual(SOURCE.findall(archived), ["operator"] * (1 if selector.endswith("09:12") else 2))

    def test_ambiguous_or_missing_selector_writes_nothing_and_names_candidates(self):
        self.path.write_text(OLD + "\n" + OLD.replace("Today is", "Runway is") + "\n")
        self.archive.write_text("Existing history\n")
        for selector in ("2026-10-24 09:12", "2026-10-23 09:12"):
            before = self.path.read_bytes(), self.archive.read_bytes()
            result = self.remember(selector)
            self.assertTrue(result.startswith("ERROR"), result)
            self.assertIn("candidates:", result)
            self.assertIn("Today is Sat", result)
            self.assertIn("Runway is Sat", result)
            self.assertEqual((self.path.read_bytes(), self.archive.read_bytes()), before)

    def retire_sleep(self, evidence, selector="2026-10-24 09:12"):
        client = mock.Mock()
        seen = []
        replies = iter([Reply("", tool_calls=[{"id": "1", "name": "retire_fact", "arguments": json.dumps(
            {"fact": selector, "evidence": evidence})}]), Reply("Retired stale calendar fact with evidence " + evidence)])
        def complete(messages, *args, **kwargs):
            seen.append((copy.deepcopy(messages), kwargs.get("tools")))
            return next(replies)
        client.complete.side_effect = complete
        entry = sleep.run(self.cfg, self.state, client)
        episode = [ep for ep in self.episodes() if ep.get("kind") == "sleep"][-1]
        return entry, seen, episode["tools"][-1]["result"]

    def test_newer_log_retires_and_older_log_refuses(self):
        self.state.append_episode({"kind": "live", "ts": "2026-10-26T10:00:00"})
        self.state.append_episode({"kind": "live", "ts": "2026-10-23T10:00:00"})
        _, _, result = self.retire_sleep("log:2026-10-23T10:00:00")
        self.assertIn("must be newer", result)
        self.assertIn(OLD, self.path.read_text())
        self.assertFalse(self.archive.exists())
        _, _, result = self.retire_sleep("log:2026-10-26T10:00:00")
        self.assertTrue(result.startswith("retired"), result)
        self.assertNotIn(OLD, self.path.read_text())
        self.assertIn(OLD + " (superseded by [2026-10-26 10:00]: log:2026-10-26T10:00:00)",
                      self.archive.read_text())

    def test_retirement_rejects_unchecked_equal_or_ambiguous_evidence_without_writes(self):
        self.path.write_text(OLD + "\n" + NEW + "\n" + NEW.replace("Mon", "Monday") + "\n")
        before = self.path.read_bytes()
        for evidence in ("log:2026-10-27T10:00:00", "web:https://example.org", "inferred",
                         "instance:bird", "2026-10-24 09:12", "2026-10-26 09:12"):
            with self.subTest(evidence=evidence):
                _, _, result = self.retire_sleep(evidence)
                self.assertTrue(result.startswith("ERROR"), result)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.archive.exists())

    def test_retirement_web_requires_successful_fetch_and_operator_uses_now(self):
        for source in ("web:https://example.org/page", "operator"):
            with self.subTest(source=source):
                self.path.write_text(OLD + "\n")
                self.box.log = [{"tool": "fetch_page", "args": '{"url":"https://example.org/page"}',
                                 "result": "A newer observation"}]
                result = self.box._t_retire_fact("2026-10-24 09:12", source)
                self.assertTrue(result.startswith("retired"), result)
                self.assertIn("superseded by [2026-10-28 14:05]: " + source, self.archive.read_text())

    def test_implicit_conflict_in_fake_sleep_retires_from_listed_pair(self):
        self.path.write_text(OLD + "\n" + NEW + "\n")
        entry, seen, result = self.retire_sleep("2026-10-26 09:12")
        prompt = seen[0][0][1]["content"]
        self.assertIn("Older: " + OLD, prompt)
        self.assertIn("Newer: " + NEW, prompt)
        self.assertIn("11. Check current facts", prompt)
        self.assertIn("even when nobody said anything changed", prompt)
        self.assertIn("List each retirement and supersession with its evidence", prompt)
        self.assertIn("retire_fact", [spec["function"]["name"] for spec in seen[0][1]])
        self.assertTrue(result.startswith("retired"), result)
        self.assertNotIn(OLD, self.path.read_text())
        self.assertIn(NEW, self.path.read_text())
        self.assertIn("Retired stale calendar fact", entry)
        self.assertIn("Retired stale calendar fact", self.state.journal.read_text())
        self.assertIn("ERROR: unknown tool", Toolbox(self.root).run("retire_fact", "{}"))

    def test_candidates_subjects_dates_and_cap(self):
        self.path.write_text(OLD + "\n" + NEW + "\n")
        pairs = sleep.fact_candidates(self.root)
        self.assertIn(OLD, pairs)
        self.assertIn(NEW, pairs)
        for subject in ("Runway", "Cap-table", "the orchard irrigation schedule"):
            self.path.write_text(f"- [2026-10-24 09:12] {subject} was stable\n"
                                 f"- [2026-10-26 09:12] {subject} changed\n")
            self.assertIn("Older:", sleep.fact_candidates(self.root))
        self.path.write_text("\n".join(f"- [2026-10-{day:02d} 09:12] Today is day {day}" for day in range(1, 20)))
        self.assertEqual(sleep.fact_candidates(self.root).count("- Older:"), 15)
        # #143: a name most facts share pairs everything with everything (on tundra's real facts, "Daniele" filled
        # all 15 pairs); only rare keys count, and an older fact gets one pair
        self.path.write_text("\n".join(f"- [2026-10-{day:02d} 09:12] Daniele said item{day} is fine" for day in range(1, 9))
                             + "\n- [2026-10-09 09:12] Rizzo moved the bank meeting\n- [2026-10-10 09:12] Rizzo cancelled it")
        pairs = sleep.fact_candidates(self.root)
        self.assertEqual(pairs.count("- Older:"), 1)
        self.assertIn("Rizzo moved", pairs)
        self.path.write_text(OLD + "\n" + OLD.replace("Sat", "Mon"))
        self.assertNotIn("Older:", sleep.fact_candidates(self.root))

    def test_candidate_read_error_does_not_stop_sleep(self):
        original = type(self.path).read_text
        def read(path, *args, **kwargs):
            if path == self.path:
                raise OSError("denied")
            return original(path, *args, **kwargs)
        with mock.patch.object(type(self.path), "read_text", read):
            self.assertIn("fact candidates could not be read: OSError: denied", sleep.fact_candidates(self.root))
        with mock.patch.object(sleep, "fact_candidates", return_value="(fact candidates could not be read: OSError: denied)"):
            client = mock.Mock()
            client.complete.return_value = Reply("Continued after candidate error.")
            self.assertIn("Continued", sleep.run(self.cfg, self.state, client))
            self.assertIn("fact candidates could not be read", client.complete.call_args.args[0][1]["content"])

    def test_retired_fact_search_and_old_format_still_load(self):
        self.path.write_text(OLD + "\n- [2026-10-01] Legacy orchard note\n")
        self.remember("2026-10-24 09:12")
        with mock.patch.object(memory, "_semantic", return_value=[]):
            result = self.box.run("search_memory", json.dumps({"query": "Sat 24 Oct", "k": 10}))
        self.assertIn("memory/facts-archive.md", result)
        self.assertIn("Today is Sat 24 Oct", result)
        docs = memory.corpus(self.root)
        self.assertTrue(any("Legacy orchard note" in text for _, text in docs))
        self.assertTrue(any("src operator" in source and "superseded by" in text for source, text in docs))
        prompt = prompts.system_prompt(self.state, self.cfg, prompts.SLEEP_MODE)
        self.assertNotIn("Today is Sat 24 Oct", prompt)
        self.assertIn("Retired facts are in memory/facts-archive.md, found by search_memory.", prompt)
        self.assertIn("Legacy orchard note", prompt)
        spec = next(spec["function"] for spec in BUILTINS if spec["function"]["name"] == "remember")
        self.assertEqual(spec["parameters"]["required"], ["fact", "source"])
        self.assertIn("supersedes", spec["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
