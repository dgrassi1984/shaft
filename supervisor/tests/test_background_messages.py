"""Next cycle (docs/plans/PLAN-background-messages.md): a chat message or a document comment the operator leaves for
the instance's next cycle goes to its inbox now — never a chat turn, a reply or an API call that would hold the
instance — shows in the chat as theirs (waiting, then read), and a comment's answer comes back in a send_message."""
from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from supervisor import autonomy, broker, core, doc_comments as comments, docs, messages, web, web_chat, web_views
from supervisor.tests.test_web import ME, WebTest

TEMPLATE = Path(__file__).resolve().parents[2] / "template"


class Fixture:
    """A project with one instance, wren, its supervisor folder and message log; the API must never be called."""

    def make(self, case: unittest.TestCase) -> None:
        self.project = Path(tempfile.mkdtemp(prefix="shaft-later-")).resolve()
        case.addCleanup(shutil.rmtree, self.project, True)
        self.sup = self.project / ".supervisor"
        self.root = self.project / "instances" / "wren"
        (self.root / "shaft").mkdir(parents=True)
        (self.root / ".git").write_text("gitdir: x")
        (self.root / "notes/docs").mkdir(parents=True)
        (self.root / "notes/docs/plan.md").write_text("a plan to review\n")
        self.api = mock.Mock(side_effect=AssertionError("a message for the next cycle called the API"))
        for p in (mock.patch.object(messages, "SUP_DIR", self.sup),
                  mock.patch.object(messages, "PROJECT", self.project),
                  mock.patch.object(messages, "INSTANCES", self.project / "instances"),
                  mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                  mock.patch.object(messages, "log", lambda *_: None),
                  mock.patch.object(core, "INSTANCES", self.project / "instances"),
                  mock.patch.object(core, "SUP_DIR", self.sup),
                  mock.patch.object(docs, "instance_root", return_value=self.root),
                  mock.patch.object(web_chat, "SUP_DIR", self.sup),
                  mock.patch.object(web_chat, "log", lambda *_: None),
                  mock.patch.object(web_chat, "_post", self.api),
                  mock.patch.object(web_chat, "_start", self.api),
                  mock.patch.object(web_chat, "_sync_copies"),
                  mock.patch.object(autonomy, "next_cycle", lambda n: f"for {n}'s next cycle (≈14:20)"),
                  mock.patch("supervisor.broker.notify", lambda *_: None, create=True)):
            p.start()
            case.addCleanup(p.stop)
        messages._cache.update(key=None, rows=[])

    def inbox(self) -> list[Path]:
        return sorted((self.root / "inbox").glob("*.md"))

    def cycle_reads_inbox(self) -> None:
        """What template/shaft/live.py does after a cycle: the messages it read move to memory/messages/."""
        (self.root / "memory/messages").mkdir(parents=True, exist_ok=True)
        for p in self.inbox():
            p.rename(self.root / "memory/messages" / p.name)


class NextCycleMessageTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.make(self)

    def test_goes_to_the_inbox_and_the_log_never_the_chat_or_the_api(self):
        file, said = web_chat.background("wren", ME, "look into\r\nthe flaky test when you can", [], mid="m1")
        self.assertEqual(said, "for wren's next cycle (≈14:20)")
        [path] = self.f.inbox()
        self.assertEqual(path.name, file)
        text = path.read_text()
        self.assertIn("From: operator\nTo: wren\nKind: message\n", text)
        self.assertIn(f"Via: {messages.BACKGROUND_NOTE}\n", text)
        self.assertTrue(text.endswith("\n\nlook into\nthe flaky test when you can\n"))   # a form's \r\n: not in its file
        [row] = messages.history()
        self.assertEqual((row["from"], row["to"], row["via"], row["mid"], row["read"]),
                         ("operator", "wren", messages.BACKGROUND, "m1", False))
        self.assertEqual(web_chat.chats("wren"), [])         # no chat opened: nothing holds the instance
        self.f.api.assert_not_called()

    def test_a_retry_with_the_same_mid_is_delivered_once_in_any_mode(self):
        web_chat.background("wren", ME, "once", [], mid="m1")
        _, said = web_chat.background("wren", ME, "once", [], mid="m1")
        self.assertEqual(said, "already sent to wren")
        _, said = web_chat.send("wren", ME, "once", [], mid="m1")    # the page retried with Send now instead
        self.assertEqual(said, "already sent to wren for its next cycle")
        self.assertEqual(len(self.f.inbox()), 1)
        self.assertEqual(len(messages.history()), 1)
        self.f.api.assert_not_called()

    def test_empty_and_too_long_are_refused(self):
        with self.assertRaises(web_chat.Refused):
            web_chat.background("wren", ME, "   ", [])
        with self.assertRaises(web_chat.Refused):
            web_chat.background("wren", ME, "x" * (web_chat.MAX_TEXT + 1), [])
        self.assertEqual(self.f.inbox(), [])

    def test_files_are_saved_in_its_folder_and_listed_where_a_cycle_finds_them(self):
        web_chat.background("wren", ME, "see the screenshot", [("shot.png", b"\x89PNG fake")])
        text = self.f.inbox()[0].read_text()
        self.assertIn("[The operator attached, saved in my folder:]\n- attachments/from-operator/", text)
        [row] = messages.history()
        self.assertEqual(row["shown"], "see the screenshot")   # the card shows what they wrote, the chips the files
        self.assertEqual(row["attachments"][0]["name"], "shot.png")
        self.assertTrue((self.f.project / row["attachments"][0]["path"]).is_file())

    def test_a_quoted_question_is_answered_and_the_message_is_its_reply(self):
        b = broker.Broker("wren", self.f.project, {}, self.f.project / "broker.log")
        with mock.patch("supervisor.channel.route", return_value=("web", "test")):
            self.assertTrue(b.call("send_message", {"to": "operator", "kind": "question",
                                                    "text": "Should I rewrite the parser?"})["ok"])
        [question] = messages.pending()
        _, said = web_chat.background("wren", ME, "> Should I rewrite the parser?\n\nYes, next cycle.", [])
        self.assertTrue(said.endswith(" · answers its question"))
        self.assertEqual(messages.pending(), [])             # archived from the operator inbox, as a Reply does
        text = self.f.inbox()[0].read_text()
        self.assertIn(f"Kind: reply\nIn-Reply-To: {question['path'].name}\n", text)

    def test_the_chat_shows_it_as_yours_waiting_then_read(self):
        chat = web_chat.new_chat("wren", ME)
        web_chat.background("wren", ME, "> quoted\n\nfor later", [("notes.txt", b"hello")])
        sent = web._sent_during("wren", chat)
        self.assertEqual([m["via"] for m in sent], [messages.BACKGROUND])
        feed = str(web_views.chat_feed("wren", chat, sent))
        self.assertIn('class="turn me later"', feed)
        self.assertIn("next cycle · waiting", feed)
        self.assertIn("<blockquote", feed)                     # its own text, quotes as quotes
        self.assertIn("📎 notes.txt", feed)
        self.assertNotIn("wren sent you", feed)                # not mistaken for the instance's message
        self.f.cycle_reads_inbox()
        feed = str(web_views.chat_feed("wren", chat, web._sent_during("wren", chat)))
        self.assertIn("next cycle · read", feed)

    def test_the_composer_offers_next_cycle_and_its_sent_mids_include_it(self):
        web_chat.background("wren", ME, "later", [], mid="m-later")
        sent = web._sent_during("wren", None)
        page = str(web_views.chat_page("wren", None, sent, [], {}, ["wren"], "csrf", later="for wren's next cycle (≈14:20)"))
        self.assertIn('data-background="1"', page)
        self.assertIn('name="background"', page)
        self.assertIn("Not answered now: to wren&#x27;s inbox, for wren&#x27;s next cycle (≈14:20)", page)
        self.assertIn("m-later", page)                         # a reload after a lost answer does not offer it again


class NextCycleRouteTest(WebTest):
    def setUp(self):
        super().setUp()
        for p in (mock.patch.object(web, "_names", return_value=["wren"]),):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()

    def test_background_field_reaches_background_not_send_or_queue(self):
        body = (b'--B\r\nContent-Disposition: form-data; name="text"\r\n\r\nlater\r\n'
                b'--B\r\nContent-Disposition: form-data; name="background"\r\n\r\n1\r\n'
                b'--B\r\nContent-Disposition: form-data; name="queue"\r\n\r\n\r\n'
                b'--B\r\nContent-Disposition: form-data; name="mid"\r\n\r\nm9\r\n--B--\r\n')
        headers = {"content-type": "multipart/form-data; boundary=B", "sec-fetch-site": "same-origin",
                   "x-csrf-token": self.csrf(self.cookie)}
        with mock.patch.object(web_chat, "background", return_value=("f.md", "for wren's next cycle")) as later, \
                mock.patch.object(web_chat, "send") as send, mock.patch.object(web_chat, "queue") as queue:
            r = self.req("/chat/wren/send", "POST", cookie=self.cookie, body=body, headers=headers)
        self.assertEqual(r.status, 200, r.body)
        self.assertIn(b"for wren&#x27;s next cycle", r.body)
        later.assert_called_once_with("wren", ME, "later", [], mid="m9")
        send.assert_not_called()
        queue.assert_not_called()


class NextCycleCommentTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.make(self)
        self.tid = comments.add_thread("wren", "p-0000000000000001", "plan.md", None, "sha", "Tighten section 2")

    def operator_message(self, text: str, cls=broker.Broker, **kw) -> dict:
        b = cls("wren", self.f.project, {}, self.f.project / "broker.log", **kw)
        with mock.patch("supervisor.channel.route", return_value=("web", "test")):
            return b.call("send_message", {"to": "operator", "text": text})

    def test_sent_for_its_next_cycle_goes_to_the_inbox_and_freezes_the_post(self):
        said = web_chat.send_comment_background("wren", ME, "p-0000000000000001")
        self.assertEqual(said, "for wren's next cycle (≈14:20)")
        [path] = self.f.inbox()
        text = path.read_text()
        self.assertIn(comments.block("wren", self.tid, "p-0000000000000001", cycle=True), text)
        self.assertIn('send_message to the operator, under a line "### c1"', text)
        post = comments.get_post("wren", "p-0000000000000001")
        self.assertEqual(post["sent"], f"inbox/{path.name}")
        with self.assertRaises(comments.Refused):              # sent: no edit, as a chat-sent comment
            comments.edit("wren", "p-0000000000000001", "changed")
        self.assertEqual(web_chat.send_comment_background("wren", ME, "p-0000000000000001"), "already sent to wren")
        self.assertEqual(web_chat.send_comment("wren", ME, "p-0000000000000001")[1], "already sent to wren")
        self.assertEqual(len(self.f.inbox()), 1)
        [row] = messages.history()
        self.assertEqual(row["comments"], [{"id": self.tid, "doc": "plan.md"}])
        self.assertEqual(row["shown"], "")
        self.f.api.assert_not_called()

    def test_its_answer_in_a_message_lands_in_the_thread(self):
        web_chat.send_comment_background("wren", ME, "p-0000000000000001")
        self.assertTrue(self.operator_message("Done this cycle.\n\n### c1\nTightened section 2: two sentences cut.")["ok"])
        thread = comments.threads("wren")[0]
        answer = thread["posts"][-1]
        self.assertEqual((answer["by"], answer["text"]), ("wren", "Tightened section 2: two sentences cut."))
        file = messages.history()[-1]["file"]
        self.assertEqual(answer["turn"], f"message:{file}")
        self.assertIn("Tightened section 2", (self.f.root / "notes/comments/plan.md").read_text())

    def test_a_message_without_its_section_attaches_nothing(self):
        web_chat.send_comment_background("wren", ME, "p-0000000000000001")
        self.operator_message("I looked at plan.md and will come back to it.")
        self.assertEqual(len(comments.threads("wren")[0]["posts"]), 1)

    def test_a_trial_replay_attaches_nothing(self):
        web_chat.send_comment_background("wren", ME, "p-0000000000000001")
        state = Path(tempfile.mkdtemp(dir=self.f.project, prefix="copy-"))
        r = self.operator_message("### c1\nreplayed answer", broker.ReplayBroker, state_root=state, start_commit="0",
                                  as_of=datetime.now(), cache={}, cache_lock=threading.Lock(), billed="wren@trial:t",
                                  tools_log=self.f.sup / "t.log")
        self.assertTrue(r["ok"])
        self.assertEqual(len(comments.threads("wren")[0]["posts"]), 1)


class NextCycleCommentRouteTest(WebTest):
    def setUp(self):
        super().setUp()
        self.root = self.sup / "mind" / "wren"
        (self.root / "notes/docs").mkdir(parents=True)
        (self.root / "notes/docs/plan.md").write_text("a plan")
        for p in (mock.patch.object(core, "SUP_DIR", self.sup),
                  mock.patch.object(core, "instance_root", return_value=self.root),
                  mock.patch.object(docs, "instance_root", return_value=self.root),
                  mock.patch.object(web, "_names", return_value=["wren"]),
                  mock.patch.object(web, "log")):
            p.start()
            self.addCleanup(p.stop)
        self.cookie = self.paired()

    def post(self, action="", **form):
        import urllib.parse
        headers = {"sec-fetch-site": "same-origin", "content-type": "application/x-www-form-urlencoded",
                   "x-csrf-token": self.csrf(self.cookie)}
        return self.req("/docs/wren/comment" + (f"/{action}" if action else ""), "POST", cookie=self.cookie,
                        body=urllib.parse.urlencode(form).encode(), headers=headers)

    def test_send_cycle_and_the_next_cycle_button_reach_the_background_send(self):
        with mock.patch.object(web_chat, "send_comment_background", return_value="for wren's next cycle") as later, \
                mock.patch.object(web_chat, "send_comment") as now:
            r = self.post(pid="p-0000000000000001", doc="plan.md", anchor="", sha="s", text="Later", send="cycle")
            self.assertEqual(r.status, 200, r.body)
            later.assert_called_once_with("wren", ME, "p-0000000000000001")
            self.post(pid="p-0000000000000002", doc="plan.md", anchor="", sha="s", text="Kept")
            r = self.post("send", pid="p-0000000000000002", when="cycle")
            self.assertEqual(r.status, 200, r.body)
            self.assertEqual(later.call_args_list[-1], mock.call("wren", ME, "p-0000000000000002"))
            now.assert_not_called()

    def test_a_kept_post_offers_next_cycle_and_a_sent_one_says_where_it_went(self):
        comments.add_thread("wren", "p-0000000000000001", "plan.md", None, "s", "Kept")
        comments.add_thread("wren", "p-0000000000000002", "plan.md", None, "s", "Later")
        comments.mark_sent("wren", "p-0000000000000002", "inbox/20261022-140000-000001-from-operator.md")
        html = str(web_views.threads_list("wren", "plan.md", "s", comments.threads("wren", "plan.md")))
        kept, later = html.split('data-pid="p-0000000000000002"')
        self.assertIn('name="when" value="cycle"', kept)
        self.assertIn('href="/messages/20261022-140000-000001-from-operator.md">⏾ for its next cycle', later)
        self.assertNotIn('value="cycle"', later)


class NextWakeTest(unittest.TestCase):
    def setUp(self):
        self.st = {}
        for p in (mock.patch.object(autonomy, "_state", lambda: self.st),
                  mock.patch.object(autonomy, "paused", return_value=False),
                  mock.patch("supervisor.runner.busy_with", return_value=None)):
            p.start()
            self.addCleanup(p.stop)

    def test_next_wake_is_the_last_wake_plus_the_interval(self):
        now = datetime(2026, 10, 22, 13, 0)
        hour = timedelta(minutes=60)
        self.assertEqual(autonomy.next_wake("wren", now, None), "never")
        self.assertEqual(autonomy.next_wake("wren", now, hour), "due")
        self.st["wakes"] = {"wren": "2026-10-22T12:40:00"}
        self.assertEqual(autonomy.next_wake("wren", now, hour), "13:40")
        self.assertEqual(autonomy.next_wake("wren", now + hour, hour), "due")

    def test_next_cycle_says_where_a_message_left_now_stands(self):
        self.st["wakes"] = {"wren": (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")}
        with mock.patch.object(autonomy, "wake_interval", return_value=timedelta(minutes=60)):
            self.assertRegex(autonomy.next_cycle("wren"), r"^for wren's next cycle \(≈\d\d:\d\d\)$")
            with mock.patch("supervisor.runner.busy_with", return_value="live since 12:58"):
                self.assertIn("a cycle is running and read its inbox when it began", autonomy.next_cycle("wren"))
            with mock.patch.object(autonomy, "paused", return_value=True):
                self.assertIn("wren is paused", autonomy.next_cycle("wren"))
        with mock.patch.object(autonomy, "wake_interval", return_value=None):
            self.assertIn("no scheduled cycles", autonomy.next_cycle("wren"))


@unittest.skipUnless((TEMPLATE / "shaft" / "live.py").is_file(), "the template worktree is not checked out")
class CycleReadsItTest(unittest.TestCase):
    def test_the_cycle_reads_a_next_cycle_message_with_its_header(self):
        """Pins the inbox format a cycle reads: a change to read_inbox that dropped it would leave it unread."""
        f = Fixture()
        f.make(self)
        web_chat.background("wren", ME, "rework section 2", [])
        sys.path.insert(0, str(TEMPLATE))
        self.addCleanup(sys.path.remove, str(TEMPLATE))
        from shaft import live
        state = mock.Mock(root=f.root)
        text = live.inbox_text(live.read_inbox(state))
        self.assertIn("You have 1 new message(s)", text)
        self.assertIn("Via: the web UI, for your next cycle", text)
        self.assertIn("rework section 2", text)


if __name__ == "__main__":
    unittest.main()
