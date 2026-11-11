"""From answers like a thread (the operator, 2026-11-11: "A message the user sends to the From thread should work same
as the others, not being queued").

Before: From's composer only left messages for the instance's next cycle — nothing written there was answered now.
Now From holds a conversation of its own (web_chat.from_chat): a message written there gets a reply now, shown in
From, and the reply reads what the instance sent there from its cycles. From is still no thread: it never ends, and
no thread list, Earlier chats or End/Clear/Rename touches it; pages, apps, pushes and seen marks name it "from"."""
from __future__ import annotations

import json
import unittest
from unittest import mock

from supervisor import app_api, broker, messages, web, web_alerts, web_chat, web_views
from supervisor.tests.test_background_messages import Fixture
from supervisor.tests.test_web import ME


def multipart(fields: dict) -> tuple[bytes, str]:
    body = "".join(f'--B\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
    return (body + "--B--\r\n").encode(), "multipart/form-data; boundary=B"


class FromConversationTest(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.f.make(self)
        self.started: list[str] = []
        for p in (mock.patch("supervisor.channel.route", return_value=("web", "test")),
                  mock.patch("supervisor.feedback.by_target", return_value={}),
                  mock.patch.object(web_chat, "_start", side_effect=self.started.append),
                  mock.patch.object(web_chat, "_docs_now", return_value={}),
                  mock.patch.object(web, "_names", return_value=["wisp"])):
            p.start()
            self.addCleanup(p.stop)

    def cycle(self) -> broker.Broker:
        return broker.Broker("wisp", self.f.project, {}, self.f.project / "broker.log")

    def from_cycle(self, text: str) -> dict:
        """A message from a cycle, logged a minute back: in time order before what is written next (the log and the
        chat stamp to the second)."""
        sent = self.cycle().call("send_message", {"to": "operator", "text": text})
        self.assertTrue(sent["ok"], sent)
        log = messages.SUP_DIR / messages.LOG_NAME
        lines = log.read_text().splitlines()
        row = json.loads(lines[-1])
        row["ts"] = f"2026-11-11T08:{len(lines):02d}:00"
        log.write_text("\n".join(lines[:-1] + [json.dumps(row)]) + "\n")
        return messages.history()[-1]

    def write_in_from(self, text: str, mid: str = "m-1", **more) -> web.Response:
        body, ctype = multipart({"text": text, "mid": mid, "cid": "from", **more})
        return web._post(web.Request("POST", "/chat/wisp/send", {"content-type": ctype}, body), "device")

    # -- the request ------------------------------------------------------------------------------------------------
    def test_a_message_written_in_from_is_answered_now_in_from_knowing_what_its_cycle_sent_there(self):
        """The bug: From's composer only queued for the next cycle — the message went to the inbox, no reply ran, and
        nothing the operator wrote there was answered until a cycle came."""
        row = self.from_cycle("Weekly report: the import job failed twice.")
        self.assertEqual(self.write_in_from("Why did it fail?").status, 200)

        fc = web_chat.from_chat("wisp")
        self.assertIsNotNone(fc, "the first message made the From conversation")
        self.assertEqual(self.started, [fc["id"]], "a reply started now, in From")
        self.assertEqual(self.f.inbox(), [], "nothing was left in the inbox for a cycle")
        self.assertFalse([m for m in messages.history() if m["from"] == "operator"], "no next-cycle note was logged")
        self.assertEqual([t["role"] for t in fc["turns"]], ["user"])

        feed = web_chat.from_feed("wisp")
        self.assertEqual([(i["type"], i.get("text", "")) for i in feed["items"]],
                         [("sent", "Weekly report: the import job failed twice."), ("user", "Why did it fail?")])
        self.assertEqual(feed["chat"]["id"], fc["id"])
        self.assertFalse(feed["can_end"] or feed["can_clear"], "From never ends and is not cleared")

        api = web_chat._api_messages(fc)
        self.assertEqual(api[0]["role"], "system")
        self.assertIn("This is your main conversation with the operator", api[0]["content"])
        self.assertEqual([m["role"] for m in api[1:]], ["assistant", "user"])
        self.assertTrue(api[1]["content"].startswith(f"[sent from my cycle at {row['ts'][11:16]}, shown in From]"))
        self.assertIn("the import job failed twice", api[1]["content"])
        self.assertTrue(api[-1]["content"].startswith("Why did it fail?"))

    def test_next_cycle_from_from_still_queues_and_names_the_from_conversation(self):
        """⏾ stays a choice in From: the note goes to the inbox, names the From conversation, and its answer from the
        cycle comes back to From (after reading it), pushed as From."""
        body, ctype = multipart({"text": "Look at the logs tonight.", "mid": "m-2", "cid": "from", "background": "1"})
        self.assertEqual(web._post(web.Request("POST", "/chat/wisp/send", {"content-type": ctype}, body), "device")
                         .status, 200)
        self.assertEqual(self.started, [], "no reply now: it is for the next cycle")
        fc = web_chat.from_chat("wisp")
        [note] = self.f.inbox()
        self.assertIn(f"Conversation: {fc['id']} («main conversation»)", note.read_text())
        [row] = messages.history()
        self.assertEqual(row["thread"], fc["id"])
        self.assertEqual([i["type"] for i in web_chat.from_feed("wisp")["items"]], ["later"])

        b = self.cycle()
        self.assertTrue(b.call("read_thread", {"id": fc["id"]})["ok"])
        sent = b.call("send_message", {"to": "operator", "text": "Logs read: a full disk.", "conversation": fc["id"]})
        self.assertTrue(sent["ok"], sent)
        answer = messages.history()[-1]
        self.assertEqual(answer["thread"], fc["id"])
        self.assertEqual([i["type"] for i in web_chat.from_feed("wisp")["items"]], ["later", "sent"])
        self.assertEqual(web_alerts._cid_of_message(answer), "from")
        self.assertEqual(web_alerts.event(answer)["url"], "/i/wisp/chat")

    # -- From is no thread ------------------------------------------------------------------------------------------
    def test_from_is_in_no_thread_list_and_refuses_end_clear_and_rename(self):
        fc = web_chat.from_chat("wisp", ME, create=True)
        thread = web_chat.new_thread("wisp", ME)
        self.assertEqual([c["id"] for c in web_chat.threads("wisp")], [thread["id"]])
        self.assertEqual([c["id"] for c in web_chat.chats("wisp")], [thread["id"]])
        self.assertEqual(web_chat.target("wisp", "", writing=False)["id"], fc["id"], "no cid is Main, not a thread")
        self.assertEqual({c["id"] for c in web_chat.open_chats("wisp")}, {thread["id"], fc["id"]})
        for act in (lambda: web_chat.end(fc["id"]), lambda: web_chat.clear("wisp", "from"),
                    lambda: web_chat.clear("wisp", fc["id"]), lambda: web_chat.rename("wisp", "from", "x")):
            with self.assertRaises(web_chat.Refused):
                act()
        self.assertEqual(web_chat.from_chat("wisp", create=True)["id"], fc["id"], "one per instance")

    def test_watching_and_pushes_name_from_by_its_id_or_as_from_alike(self):
        """A device on From suppresses the From reply's push whichever name the feed used (the stream watches by the
        chat's id, the From page as "from"); the reply's push opens From."""
        fc = web_chat.from_chat("wisp", ME, create=True)
        web_chat.watching("wisp", "dev-1", cid=fc["id"])
        self.assertIn("dev-1", web_chat.watchers("wisp", "from"))
        fc["turns"] = [{"role": "user", "ts": "2026-11-11T09:00:00", "content": "hi"},
                       {"role": "assistant", "ts": "2026-11-11T09:00:05", "content": "Hello."}]
        event = web_chat.reply_event(fc)
        self.assertEqual((event["cid"], event["url"]), ("from", "/i/wisp/chat"))
        self.addCleanup(web_chat._watched.clear)

    def test_a_from_replys_own_messages_show_in_from_but_are_not_routed_in_again(self):
        fc = web_chat.from_chat("wisp", ME, create=True)
        fc["talks"] = ["api-0from0talk"]
        web_chat.save(fc)
        mid_reply = self.cycle()
        mid_reply.talk_id = "api-0from0talk"                  # a reply of the From conversation, as runner sets it
        self.assertTrue(mid_reply.call("send_message", {"to": "operator", "text": "Attached the log."})["ok"])
        row = messages.history()[-1]
        self.assertIn(row["file"], [m["file"] for m in web_chat.from_rows("wisp")])
        self.assertNotIn(row["file"], [m["file"] for m in web_chat.routed("wisp", fc["id"])])

    def test_a_long_from_reads_only_its_newest_part_never_cut_after_the_operators_message(self):
        """From never ends and holds every cycle's message: a reply's request is bounded (FROM_CHARS), each message
        routed in is cut (FROM_ITEM_CHARS), and the operator's message it answers is always in it."""
        for k in range(40):
            self.from_cycle(f"Report {k}: " + "x" * 5000)
        self.write_in_from("Summarise the last reports.")
        api = web_chat._api_messages(web_chat.from_chat("wisp"))
        body = api[1:]
        self.assertTrue(body[-1]["content"].startswith("Summarise the last reports."))
        self.assertLessEqual(sum(len(m["content"]) for m in body), web_chat.FROM_CHARS)
        self.assertTrue(all(len(m["content"]) < web_chat.FROM_ITEM_CHARS + 100 for m in body))
        self.assertIn("Report 39", body[-2]["content"])
        self.assertNotIn("Report 0:", "".join(m["content"] for m in body))

    # -- the page and the apps --------------------------------------------------------------------------------------
    def test_main_tab_title_uses_only_the_instance_name(self):
        """The Main tab still read '· from ·' after the rename."""
        page = str(web_views.from_page("wisp", web_chat.from_feed("wisp"), [], {}, ["wisp"], "tok"))
        title = page.split("<title>", 1)[1].split("</title>", 1)[0]
        self.assertEqual(title, "wisp · SHAFT")
        self.assertNotIn("from", title.lower())

    def test_the_from_page_composer_sends_now_with_next_cycle_behind_the_arrow(self):
        html = str(web_views.from_page("wisp", web_chat.from_feed("wisp"), [], {}, ["wisp"], "tok"))
        self.assertIn('<input type="hidden" name="cid" value="from">', html)
        self.assertIn('<input type="hidden" name="background" value="">', html)
        self.assertNotIn('data-always="1"', html, "the old From composer forced every message to the next cycle")
        self.assertIn('title="Send now"', html)
        self.assertIn("Next cycle<span", html)

    def test_the_from_feed_draws_its_turns_marked_seen_as_from(self):
        self.from_cycle("Weekly report.")
        self.write_in_from("Thanks.")
        fc = web_chat.from_chat("wisp")
        fc["turns"].append({"role": "assistant", "ts": "2099-01-01T00:00:00", "content": "You are welcome."})
        web_chat.save(fc)
        html = str(web_views.feed_html(web_chat.from_feed("wisp"), from_feed=True))
        self.assertIn("You are welcome.", html)
        self.assertIn("Thanks.", html)
        self.assertNotIn(f'data-seen-key="{fc["id"]}"', html)
        self.assertIn('data-seen-key="from"', html)

    def test_a_reply_in_from_pulses_froms_dot_in_the_rail(self):
        """Without it, a reply running in From showed nowhere in the rail: the dot stayed From's plain mark."""
        fc = web_chat.from_chat("wisp", ME, create=True)
        from supervisor import web_pages
        idle = str(web_pages.rail(["wisp"], "i:wisp", chats=web._rail_chats(["wisp"]), thread="from"))
        self.assertIn('<span class="dot st-idle" title="idle" aria-hidden="true"></span>', idle)
        fc["state"] = "replying"
        web_chat.save(fc)
        rail = web._rail_chats(["wisp"])["wisp"]
        self.assertEqual(rail["from_state"], "replying")
        self.assertIn('<span class="tdot t-replying" title="replying" aria-hidden="true"></span>',
                      str(web_pages.rail(["wisp"], "i:wisp", chats={"wisp": rail}, thread="from")))
        self.assertTrue(web_chat.summary("wisp", [])["replying"])

    def test_stop_from_the_app_names_from_and_stops_the_from_reply(self):
        """Found by the apps lane: Stop with cid "from" looked for a relay under "from", but relays are kept under the
        chat's id — "no reply is running" while one ran in From."""
        fc = web_chat.from_chat("wisp", ME, create=True)
        relay = mock.Mock()
        with mock.patch.dict(web_chat._relays, {fc["id"]: relay}):
            req = mock.Mock(body=json.dumps({"cid": "from"}).encode(), headers={})
            with mock.patch.object(app_api, "_body", return_value={"cid": "from"}), \
                    mock.patch.object(app_api, "_login", return_value=ME):
                app_api._action(req, "wisp", "stop")
        relay.stop.assert_called_once_with()

    def test_a_document_card_in_main_opens_in_its_own_panel(self):
        """Review finding: From had no documents panel, so a card's "?doc=" link reloaded From and nothing opened. Main
        (the instance's own page) has the panel: the card opens the document beside it."""
        fc = web_chat.from_chat("wisp", ME, create=True)
        fc["turns"] = [{"role": "user", "ts": "2099-01-01T00:00:00", "content": "write it up"},
                       {"role": "assistant", "ts": "2099-01-01T00:00:05", "content": "Done.",
                        "docs": [{"path": "notes/docs/plan.md", "created": True}]}]
        web_chat.save(fc)
        html = str(web_views.feed_html(web_chat.from_feed("wisp"), from_feed=True))
        self.assertIn('href="?doc=notes%2Fdocs%2Fplan.md" data-open-doc="notes/docs/plan.md"', html)
        page = str(web_views.from_page("wisp", web_chat.from_feed("wisp"), [], {}, ["wisp"], "t"))
        self.assertIn('<aside class="docs" id="docs"', page)

    def test_from_shows_its_earlier_turns_on_request_not_in_messages_which_has_none(self):
        """Review finding: past CHAT_WINDOW items From's "earlier" link went to Messages, where its own turns are not."""
        fc = web_chat.from_chat("wisp", ME, create=True)
        fc["turns"] = [{"role": "user" if k % 2 == 0 else "assistant", "ts": f"2099-01-01T00:{k // 60:02d}:{k % 60:02d}",
                        "content": f"turn {k}"} for k in range(web_chat.CHAT_WINDOW + 10)]
        web_chat.save(fc)
        windowed = web_chat.from_feed("wisp")
        self.assertEqual(windowed["earlier"], 10)
        self.assertIn('href="?all=1"', str(web_views.feed_html(windowed, from_feed=True)))
        whole = web_chat.from_feed("wisp", whole=True)
        self.assertEqual(len(whole["items"]), web_chat.CHAT_WINDOW + 10)
        self.assertIn("/fragment/chat/wisp?all=1", str(web_views.from_page("wisp", whole, [], {}, ["wisp"], "t",
                                                                           whole=True)))

    def test_a_bakeoff_reads_the_operators_request_made_in_from(self):
        """Review finding: the operator-request context of a bake-off case looked only at threads."""
        from supervisor import bakeoff
        fc = web_chat.from_chat("wisp", ME, create=True)
        fc["turns"] = [{"role": "user", "ts": "2026-11-11T09:00:00", "content": "Clean the import job"}]
        web_chat.save(fc)
        asked = bakeoff._asked("wisp", "2026-11-11T10:00:00", lambda t: t)
        self.assertEqual([a["text"] for a in asked], ["Clean the import job"])

    def test_the_app_api_finds_from_by_name_and_answers_with_from(self):
        with self.assertRaises(app_api.Refusal):
            app_api._chat_of("wisp", "from")                  # none until its first message
        fc = web_chat.from_chat("wisp", ME, create=True)
        self.assertEqual(app_api._chat_of("wisp", "from")["id"], fc["id"])
        self.assertEqual(web_chat.public_id("wisp", fc["id"]), "from")
        self.assertEqual(web_chat.thread_of("wisp", "from")["id"], fc["id"])


if __name__ == "__main__":
    unittest.main()
