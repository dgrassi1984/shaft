"""An instance knows whether and when it slept, and what came of it — including sleeps that failed."""
from __future__ import annotations

import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from supervisor import runner

TEMPLATE = Path(__file__).resolve().parents[2] / "template"

SLEEP = "## 2026-10-12 04:00 — Sleep\n\nI consolidated the agenda.\n\n*Consolidation actions:* remember: x"
CHAT = "## 2026-10-12 {h}:00 — Conversation with Daniele\n\n" + "words " * 400


def last_sleep(entries: list[str], window: int = 16000) -> str:
    root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
    (root / "memory").mkdir()
    (root / "memory" / "journal.md").write_text("".join(f"\n---\n{e}\n" for e in entries))
    code = ("from shaft.state import State; from shaft import prompts; from pathlib import Path; import sys\n"
            f"s = State(Path({str(root)!r})); j, _, _ = s.recent_journal({window})\n"
            "print(prompts.last_sleep(s, j))")
    r = subprocess.run([sys.executable, "-E", "-s", "-c", code], cwd=TEMPLATE, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class LastSleepTest(unittest.TestCase):
    def test_never_slept(self):
        self.assertIn("not slept yet", last_sleep([CHAT.format(h=10)]))

    def test_sleep_scrolled_out_of_the_journal_window_is_still_known(self):
        # The point of the section: after a few conversations the sleep entry is no longer in the
        # recent journal, and "how did you sleep?" had nothing to go on.
        out = last_sleep([SLEEP] + [CHAT.format(h=h) for h in range(10, 20)], window=6000)
        self.assertIn("2026-10-12 04:00", out)
        self.assertIn("I consolidated the agenda.", out)

    def test_sleep_in_view_is_only_pointed_to(self):
        out = last_sleep([SLEEP, CHAT.format(h=10)])
        self.assertIn('the entry "Sleep" in my recent journal above', out)
        self.assertNotIn("I consolidated", out)

    def test_a_failed_sleep_counts(self):
        note = ("## 2026-10-12 04:00 — Supervisor: sleep failed\n\nMy sleep run ended with an error (exit 1)"
                ", so it did not complete.")
        self.assertIn("sleep failed", last_sleep([SLEEP, note] + [CHAT.format(h=h) for h in range(10, 20)], 6000))


    def test_a_supervisor_note_that_merely_mentions_sleep_is_not_a_sleep(self):
        note = ("## 2026-10-12 05:00 — Supervisor: code change reverted\n\nMy changes to shaft/sleep.py were "
                "reverted because the tests failed.")
        stopped = ("## 2026-10-12 04:30 — Supervisor: run stopped\n\nMy sleep run was stopped after its 90-minute "
                   "budget, before I finished it.")
        out = last_sleep([SLEEP, stopped, note] + [CHAT.format(h=h) for h in range(10, 20)], 6000)
        self.assertIn("2026-10-12 04:30", out)
        self.assertNotIn("05:00", out)


class FailedRunTest(unittest.TestCase):
    def test_failed_run_is_noted_in_the_journal_with_its_reason(self):
        # Regression: a sleep that died (e.g. model server down) left no trace the instance could see.
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        cmd = [sys.executable, "-c", "import sys; sys.stderr.write('cannot reach LLM at 127.0.0.1:8780'); sys.exit(1)"]
        broker = mock.Mock(handoffs=[], counts={})
        with mock.patch.object(runner, "_launch", return_value=(broker, cmd, {})), \
             mock.patch.object(runner, "_snapshot", return_value={}), \
             mock.patch.object(runner, "_settle", return_value=True), \
             mock.patch.object(runner, "commit_state", return_value=True), \
             mock.patch.object(runner, "log"), \
             mock.patch.object(runner, "journal_note") as note, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            code = runner._run("shaft", root, "sleep", ["sleep"], interactive=False)
        self.assertEqual(code, 1)
        title, body = note.call_args.args[1:]
        self.assertEqual(title, "Supervisor: sleep failed")
        self.assertIn("cannot reach LLM", body)


if __name__ == "__main__":
    unittest.main()
