"""Source validation, recall labels, and observation claims checked against the record."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "template"))
from shaft import clock, memory
from shaft.tools import BUILTINS, Toolbox
from supervisor import core, metrics, provenance

TEXT = "The orchard survey found eight healthy trees beside the eastern gate."
TS = "2026-10-01T09:00:00"


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-provenance-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "bird"
        (self.root / "memory/episodes").mkdir(parents=True)
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(core, "INSTANCES", self.base))
        self.box = Toolbox(self.root)
        self.episode = {"kind": "live", "ts": TS, "tools": [
            {"tool": "fetch_page", "args": json.dumps({"url": "https://example.org/trees/"}), "result": TEXT}]}
        self.write_episodes([self.episode])

    def write_episodes(self, episodes):
        (self.root / "memory/episodes/test.jsonl").write_text("\n".join(map(json.dumps, episodes)))

    def remember(self, **args):
        return self.box.run("remember", json.dumps(args))

    def fact(self, source="log:" + TS):
        return {"date": "2026-10-02 10:00", "text": TEXT, "src": source, "file": "memory/facts.md"}

    def test_remember_valid_sources_and_format(self):
        self.box.log = [{"tool": "fetch_page", "args": '{"url":"http://example.org/trees/"}', "result": "ok"}]
        sources = ["operator", "inferred", "instance:bird-2_a", "log:" + TS, "log:" + TS + "#1",
                   "log:2026-10-01 09:00", "log:2026-10-01T09:00#1", "web:https://example.org/trees"]
        with mock.patch.object(clock, "now", return_value=datetime(2026, 10, 26, 12, 34)):
            for source in sources:
                with self.subTest(source=source):
                    self.assertEqual(self.remember(fact="  A durable fact.  ", source=source), "remembered")
        lines = (self.root / "memory/facts.md").read_text().splitlines()[2:]
        self.assertEqual(lines, [f"- [2026-10-26 12:34] A durable fact. (src: {source})" for source in sources])
        spec = next(s["function"] for s in BUILTINS if s["function"]["name"] == "remember")
        self.assertEqual(spec["parameters"]["required"], ["fact", "source"])

    def test_remember_refuses_invalid_or_missing_source(self):
        for source in [None, "", "world", "instance:Bird", "instance:../bird", "instance:",
                       "log:2026-10-02T09:00:00", "log:" + TS + "#2", "log:" + TS + "#0",
                       "log:" + TS + "#x", "log:2026-10-01", "web:https://example.org/trees", "web:trees"]:
            with self.subTest(source=source):
                result = self.remember(fact=TEXT, source=source)
                self.assertTrue(result.startswith("ERROR:"), result)
                for form in ("log:", "web:", "operator", "instance:", "inferred"):
                    self.assertIn(form, result)
                self.assertIn("if you did not observe it in this run's tool results or your record, use source=inferred", result)
        self.assertIn("source=inferred", self.remember(fact=TEXT))
        self.assertFalse((self.root / "memory/facts.md").exists())

    def test_web_source_result_and_url_boundaries(self):
        self.box.log = [{"tool": "fetch_page", "args": '{"url": "https://example.org/trees/"}', "result": "Trees"}]
        self.assertEqual(self.remember(fact=TEXT, source="web:http://example.org/trees"), "remembered")
        # a URL only mentioned in a result is not a page I fetched (Codex review, 2026-10-26)
        self.box.log = [{"tool": "web_search", "args": "{}", "result": "Read https://example.org/trees/ now"}]
        self.assertTrue(self.remember(fact=TEXT, source="web:https://example.org/trees").startswith("ERROR:"))
        self.box.log = [{"tool": "fetch_page", "args": '{"url": "https://example.org/trees/"}', "result": "Trees"}]
        self.assertTrue(self.remember(fact=TEXT, source="web:https://example.org/tree").startswith("ERROR:"))
        self.box.log = []
        self.assertTrue(self.remember(fact=TEXT, source="web:https://example.org/trees").startswith("ERROR:"))

    def test_rewrite_keeps_tagged_lines(self):
        self.assertEqual(self.remember(fact=TEXT, source="inferred"), "remembered")
        line = "- [2026-10-01 12:00] Consolidated fact. (src: operator)"
        self.assertTrue(self.box._t_rewrite_facts(line).startswith("facts consolidated:"))
        self.assertIn(line, (self.root / "memory/facts.md").read_text())
        self.assertIn("(src: inferred)", (self.root / "memory/facts-archive.md").read_text())

    def test_corpus_source_age_and_unchanged_text(self):
        tagged = "[2026-10-14 09:00] Trees are healthy. (src: web:https://example.org/trees)"
        old = "[2026-10-01] An older fact."
        (self.root / "memory/facts.md").write_text(f"- {tagged}\n- {old}\n")
        (self.root / "memory/facts-archive.md").write_text("- [2026-10-02] Archived. (src: inferred)\n")
        with mock.patch.object(clock, "now", return_value=datetime(2026, 10, 26, 8)):
            docs = memory.corpus(self.root)
        self.assertIn(("fact, src web:https://example.org/trees, 12 days old", tagged), docs)
        self.assertIn(("fact, src unknown, 25 days old", old), docs)
        self.assertIn(("memory/facts-archive.md, src inferred, 24 days old",
                       "[2026-10-02] Archived. (src: inferred)"), docs)
        with mock.patch.object(memory, "_semantic", return_value=[]), mock.patch.object(clock, "now", return_value=datetime(2026, 10, 26)):
            self.assertIn("src web:https://example.org/trees, 12 days old", memory.recall(self.root, "healthy"))

    def test_check_matches_content_and_cited_call(self):
        self.assertEqual(provenance.check("bird", self.fact()), "matched")
        self.assertEqual(provenance.check("bird", self.fact("log:2026-10-01 09:00#1")), "matched")
        self.assertEqual(provenance.check("bird", self.fact("web:http://example.org/trees")), "matched")
        self.assertEqual(provenance.check("bird", self.fact("web:https://example.org/other")), "unmatched")
        self.assertEqual(provenance.check("bird", self.fact("log:" + TS + "#2")), "unmatched")
        self.episode["tools"].insert(0, {"tool": "fetch_page", "args": "{}", "result": "Unrelated first mention."})
        self.write_episodes([self.episode])
        self.assertEqual(provenance.check("bird", self.fact()), "matched")
        self.assertEqual(provenance.check("bird", self.fact("log:" + TS + "#1")), "unmatched")

    def test_self_generated_finish_and_journal_before_fact_only(self):
        self.episode["tools"][0]["result"] = "No observations."
        for field in ("what_happened", "learned"):
            self.episode["finish"] = {field: TEXT}
            self.write_episodes([self.episode])
            self.assertEqual(provenance.check("bird", self.fact()), "self-generated")
        self.episode.pop("finish")
        self.write_episodes([self.episode])
        journal = self.root / "memory/journal.md"
        journal.write_text("## 2026-10-01 12:00 — Thought\n\n" + TEXT)
        self.assertEqual(provenance.check("bird", self.fact()), "self-generated")
        journal.write_text("## 2026-10-03 12:00 — Later\n\n" + TEXT)
        self.assertEqual(provenance.check("bird", self.fact()), "unmatched")

    def test_unmatched_future_record_and_na(self):
        self.episode["ts"] = "2026-10-03T09:00:00"
        self.write_episodes([self.episode])
        self.assertEqual(provenance.check("bird", self.fact("web:https://example.org/trees")), "unmatched")
        for source in (None, "operator", "inferred", "instance:bird"):
            self.assertEqual(provenance.check("bird", self.fact(source)), "n/a")

    def test_facts_section_report_and_safe_reads(self):
        path = self.root / "memory/facts.md"
        path.write_text(f"- [2026-10-02 10:00] {TEXT}\n")
        self.assertEqual(provenance.section("bird"), [])
        path.write_text(f"- [2026-10-02 10:00] {TEXT} (src: log:{TS})\n")
        (self.root / "memory/facts-archive.md").write_text("- [2026-10-02] Other fact. (src: inferred)\n")
        self.assertEqual(provenance.facts("bird")[0], self.fact())
        section = provenance.section("bird")
        self.assertEqual(section[0], "## Where my facts come from")
        self.assertLessEqual(len(section), 6)
        self.assertIn("1 matched the record", section[-1])
        self.assertIn("inferred 1", section[2])
        with mock.patch.object(metrics, "live_episodes", return_value=[]), mock.patch.object(metrics, "judgments", return_value=[]):
            self.assertIn("| log | 1 |", provenance.report(["bird"]))
            self.episode["tools"] = []
            self.write_episodes([self.episode])
            self.assertIn("unmatched: [2026-10-02 10:00] " + TEXT, provenance.report(["bird"]))
        path.unlink()
        outside = self.base / "outside.md"
        outside.write_text("- [2026-10-01] Must not be read. (src: operator)\n")
        path.symlink_to(outside)
        self.assertEqual(len(provenance.facts("bird")), 1)

    def test_exact_timestamp_and_same_episode_log_claim(self):
        self.episode["ts"] = "2026-10-02T10:00:30.123456"
        self.write_episodes([self.episode])
        source = "log:" + self.episode["ts"] + "#1"
        self.assertEqual(self.remember(fact=TEXT, source=source), "remembered")
        self.assertEqual(provenance.check("bird", self.fact(source)), "matched")
        # Regression (Grok's review): the cycle that fetched the page and remembered the fact ends after the fact's
        # minute; it was never "before" the fact, so a web: fact could not match its own fetch.
        self.assertEqual(provenance.check("bird", self.fact("web:https://example.org/trees")), "matched")

    def test_a_short_fact_is_matched_by_its_whole_text(self):
        """Regression (Grok's review): under 8 words a fact had no shingle and was always "unmatched"."""
        self.episode["tools"][0]["result"] = "Status: the orchard has 12 rows today."
        self.write_episodes([self.episode])
        short = {"date": "2026-10-02 10:00", "text": "the orchard has 12 rows", "src": "log:" + self.episode["ts"]}
        self.assertEqual(provenance.check("bird", short), "matched")
        self.assertEqual(provenance.check("bird", {**short, "text": "12 rows"}), "n/a")

    def test_section_and_report_count_self_generated_claims(self):
        (self.root / "memory/facts.md").write_text(f"- [2026-10-02 10:00] {TEXT} (src: log:{TS})\n")
        self.episode["tools"] = []
        self.episode["finish"] = {"learned": TEXT}
        self.write_episodes([self.episode])
        self.assertIn("1 self-generated", provenance.section("bird")[-1])
        with mock.patch.object(metrics, "live_episodes", return_value=[]), mock.patch.object(metrics, "judgments", return_value=[]):
            self.assertIn("self-generated: [2026-10-02 10:00] " + TEXT, provenance.report(["bird"]))

    def test_reliance_groups_judgments_by_surfaced_fact(self):
        results = [
            ("search_memory", {}, "[fact, src inferred, 2 days old] [2026-10-01 09:00] A thought. (src: inferred)"),
            ("read_file", {"path": "memory/facts.md"}, "- [2026-10-01] Old untagged fact."),
            ("search_memory", {}, "[fact, src operator, 2 days old] [2026-10-01] Said. (src: operator)"),
            ("read_file", {"path": "notes/test.md"}, "- [2026-10-01] Some note."),
            ("search_memory", {}, "[fact, src unknown, 2 days old] [2026-10-01] Legacy fact."),
        ]
        episodes = [{"ts": str(i), "tools": [{"tool": tool, "args": json.dumps(args), "result": result}]}
                    for i, (tool, args, result) in enumerate(results)]
        judgments = [{"episode_ts": str(i), "claim_supported": supported, "judge": "current"}
                     for i, supported in enumerate((False, True, True, False, False))]
        judgments += [{"episode_ts": "0", "claim_supported": False, "judge": "retired"},
                      {"episode_ts": "2", "claim_supported": None, "judge": "current"}]
        with mock.patch.object(metrics, "live_episodes", return_value=episodes), mock.patch.object(metrics, "judgments", return_value=judgments), mock.patch.object(metrics, "by_current_judge", side_effect=lambda j: j["judge"] == "current"):
            self.assertEqual(provenance.reliance("bird"), {
                "exposed": {"n": 3, "unsupported": 2, "share": 2 / 3},
                "other": {"n": 2, "unsupported": 1, "share": 0.5}})


if __name__ == "__main__":
    unittest.main()
