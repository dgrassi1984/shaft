"""Where an instance's message to the operator alerts them — the web UI or Telegram —
chosen once when it is delivered, recorded in the message log, and sent by the one service it names."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import api, channel, core, messages, pushes, web_alerts, web_chat, webpush
from supervisor.tests import test_api
from supervisor.tests.test_api import KEY, ME, fake_run_agent


class Sandbox(unittest.TestCase):
    """A supervisor folder of its own: api talks, operator settings, the message log, the operator inbox."""

    def setUp(self):
        self.sup = Path(tempfile.mkdtemp(prefix="shaft-chan-")).resolve()
        self.addCleanup(shutil.rmtree, self.sup, True)
        self.talks_file = self.sup / "api_talks.json"
        self.inbox = self.sup / "operator-inbox"
        self.alive = {"api": True, "web": True}
        self.reach = {"telegram": True, "web": True}
        for p in (mock.patch.object(pushes, "PUSH_HOLD", 0),  # routing tests send immediately at any local hour
                  mock.patch.object(core, "SUP_DIR", self.sup), mock.patch.object(core, "API_TALKS", self.talks_file),
                  mock.patch.object(messages, "SUP_DIR", self.sup), mock.patch.object(messages, "OPERATOR_INBOX", self.inbox),
                  mock.patch("supervisor.version.service", lambda n, stale=True: {"pid": 1} if self.alive.get(n) else None),
                  mock.patch.object(channel, "can_alert",
                                    lambda name, c: "" if self.reach[c] else f"{c} cannot reach the operator"),
                  mock.patch.object(web_alerts, "log"), mock.patch.object(webpush, "log")):
            p.start()
            self.addCleanup(p.stop)

    def talk(self, model: str, ch: str, last: float, written: bool = False) -> None:
        data = json.loads(self.talks_file.read_text()) if self.talks_file.exists() else {}
        data[f"k{len(data)}"] = {"talk_id": f"api-{len(data)}", "model": model, "channel": ch, "last": last,
                                 "written": written}
        self.talks_file.write_text(json.dumps(data))

    def message(self, who: str, text: str, kind: str = "note", alert: tuple[str, str] | None = None,
                at: datetime | None = None) -> dict:
        self.inbox.mkdir(exist_ok=True)
        at = at or datetime.now()
        path = self.inbox / f"{at:%Y%m%d-%H%M%S-%f}-from-{who}.md"
        path.write_text(f"From: {who}\nTo: operator\nKind: {kind}\nDate: {at:%Y-%m-%d %H:%M}\n\n{text}\n")
        messages.record(self.sup, path, who, "operator", kind, at, text, alert=alert)
        return next(m for m in messages.history() if m["file"] == path.name)


class RouteTest(Sandbox):
    def test_an_open_conversation_wins_then_the_setting_then_where_you_last_wrote(self):
        self.talk("wisp", "telegram", 100, written=True)
        self.talk("wisp", "web", 200, written=True)
        self.assertEqual(channel.route("wisp")[0], "web")                    # auto: where you last wrote to it
        self.assertIn("last wrote", channel.route("wisp")[1])
        channel.set_preference("wisp", "telegram")
        self.assertEqual(channel.route("wisp"), ("telegram", "the operator set it for wisp"))
        self.talk("wisp", "web", 300)                                         # a web conversation open now
        self.assertEqual(channel.route("wisp")[0], "web")
        self.assertIn("is open", channel.route("wisp")[1])
        self.alive["api"] = False                                             # no API: nothing is open
        self.assertEqual(channel.route("wisp")[0], "telegram")

    def test_other_instances_and_plain_api_clients_do_not_count(self):
        self.talk("kiln", "telegram", 500)
        self.talk("wisp", "api", 600)
        self.talk("wisp", "web", 100, written=True)
        self.assertEqual(channel.route("wisp")[0], "web")

    def test_no_conversation_yet_telegram_if_it_reaches_else_the_web(self):
        self.assertEqual(channel.route("wisp")[0], "telegram")
        self.reach["telegram"] = False
        self.assertEqual(channel.route("wisp")[0], "web")

    def test_a_channel_that_cannot_alert_gives_way_and_says_so(self):
        channel.set_preference("wisp", "web")
        self.reach["web"] = False
        where, why = channel.route("wisp")
        self.assertEqual(where, "telegram")
        self.assertIn("web cannot reach the operator", why)
        self.reach["telegram"] = False                                        # neither: it waits in the inbox, said
        where, why = channel.route("wisp")
        self.assertEqual(where, "web")
        self.assertIn("waits in the inbox", why)

    def test_the_setting_is_the_operators_not_the_instances(self):
        channel.set_preference("wisp", "web")
        self.assertEqual(json.loads((self.sup / "tools" / "wisp.json").read_text())["channel"], "web")
        channel.set_preference("wisp", "auto")                                # the default: no key left behind
        self.assertNotIn("channel", json.loads((self.sup / "tools" / "wisp.json").read_text()))
        with self.assertRaises(ValueError):
            channel.set_preference("wisp", "sms")


class DeliverTest(Sandbox):
    def test_the_broker_records_where_the_message_alerts_you(self):
        from supervisor import broker
        project = self.sup.parent / f"{self.sup.name}-project"
        project.mkdir()
        self.addCleanup(shutil.rmtree, project, True)
        b = broker.Broker.__new__(broker.Broker)
        b.name, b.project = "wisp", project
        b.talk_id = None
        channel.set_preference("wisp", "web")
        with mock.patch.object(broker, "notify"), mock.patch.object(messages, "SUP_DIR", project / ".supervisor"):
            b._deliver("operator", "20991231-000000-000000-from-wisp.md", "From: wisp\n\nhello", "question",
                       datetime(2099, 12, 31), "hello")
        row, = [json.loads(l) for l in (project / ".supervisor" / messages.LOG_NAME).read_text().splitlines()]
        self.assertEqual((row["alert"], row["alert_why"]), ("web", "the operator set it for wisp"))


class WebAlertsTest(Sandbox):
    def setUp(self):
        self.enterContext(mock.patch.object(pushes, "QUIET_HOURS", None))
        super().setUp()
        web_chat._watched.clear()
        self.pushed = []

        def notify(event, skip=frozenset()):              # webpush.notify: the devices it woke
            self.pushed.append(event)
            return ["phone"]
        self.fake_notify = p = mock.patch.object(webpush, "notify", notify)
        p.start()
        self.addCleanup(p.stop)

    def test_a_message_routed_to_the_web_wakes_the_phone_once(self):
        """The bug (2026-10-20): the web UI pushed only when a chat reply ended, so a message an instance sent the operator
        never reached their phone through the web UI — only Telegram carried it."""
        self.message("wisp", "old, before the service started", alert=("web", "x"))
        self.assertEqual(web_alerts.forward_new(), 0)                         # first run: from now on, no backlog
        q = self.message("wisp", "Which one?\nsecond line", kind="question", alert=("web", "x"))
        self.message("wisp", "for Telegram", alert=("telegram", "x"))
        n = self.message("tundra", "a note", alert=("web", "x"))
        self.assertEqual(web_alerts.forward_new(), 2)
        self.assertEqual(self.pushed, [                                     # each its own tag: none replaces another
            {"title": "wisp asks", "body": "Which one?", "url": "/i/wisp/chat", "cid": "from", "tag": f"wisp-{q['file']}"},
            {"title": "tundra says", "body": "a note", "url": "/i/tundra/chat", "cid": "from", "tag": f"tundra-{n['file']}"}])
        self.assertEqual(web_alerts.forward_new(), 0)                         # each once
        self.assertEqual(len(self.pushed), 2)

    def test_a_message_logged_after_a_later_stamped_one_is_still_pushed(self):
        """Codex review 2026-10-20: a message is stamped before it is routed and logged, so two instances sending at once
        can log the earlier-stamped one second; a cursor on timestamps skipped it for good. It follows the log."""
        web_alerts.forward_new()
        self.message("kiln", "stamped second, logged first", alert=("web", "x"), at=datetime(2026, 10, 20, 12, 0, 1))
        self.assertEqual(web_alerts.forward_new(), 1)
        self.message("wisp", "stamped first, logged second", alert=("web", "x"), at=datetime(2026, 10, 20, 12, 0, 0))
        self.assertEqual(web_alerts.forward_new(), 1)
        self.assertEqual([e["body"] for e in self.pushed], ["stamped second, logged first", "stamped first, logged second"])

    def test_a_line_not_yet_whole_waits_for_the_next_call(self):
        web_alerts.forward_new()
        with (self.sup / messages.LOG_NAME).open("a") as f:
            f.write('{"ts": "2026-10-20T12:00:00", "from": "wisp", "to": "operator", "kind": "note", "file": "x.md", '
                    '"text": "half", "alert": "web"')
        self.assertEqual(web_alerts.forward_new(), 0)
        with (self.sup / messages.LOG_NAME).open("a") as f:
            f.write(', "alert_why": "x"}\n')
        self.assertEqual(web_alerts.forward_new(), 1)

    def test_none_to_the_device_whose_chat_is_on_screen_only_to_the_others(self):
        """W9 (review 2026-10-24): what was looked at was global per instance, so a desktop tab left open on wisp's chat
        silenced every push of wisp's messages to the phone. Only the device looking at it is skipped."""
        (self.sup / "web").mkdir(parents=True, exist_ok=True)
        (self.sup / "web" / "devices.json").write_text(json.dumps({"laptop": {}, "phone": {}}))
        for device in ("laptop", "phone"):
            webpush.subscribe(device, {"endpoint": "https://web.push.apple.com/x-" + device})
        self.fake_notify.stop()                          # the real notify, with its wakes caught
        p = mock.patch.object(webpush, "_wake")
        p.start()
        self.addCleanup(p.stop)
        web_alerts.forward_new()
        web_chat.watching("wisp", "laptop", cid="from")
        self.message("wisp", "seen on the laptop", alert=("web", "x"))
        self.assertEqual(web_alerts.forward_new(), 1)
        woke = webpush._wake.call_args[0][0]
        self.assertEqual(set(woke), {"phone"})                                  # the phone is told; the laptop is not
        self.assertEqual([e["body"] for e in webpush.take("phone")], ["seen on the laptop"])
        self.assertEqual(webpush.take("laptop"), [webpush.NOTHING])
        web_chat.watching("wisp", "phone", cid="from")                                       # both looking: nobody is woken
        webpush._wake.reset_mock()
        self.message("wisp", "seen on both", alert=("web", "x"))
        self.assertEqual(web_alerts.forward_new(), 0)
        webpush._wake.assert_not_called()


class TalksFileTest(unittest.TestCase):
    def test_the_api_saves_its_conversations_whole(self):
        """Codex review 2026-10-20: a half-written api_talks.json read by channel.route counted as "no conversation" and
        sent an alert to the wrong channel. Saved to a temporary file, then renamed over it."""
        sup = Path(tempfile.mkdtemp(prefix="shaft-talks-"))
        self.addCleanup(shutil.rmtree, sup, True)
        with mock.patch.object(api, "TALKS", sup / "api_talks.json"), \
                mock.patch.object(api.os, "replace", wraps=api.os.replace) as replace:
            c = api.Conversations()
            c.talk_id("wisp", [{"role": "user", "content": "hi"}], "web-1", "web")
        (src, dst), _ = replace.call_args
        self.assertEqual(Path(dst), sup / "api_talks.json")
        self.assertEqual([p.name for p in sup.iterdir()], ["api_talks.json"])   # no temporary file left behind


class ChatShowsWhatItSentTest(Sandbox):
    def legacy_rows(self):
        path = self.sup / messages.LOG_NAME
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        for row in rows:
            row.pop("talk")
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))

    def test_a_legacy_message_after_the_chat_ended_shows_under_it_with_reply(self):
        from supervisor import web, web_views
        chat = {"id": "web-1", "instance": "wisp", "created": "2026-10-20T09:00:00", "state": "ended",
                "ended": "2026-10-20T09:30:00", "turns": [{"role": "user", "content": "hi", "ts": "2026-10-20T09:00:00"}]}
        self.message("wisp", "during", at=datetime(2026, 10, 20, 9, 10))
        self.message("wisp", "Ship it now?", kind="question", at=datetime(2026, 10, 20, 11, 0))
        self.legacy_rows()
        sent = web._sent_during("wisp", chat)
        self.assertEqual([m["text"] for m in sent], ["during", "Ship it now?"])
        self.assertEqual([m["text"] for m in web._sent_during("wisp", chat, archived=True)], ["during"])
        html = str(web_views.chat_feed("wisp", chat, sent))
        self.assertLess(html.index('data-k="ended"'), html.index("Ship it now?"))   # below the line, in time order
        card = next(c for c in html.split('<div class="turn sent"') if "Ship it now?" in c)
        self.assertIn('data-raw="Ship it now?"', card)
        self.assertIn("data-reply-message", card)
        self.assertIn("a question", card)

    def test_with_no_chat_its_unread_legacy_messages_show(self):
        from supervisor import web, web_views
        self.message("wisp", "read already")
        messages._archive(next(self.inbox.glob("*.md")))
        self.message("wisp", "still waiting")
        self.legacy_rows()
        with mock.patch.object(web_chat, "chats", return_value=[]):
            sent = web._sent_during("wisp", None)
        self.assertEqual([m["text"] for m in sent], ["still waiting"])
        self.assertIn("still waiting", str(web_views.chat_feed("wisp", None, sent)))


class AnswerQuotedTest(Sandbox):
    def test_quoting_a_question_answers_it_and_a_note_or_a_scrap_does_not(self):
        q = self.message("wisp", "Should I notify you once a day, or every time a label gets new mail?", kind="question")
        self.message("wisp", "A note you can quote.")
        self.assertEqual(messages.answer_quoted("wisp", "> A note you can quote.\n\nok"), [])
        self.assertEqual(messages.answer_quoted("wisp", "> once\n\nonce"), [])            # a scrap of it: not an answer
        self.assertEqual(messages.answer_quoted("kiln", f"> {q['text']}\n\nx"), [])      # another instance's
        quoted = "> Should I notify you once a day, or every time a\n> label gets new mail?\n\nOnce a day."
        self.assertEqual(messages.answer_quoted("wisp", quoted), [q["file"]])
        self.assertEqual([m["kind"] for m in messages.pending()], ["note"])

    def test_a_question_quoted_with_other_pieces_is_answered(self):
        """The web composer sends several quotes as separate "> " blocks, a blank line apart: each block is matched on
        its own. Glued into one block (as the composer once did), the question was not found and stayed open."""
        q = self.message("wisp", "Should I notify you once a day, or every time a label gets new mail?", kind="question")
        self.message("wisp", "A note you can quote.")
        text = f"> A note you can quote.\n\n> {q['text']}\n\nOnce a day."
        self.assertEqual(messages.answer_quoted("wisp", text), [q["file"]])


class ApiChannelTest(unittest.TestCase):
    setUp = test_api.ApiTest.setUp   # through the module: the class imported here was discovered and run again

    def post(self, channel_header):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}", "Tailscale-User-Login": ME,
                   "X-Shaft-Conversation": f"c-{time.monotonic_ns()}"}
        if channel_header is not None:
            headers["X-Shaft-Channel"] = channel_header
        body = {"model": "wisp", "messages": [{"role": "user", "content": "hi"}]}
        r = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_each_conversation_keeps_the_channel_its_client_names(self):
        for given, kept in (("web", "web"), ("Telegram", "telegram"), (None, "api")):
            self.assertEqual(self.post(given), 200)
            talk = next(t for t in api.Handler.conversations.talks.values()
                        if t["talk_id"] == fake_run_agent.request["talk_id"])
            self.assertEqual(talk["channel"], kept)
        self.assertEqual(self.post("sms"), 400)

    def test_the_web_ui_and_telegram_name_theirs(self):
        from supervisor import telegram
        with mock.patch.object(core, "secrets_env", return_value={"SHAFT_API_KEY": KEY}):
            self.assertEqual(web_chat._headers({"login": ME, "id": "web-1"})["X-Shaft-Channel"], "web")
        seen = {}

        def urlopen(req, timeout):
            seen.update(req.headers)
            raise urllib.error.URLError("stop here")
        with mock.patch.object(telegram, "secrets_env", return_value={"SHAFT_API_KEY": KEY}), \
                mock.patch.object(api, "allowed_logins", return_value={ME}), \
                mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(telegram.TelegramError):
                telegram.ask_api("wisp", [{"role": "user", "content": "hi"}])
        self.assertEqual(seen.get("X-shaft-channel"), "telegram")


class CommandTest(Sandbox):
    def test_shaft_channel_sets_it_and_says_where_a_message_goes_now(self):
        from supervisor import __main__ as cli
        with mock.patch.object(cli, "instance_root"), mock.patch.object(cli, "log"), \
                mock.patch("builtins.print") as out:
            self.assertEqual(cli.cmd_channel("wisp", "web"), 0)
        self.assertEqual(channel.preference("wisp"), "web")
        self.assertIn("wisp reaches you on: web — a message now would go to web", out.call_args[0][0])


if __name__ == "__main__":
    unittest.main()


class NativeAppsCanAlertTest(unittest.TestCase):
    """Bug 2026-11-12: an operator with only the native apps (APNs tokens, no Web Push subscription) was told
    "no device has the web UI's notifications on", so every alert fell back to Telegram even with the channel set
    to web. The real can_alert, not the Sandbox's stub."""

    def setUp(self):
        for p in (mock.patch("supervisor.version.service", lambda n, stale=True: {"pid": 1}),
                  mock.patch.object(webpush, "subscriptions", lambda: {})):
            p.start()
            self.addCleanup(p.stop)

    def test_native_devices_with_apns_configured_can_alert_on_the_web(self):
        from supervisor import apns
        with mock.patch.object(apns, "devices", lambda: {"iphone": {"token": "t"}}), \
                mock.patch.object(apns, "configured", lambda: None):
            self.assertEqual(channel.can_alert("wisp", "web"), "")
            self.assertTrue(webpush.can_wake())

    def test_native_devices_without_apns_configured_cannot(self):
        from supervisor import apns
        with mock.patch.object(apns, "devices", lambda: {"iphone": {"token": "t"}}), \
                mock.patch.object(apns, "configured", lambda: "native push off: missing SHAFT_APNS_KEY_ID"):
            self.assertIn("notifications on", channel.can_alert("wisp", "web"))
            self.assertFalse(webpush.can_wake())

    def test_no_device_at_all_cannot(self):
        from supervisor import apns
        with mock.patch.object(apns, "devices", lambda: {}), mock.patch.object(apns, "configured", lambda: None):
            self.assertIn("notifications on", channel.can_alert("wisp", "web"))
