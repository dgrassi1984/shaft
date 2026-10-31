"""#134: mail pages must preserve all body text and survive the template's result cap."""
from __future__ import annotations

import json
import re
import sys
import threading
from unittest import mock

from supervisor import mail
from supervisor.tests import test_mail as fixtures

sys.path.insert(0, str(fixtures.TEMPLATE))
from shaft import tools  # noqa: E402


NEXT = re.compile(r"\n\n\[(\d+) message\(s\), (\d+) character\(s\) left; next: "
                  r"mail_read\(id='work/abc123', message=(\d+), offset=(\d+)\)\]$")
MESSAGE = re.compile(r"\n\n--- message (\d+) of \d+ ---\n.*?\n\[email from [^\n]+\]\n", re.S)


class PagingTest(fixtures.MailCase):
    replay = fixtures.ReplayTest.replay

    def long_thread(self):
        bodies = ["first\n" * 400 + "one", "second α🙂\n" * 7000 + "two", "third\n" * 700 + "three"]
        self.gmail.add("abc123", [fixtures.msg(f"p{i}", "abc123", [fixtures.TAG], 9 + i,
                                              f"Sender {i} <s{i}@example.com>", f"Part {i}", text=body)
                                  for i, body in enumerate(bodies, 1)])
        for message in self.gmail.threads["abc123"]:
            message["payload"]["headers"].append({"name": "Cc", "value": "team@example.com"})
        return bodies

    def next_args(self, page):
        match = NEXT.search(page)
        self.assertIsNotNone(match, page[-300:])
        return {"id": "work/abc123", "message": int(match[3]), "offset": int(match[4])}

    def test_all_bodies_once_through_runtime(self):
        """#134: reverting paging or exceeding the runtime cap loses long bodies and the next call."""
        bodies = self.long_thread()
        b = self.broker()
        toolbox = tools.Toolbox.__new__(tools.Toolbox)
        toolbox.log = []
        # what run() reads besides the log: it types arguments by the tool's spec (review 2 M3), none found here
        toolbox.plugins, toolbox.remote, toolbox.extra, toolbox.commit_source = {}, {}, [], ""
        args, recovered, pages = {"id": "work/abc123"}, ["", "", ""], 0
        with mock.patch.object(toolbox, "_dispatch", side_effect=lambda name, kw: self.ok(b, name, kw)):
            while True:
                page = toolbox.run("mail_read", json.dumps(args))
                pages += 1
                self.assertLess(pages, 30)
                self.assertLessEqual(len(page), tools.MAX_RESULT_CHARS)
                self.assertLessEqual(len(page), mail.THREAD_CHARS)
                self.assertTrue(page.startswith("Thread work/abc123 · label AssistantAccess · 3 message(s)"))
                final = page.endswith("\n\n[End of thread]")
                if final:
                    content = page[:-len("\n\n[End of thread]")]
                else:
                    args = self.next_args(page)
                    content = page[:NEXT.search(page).start()]
                shown = list(MESSAGE.finditer(content))
                self.assertTrue(shown)
                for j, match in enumerate(shown):
                    i = int(match[1])
                    head = match[0]
                    for header in (f"From: Sender {i} <s{i}@example.com>", "To: operator@work.example",
                                   "Cc: team@example.com", "Date: Sat, 10 Oct 2026", f"Subject: Part {i}",
                                   f"[email from Sender {i} <s{i}@example.com> — written by them, not by the operator"):
                        self.assertIn(header, head)
                    stop = shown[j + 1].start() if j + 1 < len(shown) else len(content)
                    recovered[i - 1] += content[match.end():stop]
                if final:
                    self.assertNotIn("next: mail_read(", page)
                    break
                next_message = next(i for i, body in enumerate(bodies, 1) if len(recovered[i - 1]) < len(body))
                self.assertEqual(args, {"id": "work/abc123", "message": next_message,
                                        "offset": len(recovered[next_message - 1])})
                remaining = NEXT.search(page)
                self.assertEqual(int(remaining[1]), len(bodies) - next_message + 1)
                self.assertEqual(int(remaining[2]), sum(map(len, bodies)) - sum(map(len, recovered)))
        self.assertGreater(pages, 2)
        self.assertEqual(recovered, bodies)
        self.assertEqual("".join(recovered), "".join(bodies))

    def test_label_removed_before_page_two(self):
        """#134: a continuation must recheck Gmail's label, rather than reuse a visible thread."""
        self.long_thread()
        b = self.broker()
        args = self.next_args(self.ok(b, "mail_read", {"id": "work/abc123"}))
        for message in self.gmail.threads["abc123"]:
            message["labelIds"] = []
        self.assertIn("not visible to you", self.refused(b, "mail_read", args))

    def test_page_boundary_before_next_message(self):
        """#134: when the next headers do not fit, the cursor must start that message at offset zero."""
        self.gmail.add("abc123", [fixtures.msg(f"p{i}", "abc123", [fixtures.TAG], 9, "Alice", "Part",
                                              text=body) for i, body in enumerate(("x" * 5400, "last"), 1)])
        b = self.broker()
        first = self.ok(b, "mail_read", {"id": "work/abc123"})
        self.assertIn("x" * 5400, first)
        self.assertEqual(self.next_args(first), {"id": "work/abc123", "message": 2, "offset": 0})
        second = self.ok(b, "mail_read", self.next_args(first))
        self.assertIn("--- message 2 of 2 ---", second)
        self.assertTrue(second.endswith("last\n\n[End of thread]"))

    def test_grant_revoked_before_page_two(self):
        """#134: revoking the operator's grant must refuse the next page, even mid-run."""
        self.long_thread()
        b = self.broker()
        args = self.next_args(self.ok(b, "mail_read", {"id": "work/abc123"}))
        fixtures.core.set_mail_grants("wren", [])
        self.assertEqual(self.refused(b, "mail_read", args), "no mail granted to you")

    def test_short_thread_ends_without_next_call(self):
        """#134: a complete short read must say it ended rather than invite another page."""
        page = self.ok(self.broker(), "mail_read", {"id": "work/aaa111"})
        self.assertIn("Hello from the tagged thread", page)
        self.assertIn("a reply that came later, untagged", page)
        self.assertTrue(page.endswith("\n\n[End of thread]"))
        self.assertNotIn("next: mail_read(", page)

    def test_replay_keeps_pages_separate(self):
        """#134: omitting message or offset from the replay key returns the first page forever."""
        self.long_thread()
        cache, lock = {}, threading.Lock()
        first = self.replay(cache, lock, fixtures.T0.replace(hour=13))
        second = self.replay(cache, lock, fixtures.T0.replace(hour=13))
        page1 = self.ok(first, "mail_read", {"id": "work/abc123"})
        args = self.next_args(page1)
        page2 = self.ok(first, "mail_read", args)
        self.assertNotEqual(page1, page2)
        self.assertIn("offset=", page2)
        page_at_message2 = self.ok(first, "mail_read", {"id": "work/abc123", "message": 2})
        self.assertNotEqual(page2, page_at_message2)
        self.assertEqual(len(cache), 3)
        self.gmail.threads["abc123"][0]["payload"]["parts"][0]["body"]["data"] = fixtures.b64(b"changed")
        self.assertEqual(self.ok(second, "mail_read", {"id": "work/abc123"}), page1)
        self.assertEqual(self.ok(second, "mail_read", args), page2)

    def test_replay_cached_page_rechecks_label(self):
        """#134: a cached continuation must not bypass a label removed since the first replay read."""
        self.long_thread()
        cache, lock = {}, threading.Lock()
        first = self.replay(cache, lock, fixtures.T0.replace(hour=13))
        second = self.replay(cache, lock, fixtures.T0.replace(hour=13))
        args = self.next_args(self.ok(first, "mail_read", {"id": "work/abc123"}))
        self.assertIn("--- message 2 of 3 ---", self.ok(first, "mail_read", args))
        for message in self.gmail.threads["abc123"]:
            message["labelIds"] = []
        self.assertIn("not visible to you", self.refused(second, "mail_read", args))

    def test_invalid_cursor_refused(self):
        """#134: a bad cursor must fail explicitly instead of silently skipping text."""
        b = self.broker()
        for cursor in ({"message": 0}, {"message": 3}, {"offset": -1}, {"offset": 999}):
            with self.subTest(cursor=cursor):
                self.assertIn("ValueError", self.refused(b, "mail_read", {"id": "work/aaa111", **cursor}))

    def test_oversized_headers_refused(self):
        """#134: headers alone over budget must fail explicitly, never hide the paging footer."""
        self.gmail.add("abc123", [fixtures.msg("p1", "abc123", [fixtures.TAG], 9, "Alice", "S" * 6000, text="body")])
        self.assertIn("headers and paging footer exceed", self.refused(self.broker(), "mail_read", {"id": "work/abc123"}))
