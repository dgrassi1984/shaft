"""Incremental SQLite provenance evidence index (#231)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import core, metrics, provenance

LOG_TEXT = "The northern orchard survey counted fourteen healthy apple trees near the stone wall."
WEB_TEXT = "The harbour tide gauge recorded a peak water level of three metres at dawn."
FINISH_TEXT = "The eastern meadow inspection suggested nine wooden posts needed replacement soon."
LEARNED_TEXT = "The southern canal lock gate showed minor rust along the lower hinge bracket."
JOURNAL_TEXT = "The western ridge weather station logged steady gusts across the upper pasture."
LATER_OBS_TEXT = "The valley bridge inspection confirmed six granite piers remained completely sound."


class ProvenanceIndexTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-provenance-index-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.instances = self.base / "instances"
        self.sup = self.base / "supervisor"
        self.root = self.instances / "bird"
        (self.root / "memory/episodes").mkdir(parents=True)
        self.enterContext(mock.patch.object(core, "INSTANCES", self.instances))
        self.enterContext(mock.patch.object(core, "SUP_DIR", self.sup))
        self.enterContext(mock.patch.object(metrics, "live_episodes", return_value=[]))
        self.enterContext(mock.patch.object(metrics, "judgments_for_stats", return_value=[]))

    def db_path(self) -> Path:
        return self.sup / "provenance" / "bird.sqlite"

    def write_facts(self, rows: list[dict]) -> None:
        lines = [f"- [{r['date']}] {r['text']}" + (f" (src: {r['src']})" if r.get("src") else "") for r in rows]
        (self.root / "memory/facts.md").write_text("\n".join(lines) + "\n")

    def in_memory_statuses(self) -> list[tuple[dict, str]]:
        episodes = provenance._episodes("bird")
        journal = core.safe_read(self.root, "memory/journal.md", "", errors="replace")
        evidence = provenance._Evidence(episodes, journal)
        return [(row, evidence.check(row)) for row in provenance.facts("bird")]

    def assert_matches_from_scratch(self) -> list[tuple[dict, str]]:
        kept_pairs = provenance.checked("bird")
        kept_section = provenance.section("bird")
        kept_report = provenance.report(["bird"])
        expected_pairs = self.in_memory_statuses()
        self.assertEqual(kept_pairs, expected_pairs)
        for row, expected in expected_pairs:
            self.assertEqual(provenance.check("bird", row), expected)
        self.db_path().unlink(missing_ok=True)
        self.assertEqual(provenance.checked("bird"), expected_pairs)
        self.assertEqual(provenance.section("bird"), kept_section)
        self.assertEqual(provenance.report(["bird"]), kept_report)
        return kept_pairs

    def test_kept_index_matches_in_memory_across_all_statuses_short_facts_and_malformed_episodes(self):
        episodes_path = self.root / "memory/episodes/2026-10-01.jsonl"
        rows = [
            json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                        "intent": {"at": "2026-10-01T07:58:00"},
                        "tools": [{"tool": "run", "args": "{}", "result": LOG_TEXT},
                                  {"tool": "fetch_page", "args": '{"url":"https://example.org/tide/"}',
                                   "result": WEB_TEXT}],
                        "finish": {"what_happened": FINISH_TEXT, "learned": LEARNED_TEXT}}),
            "{broken json line",
            json.dumps(["not", "a", "dict"]),
            json.dumps({"kind": "live", "ts": 12345, "intent": "bad", "tools": [5, "bad"], "finish": ["bad"]}),
            json.dumps({"kind": "live", "ts": "2026-10-01T09:00:00",
                        "tools": [{"tool": "run", "args": "{}", "result": " cedar grove thrives today "}],
                        "finish": {"learned": "pine seedlings sprouted early"}}),
            json.dumps({"kind": "live", "ts": "2026-10-03T09:00:00",
                        "tools": [{"tool": "run", "args": "{}", "result": LATER_OBS_TEXT}]}),
        ]
        episodes_path.write_text("\n".join(rows) + "\n")
        (self.root / "memory/journal.md").write_text(
            "## 2026-10-01 08:30 — Morning walk\n\n" + JOURNAL_TEXT + "\n"
        )
        fact_rows = [
            {"date": "2026-10-02 10:00", "text": LOG_TEXT, "src": "log:2026-10-01T08:00:00#1"},
            {"date": "2026-10-02 10:00", "text": WEB_TEXT, "src": "web:https://example.org/tide"},
            {"date": "2026-10-02 10:00", "text": LOG_TEXT, "src": "log:2026-10-01T08:00:00#2"},
            {"date": "2026-10-02 10:00", "text": LATER_OBS_TEXT, "src": "log:2026-10-01T08:00:00#1"},
            {"date": "2026-10-02 10:00", "text": "An entirely invented observation with eight distinct words here.",
             "src": "web:https://example.org/tide"},
            {"date": "2026-10-02 10:00", "text": FINISH_TEXT, "src": "log:2026-10-01T08:00:00#1"},
            {"date": "2026-10-02 10:00", "text": LEARNED_TEXT, "src": "web:https://example.org/tide"},
            {"date": "2026-10-02 10:00", "text": JOURNAL_TEXT, "src": "log:2026-10-01T08:00:00#1"},
            {"date": "2026-10-02 10:00", "text": "cedar grove thrives today", "src": "log:2026-10-01T09:00:00#1"},
            {"date": "2026-10-02 10:00", "text": "pine seedlings sprouted early", "src": "log:2026-10-01T09:00:00#1"},
            {"date": "2026-10-02 10:00", "text": "maple saplings withered fast", "src": "log:2026-10-01T09:00:00#1"},
            {"date": "2026-10-02 10:00", "text": "two words", "src": "log:2026-10-01T09:00:00#1"},
            {"date": "2026-10-02 10:00", "text": LOG_TEXT, "src": "inferred"},
        ]
        self.write_facts(fact_rows)
        pairs = self.assert_matches_from_scratch()
        self.assertEqual([status for _, status in pairs], [
            "matched",
            "matched",
            "unmatched",
            "unmatched",
            "unmatched",
            "self-generated",
            "self-generated",
            "self-generated",
            "matched",
            "self-generated",
            "unmatched",
            "n/a",
            "n/a",
        ])
        self.assertTrue(self.db_path().exists())
        with sqlite3.connect(self.db_path()) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"meta", "sources", "obs_8", "wr_8", "obs_4", "wr_4"} <= tables)

    def test_231_second_build_does_not_reshingle_earlier_episodes(self):
        episodes_path = self.root / "memory/episodes/2026-10-01.jsonl"
        ep1 = {"kind": "live", "ts": "2026-10-01T08:00:00",
               "tools": [{"tool": "run", "args": "{}", "result": LOG_TEXT}],
               "finish": {"what_happened": FINISH_TEXT, "learned": LEARNED_TEXT}}
        ep2 = {"kind": "live", "ts": "2026-10-01T09:00:00",
               "tools": [{"tool": "fetch_page", "args": '{"url":"https://example.org/tide/"}', "result": WEB_TEXT}],
               "finish": {"what_happened": "Second cycle prose summary with enough words to shingle.",
                          "learned": "Second cycle learned note with enough words to shingle."}}
        episodes_path.write_text(json.dumps(ep1) + "\n" + json.dumps(ep2) + "\n")
        (self.root / "memory/journal.md").write_text(
            "## 2026-10-01 08:30 — First entry\n\n" + JOURNAL_TEXT + "\n\n"
            "## 2026-10-01 09:30 — Second entry\n\nSecond journal entry prose stays unchanged here.\n"
        )
        new_prose = "Appended third episode learned prose about repairing the reservoir sluice valve."
        new_obs = "Appended third episode tool result measuring reservoir outflow rate today."
        self.write_facts([
            {"date": "2026-10-02 10:00", "text": FINISH_TEXT, "src": "log:2026-10-01T10:00:00#1"},
            {"date": "2026-10-02 10:00", "text": new_prose, "src": "log:2026-10-01T10:00:00#1"},
        ])
        first = provenance.checked("bird")
        self.assertEqual([s for _, s in first], ["self-generated", "unmatched"])

        ep3 = {"kind": "live", "ts": "2026-10-01T10:00:00",
               "tools": [{"tool": "run", "args": "{}", "result": new_obs}],
               "finish": {"learned": new_prose}}
        with episodes_path.open("a") as f:
            f.write(json.dumps(ep3) + "\n")

        earlier_texts = {
            LOG_TEXT, WEB_TEXT, FINISH_TEXT, LEARNED_TEXT, JOURNAL_TEXT,
            ep2["finish"]["what_happened"], ep2["finish"]["learned"],
        }
        with mock.patch.object(provenance, "_shingles", wraps=provenance._shingles) as shingles:
            second = provenance.checked("bird")
        self.assertEqual([s for _, s in second], ["self-generated", "self-generated"])
        shingled_texts = {call.args[0] for call in shingles.call_args_list}
        for old in (LOG_TEXT, WEB_TEXT, LEARNED_TEXT, JOURNAL_TEXT,
                    ep2["finish"]["what_happened"], ep2["finish"]["learned"]):
            self.assertNotIn(old, shingled_texts)
        self.assertIn(new_prose, shingled_texts)
        self.assertIn(new_obs, shingled_texts)
        self.assert_matches_from_scratch()

    def test_unterminated_trailing_episode_line_is_not_consumed_until_newline(self):
        episodes_path = self.root / "memory/episodes/2026-10-01.jsonl"
        ep1_line = json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                               "tools": [{"tool": "run", "args": "{}", "result": LOG_TEXT}]}) + "\n"
        partial = '{"kind": "live", "ts": "2026-10-01T09:00:00", "finish": {"learned": "'
        episodes_path.write_text(ep1_line + partial)
        self.write_facts([{"date": "2026-10-02 10:00", "text": LEARNED_TEXT, "src": "log:2026-10-01T09:00:00#1"}])
        self.assertEqual(provenance.checked("bird")[0][1], "unmatched")
        with sqlite3.connect(self.db_path()) as conn:
            offset = conn.execute("SELECT offset FROM sources WHERE file='memory/episodes/2026-10-01.jsonl'").fetchone()[0]
        self.assertEqual(offset, len(ep1_line.encode()))

        with episodes_path.open("a") as f:
            f.write(LEARNED_TEXT + '"_}}\n'.replace('"_', '"'))
        with mock.patch.object(provenance, "_shingles", wraps=provenance._shingles) as shingles:
            self.assertEqual(provenance.checked("bird")[0][1], "self-generated")
        self.assertNotIn(LOG_TEXT, {call.args[0] for call in shingles.call_args_list})
        self.assert_matches_from_scratch()

    def test_trailing_line_indexed_without_newline_then_extended_on_the_same_line_rebuilds(self):
        """A valid JSON tail with no "\\n" is indexed; if the same line then grows (so it no longer parses), its
        prose must leave the index, as a build from scratch would never have read it (review of #231)."""
        episodes_path = self.root / "memory/episodes/2026-10-01.jsonl"
        ep1_line = json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                               "tools": [{"tool": "run", "args": "{}", "result": LOG_TEXT}]}) + "\n"
        tail = json.dumps({"kind": "live", "ts": "2026-10-01T09:00:00", "finish": {"learned": LEARNED_TEXT}})
        episodes_path.write_text(ep1_line + tail)
        self.write_facts([{"date": "2026-10-02 10:00", "text": LEARNED_TEXT, "src": "log:2026-10-01T08:00:00#1"}])
        self.assertEqual(provenance.checked("bird")[0][1], "self-generated")
        with episodes_path.open("a") as f:
            f.write(' trailing junk on the same line\n')
        self.assertEqual(provenance.checked("bird")[0][1], "unmatched")
        self.assert_matches_from_scratch()

    def test_edited_deleted_or_truncated_episode_files_trigger_full_rebuild(self):
        first_file = self.root / "memory/episodes/2026-10-01.jsonl"
        second_file = self.root / "memory/episodes/2026-10-02.jsonl"
        text_a = "Alpha orchard survey counted twelve healthy apple trees beside northern gate."
        text_b = "Bravo harbour patrol spotted eleven berthed cargo ships beyond southern pier."
        self.assertEqual(len(text_a), len(text_b))
        self.write_facts([
            {"date": "2026-10-03 10:00", "text": text_a, "src": "log:2026-10-02T12:00:00#1"},
            {"date": "2026-10-03 10:00", "text": text_b, "src": "log:2026-10-02T12:00:00#1"},
            {"date": "2026-10-03 10:00", "text": FINISH_TEXT, "src": "log:2026-10-02T12:00:00#1"},
        ])

        with self.subTest("same_length_edit"):
            first_file.write_text(json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                                              "finish": {"learned": text_a}}) + "\n")
            second_file.write_text(json.dumps({"kind": "live", "ts": "2026-10-02T08:00:00",
                                               "finish": {"learned": FINISH_TEXT}}) + "\n")
            self.assertEqual([s for _, s in provenance.checked("bird")],
                             ["self-generated", "unmatched", "self-generated"])
            first_file.write_text(json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                                              "finish": {"learned": text_b}}) + "\n")
            self.assertEqual([s for _, s in self.assert_matches_from_scratch()],
                             ["unmatched", "self-generated", "self-generated"])

        with self.subTest("different_length_edit"):
            first_file.write_text(json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                                              "finish": {"learned": text_a + " Extra words added here."}}) + "\n")
            self.assertEqual([s for _, s in self.assert_matches_from_scratch()],
                             ["self-generated", "unmatched", "self-generated"])

        with self.subTest("truncated_file"):
            ep_extra = json.dumps({"kind": "live", "ts": "2026-10-01T09:00:00",
                                   "finish": {"learned": text_b}}) + "\n"
            first_file.write_text(json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                                              "finish": {"learned": text_a}}) + "\n" + ep_extra)
            self.assertEqual([s for _, s in provenance.checked("bird")],
                             ["self-generated", "self-generated", "self-generated"])
            first_file.write_text(json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                                              "finish": {"learned": text_a}}) + "\n")
            self.assertEqual([s for _, s in self.assert_matches_from_scratch()],
                             ["self-generated", "unmatched", "self-generated"])

        with self.subTest("deleted_episode_file"):
            second_file.unlink()
            self.assertEqual([s for _, s in self.assert_matches_from_scratch()],
                             ["self-generated", "unmatched", "unmatched"])

    def test_journal_appending_to_last_block_and_adding_new_block_match_from_scratch(self):
        (self.root / "memory/episodes/2026-10-01.jsonl").write_text(
            json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00", "tools": []}) + "\n"
        )
        journal = self.root / "memory/journal.md"
        appended_text = "The afternoon patrol noticed three falcons circling above the limestone cliff."
        new_block_text = "The evening check confirmed the greenhouse heaters held twenty degrees steady."
        self.write_facts([
            {"date": "2026-10-02 10:00", "text": JOURNAL_TEXT, "src": "log:2026-10-01T08:00:00#1"},
            {"date": "2026-10-02 10:00", "text": appended_text, "src": "log:2026-10-01T08:00:00#1"},
            {"date": "2026-10-02 10:00", "text": new_block_text, "src": "log:2026-10-01T08:00:00#1"},
        ])
        journal.write_text(
            "## 2026-10-01 09:00 — First block\n\n" + JOURNAL_TEXT + "\n\n"
            "## 2026-10-01 12:00 — Second block\n\nInitial paragraph in the last journal block.\n"
        )
        self.assertEqual([s for _, s in provenance.checked("bird")],
                         ["self-generated", "unmatched", "unmatched"])

        with journal.open("a") as f:
            f.write(appended_text + "\n")
        with mock.patch.object(provenance, "_shingles", wraps=provenance._shingles) as shingles:
            self.assertEqual([s for _, s in provenance.checked("bird")],
                             ["self-generated", "self-generated", "unmatched"])
        self.assertNotIn("\n\n" + JOURNAL_TEXT + "\n\n", {call.args[0] for call in shingles.call_args_list})
        self.assert_matches_from_scratch()

        with journal.open("a") as f:
            f.write("\n## 2026-10-01 18:00 — Third block\n\n" + new_block_text + "\n")
        self.assertEqual([s for _, s in self.assert_matches_from_scratch()],
                         ["self-generated", "self-generated", "self-generated"])

    def test_journal_append_continuing_last_word_rebuilds(self):
        journal = self.root / "memory/journal.md"
        journal.write_text("## 2026-10-01 08:30 — x\n\none two three four five six seven eight")
        self.write_facts([{"date": "2026-10-02 10:00", "text": "one two three four five six seven eight",
                           "src": "log:2026-10-01T08:00:00#1"}])
        self.assertEqual(provenance.checked("bird")[0][1], "self-generated")
        with journal.open("a") as f:
            f.write("EXTRA")
        self.assertEqual([s for _, s in self.assert_matches_from_scratch()], ["unmatched"])

    def test_journal_append_with_newline_does_not_rebuild(self):
        journal = self.root / "memory/journal.md"
        earlier = "The earlier journal block prose that must not be re-shingled."
        journal.write_text("## 2026-10-01 08:30 — First\n\n" + earlier + "\n\n"
                           "## 2026-10-01 09:30 — Second\n\nsecond block words here for shingles\n")
        self.write_facts([{"date": "2026-10-02 10:00", "text": JOURNAL_TEXT, "src": "log:2026-10-01T08:00:00#1"}])
        self.assertEqual(provenance.checked("bird")[0][1], "unmatched")
        with journal.open("a") as f:
            f.write("\nmore words here")
        with mock.patch.object(provenance, "_shingles", wraps=provenance._shingles) as shingles:
            provenance.checked("bird")
        shingled = {call.args[0] for call in shingles.call_args_list}
        self.assertFalse(any(earlier in text for text in shingled))

    def test_new_size_syncs_existing_sizes_from_the_same_read(self):
        episodes_path = self.root / "memory/episodes/2026-10-01.jsonl"
        episodes_path.write_text(json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                                             "tools": [{"tool": "run", "args": "{}", "result": LOG_TEXT}]}) + "\n")
        fact8 = {"date": "2026-10-02 10:00", "text": "eight word observation text for the first fact here",
                 "src": "log:2026-10-01T08:00:00#1"}
        fact4 = {"date": "2026-10-02 10:00", "text": "four short words here", "src": "log:2026-10-01T08:00:00#1"}
        self.write_facts([fact8, fact4])

        evidence = provenance._evidence("bird")
        evidence.check(fact8)

        new_obs = "appended episode observation text with more than eight distinct words here"
        with episodes_path.open("a") as f:
            f.write(json.dumps({"kind": "live", "ts": "2026-10-01T11:00:00",
                                "tools": [{"tool": "run", "args": "{}", "result": new_obs}]}) + "\n")

        evidence.check(fact4)

        for size in (4, 8):
            shingles = provenance._shingles(new_obs, size)
            self.assertTrue(shingles)
            hashes = [provenance._hash(s) for s in shingles]
            marks = ",".join("?" * len(hashes))
            with sqlite3.connect(self.db_path()) as conn:
                n = conn.execute(f"SELECT COUNT(DISTINCT h) FROM obs_{size} WHERE h IN ({marks})",
                                 hashes).fetchone()[0]
            self.assertEqual(n, len(hashes))

    def test_busy_locked_database_is_not_deleted(self):
        episodes_path = self.root / "memory/episodes/2026-10-01.jsonl"
        episodes_path.write_text(json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                                             "finish": {"learned": LEARNED_TEXT}}) + "\n")
        self.write_facts([{"date": "2026-10-02 10:00", "text": LEARNED_TEXT, "src": "log:2026-10-01T08:00:00#1"}])
        provenance.checked("bird")
        self.assertTrue(self.db_path().exists())

        holder = sqlite3.connect(self.db_path(), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(provenance, "BUSY_TIMEOUT_S", 0.1):
                with self.assertRaises(sqlite3.OperationalError):
                    provenance.checked("bird")
            self.assertTrue(self.db_path().exists())
        finally:
            holder.execute("ROLLBACK")
            holder.close()

        with sqlite3.connect(self.db_path()) as conn:
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 0)

    def test_garbage_db_file_and_version_mismatch_are_replaced(self):
        (self.root / "memory/episodes/2026-10-01.jsonl").write_text(
            json.dumps({"kind": "live", "ts": "2026-10-01T08:00:00",
                        "finish": {"learned": LEARNED_TEXT}}) + "\n"
        )
        self.write_facts([{"date": "2026-10-02 10:00", "text": LEARNED_TEXT, "src": "log:2026-10-01T08:00:00#1"}])
        self.db_path().parent.mkdir(parents=True, exist_ok=True)
        self.db_path().write_bytes(b"not a valid sqlite3 database file at all")
        self.assertEqual([s for _, s in self.assert_matches_from_scratch()], ["self-generated"])

        with sqlite3.connect(self.db_path()) as conn:
            conn.execute("DELETE FROM wr_8")
            conn.execute("UPDATE meta SET value='999' WHERE key='version'")
        self.assertEqual([s for _, s in self.assert_matches_from_scratch()], ["self-generated"])

    def test_delete_removes_the_kept_index_but_does_not_archive_it(self):
        """`shaft delete` lists every file an instance owns under .supervisor/: the index must go with it (else a
        deleted instance leaves 100+ MB behind), and stays out of the archive, which keeps the episodes it is built
        from. Another instance's index is not its."""
        from supervisor import battery, delete
        folder = self.sup / "provenance"
        folder.mkdir(parents=True)
        own = [folder / "bird.sqlite", folder / "bird.sqlite-journal"]
        for path in [*own, folder / "bird-2.sqlite"]:
            path.write_bytes(b"x")
        with mock.patch.object(delete, "SUP_DIR", self.sup), \
             mock.patch.object(battery, "BACKUP", self.base / "battery-backup"):
            found = delete.own_files("bird")
            self.assertEqual({p for p in found if p.parent == folder}, set(own))
            part = self.base / "archive.tar.gz"
            delete._write_archive(delete.Found("bird", None, False, None, files=found), "20261121-000000", part)
        import tarfile
        with tarfile.open(part) as tar:
            self.assertFalse([m for m in tar.getnames() if "provenance" in m])


if __name__ == "__main__":
    unittest.main()
