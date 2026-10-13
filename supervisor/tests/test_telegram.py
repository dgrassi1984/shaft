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
    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []      # (token, method, params)
        self.next_id = 100
        self.fail = set()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                _, bot, method = self.path.split("/", 2)
                token = bot.removeprefix("bot")
                params = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                fake.calls.append((token, method, params))
                if method in fake.fail or token not in TOK.values():
                    body, code = {"ok": False, "description": "Unauthorized"}, 401
                elif method == "getMe":
                    body, code = {"ok": True, "result": {"username": "wren_shaft_bot"}}, 200
                elif method == "sendMessage":
                    fake.next_id += 1
                    body, code = {"ok": True, "result": {"message_id": fake.next_id}}, 200
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
        self.assertIn("Question from wren", p["text"])
        self.assertEqual(telegram.forward_new(), 0)                  # sent once
        mid = self.api.next_id
        self.upd(_msg(ME, "Once.", mid=30, reply_to={"message_id": mid, "text": p["text"]}))
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


if __name__ == "__main__":
    unittest.main()
