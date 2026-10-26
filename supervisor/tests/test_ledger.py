"""Spend ledgers read incrementally (ledger.spend): the same totals as parsing the whole file, at the cost of what was
appended. Every web page paid ~1 s for spent_today re-parsing cloud_usage.jsonl 8 times an overview (2026-10-26)."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import cloudllm, coder, ledger


def whole_file(path: Path, instance: str, until: datetime | None = None) -> float:
    """spent_today as it was: the whole file parsed on every call."""
    today = (until.date() if until else date.today()).isoformat()
    total = 0.0
    for line in path.read_text().splitlines():
        r = json.loads(line)
        if (r["instance"] == instance and r["ts"].startswith(today)
                and (until is None or datetime.fromisoformat(r["ts"]) < until)):
            total += r.get("usd") or 0.0
    return total


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "cloud_usage.jsonl"
        self.today = date.today().isoformat()
        self.patch = mock.patch.object(cloudllm, "USAGE", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def row(self, who, usd, ts=None):
        return json.dumps({"ts": ts or f"{self.today}T10:00:00", "instance": who, "usd": usd}) + "\n"

    def append(self, *lines):
        with self.path.open("a") as f:
            f.write("".join(lines))

    def test_same_totals_as_the_whole_file_with_and_without_until(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.append(self.row("wren", 0.1), self.row("wren", 0.2, f"{self.today}T12:00:00"), self.row("kant", 1.0),
                    self.row("wren", 5.0, f"{yesterday}T23:00:00"), self.row("wren", None), self.row("wren", 0.03))
        noon = datetime.fromisoformat(f"{self.today}T12:00:00")
        for who in ("wren", "kant", "nobody"):
            for until in (None, noon, noon - timedelta(days=1, hours=-11)):
                self.assertEqual(cloudllm.spent_today(who, until), whole_file(self.path, who, until), (who, until))
        self.assertAlmostEqual(cloudllm.spent_today("wren"), 0.33)

    def test_an_append_is_counted_and_only_it_is_parsed(self):
        self.append(self.row("wren", 0.5))
        self.assertEqual(cloudllm.spent_today("wren"), 0.5)
        self.append(self.row("wren", 0.25))
        with mock.patch.object(ledger.json, "loads", wraps=json.loads) as loads:
            self.assertEqual(cloudllm.spent_today("wren"), 0.75)
        self.assertEqual(loads.call_count, 1, "an append re-parsed the whole ledger")

    def test_a_replaced_file_is_read_again(self):
        """shaft delete rewrites the ledger without an instance's rows (a new inode): its spend must go."""
        self.append(self.row("wren", 0.5), self.row("kant", 2.0))
        self.assertEqual(cloudllm.spent_today("kant"), 2.0)
        tmp = self.dir / ".new"
        tmp.write_text(self.row("wren", 0.5) + self.row("thelos", 0.1) + self.row("thelos", 0.1))
        os.replace(tmp, self.path)
        self.assertEqual(cloudllm.spent_today("kant"), 0.0)
        self.assertAlmostEqual(cloudllm.spent_today("thelos"), 0.2)

    def test_a_shrunk_file_is_read_again(self):
        self.append(self.row("wren", 0.5), self.row("wren", 0.5))
        self.assertEqual(cloudllm.spent_today("wren"), 1.0)
        with self.path.open("r+") as f:
            f.truncate(len(self.row("wren", 0.5)))
        self.assertEqual(cloudllm.spent_today("wren"), 0.5)

    def test_a_file_rewritten_shorter_in_place_is_read_again_even_past_its_last_whole_line(self):
        """Review (Codex, 2026-10-26): shrunk was judged against the last whole line read, not the size seen; a file
        rewritten in place, shorter but still past that line, kept the old rows for good."""
        self.append(self.row("wren", 0.5), self.row("wren", 0.25).rstrip("\n") + " " * 200)
        self.assertEqual(cloudllm.spent_today("wren"), 0.75)
        with self.path.open("r+") as f:                 # same inode: the first row now says 0.90
            f.write(self.row("wren", 0.9) + self.row("wren", 0.25).rstrip("\n"))
            f.truncate()
        self.assertEqual(cloudllm.spent_today("wren"), whole_file(self.path, "wren"))
        self.assertAlmostEqual(cloudllm.spent_today("wren"), 1.15)

    def test_a_line_still_being_written_is_not_kept(self):
        """A last line without its newline is read as the whole file read it, and read again once it is finished."""
        self.append(self.row("wren", 0.5), self.row("wren", 0.25).rstrip("\n"))
        self.assertEqual(cloudllm.spent_today("wren"), 0.75)
        self.append("\n", self.row("wren", 1.0))
        self.assertEqual(cloudllm.spent_today("wren"), 1.75, "the unfinished line was counted twice or lost")

    def test_a_malformed_line_raises_and_is_never_counted_as_no_spend(self):
        self.append(self.row("wren", 0.5))
        self.assertEqual(cloudllm.spent_today("wren"), 0.5)
        self.append("{not json\n")
        for _ in range(2):                              # every call: the cache never skips past it
            with self.assertRaises(ValueError):
                cloudllm.spent_today("wren")

    def test_no_file_is_no_spend_and_the_coder_reads_its_own_ledger(self):
        self.assertEqual(cloudllm.spent_today("wren"), 0.0)
        path = self.dir / "coder_usage.jsonl"
        path.write_text(self.row("wren", 0.4) + self.row("wren", 0.6))
        with mock.patch.object(coder, "USAGE", path):
            self.assertEqual(coder.spent_today("wren"), 1.0)
        self.assertEqual(cloudllm.spent_today("wren"), 0.0, "the two ledgers' caches got mixed")


if __name__ == "__main__":
    unittest.main()
