"""Check fact source labels, later restatements, paging, and claim matching."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import core, factledger, provenance

TEXT = "The orchard survey found eight healthy trees beside the eastern gate."
DATE = "2026-10-02 10:00"


class FactLedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-factledger-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "ember"
        (self.root / "memory/episodes").mkdir(parents=True)
        self.enterContext(mock.patch.object(core, "INSTANCES", self.base))

    def write_facts(self, facts, archive=False):
        file = "facts-archive.md" if archive else "facts.md"
        (self.root / "memory" / file).write_text("\n".join(
            f"- [{fact['date']}] {fact['text']} (src: {fact['src']})" for fact in facts))

    def write_episodes(self, episodes):
        (self.root / "memory/episodes/test.jsonl").write_text("\n".join(map(json.dumps, episodes)))

    def fact(self, text=TEXT, date=DATE, src="inferred"):
        return {"date": date, "text": text, "src": src, "file": "memory/facts.md"}

    def episode(self, ts, **fields):
        return {"kind": "live", "ts": ts, **fields}

    def test_distinct_later_cycles_and_latest_use(self):
        """Repeated fields and duplicate log rows count once, and last_use follows time, not file order."""
        self.write_facts([self.fact()])
        first = self.episode("2026-10-03T12:00:00", finish={"what_happened": TEXT, "learned": TEXT})
        last = self.episode("2026-10-04T13:00:00", finish={"learned": TEXT})
        self.write_episodes([last, first, first])
        self.assertEqual(factledger.rows("ember"), [
            {"date": DATE, "text": TEXT, "src": "inferred", "status": "n/a",
             "uses": 2, "last_use": "2026-10-04T13:00:00"}])

    def test_only_later_live_prose_counts(self):
        """Earlier/equal cycles, non-live runs, intent, and tool output are not later restatements."""
        self.write_facts([self.fact()])
        self.write_episodes([
            self.episode("2026-10-01T10:00:00", finish={"learned": TEXT}),
            self.episode("2026-10-02T10:00:00", finish={"learned": TEXT}),
            {"kind": "sleep", "ts": "2026-10-03T10:00:00", "finish": {"learned": TEXT}},
            self.episode("2026-10-04T10:00:00", intent={"goal": TEXT}, tools=[
                {"tool": "fetch_page", "result": TEXT},
                {"tool": "run", "args": {"content": TEXT}, "result": TEXT}]),
        ])
        self.assertEqual(factledger.rows("ember")[0]["uses"], 0)
        self.assertIsNone(factledger.rows("ember")[0]["last_use"])

    def test_successful_messages_text_and_content(self):
        """JSON/dict arguments and legacy success results count; failed and malformed calls do not."""
        self.write_facts([self.fact()])
        self.write_episodes([
            self.episode("2026-10-03T10:00:00", tools=[
                {"tool": "send_message", "args": json.dumps({"text": TEXT}), "ok": True, "result": "delivered"}]),
            self.episode("2026-10-04T10:00:00", tools=[
                {"tool": "send_message", "args": {"content": TEXT}, "result": "delivered"}]),
            self.episode("2026-10-05T10:00:00", tools=[
                {"tool": "send_message", "args": {"text": TEXT}, "ok": False, "result": "refused"},
                {"tool": "send_message", "args": {"text": TEXT}, "result": "ERROR: refused"},
                {"tool": "send_message", "args": "broken JSON", "result": "delivered"},
                {"tool": "send_message", "args": [TEXT], "result": "delivered"}, None]),
        ])
        row = factledger.rows("ember")[0]
        self.assertEqual((row["uses"], row["last_use"]), (2, "2026-10-04T10:00:00"))

    def test_archive_facts_are_excluded(self):
        """Archived rows cannot inflate current fact totals."""
        self.write_facts([self.fact()])
        self.write_facts([self.fact("The archived survey recorded several damaged trees.")], archive=True)
        self.assertEqual([row["text"] for row in factledger.rows("ember")], [TEXT])

    def test_status_checks_use_real_provenance(self):
        """The ledger preserves all four statuses returned by provenance."""
        observed = "The river survey recorded seven repaired bridges beside the northern road."
        missing = "The hill survey recorded nine broken fences beside the western path."
        stamp = "2026-10-01T09:00:00"
        self.write_facts([self.fact(src="log:" + stamp), self.fact(observed, src="log:" + stamp),
                          self.fact(missing, src="log:" + stamp), self.fact("A separate inferred fact.")])
        self.write_episodes([self.episode(stamp, finish={"learned": TEXT}, tools=[
            {"tool": "fetch_page", "args": "{}", "result": observed}])])
        self.assertEqual([row["status"] for row in factledger.rows("ember")],
                         ["self-generated", "matched", "unmatched", "n/a"])

    def test_status_order_usage_and_plain_words(self):
        """Unsourced groups precede matched facts; each group sorts by descending usage."""
        facts = [{**self.fact(f"Fact number {i} is a detailed invented observation."), "status": status,
                  "uses": uses, "last_use": "2026-10-04T13:00:00" if uses else None}
                 for i, (status, uses) in enumerate([
                     ("matched", 9), ("n/a", 3), ("unmatched", 1),
                     ("self-generated", 1), ("self-generated", 2), ("unmatched", 4)])]
        with mock.patch.object(factledger, "rows", return_value=facts):
            text = factledger.record_text("ember")
            lines = text.splitlines()
            self.assertEqual([int(line.split("number ")[1].split()[0]) for line in lines[2:-1]], [4, 3, 5, 2, 1, 0])
            for words in factledger.SOURCES.values():
                self.assertIn("source: " + words, text)
            self.assertIn("restated in 1 later cycle, last 2026-10-04T13:00", text)
            self.assertIn("restated in 2 later cycles", text)
            self.assertTrue(lines[-1].endswith("Information only."))

    def test_paging_counts_filter_and_cap(self):
        """Totals cover all current facts; page ranges cover the filtered result and limit never exceeds 200."""
        pairs = [(self.fact(f"Invented orchard observation number {i} has enough words."),
                  "unmatched" if i < 204 else "matched") for i in range(205)]
        with mock.patch.object(provenance, "facts", return_value=[f for f, _ in pairs]), \
                mock.patch.object(provenance._Evidence, "check", side_effect=[s for _, s in pairs] * 4):
            page = factledger.record_text("ember", limit=2, offset=2, status="unmatched")
            self.assertIn("self-generated: 0; unmatched: 204; n/a: 0; matched: 1", page)
            self.assertIn("204 facts; showing 3–4", page)
            self.assertEqual(len(page.splitlines()), 5)
            self.assertIn("observation number 2 ", page)
            self.assertNotIn("observation number 4 ", page)
            capped = factledger.record_text("ember", limit=999)
            self.assertIn("205 facts; showing 1–200", capped)
            self.assertEqual(len(capped.splitlines()), 203)
            self.assertIn("205 facts; showing 0–0", factledger.record_text("ember", offset=999))

    def test_empty_short_and_truncated_display(self):
        """Empty pages stay readable, short facts do not claim zero uses, and display text stops at 160 characters."""
        self.assertIn("0 facts; showing 0–0", factledger.record_text("ember"))
        long = "invented " * 30
        self.write_facts([self.fact("Two words"), self.fact(long.strip())])
        self.write_episodes([self.episode("2026-10-03T10:00:00", finish={"learned": "Two words"})])
        short = factledger.rows("ember")[0]
        self.assertEqual((short["uses"], short["last_use"]), (None, None))
        text = factledger.record_text("ember")
        self.assertIn("restatements not counted (fact too short)", text)
        self.assertIn(long[:160] + " — source:", text)
        self.assertIn("never restated since", text)

    def test_invalid_status_names_allowed_values(self):
        """Invalid status filters fail before reading records and name every allowed status."""
        with mock.patch.object(factledger, "rows") as read:
            with self.assertRaises(ValueError) as raised:
                factledger.record_text("ember", status="unknown")
            read.assert_not_called()
        for status in factledger.SOURCES:
            self.assertIn(status, str(raised.exception))

    def test_as_of_is_exclusive_and_accepts_timezone(self):
        """The cutoff excludes cycles at or after it, including timestamps with timezone offsets."""
        self.write_facts([self.fact()])
        self.write_episodes([self.episode(f"2026-10-0{day}T10:00:00+00:00", finish={"learned": TEXT})
                             for day in (3, 4, 5)])
        cutoff = datetime.fromisoformat("2026-10-04T10:00:00+00:00")
        row = factledger.rows("ember", cutoff)[0]
        self.assertEqual((row["uses"], row["last_use"]), (1, "2026-10-03T10:00:00+00:00"))
        self.assertIn("restated in 1 later cycle", factledger.record_text("ember", as_of=cutoff))

    def test_short_shingles_and_field_boundaries(self):
        """Three-word facts match within longer prose; separate prose fields cannot manufacture a match."""
        self.write_facts([self.fact("orchard trees thrive"), self.fact("red green blue yellow")])
        self.write_episodes([self.episode("2026-10-03T10:00:00", finish={
            "what_happened": "Today orchard trees thrive again. red green", "learned": "blue yellow"})])
        self.assertEqual([row["uses"] for row in factledger.rows("ember")], [1, 0])

    def test_match_claim_overlap_ties_and_matched_exclusion(self):
        """Most overlap wins, ties choose the newest fact, and checked sources never match unsupported claims."""
        facts = [self.fact(), self.fact(date="2026-10-03 10:00"),
                 self.fact(TEXT + " Extra detailed report about the southern valley survey.", date="2026-10-04 10:00")]
        pairs = [(facts[0], "unmatched"), (facts[1], "n/a"), (facts[2], "matched")]
        cutoff = datetime(2026, 10, 4)
        rows = [{**f, "status": status} for f, status in pairs]
        with mock.patch.object(factledger, "rows", side_effect=AssertionError("unexpected rebuild")):
            result = factledger.match_claim("ember", facts[2]["text"], as_of=cutoff, rows=rows)
            self.assertEqual((result["date"], result["text"], result["status"]),
                             (facts[1]["date"], TEXT, "n/a"))
            self.assertIsNone(factledger.match_claim("ember", "No common words in this invented quotation.", rows=rows))
            self.assertIsNone(factledger.match_claim("ember", facts[2]["text"], rows=[rows[2]]))
            self.assertEqual(factledger.match_claim("ember", TEXT, rows=[
                rows[0], {**self.fact("orchard survey found", date="2026-10-05"), "status": "n/a"}])["text"], TEXT)

    def test_malformed_episode_fields_do_not_stop_valid_cycles(self):
        """Malformed record fields do not hide a later valid restatement."""
        self.write_facts([self.fact()])
        self.write_episodes([self.episode("invalid", finish={"learned": TEXT}),
                             self.episode("2026-10-03T10:00:00", finish=[], tools="invalid"),
                             self.episode("2026-10-04T10:00:00", finish={"learned": TEXT})])
        self.assertEqual(factledger.rows("ember")[0]["uses"], 1)

    def test_300_facts_by_300_cycles_under_five_seconds(self):
        """A full fixture rebuild stays below five seconds and builds evidence once."""
        facts = [self.fact(f"Orchard plot {i} contains healthy trees beside the eastern gate.",
                           src="log:2026-10-01T09:00:00")
                 for i in range(300)]
        self.write_facts(facts)
        self.write_episodes([self.episode(f"2026-10-03T{hour:02d}:{minute:02d}:00",
                                         finish={"learned": facts[hour * 60 + minute]["text"]})
                             for hour in range(5) for minute in range(60)])
        started = time.perf_counter()
        with mock.patch.object(provenance, "_Evidence", wraps=provenance._Evidence) as evidence, \
                mock.patch.object(provenance, "_episodes", wraps=provenance._episodes) as episodes:
            result = factledger.rows("ember")
            self.assertEqual(evidence.call_count, 1)
            episodes.assert_called_once_with("ember")
        elapsed = time.perf_counter() - started
        self.assertEqual(len(result), 300)
        self.assertTrue(all(row["uses"] == 1 for row in result))
        self.assertLess(elapsed, 5, f"300 facts x 300 cycles took {elapsed:.3f}s")

    def test_cycle_shingles_are_built_once_per_size(self):
        """Adding facts of the same size must not rebuild each cycle's prose shingles."""
        pairs = [(self.fact(f"Fact {i} describes orchard trees beside the eastern gate."), "n/a")
                 for i in range(20)]
        self.write_episodes([self.episode("2026-10-03T10:00:00", finish={"learned": TEXT})])
        with mock.patch.object(provenance, "facts", return_value=[f for f, _ in pairs]), \
                mock.patch.object(provenance, "_shingles", wraps=provenance._shingles) as shingles:
            factledger.rows("ember")
        prose_calls = [call for call in shingles.call_args_list if call.args[0] == TEXT]
        self.assertEqual(prose_calls, [mock.call(TEXT, provenance.SHINGLE)])


if __name__ == "__main__":
    unittest.main()
