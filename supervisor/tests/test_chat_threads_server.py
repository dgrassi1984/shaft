"""Thread scheduling, context and message ownership through the server paths."""
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from supervisor import api, broker, messages, runner, web_chat


class ServerThreadsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.patch(web_chat, "SUP_DIR", self.root)
        self.patch("supervisor.feedback", "by_target", return_value={})

    def patch(self, target, name, *args, **kwargs):
        if isinstance(target, str):
            import importlib
            target = importlib.import_module(target)
        p = mock.patch.object(target, name, *args, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    def chat(self, cid="web-a", name="example", state="idle", turns=None):
        c = dict(id=cid, instance=name, login="operator@example.com", title=cid, created="2026-01-01",
                 state=state, turns=turns or [], talks=["talk-a"])
        web_chat.save(c)
        return c

    def test_two_waiters_run_in_arrival_order_behind_a_held_reply(self):
        q = api.ReplyQueue()
        a, b, c = [q.join("example", title) for title in ("A", "B", "C")]
        cancel = threading.Event()
        order, phases = [], []
        waiting = {b: threading.Event(), c: threading.Event()}
        def run(ticket):
            try:
                def status(ev):
                    phases.append(ev["phase"])
                    waiting[ticket].set()
                if q.wait("example", ticket, cancel, status):
                    order.append(ticket[1])
            finally:
                q.leave("example", ticket)
        workers = [threading.Thread(target=run, args=(t,)) for t in (c, b)]
        for worker in workers:
            worker.start()
        try:
            for signal in waiting.values():
                self.assertTrue(signal.wait(2))
            self.assertEqual(order, [])
        finally:
            q.leave("example", a)
            for worker in workers:
                worker.join(2)
        self.assertEqual(order, ["B", "C"])
        self.assertIn("waiting: answering in «A»", phases)
        self.assertEqual(q.queues, {})

    def test_cancelled_waiter_leaves_without_running(self):
        q = api.ReplyQueue()
        a, b = q.join("example", "A"), q.join("example", "B")
        cancelled = threading.Event()
        cancelled.set()
        self.assertFalse(q.wait("example", b, cancelled, self.fail))
        q.leave("example", b)
        q.leave("example", a)
        self.assertEqual(q.queues, {})

    def test_thread_and_from_feed_route_new_rows_and_preserve_legacy_span(self):
        c = self.chat()
        def row(file, **kw):
            return dict({"file": file, "from": "example", "to": "operator", "ts": "2026-02-01",
                         "kind": "note", "text": file, "read": False}, **kw)
        rows = [row("own", talk="talk-a"), row("other", talk="talk-b"), row("cycle", talk=None),
                row("legacy"), row("old", ts="2025-01-01"),
                row("background", **{"from": "operator", "to": "example", "talk": None,
                                     "via": messages.BACKGROUND})]
        self.patch(messages, "history", return_value=rows)
        self.assertEqual([m["file"] for m in web_chat.sent_during("example", c)], ["own", "legacy"])
        feed = web_chat.from_feed("example")
        self.assertIsNone(feed["chat"])
        # "other" ran in a conversation no web thread holds (a terminal talk, Telegram): From shows it
        self.assertEqual([i["file"] for i in feed["items"]], ["other", "cycle", "background"])
        self.assertEqual((feed["last"], feed["last_count"]), ("2026-02-01", 2))
        self.patch(web_chat, "CHAT_WINDOW", 1)
        window = web_chat.from_feed("example")
        self.assertEqual([i["file"] for i in window["items"]], ["background"])
        self.assertEqual((window["last"], window["last_count"], window["version"]),
                         (feed["last"], feed["last_count"], feed["version"]))

    def test_a_message_from_a_conversation_with_no_web_thread_shows_in_from_and_pushes_there(self):
        """Integration review: a send_message during a terminal talk or a Telegram conversation carries a talk id that
        no web thread holds, so it was in no thread and not in From either: on the web it showed nowhere."""
        from supervisor import web_alerts
        c = self.chat()
        def row(file, **kw):
            return dict({"file": file, "from": "example", "to": "operator", "ts": "2026-02-01",
                         "kind": "note", "text": file, "read": False}, **kw)
        rows = [row("own", talk="talk-a"), row("telegram", talk="api-telegram"), row("cycle", talk=None)]
        self.patch(messages, "history", return_value=rows)
        self.assertEqual([i["file"] for i in web_chat.from_feed("example")["items"]], ["telegram", "cycle"])
        self.assertEqual([m["file"] for m in web_chat.sent_during("example", c)], ["own"])
        self.assertEqual([web_alerts._cid_of_message(r) for r in rows], [c["id"], "from", "from"])
        self.assertEqual(web_alerts.event(rows[0])["url"], f"/i/example/chat?id={c['id']}")

    def test_digest_lists_only_other_open_threads_newest_first(self):
        a = self.chat(turns=[dict(role="user", content="current", ts="2026-02-01")])
        self.assertEqual(web_chat._api_messages(a), [{"role": "user", "content": "current"}])
        self.chat("web-b", turns=[dict(role="user", content="question\nextra", ts="2026-02-02"),
                                  dict(role="assistant", content="answer\nextra", ts="2026-02-03")])
        self.chat("web-c", turns=[dict(role="user", content="newest", ts="2026-02-04")])
        self.chat("web-d", state="ended")
        self.chat("web-e", name="other")
        digest = web_chat._api_messages(a)[0]
        self.assertEqual(digest["role"], "system")
        self.assertEqual(digest["content"], "Other open threads:\n"
                         "- web-c «web-c» (last moved 2026-02-04): you: newest / me: \n"
                         "- web-b «web-b» (last moved 2026-02-03): you: question / me: answer\n"
                         "read_thread reads any of them.")

    def test_read_thread_lists_reads_scopes_and_refuses_replay(self):
        b = broker.Broker.__new__(broker.Broker)
        b.name, b.as_of, b.disabled, b.thread_reads = "example", None, set(), {}
        self.chat(turns=[dict(role="user", ts="2026-02-01", content="hello", activity=["hidden"])])
        for i in range(22):
            c = self.chat(f"web-{i}", state="ended")
            c["created"] = f"2026-02-{i+1:02d}"
            web_chat.save(c)
        self.chat("web-foreign", name="other")
        listing = b._read_thread().splitlines()
        self.assertEqual(len(listing), 21)
        self.assertTrue(listing[0].startswith("web-a "))
        self.assertTrue(listing[1].startswith("web-21 "))
        self.assertEqual(b._read_thread("web-a"), "operator (2026-02-01): hello")
        self.chat("web-long", state="ended", turns=[dict(role="assistant", ts="2026-02-01", content="x" * 13000)])
        # its own long answer is cut, so it cannot push what the operator said out of the view (review of routing)
        self.assertEqual(b._read_thread("web-long"), "example (2026-02-01): " + "x" * 3000 + " … [10000 more characters cut]")
        many = [dict(role="assistant", ts=f"2026-02-0{i}", content=str(i) * 3000) for i in range(1, 7)]
        self.chat("web-many", state="ended", turns=many)
        self.assertEqual(len(b._read_thread("web-many")), 12000)          # the tail of the thread, as before
        for cid in ("web-foreign", "web-unknown", "../escape"):
            with self.assertRaises(web_chat.Refused):
                b._read_thread(cid)
        for method in ("mail_grants", "calendar_grants", "gdrive_grants", "gchat_grants"):
            self.patch(b, method, return_value=[])
        self.patch(b, "_channel_line", return_value="")
        self.patch(b, "_orders_line", return_value="")
        self.patch("supervisor.telegram", "reachable", return_value="")
        self.assertIn("read_thread", [s["function"]["name"] for s in b.tool_specs()])
        b.as_of = datetime(2026, 1, 1)
        self.assertNotIn("read_thread", [s["function"]["name"] for s in b.tool_specs()])
        with self.assertRaises(PermissionError):
            b._read_thread("web-a")

    def test_broker_delivery_records_api_talk_and_cycle_null(self):
        b = broker.Broker.__new__(broker.Broker)
        b.project, b.name = self.root, "example"
        self.patch("supervisor.channel", "route", return_value=("web", "test"))
        self.patch(broker, "notify")
        self.patch(b, "_answer_comments")
        self.patch("supervisor.pushes", "hold_until", return_value=None)
        for talk, file in (("talk-a", "api.md"), (None, "cycle.md")):
            b.talk_id = talk
            b._deliver("operator", file, "text", "note", datetime(2026, 1, 1), "text")
        messages.record(self.root / ".supervisor", self.root / "operator.md", "operator", "example", "note",
                        datetime(2026, 1, 1), "background", via=messages.BACKGROUND)
        rows = [json.loads(s) for s in (self.root / ".supervisor" / messages.LOG_NAME).read_text().splitlines()]
        self.assertEqual([r["talk"] for r in rows], ["talk-a", None, None])

    def test_relay_records_talk_and_resets_documents_when_started(self):
        c = self.chat(state="replying")
        c["reply"] = {"docs_before": {}, "interjections": 0}
        web_chat.save(c)
        self.patch("supervisor.core", "secrets_env", return_value={"SHAFT_API_KEY": "test"})
        self.patch(web_chat, "_docs_now", return_value={"other.md": "other reply"})
        snapshots = []
        def stream():
            for phase in ("waiting: answering in «A»", "started"):
                yield ('data: ' + json.dumps({"shaft_event": {"type": "status", "phase": phase}})).encode()
                snapshots.append(web_chat.load(c["id"])["reply"].copy())
            yield b'data: [DONE]'
        response = mock.Mock(status=200, getheader=mock.Mock(return_value="talk-b"))
        response.__iter__ = lambda _: stream()
        conn = mock.Mock(getresponse=mock.Mock(return_value=response))
        self.patch(web_chat, "_api_conn", return_value=conn)
        self.patch(web_chat, "_finish", return_value=[])
        self.patch(web_chat, "_sync_copies")
        relay = web_chat.Relay(c["id"])
        web_chat._run(relay)
        # the apps' stream shows the wait, never the internal `started` signal (integration review)
        self.assertEqual([e.get("phase") for e in relay.events if e["type"] == "status"], ["waiting: answering in «A»"])
        self.assertEqual(snapshots[0]["waiting"], "waiting: answering in «A»")
        self.assertIsNone(snapshots[1]["waiting"])
        self.assertEqual(snapshots[1]["docs_before"], {"other.md": "other reply"})
        self.assertEqual(web_chat.load(c["id"])["talks"], ["talk-a", "talk-b"])
        web_chat._run(web_chat.Relay(c["id"]))
        self.assertEqual(web_chat.load(c["id"])["talks"], ["talk-a", "talk-b"])

    def test_thread_title_header_is_ascii_safe(self):
        c = self.chat()
        c["title"] = "caffè ☕"
        self.patch("supervisor.core", "secrets_env", return_value={"SHAFT_API_KEY": "test"})
        self.assertEqual(web_chat._headers(c)["X-Shaft-Thread-Title"], "caff%C3%A8%20%E2%98%95")

    def test_api_passes_talk_to_runner_returns_header_and_decodes_title(self):
        from supervisor.tests.test_api import FakeProc
        self.patch(api, "TALKS", self.root / "talks.json")
        self.patch(api, "log")
        self.patch(api.tom, "heard")
        self.patch(api, "busy_with", return_value=None)
        self.patch(api, "Reply", return_value=mock.MagicMock(f=None))
        handler = api.Handler.__new__(api.Handler)
        handler.conversations = api.Conversations()
        handler.live = mock.Mock()
        handler.reply_queue = api.ReplyQueue()
        handler.headers = {"X-Shaft-Thread-Title": "caff%C3%A8%20%E2%98%95"}
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._client_gone = lambda: False
        def run(*args, driver, **kwargs):
            self.assertEqual(kwargs["talk_id"], handler._response_talk)
            driver(FakeProc())
            return 0
        launched = self.patch(api, "run_agent", side_effect=run)
        handler._chat("example", [{"role": "user", "content": "hello"}], True, "test", "web-a", channel="web")
        launched.assert_called_once()
        handler.send_header.assert_any_call("X-Shaft-Talk", handler._response_talk)
        self.assertEqual(next(iter(handler.conversations.talks.values()))["title"], "caffè ☕")
        self.assertIn('"phase": "started"', handler.wfile.getvalue().decode())
        self.assertEqual(handler.reply_queue.queues, {})
        self.patch(api, "ANSWERED", self.root / "answered.json")
        self.patch(api.Handler, "answered", api.Answered())
        handler.wfile = io.BytesIO()
        handler._chat("example", [{"role": "user", "content": "hello"}], False, "test", "web-a", rid="request-a")
        answer = json.loads(handler.wfile.getvalue())
        talk = answer["talk_id"]
        handler.wfile = io.BytesIO()
        handler.send_header.reset_mock()
        handler._chat("example", [{"role": "user", "content": "hello"}], False, "test", "web-a", rid="request-a")
        self.assertEqual(launched.call_count, 2)
        handler.send_header.assert_any_call("X-Shaft-Talk", talk)
        self.assertTrue(json.loads(handler.wfile.getvalue())["replayed"])

    def test_launch_sets_broker_talk_before_broker_starts(self):
        b = mock.Mock()
        self.patch(runner, "Broker", return_value=b)
        self.patch(runner, "instance_config", return_value={})
        self.patch(runner.localmodel, "is_service", return_value=False)
        self.patch(runner, "secrets_env", return_value={})
        self.patch(runner, "SUP_DIR", self.root)
        self.patch(runner, "rebuild_env")
        self.patch("supervisor.autonomy", "describe_schedule", return_value="")
        self.patch(runner, "open_conversations", return_value=[])
        self.patch(runner.protected, "make", return_value=self.root / "protected")
        self.patch(runner, "agent_command", return_value=([], {}))
        b.start.side_effect = lambda: self.assertEqual(b.talk_id, "talk-a")
        got, _, _ = runner._launch("example", self.root, "api", ["answer"], None, {}, talk_id="talk-a")
        self.assertIs(got, b)

    def test_run_agent_and_run_forward_talk_to_launch(self):
        self.patch(runner, "PROJECT", self.root)
        (self.root / ".git").mkdir()
        self.patch(runner, "instance_root", return_value=self.root)
        self.patch(runner, "_Held")
        self.patch(runner, "log")
        self.patch(runner, "describe_knobs", return_value="")
        self.patch(runner, "_clean_tmp")
        self.patch(runner, "_reconcile", return_value={})
        self.patch(runner, "_snapshot", return_value={"code": {}})
        launch = self.patch(runner, "_launch", side_effect=RuntimeError("launch reached"))
        with self.assertRaisesRegex(RuntimeError, "launch reached"):
            runner.run_agent("example", "api", ["answer"], True, talk_id="talk-a")
        self.assertEqual(launch.call_args.kwargs["talk_id"], "talk-a")

    def test_warm_launch_sets_broker_talk_before_agent_starts(self):
        self.patch(runner, "instance_root", return_value=self.root)
        self.patch(runner, "safe_read_bytes", return_value=b"")
        b = mock.Mock()
        self.patch(runner, "_launch", return_value=(b, [], {}))
        self.patch(runner, "Presence")
        self.patch(runner.threading, "Thread")
        self.patch(runner, "log")
        self.patch(runner, "describe_knobs", return_value="")
        warm = runner.Warm("example", "api", ["answer"], "talk-a")
        warm.turn.before = {"code": {}}
        def start(*args):
            self.assertEqual(b.talk_id, "talk-a")
            return mock.Mock(pid=123)
        self.patch(runner, "_popen", side_effect=start)
        warm._start()
        self.assertEqual(warm.broker.talk_id, "talk-a")

    def test_documents_written_while_waiting_do_not_become_reply_cards(self):
        c = self.chat(state="replying")
        c["reply"] = {"docs_before": {"other.md": "other reply"}, "waiting": None}
        web_chat.save(c)
        self.patch(web_chat, "_docs_now", return_value={"other.md": "other reply", "own.md": "this reply"})
        self.patch(web_chat, "log")
        self.patch("supervisor.webpush", "notify")
        self.patch("supervisor.doc_comments", "parse_answer", return_value={"answered": [], "opened": []})
        web_chat._finish(web_chat.Relay(c["id"]), "answer", [], None, 0)
        self.assertEqual(web_chat.load(c["id"])["turns"][-1]["docs"], [{"path": "own.md", "created": True}])

    def test_replay_delivery_stays_simulated_with_a_talk_id(self):
        b = broker.ReplayBroker.__new__(broker.ReplayBroker)
        b.name, b.project, b.as_of, b.talk_id = "example", self.root, datetime(2026, 1, 1), "talk-a"
        b.operator_sent, b.operator_files = 0, frozenset()
        self.patch(b, "_tag")
        self.patch("supervisor.pushes", "hold_until", return_value=None)
        b._deliver("operator", "message.md", "text", "note", datetime(2026, 1, 1), "text")
        self.assertEqual(b.operator_files, {"message.md"})
        self.assertFalse((self.root / ".supervisor").exists())

    def test_cancelled_waiting_reply_has_no_document_cards(self):
        c = self.chat(state="replying")
        c["reply"] = {"docs_before": {}, "waiting": "waiting: answering in «A»"}
        web_chat.save(c)
        self.patch(web_chat, "_docs_now", return_value={"other.md": "other reply"})
        self.patch(web_chat, "log")
        relay = web_chat.Relay(c["id"])
        relay.stopped = True
        web_chat._finish(relay, "", [], None, 0)
        self.assertNotIn("docs", web_chat.load(c["id"])["turns"][-1])
