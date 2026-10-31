"""Replies target the quoted question, not an older unanswered one."""
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from supervisor import core, messages


class ReplyTargetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="shaft-reply-")
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.sup = self.project / "supervisor"
        self.roots = self.project / "recipients"
        (self.roots / "a" / "shaft").mkdir(parents=True)
        (self.roots / "a" / ".git").write_text("gitdir: test")
        self.inbox = self.sup / "operator-inbox"
        self.inbox.mkdir(parents=True)
        for module in (core, messages):
            for key, value in (("SUP_DIR", self.sup), ("INSTANCES", self.roots)):
                patch = mock.patch.object(module, key, value)
                patch.start()
                self.addCleanup(patch.stop)
        patch = mock.patch.object(messages, "OPERATOR_INBOX", self.inbox)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(messages, "log")
        self.log = patch.start()
        self.addCleanup(patch.stop)
        self.older = self.inbox / "20261017-120000-000000-from-a.md"
        self.newer = self.inbox / "20261031-125004-085758-from-a.md"
        self.older.write_text("From: a\nKind: question\nDate: 2026-10-17 12:00\n\nShould I keep the old plan?\n")
        self.newer.write_text("From: a\nKind: question\nDate: 2026-10-31 12:50\n\nShould I start the new plan?\n")
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.enterContext(redirect_stdout(self.out))
        self.enterContext(redirect_stderr(self.err))

    def assert_answered(self, answered, waiting, text):
        self.assertEqual([m["path"] for m in messages.pending()], [waiting])
        self.assertTrue(waiting.is_file())
        self.assertFalse(answered.exists())
        self.assertTrue((self.inbox / "read" / answered.name).is_file())
        delivered, = (self.roots / "a" / "inbox").glob("*.md")
        head, _, body = delivered.read_text().partition("\n\n")
        self.assertIn(f"In-Reply-To: {answered.name}", head.splitlines())
        self.assertIn("Kind: reply", head.splitlines())
        self.assertEqual(body, text + "\n")
        self.assertIn(answered.name, self.out.getvalue())
        self.log.assert_called_once_with(f"operator -> a: reply to {answered.name}")

    def test_quoted_newer_question_leaves_older_pending(self):
        text = "> Should I start the new plan?\n\nYes, go ahead."
        self.assertEqual(messages.reply("a", text), 0)
        self.assert_answered(self.newer, self.older, text)

    def test_explicit_newer_question_leaves_older_pending(self):
        self.assertEqual(messages.reply("a", "Yes, go ahead.", question=self.newer.name), 0)
        self.assert_answered(self.newer, self.older, "> Should I start the new plan?\n\nYes, go ahead.")

    def test_non_open_question_delivers_nothing(self):
        self.assertEqual(messages.reply("a", "Yes.", question="closed.md"), 1)
        self.assertEqual([m["path"] for m in messages.pending()], [self.older, self.newer])
        self.assertEqual(list((self.roots / "a" / "inbox").glob("*.md")), [])
        self.assertFalse((self.sup / messages.LOG_NAME).exists())
        for name in ("closed.md", self.older.name, self.newer.name):
            self.assertIn(name, self.err.getvalue())
        self.log.assert_not_called()

    def test_unquoted_reply_still_answers_oldest(self):
        self.assertEqual(messages.reply("a", "Yes."), 0)
        self.assert_answered(self.older, self.newer, "> Should I keep the old plan?\n\nYes.")
