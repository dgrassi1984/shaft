"""Telegram (Daniele, 2026-10-13): one bot per instance, ONE paired account, live conversation through the API,
instances' messages to the operator forwarded from their own bot. Against a fake Bot API server."""
from __future__ import annotations

import io
import json
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from supervisor import messages, telegram

TOK = {"wren": "111:wren-secret", "shaft": "222:shaft-secret"}
ME, STRANGER = {"id": 42, "username": "daniele"}, {"id": 666, "username": "someone"}


class FakeBotAPI:
    FILE = b"%PDF-1.7 fake"

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []      # (token, method, params)
        self.next_id = 100
        self.fail = set()
        self.reject_html = False
        self.uploads: list[str] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):                     # /file/bot<token>/<path>: a file Daniele sent
                data = fake.FILE
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                _, bot, method = self.path.split("/", 2)
                token = bot.removeprefix("bot")
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers["Content-Type"].startswith("multipart/form-data"):
                    fake.uploads.append(raw.decode("latin-1"))
                    params = {}
                else:
                    params = json.loads(raw or b"{}")
                fake.calls.append((token, method, params))
                if method in fake.fail or token not in TOK.values():
                    body, code = {"ok": False, "description": "Unauthorized"}, 401
                elif fake.reject_html and params.get("parse_mode"):
                    body, code = {"ok": False, "description": "Bad Request: can't parse entities: bad tag"}, 400
                elif method == "getMe":
                    body, code = {"ok": True, "result": {"username": "wren_shaft_bot"}}, 200
                elif method in ("sendMessage", "sendPhoto", "sendDocument"):
                    fake.next_id += 1
                    body, code = {"ok": True, "result": {"message_id": fake.next_id}}, 200
                elif method == "getFile":
                    body, code = {"ok": True, "result": {"file_path": f"documents/{params.get('file_id')}"}}, 200
                else:
                    body, code = {"ok": True, "result": True}, 200
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def sent(self, token=None):
        return [(t, p) for t, m, p in self.calls if m == "sendMessage" and (token is None or t == token)]


def _msg(user, text, mid=1, chat_type="private", reply_to=None):
    m = {"message_id": mid, "from": user, "chat": {"id": user["id"], "type": chat_type}, "text": text}
    if reply_to:
        m["reply_to_message"] = reply_to
    return {"update_id": mid, "message": m}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="shaft-tg-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.sup = self.project / ".supervisor"
        self.sup.mkdir()
        self.api = FakeBotAPI()
        self.addCleanup(self.api.server.shutdown)
        self.asked = []
        env = {telegram.token_key(n): t for n, t in TOK.items()}
        for p in (mock.patch.object(telegram, "SUP_DIR", self.sup), mock.patch.object(telegram, "API_BASE", self.api.url),
                  mock.patch.object(telegram, "secrets_env", lambda: env),
                  mock.patch.object(telegram, "instance_names", lambda: ["shaft", "wren", "kant"]),
                  mock.patch.object(telegram, "log", lambda *_: None),
                  mock.patch.object(telegram, "ask_api", self._ask),
                  mock.patch.object(messages, "SUP_DIR", self.sup),
                  mock.patch.object(messages, "OPERATOR_INBOX", self.sup / "operator-inbox"),
                  mock.patch.object(messages, "INSTANCES", self.project / "instances"),
                  mock.patch.object(messages, "log", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        messages._cache.update(key=None, rows=[])
        self.chat = telegram.Chat("wren", TOK["wren"])

    def _ask(self, name, history):
        self.asked.append((name, [dict(h) for h in history]))
        return f"answer {len(self.asked)}"

    def upd(self, u, chat=None):
        telegram.handle_update("wren", TOK["wren"], chat or self.chat, u)

    def pair(self):
        code = telegram.new_pairing_code()
        self.upd(_msg(ME, f"/pair {code}"))
        return code

    def test_only_one_account_pairs_and_only_it_is_answered(self):
        self.upd(_msg(ME, "hello"))                                   # not paired yet: silence
        self.upd(_msg(ME, "/pair WRONG123"))
        self.assertEqual(self.api.sent(), [])
        code = self.pair()
        self.assertEqual(telegram.paired_id(), 42)
        self.assertIn("Paired", self.api.sent()[-1][1]["text"])
        n = len(self.api.calls)
        self.upd(_msg(STRANGER, f"/pair {code}", mid=5))              # the code is spent; a second account never pairs
        self.upd(_msg(STRANGER, "hi wren", mid=6))
        self.assertEqual(len(self.api.calls), n)                      # nothing answered, nothing asked
        self.assertEqual(self.asked, [])
        self.assertEqual(telegram.paired_id(), 42)
        with self.assertRaisesRegex(telegram.TelegramError, "already paired"):
            telegram.new_pairing_code()                               # only `shaft telegram unpair` frees it
        self.upd(_msg(ME, "group hi", mid=7, chat_type="group"))      # groups are ignored, even from the owner
        self.assertEqual(self.asked, [])

    def test_an_expired_code_does_not_pair(self):
        code = telegram.new_pairing_code()
        telegram.update(lambda s: s["pairing"].update(until=datetime(2020, 1, 1).isoformat()))
        self.upd(_msg(ME, f"/pair {code}"))
        self.assertIsNone(telegram.paired_id())

    def test_a_message_is_a_turn_of_a_live_conversation(self):
        self.pair()
        self.upd(_msg(ME, "what did you find?", mid=10))
        self.upd(_msg(ME, "and then?", mid=11))
        self.assertEqual([h["content"] for h in self.asked[1][1]], ["what did you find?", "answer 1", "and then?"])
        self.assertEqual([p["text"] for _, p in self.api.sent(TOK["wren"])][-2:], ["answer 1", "answer 2"])
        self.chat.last -= telegram.IDLE_MINUTES * 60 + 1                 # idle: the API wrote it up — a new one
        self.upd(_msg(ME, "new topic", mid=12))
        self.assertEqual([h["content"] for h in self.asked[2][1]], ["new topic"])
        self.upd(_msg(ME, "/new", mid=13))
        self.upd(_msg(ME, "fresh", mid=14))
        self.assertEqual(len(self.asked[3][1]), 1)

    def test_api_failure_is_said_not_swallowed(self):
        self.pair()
        with mock.patch.object(telegram, "ask_api", side_effect=telegram.TelegramError("the API is not reachable")):
            self.upd(_msg(ME, "hi", mid=20))
        self.assertIn("(no answer: the API is not reachable)", self.api.sent()[-1][1]["text"])
        self.assertEqual(self.chat.history, [])                      # the failed turn is not kept

    def test_instances_messages_arrive_from_their_own_bot_and_a_reply_answers_the_question(self):
        self.pair()
        self.assertEqual(telegram.forward_new(), 0)                  # first run: from now on, no backlog
        inbox = self.sup / "operator-inbox"
        inbox.mkdir()
        for who, kind, text in (("wren", "question", "Notify once or every time?"), ("kant", "note", "no bot")):
            path = inbox / f"20991231-235959-00000{len(text)}-from-{who}.md"
            path.write_text(f"From: {who}\nKind: {kind}\n\n{text}\n")
            messages.record(self.sup, path, who, "operator", kind, datetime(2099, 12, 31, 23, 59, 59), text)
        self.assertEqual(telegram.forward_new(), 1)                  # kant has no bot: skipped, not stuck
        (tok, p), = self.api.sent(TOK["wren"])[-1:]
        self.assertEqual(p["chat_id"], 42)
        self.assertIn("❓ <b>Question</b> from wren", p["text"])
        self.assertEqual(telegram.forward_new(), 0)                  # sent once
        mid = self.api.next_id
        shown = "❓ Question from wren\n\nNotify once or every time?"     # Telegram quotes the rendered text
        self.upd(_msg(ME, "Once.", mid=30, reply_to={"message_id": mid, "text": shown}))
        self.assertTrue(self.asked[-1][1][-1]["content"].startswith("> ❓ Question from wren"))
        self.assertFalse(any(p.name.endswith("from-wren.md") for p in inbox.glob("*.md")))   # answered: archived

    def test_a_failed_forward_is_retried(self):
        self.pair()
        telegram.forward_new()
        inbox = self.sup / "operator-inbox"
        inbox.mkdir()
        path = inbox / "20991231-235959-000001-from-wren.md"
        path.write_text("x")
        messages.record(self.sup, path, "wren", "operator", "note", datetime(2099, 12, 31, 23, 59, 59), "later")
        self.api.fail.add("sendMessage")
        self.assertEqual(telegram.forward_new(), 0)
        self.api.fail.clear()
        self.assertEqual(telegram.forward_new(), 1)

    def test_commands(self):
        self.pair()
        with mock.patch("supervisor.autonomy.start_wake", return_value="started a cycle for wren"):
            self.upd(_msg(ME, "/wake", mid=40))
        self.assertEqual(self.api.sent()[-1][1]["text"], "started a cycle for wren")
        with mock.patch("supervisor.dashboard.instance_state", return_value={
                "state": "idle", "since": "", "doing": "", "goal": "", "next_wake": "21:30", "calls_today": 3,
                "usd_today": 0.12}):
            self.upd(_msg(ME, "/status", mid=41))
        self.assertIn("next wake 21:30", self.api.sent()[-1][1]["text"])
        self.assertEqual(self.asked, [])                              # commands are not conversation turns

    def test_the_token_never_reaches_an_error_or_log(self):
        with mock.patch.object(telegram, "API_BASE", "http://127.0.0.1:9"):   # nothing listens there
            with self.assertRaises(telegram.TelegramError) as ctx:
                telegram.call(TOK["wren"], "getMe", http_timeout=2)
        self.assertNotIn("wren-secret", str(ctx.exception))
        with self.assertRaises(telegram.TelegramError) as ctx:
            telegram.call("999:bad", "getMe")
        self.assertNotIn("999:bad", str(ctx.exception))

    def test_long_answers_are_split(self):
        ids = telegram.send(TOK["wren"], 42, "x" * (telegram.MAX_TEXT * 2 + 10))
        self.assertEqual(len(ids), 3)
        self.assertTrue(all(len(p["text"]) <= telegram.MAX_TEXT for _, p in self.api.sent()))

    def test_markdown_arrives_formatted_and_a_refused_part_as_plain_text(self):
        """Daniele, 2026-10-13: "It currently is raw markdown"."""
        telegram.send(TOK["wren"], 42, "**Done**: `a < b` — see [PR](https://x.org/p?a=1&b=2)")
        (_, p), = self.api.sent()
        self.assertEqual(p["parse_mode"], "HTML")
        self.assertEqual(p["text"], '<b>Done</b>: <code>a &lt; b</code> — see <a href="https://x.org/p?a=1&amp;b=2">PR</a>')
        self.api.reject_html = True
        telegram.send(TOK["wren"], 42, "**odd** text")
        last = self.api.sent()[-1][1]
        self.assertNotIn("parse_mode", last)                     # sent again, plain: never lost
        self.assertEqual(last["text"], "**odd** text")


    def test_token_command_checks_with_telegram_before_saving(self):
        saved = {}
        with mock.patch("supervisor.api._set_secret", lambda k, v: saved.update({k: v})), \
                mock.patch("sys.stdin", io.StringIO("999:bad\n")), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(telegram.command("token", "wren"), 1)
        self.assertEqual(saved, {})
        self.assertIn("not saved", out.getvalue())
        with mock.patch("supervisor.api._set_secret", lambda k, v: saved.update({k: v})), \
                mock.patch("sys.stdin", io.StringIO(TOK["wren"] + "\n")), mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(telegram.command("token", "wren"), 0)
        self.assertEqual(saved, {"TELEGRAM_TOKEN_WREN": TOK["wren"]})


class MarkdownTest(unittest.TestCase):
    def test_blocks(self):
        md = ("## Findings\n\n- first\n  - nested *it*\n- [x] done\n\n> quoted **b**\n> two\n\n"
              "| model | success |\n|---|---:|\n| mimo | 86% |\n\n```python\nif a < b:\n    x = '<ok>'\n```\n---")
        out = telegram.to_html(md)
        self.assertIn("<b>Findings</b>", out)
        self.assertIn("• first\n  • nested <i>it</i>\n• ☑ done", out)
        self.assertIn("<blockquote>quoted <b>b</b>\ntwo</blockquote>", out)
        self.assertIn("<pre>model  success\nmimo   86%</pre>", out)
        self.assertIn('<pre><code class="language-python">if a &lt; b:\n    x = \'&lt;ok&gt;\'</code></pre>', out)
        self.assertTrue(out.endswith("──────────"))

    def test_text_that_only_looks_like_markup_is_left_alone(self):
        self.assertEqual(telegram.to_html("snake_case_name, 2*3*4 and <tag> & co"),
                         "snake_case_name, 2*3*4 and &lt;tag&gt; &amp; co")
        self.assertEqual(telegram.to_html("`**not bold**`"), "<code>**not bold**</code>")

    def test_a_code_block_cut_by_the_split_is_closed_and_reopened(self):
        parts = telegram.split_markdown("```py\n" + "line\n" * 800 + "```", 1000)
        self.assertGreater(len(parts), 3)
        for p in parts:
            self.assertEqual(p.count("```") % 2, 0, p[:40])
            self.assertTrue(p.startswith("```py"))
            self.assertIn('<code class="language-py">', telegram.to_html(p))


class ToldTheyCanReachDanieleTest(unittest.TestCase):
    """Daniele, 2026-10-13: "ensure that the instances know they can contact me via telegram"."""

    def test_send_message_names_the_bot_only_when_it_works(self):
        from supervisor import broker
        b = broker.Broker.__new__(broker.Broker)
        b.name, b.disabled = "wren", set()
        with mock.patch.object(telegram, "reachable", return_value="@WrenFawlerBot"):
            spec = next(t for t in b.tool_specs() if t["function"]["name"] == "send_message")
        self.assertIn("on Telegram, from your own bot @WrenFawlerBot", spec["function"]["description"])
        self.assertNotIn("@WrenFawlerBot", broker.SPECS["send_message"]["function"]["description"])   # not shared
        with mock.patch.object(telegram, "reachable", return_value=""):
            spec = next(t for t in b.tool_specs() if t["function"]["name"] == "send_message")
        self.assertNotIn("Telegram, from your own bot", spec["function"]["description"])

    def test_reachable_needs_token_bot_pairing_and_the_service(self):
        sup = Path(tempfile.mkdtemp(prefix="shaft-tg-"))
        self.addCleanup(shutil.rmtree, sup, True)
        env = {telegram.token_key("wren"): TOK["wren"]}
        with mock.patch.object(telegram, "SUP_DIR", sup), mock.patch.object(telegram, "secrets_env", lambda: env), \
                mock.patch("supervisor.version.service", return_value={"pid": 1}) as svc:
            self.assertEqual(telegram.reachable("wren"), "")              # no bot name, not paired
            telegram.remember_bot("wren", "WrenFawlerBot")
            self.assertEqual(telegram.reachable("wren"), "")              # not paired
            telegram.update(lambda s: s.__setitem__("paired", {"user_id": 42}))
            self.assertEqual(telegram.reachable("wren"), "@WrenFawlerBot")
            self.assertEqual(telegram.reachable("shaft"), "")             # no token for shaft here
            svc.return_value = None
            self.assertEqual(telegram.reachable("wren"), "")              # service down


if __name__ == "__main__":
    unittest.main()
