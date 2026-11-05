"""The OpenAI-compatible API: who may use it, and that its output is what OpenAI clients expect."""
from __future__ import annotations

import contextlib
import subprocess
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from supervisor import api

KEY, ME = "sk-shaft-test", "operator@example.com"
EVENTS = [{"type": "ready", "name": "shaft"}, {"type": "status", "phase": "waiting for the model"},
          {"type": "tool_start", "name": "web_search", "args": '{"query": "teal"}'},
          {"type": "tool_end", "name": "web_search", "ok": True, "brief": "ok"},
          {"type": "content", "text": "Teal is "}, {"type": "content", "text": "blue-green."},
          {"type": "turn_end", "steps": 2, "ended_by": "answer"}]


class KeepingStringIO(io.StringIO):
    def close(self):              # the API closes the agent's stdin after the request; keep it readable
        self.kept = self.getvalue()
        super().close()


class FakeProc:
    def __init__(self):
        self.stdin = KeepingStringIO()
        self.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in EVENTS))
        self.pid = 0

    def poll(self):
        return 0


def fake_run_agent(name, kind, args, interactive, driver=None, **kw):
    fake_run_agent.args = args
    proc = FakeProc()
    driver(proc)
    fake_run_agent.request = json.loads(proc.stdin.kept)
    return 0


class ApiTest(unittest.TestCase):
    def setUp(self):
        patches = [mock.patch.object(api, "secrets_env", return_value={"SHAFT_API_KEY": KEY, "SHAFT_API_LOGINS": ME}),
                   mock.patch.object(api, "instance_names", return_value=["shaft", "wren"]),
                   mock.patch.object(api, "run_agent", fake_run_agent),
                   mock.patch.object(api, "busy_with", return_value=None),
                   mock.patch.object(api, "instance_root", lambda n: self.roots / n),
                   mock.patch.object(api, "log", lambda *_: None),
                   mock.patch.dict("os.environ", {"SHAFT_API_LOGINS": ""})]
        import shutil
        import tempfile
        from pathlib import Path
        self.roots = Path(tempfile.mkdtemp(prefix="shaft-api-")).resolve()
        self.addCleanup(shutil.rmtree, self.roots, True)
        for n in ("shaft", "wren"):
            (self.roots / n).mkdir()
        patches.append(mock.patch.object(api.tom, "SUP_DIR", self.roots / ".supervisor"))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        api.Handler.conversations = api.Conversations()
        api.Handler.live = api.Live()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def req(self, path, body=None, key=KEY, login=ME):
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        if login:
            headers["Tailscale-User-Login"] = login
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body else None, headers=headers)
        return urllib.request.urlopen(r, timeout=10)

    def test_chat_and_interject_record_operator_text_and_channel(self):
        for channel in ("web", "telegram", "api"):
            with self.subTest(channel=channel):
                body = {"model": "shaft", "messages": [{"role": "user", "content": "old"},
                        {"role": "assistant", "content": "reply"}, {"role": "user", "content": [
                            {"type": "text", "text": "Yes,"}, {"type": "image_url", "image_url": {}},
                            {"type": "text", "text": "go ahead."}]}]}
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}",
                           "Tailscale-User-Login": ME, "X-Shaft-Channel": channel}
                for route in ("/chat/completions", "/chat/interject"):
                    with mock.patch.object(api.Handler.live, "interject", return_value=True):
                        r = urllib.request.Request(self.base + route, data=json.dumps(body).encode(), headers=headers)
                        json.load(urllib.request.urlopen(r, timeout=10))
                rows = api.tom._rows("shaft", "-operator-said")
                self.assertEqual([(r["text"], r["channel"]) for r in rows[-2:]],
                                 [("Yes,\ngo ahead.", channel)] * 2)
                self.assertEqual(len(rows), 2 * (("web", "telegram", "api").index(channel) + 1))

    def test_empty_chat_and_rejected_interject_are_not_recorded(self):
        self.req("/chat/completions", {"model": "shaft", "messages": [{"role": "user", "content": "  "}]}).read()
        with self.assertRaises(urllib.error.HTTPError):
            self.req("/chat/interject", {"model": "wren", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(api.tom._rows("shaft", "-operator-said"), [])
        self.assertEqual(api.tom._rows("wren", "-operator-said"), [])

    def test_only_the_owner_with_the_key_gets_in(self):
        for key, login, code in ((None, ME, 401), ("wrong", ME, 401), (KEY, "someone@example.com", 403), (KEY, None, 403)):
            with self.subTest(key=key, login=login):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.req("/models", key=key, login=login)
                self.assertEqual(ctx.exception.code, code)
        models = json.load(self.req("/models"))
        self.assertEqual([m["id"] for m in models["data"]], ["shaft", "wren"])

    def test_streamed_chat_is_openai_shaped(self):
        body = {"model": "shaft", "stream": True, "messages": [{"role": "system", "content": "be nice"},
                                                               {"role": "user", "content": "what is teal?"}]}
        raw = self.req("/chat/completions", body).read().decode()
        chunks = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]
        self.assertTrue(raw.rstrip().endswith("data: [DONE]"))
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        notes = "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks)
        self.assertEqual(text, "Teal is blue-green.")
        self.assertIn("⚙ web_search", notes)
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(fake_run_agent.request["messages"][-1]["content"], "what is teal?")
        self.assertTrue(fake_run_agent.request["talk_id"].startswith("api-"))

    def test_a_request_sent_again_with_its_id_is_answered_without_running_again(self):
        """Codex on e77d5e5: Telegram replays an update after a restart, and its request ran a second reply. With
        X-Shaft-Request-Id the API answers a request it has answered already from that answer."""
        import tempfile
        from pathlib import Path
        p = mock.patch.object(api, "ANSWERED", Path(tempfile.mkdtemp(prefix="shaft-api-ans-")) / "answered.json",
                              create=True)
        p.start()
        self.addCleanup(p.stop)
        api.Handler.answered = None
        runs = []

        def counting(*a, **k):
            runs.append(1)
            return fake_run_agent(*a, **k)

        def ask(rid):
            body = {"model": "shaft", "messages": [{"role": "user", "content": "what is teal?"}]}
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}",
                       "Tailscale-User-Login": ME, "X-Shaft-Request-Id": rid, "X-Shaft-Channel": "telegram"}
            r = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
            return json.load(urllib.request.urlopen(r, timeout=10))
        with mock.patch.object(api, "run_agent", counting):
            first, again = ask("telegram-shaft-7"), ask("telegram-shaft-7")
            other = ask("telegram-shaft-8")
        self.assertEqual([(r["text"], r["channel"]) for r in api.tom._rows("shaft", "-operator-said")],
                         [("what is teal?", "telegram")] * 2)
        self.assertEqual(len(runs), 2, "the replayed request ran again")
        self.assertEqual(again["choices"][0]["message"]["content"], first["choices"][0]["message"]["content"])
        self.assertEqual(other["choices"][0]["message"]["content"], "Teal is blue-green.")

    NARRATED = [{"type": "ready", "name": "wren"}, {"type": "status", "phase": "waiting for the model"},
                {"type": "content", "text": "Let me check my notes "}, {"type": "content", "text": "on teal first."},
                {"type": "usage", "prompt_tokens": 9, "completion_tokens": 3},
                {"type": "tool_start", "name": "search_memory", "args": '{"query": "teal"}'},
                {"type": "tool_end", "name": "search_memory", "ok": True, "brief": "ok"},
                {"type": "content", "text": "Teal is "}, {"type": "content", "text": "blue-green."},
                {"type": "turn_end", "steps": 2, "ended_by": "answer"}]

    def test_only_the_last_step_is_the_reply_narration_goes_to_reasoning(self):
        # Regression (the operator, 2026-10-14: "on Telegram I get more than what he actually says"): every step's text
        # was joined into the reply, so what the instance wrote before calling a tool reached Telegram as if said.
        with mock.patch(f"{__name__}.EVENTS", self.NARRATED):
            out = json.load(self.req("/chat/completions", {"model": "wren", "messages": [{"role": "user", "content": "teal?"}]}))
        msg = out["choices"][0]["message"]
        self.assertEqual(msg["content"], "Teal is blue-green.")             # what Telegram sends: the answer only
        self.assertNotIn("Let me check", msg["content"])
        self.assertIn("Let me check my notes on teal first.", msg["reasoning_content"])
        self.assertIn("⚙ search_memory", msg["reasoning_content"])

    def test_streamed_narration_is_reasoning_and_the_answer_is_content(self):
        with mock.patch(f"{__name__}.EVENTS", self.NARRATED):
            raw = self.req("/chat/completions", {"model": "wren", "stream": True,
                                                 "messages": [{"role": "user", "content": "teal?"}]}).read().decode()
        chunks = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]
        text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
        notes = "".join(c["choices"][0]["delta"].get("reasoning_content", "") for c in chunks)
        self.assertEqual(text, "Teal is blue-green.")
        self.assertIn("Let me check my notes on teal first.", notes)

    def test_the_instance_is_told_only_its_last_step_reaches_the_operator(self):
        # Regression (2026-10-22/23, "your message got cut off"): never told the rule above, instances wrote the answer
        # alongside a tool call (remember, append_file) and ended with "Stored." or "." — all the operator got.
        system = [{"role": "system", "content": "be nice"}]
        body = {"model": "wren", "messages": [*system, {"role": "user", "content": "teal?"}]}
        self.req("/chat/completions", body).read()
        first = fake_run_agent.request
        rules = [m["content"] for m in first["messages"] if m["role"] == "system"]
        self.assertEqual(rules[0], "be nice")                                # the app's own rules come first, kept
        self.assertEqual(rules[1:], [api.LAST_STEP_NOTE])
        self.assertEqual(first["messages"][-1], {"role": "user", "content": "teal?"})
        body["messages"] += [{"role": "assistant", "content": "blue-green"}, {"role": "user", "content": "sure?"}]
        self.req("/chat/completions", body).read()
        self.assertEqual(fake_run_agent.request["talk_id"], first["talk_id"])   # still the same conversation
        self.assertEqual([m["content"] for m in fake_run_agent.request["messages"] if m["role"] == "system"],
                         ["be nice", api.LAST_STEP_NOTE])                    # once, every turn

    def test_a_reply_that_stops_before_answering_says_so(self):
        steps = [e for e in self.NARRATED[:7]] + [{"type": "turn_end", "steps": 30, "ended_by": "steps"}]
        with mock.patch(f"{__name__}.EVENTS", steps):
            out = json.load(self.req("/chat/completions", {"model": "wren", "messages": [{"role": "user", "content": "teal?"}]}))
        content = out["choices"][0]["message"]["content"]
        self.assertEqual(content, "(no answer: the reply stopped after 30 steps — steps)")
        self.assertNotIn("Let me check", content)                           # the narration is not passed off as one

    def test_thelos_error_overwrite_2026_10_28(self):
        message = "talk: reply cut off at max_tokens=16384 twice (completion_tokens=16384); nothing saved"

        def run_agent(name, kind, args, interactive, driver=None, **kw):
            proc = FakeProc()
            proc.stdout = io.StringIO("".join(json.dumps(e) + "\n" for e in errors))
            proc.stderr = io.StringIO("")
            driver(proc)
            return 0

        for stream in (True, False):
            for later_error in (False, True):
                with self.subTest(stream=stream, later_error=later_error):
                    errors = [{"type": "error", "message": message}]
                    if later_error:
                        errors.append({"type": "error", "message": "later failure"})
                    body = {"model": "shaft", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}
                    with mock.patch.object(api, "run_agent", run_agent):
                        if stream:
                            raw = self.req("/chat/completions", body).read().decode()
                        else:
                            with self.assertRaises(urllib.error.HTTPError) as ctx:
                                self.req("/chat/completions", body)
                            self.assertEqual(ctx.exception.code, 502)
                            raw = ctx.exception.read().decode()
                    self.assertIn("cut off at max_tokens=16384 twice", raw)
                    self.assertNotIn("stopped without answering", raw)
                    self.assertNotIn("later failure", raw)

    def test_agent_without_content_or_error_still_says_stopped_without_answering(self):
        def run_agent(name, kind, args, interactive, driver=None, **kw):
            proc = FakeProc()
            proc.stdout = io.StringIO("")
            proc.stderr = io.StringIO("")
            driver(proc)
            return 0

        for stream in (True, False):
            with self.subTest(stream=stream), mock.patch.object(api, "run_agent", run_agent):
                body = {"model": "shaft", "stream": stream, "messages": [{"role": "user", "content": "hi"}]}
                if stream:
                    raw = self.req("/chat/completions", body).read().decode()
                else:
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        self.req("/chat/completions", body)
                    self.assertEqual(ctx.exception.code, 502)
                    raw = ctx.exception.read().decode()
                self.assertIn("shaft stopped without answering (exit 0)", raw)

    def test_plain_chat_and_same_conversation_keeps_its_id(self):
        body = {"model": "shaft", "messages": [{"role": "user", "content": "hi"}]}
        out = json.load(self.req("/chat/completions", body))
        self.assertEqual(out["choices"][0]["message"]["content"], "Teal is blue-green.")
        first = fake_run_agent.request["talk_id"]
        body["messages"] += [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}]
        self.req("/chat/completions", body).read()
        self.assertEqual(fake_run_agent.request["talk_id"], first)

    def test_request_whose_client_left_while_waiting_never_runs(self):
        # Regression: my test request waited behind the operator's conversation; after the client gave up,
        # the queued run would still have happened later, with nobody listening.
        import http.client
        import time
        busy = {"value": "talk since 22:04"}
        calls = []
        with mock.patch.object(api, "busy_with", side_effect=lambda n: busy["value"]), \
             mock.patch.object(api, "run_agent", side_effect=lambda *a, **k: calls.append(1)):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
            conn.request("POST", "/v1/chat/completions", json.dumps(
                {"model": "shaft", "stream": True, "messages": [{"role": "user", "content": "hi"}]}),
                {"Authorization": f"Bearer {KEY}", "Tailscale-User-Login": ME, "Content-Type": "application/json"})
            resp = conn.getresponse()
            first = resp.fp.readline() + resp.fp.readline()        # role chunk
            status = b""
            while b"waiting" not in status:
                status += resp.fp.readline()
            self.assertIn(b"another conversation", status)
            resp.close()                                           # the client gives up
            conn.close()
            time.sleep(3)                                          # noticed within a second
            busy["value"] = None                                   # the conversation ends
            time.sleep(2.5)
        self.assertEqual(calls, [])

    def test_the_agent_accepts_the_arguments_the_api_sends(self):
        # Regression: the API ran `answer --json`, the agent's parser rejected --json (exit 2), and clients
        # got "the model returned an empty response". The fake agent above hid it; ask the real parser.
        import subprocess
        import sys
        from pathlib import Path
        self.req("/chat/completions", {"model": "shaft", "messages": [{"role": "user", "content": "x"}]}).read()
        import tempfile
        template = Path(__file__).resolve().parents[2] / "template"
        empty = tempfile.mkdtemp(prefix="shaft-args-")      # no config: it stops after parsing the arguments
        r = subprocess.run([sys.executable, "-E", "-s", "-m", "shaft", *fake_run_agent.args], cwd=template,
                           env={"SHAFT_ROOT": empty, "PATH": "/usr/bin:/bin"}, input="{}",
                           capture_output=True, text=True)
        for rejection in ("unrecognized arguments", "invalid choice", "the following arguments are required"):
            self.assertNotIn(rejection, r.stderr)
        self.assertIn("config error", r.stderr)               # it got past argument parsing

    def test_unknown_model_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.req("/chat/completions", {"model": "nobody", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(ctx.exception.code, 404)


class InterjectTest(unittest.TestCase):
    """the operator, 2026-10-16: "When I send a message via telegram while the instance is still working on my previous
    message, it should be injected immediately into it." Before, the second message waited for the whole first reply
    and then got a reply of its own; the reply in progress never saw it (docs/plans/PLAN-interject.md)."""

    WAIT = object()

    req = ApiTest.req

    def setUp(self):
        ApiTest.setUp(self)
        self.pre, self.mid, self.go = threading.Event(), threading.Event(), threading.Event()
        self.seen_file = []
        test = self

        class Lines:                             # the agent's stdout, stopping at WAIT until the test says go
            def __init__(self, events):
                self.events = events

            def __iter__(self):
                for e in self.events:
                    if e is test.WAIT:
                        test.mid.set()
                        test.go.wait(10)
                        continue
                    yield json.dumps(e) + "\n"

        def run_agent(name, kind, args, interactive, driver=None, **kw):
            self.pre.wait(10)                    # still waiting for the instance (another conversation)
            proc = FakeProc()
            proc.stdout = Lines(self.events)
            driver(proc)
            path = self.roots / name / ".tmp"
            self.seen_file += [l for f in path.glob("interject-*.jsonl") for l in f.read_text().splitlines()]
            return 0

        p = mock.patch.object(api, "run_agent", run_agent)
        p.start()
        self.addCleanup(p.stop)
        self.events = [{"type": "ready"}, {"type": "tool_start", "name": "calendar", "args": "{}"}, self.WAIT,
                       {"type": "tool_end", "name": "calendar", "ok": True, "brief": "ok"},
                       {"type": "heard", "text": "one"}, {"type": "heard", "text": "two"},
                       {"type": "content", "text": "Both done."}, {"type": "turn_end", "steps": 2, "ended_by": "answer"}]

    def chat(self, out):
        body = {"model": "wren", "messages": [{"role": "user", "content": "check my calendar"}]}
        out.append(json.load(self.req("/chat/completions", body)))

    def interject(self, text):
        return self.req("/chat/interject", {"model": "wren", "messages": [
            {"role": "user", "content": "check my calendar"}, {"role": "user", "content": text}]})

    def test_a_message_during_the_reply_reaches_the_running_agent_in_order(self):
        out = []
        t = threading.Thread(target=self.chat, args=(out,))
        t.start()
        for _ in range(100):                     # the reply is registered before its agent starts
            if api.Handler.live.replies:
                break
            import time
            time.sleep(0.05)
        self.assertEqual(self.interject("one").status, 202)        # held: the agent has not started yet
        self.pre.set()
        self.assertTrue(self.mid.wait(10))
        self.assertEqual(self.interject("two").status, 202)        # written while it runs
        talk_id = next(iter(api.Handler.live.replies))
        lines = (self.roots / "wren" / ".tmp" / f"interject-{talk_id}.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l)["text"] for l in lines], ["one", "two"])
        self.go.set()
        t.join(10)
        msg = out[0]["choices"][0]["message"]
        self.assertEqual(msg["content"], "Both done.")
        self.assertEqual(out[0]["interjections_heard"], 2)          # Telegram puts both before the answer
        self.assertIn("read your new message: one", msg["reasoning_content"])
        self.assertEqual(list((self.roots / "wren" / ".tmp").glob("interject-*")), [])   # removed with the reply
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.interject("three")                                 # nothing running: the client sends it anew
        self.assertEqual(ctx.exception.code, 409)

    def test_no_reply_running_is_409_and_an_empty_message_is_refused(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.interject("hello?")
        self.assertEqual(ctx.exception.code, 409)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.interject("   ")
        self.assertEqual(ctx.exception.code, 400)

    def test_a_crashed_runs_leftover_file_is_not_read_by_the_next_reply(self):
        self.pre.set()
        self.go.set()
        body = {"model": "wren", "messages": [{"role": "user", "content": "check my calendar"}]}
        talk_id = api.Handler.conversations.talk_id("wren", body["messages"])
        stale = self.roots / "wren" / ".tmp" / f"interject-{talk_id}.jsonl"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text(json.dumps({"text": "from a reply that crashed"}) + "\n")
        json.load(self.req("/chat/completions", body))
        self.assertEqual(self.seen_file, [])


class LongReplyTest(unittest.TestCase):
    """B12 and B19 (review 2026-10-24): the idle write-up counted from the request alone, and two requests in one talk
    deleted each other's interjections."""

    MESSAGES = [{"role": "user", "content": "run the long job"}]

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp(prefix="shaft-api-long-")).resolve()
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "wren" / ".tmp").mkdir(parents=True)
        self.recovered = []
        for p in (mock.patch.object(api, "TALKS", tmp / "api_talks.json"),
                  mock.patch.object(api, "instance_names", return_value=["wren"]),
                  mock.patch.object(api, "instance_root", lambda n: tmp / n),
                  mock.patch.object(api, "spawn_recover", self.recovered.append),
                  mock.patch.object(api, "log", lambda *_: None)):
            p.start()
            self.addCleanup(p.stop)
        self.file = tmp / "wren" / ".tmp"
        self.conversations, self.live = api.Conversations(), api.Live()

    def talk(self) -> dict:
        return next(iter(self.conversations.talks.values()))

    def test_a_reply_longer_than_the_idle_time_is_not_written_up_under_it(self):
        talk_id = self.conversations.talk_id("wren", self.MESSAGES)
        entry = self.live.begin("wren", talk_id)
        self.talk()["last"] -= api.IDLE_MINUTES * 60 + 5             # the request came in longer ago than that
        self.conversations.write_up_idle(self.live.running)
        self.assertFalse(self.talk()["written"], "written up while its reply was still running")
        self.conversations.replied(talk_id)                         # the reply ends now (work()'s finally)
        self.live.end(entry)
        self.conversations.write_up_idle(self.live.running)
        self.assertFalse(self.talk()["written"], "the quiet time must start when the reply ended")
        self.assertEqual(self.recovered, [])
        self.talk()["ended"] -= api.IDLE_MINUTES * 60 + 5            # then quiet for SHAFT_IDLE_MINUTES: written up
        self.conversations.write_up_idle(self.live.running)
        self.assertTrue(self.talk()["written"])
        self.assertEqual(self.recovered, ["wren"])

    def lines(self, talk_id: str) -> list[str]:
        f = self.file / f"interject-{talk_id}.jsonl"
        return [json.loads(l)["text"] for l in f.read_text().splitlines()] if f.exists() else []

    def test_overlapping_requests_in_one_talk_keep_each_others_messages(self):
        talk_id = self.conversations.talk_id("wren", self.MESSAGES)
        first = self.live.begin("wren", talk_id)
        self.live.started(first)
        self.assertTrue(self.live.interject(talk_id, "one"))
        second = self.live.begin("wren", talk_id)                   # waits for the instance while the first replies
        self.assertEqual(self.lines(talk_id), ["one"], "the second request deleted them")
        self.assertTrue(self.live.interject(talk_id, "two"))         # the reply running now hears it
        self.assertEqual(self.lines(talk_id), ["one", "two"])
        self.live.started(second)          # the run lock passed on: the first's agent is gone, its client resends
        self.assertTrue(self.live.interject(talk_id, "three"))
        self.live.end(first)                                        # its request ends after the second started
        self.assertEqual(self.lines(talk_id), ["three"], "the first request's end deleted them")
        self.live.end(second)
        self.assertEqual(self.lines(talk_id), [])

    def test_a_cancelled_newer_request_leaves_the_running_reply_registered(self):
        """Codex on e77d5e5: A replies; B, a retry in the same talk, waits and its client hangs up. end(B) removed the
        registration (B was the newest) and the file while A still ran: running() was empty, the janitor could
        close A's conversation, and A's interjections were gone."""
        talk_id = self.conversations.talk_id("wren", self.MESSAGES)
        first = self.live.begin("wren", talk_id)
        self.live.started(first)
        self.assertTrue(self.live.interject(talk_id, "one"))
        second = self.live.begin("wren", talk_id)
        self.live.end(second)                                       # cancelled while it waited
        self.assertEqual(self.live.running(), {talk_id})
        self.assertEqual(self.lines(talk_id), ["one"])
        self.assertTrue(self.live.interject(talk_id, "two"))
        self.assertEqual(self.lines(talk_id), ["one", "two"])
        self.talk()["last"] -= api.IDLE_MINUTES * 60 + 5
        self.conversations.write_up_idle(self.live.running)
        self.assertFalse(self.talk()["written"], "written up under the reply still running")
        self.live.end(first)
        self.assertEqual((self.live.running(), self.lines(talk_id)), (set(), []))

    def test_messages_sent_before_any_agent_starts_reach_the_one_that_does(self):
        talk_id = self.conversations.talk_id("wren", self.MESSAGES)
        first, second = self.live.begin("wren", talk_id), self.live.begin("wren", talk_id)
        self.assertTrue(self.live.interject(talk_id, "x"))
        self.live.end(first)                                        # cancelled before it started: "x" is not lost
        self.assertTrue(self.live.interject(talk_id, "y"))
        self.live.started(second)
        self.assertEqual(self.lines(talk_id), ["x", "y"])
        self.live.end(second)
        older, newer = self.live.begin("wren", talk_id), self.live.begin("wren", talk_id)   # twins, equal as dicts
        self.live.end(newer)                                        # the newer one cancelled, not the older
        self.live.started(older)
        self.live.end(older)
        self.assertEqual(self.live.running(), set(), "the older request was the one removed")


INBOX_KEY = "sk-shaft-inbox-test"


class InboxDoorbellTest(unittest.TestCase):
    """POST /v1/inbox/<instance> (wren's "doorbell", the operator 2026-10-13): anything that can POST on the tailnet
    drops a message in an instance's inbox, marked as outside content, rate-limited, optionally waking it."""

    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path
        from supervisor import autonomy, messages
        self.project = Path(tempfile.mkdtemp(prefix="shaft-inbox-")).resolve()
        self.addCleanup(shutil.rmtree, self.project, True)
        self.sup = self.project / ".supervisor"
        self.sup.mkdir()
        for n in ("shaft", "wren"):
            (self.project / "instances" / n / "inbox").mkdir(parents=True)
        self.wakes = []
        env = {"SHAFT_API_KEY": KEY, "SHAFT_API_LOGINS": ME, "SHAFT_INBOX_KEY": INBOX_KEY}
        patches = [mock.patch.object(api, "secrets_env", return_value=env),
                   mock.patch.object(api, "instance_names", return_value=["shaft", "wren"]),
                   mock.patch.object(api, "SUP_DIR", self.sup),
                   mock.patch.object(messages, "SUP_DIR", self.sup),
                   mock.patch.object(messages, "INSTANCES", self.project / "instances"),
                   mock.patch.object(messages, "instance_root", lambda n: self.project / "instances" / n),
                   mock.patch.object(messages, "log", lambda *_: None),
                   mock.patch.object(api, "log", lambda *_: None),
                   mock.patch.object(autonomy, "start_wake", self._wake),
                   mock.patch.dict("os.environ", {"SHAFT_API_LOGINS": ""})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        messages._cache.update(key=None, rows=[])
        api.Handler.conversations = api.Conversations()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), api.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.busy = ""

    def _wake(self, name):
        if self.busy:
            raise RuntimeError(f"{name} is already awake ({self.busy})")
        self.wakes.append(name)
        return f"started a cycle for {name}"

    def post(self, name, body, key=INBOX_KEY, headers=None, raw=False):
        h = {"Content-Type": "text/plain" if raw else "application/json", **(headers or {})}
        if key:
            h["Authorization"] = f"Bearer {key}"
        data = body.encode() if raw else json.dumps(body).encode()
        return json.load(urllib.request.urlopen(urllib.request.Request(f"{self.base}/inbox/{name}", data=data,
                                                                       headers=h), timeout=10))

    def inbox(self, name="wren"):
        return [p.read_text() for p in sorted((self.project / "instances" / name / "inbox").glob("*.md"))]

    def test_the_inbox_key_drops_a_marked_message_and_nothing_else(self):
        r = self.post("wren", {"text": "CI on main is red", "source": "ci"})
        self.assertEqual((r["instance"], r["source"]), ("wren", "ci"))
        (msg,) = self.inbox()
        self.assertIn("From: ci (outside SHAFT", msg)
        self.assertIn("Trust: outside content", msg)
        self.assertIn("CI on main is red", msg)
        from supervisor import messages
        self.assertEqual([(m["from"], m["to"], m["kind"]) for m in messages.history()], [("ci", "wren", "external")])
        # the inbox key cannot chat with or list the instances
        for path, body in (("/models", None), ("/chat/completions", {"model": "wren", "messages": [
                {"role": "user", "content": "x"}]})):
            r = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body else None,
                                       headers={"Authorization": f"Bearer {INBOX_KEY}", "Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(r, timeout=10)
            self.assertEqual(ctx.exception.code, 401)

    def test_no_key_or_a_wrong_one_writes_nothing(self):
        for key in (None, "wrong", KEY):            # the chat key needs an allowed tailnet login as well
            with self.subTest(key=key):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.post("wren", {"text": "hi"}, key=key)
                self.assertIn(ctx.exception.code, (401, 403))
        self.assertEqual(self.inbox(), [])
        r = self.post("wren", {"text": "from me"}, key=KEY, headers={"Tailscale-User-Login": ME})
        self.assertEqual(r["source"], "outside")

    def test_any_body_is_kept_and_github_names_itself(self):
        self.post("wren", {"action": "opened", "issue": {"title": "Crash on start"}},
                  headers={"X-GitHub-Event": "issues"})
        self.post("wren", "plain text from a shortcut", raw=True, headers={"X-Shaft-Source": "phone"})
        a, b = self.inbox()
        self.assertIn("From: github:issues", a)
        self.assertIn('"title": "Crash on start"', a)
        self.assertIn("From: phone", b)

    def test_bad_requests(self):
        for name, body, code in (("nobody", {"text": "x"}, 404), ("wren", {"text": "  "}, 400),
                                 ("wren", {"text": "x", "source": "../../etc"}, 400)):
            with self.subTest(name=name, body=body):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.post(name, body)
                self.assertEqual(ctx.exception.code, code)
        self.assertEqual(self.inbox(), [])

    def test_a_long_message_is_cut(self):
        from supervisor import messages
        self.post("wren", {"text": "x" * (messages.EXTERNAL_MAX_CHARS + 500)})
        self.assertIn(f"[… cut at {messages.EXTERNAL_MAX_CHARS} characters]", self.inbox()[0])

    def test_wake_now_if_idle_and_within_the_hourly_limit(self):
        self.assertEqual(self.post("wren", {"text": "a"})["wake"][:15], "not asked to wa")
        self.assertEqual(self.post("wren", {"text": "b", "wake": True})["wake"], "started a cycle for wren")
        self.busy = "live since 21:00"
        self.assertIn("already awake", self.post("wren", {"text": "c", "wake": True})["wake"])
        self.busy = ""
        with mock.patch.object(api, "INBOX_WAKES_PER_HOUR", 2):
            self.assertEqual(self.post("wren", {"text": "d"}, headers={"X-Shaft-Wake": "1"})["wake"],
                             "started a cycle for wren")
            self.assertIn("2 doorbell wakes in the last hour", self.post("wren", {"text": "e", "wake": True})["wake"])
        self.assertEqual(self.wakes, ["wren", "wren"])
        self.assertEqual(len(self.inbox()), 5)                  # every message delivered, woken or not

    def test_messages_per_hour_are_limited(self):
        with mock.patch.object(api, "INBOX_PER_HOUR", 2):
            self.post("wren", {"text": "1"})
            self.post("wren", {"text": "2"})
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self.post("wren", {"text": "3"})
            self.assertEqual(ctx.exception.code, 429)
            self.post("shaft", {"text": "other instance, own limit"})
        self.assertEqual(len(self.inbox()), 2)

    def test_inbox_key_command_creates_the_key_once(self):
        """Also the bug where api._set_secret used SUP_DIR without importing it (NameError on a first `api on`)."""
        env = {}
        with mock.patch.object(api, "secrets_env", lambda: env), mock.patch.object(api, "url", return_value="u/v1"), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(api.command("inbox-key"), 0)
        key = out.getvalue().strip()
        self.assertTrue(key.startswith("sk-shaft-inbox-"))
        self.assertIn(f"SHAFT_INBOX_KEY={key}", (self.project / ".env").read_text())
        self.assertEqual(oct((self.project / ".env").stat().st_mode & 0o777), "0o600")




class ConversationIdTest(unittest.TestCase):
    """X-Shaft-Conversation and /v1/chat/end (docs/plans/PLAN-webui.md M4): a client that names its conversations never has two
    merged because they opened the same way, and can close one now."""
    setUp = ApiTest.setUp                                        # its server, not its tests

    def post(self, path, body, cid=None):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}", "Tailscale-User-Login": ME}
        if cid:
            headers["X-Shaft-Conversation"] = cid
        r = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def chat(self, cid=None, text="hi"):
        status, _ = self.post("/chat/completions", {"model": "wren", "messages": [{"role": "user", "content": text}]},
                              cid)
        self.assertEqual(status, 200)
        return fake_run_agent.request["talk_id"]

    def test_two_named_chats_that_open_alike_are_two_conversations(self):
        a, b = self.chat("web-a"), self.chat("web-b")
        self.assertNotEqual(a, b)
        self.assertEqual(self.chat("web-a"), a)                 # the same name: the same conversation
        self.assertNotIn(self.chat(None), (a, b))                # unnamed: keyed by its first message, as before

    def test_end_closes_that_conversation_and_writes_it_up(self):
        a, b = self.chat("web-a"), self.chat("web-b")
        with mock.patch.object(api, "spawn_recover") as recover:
            status, body = self.post("/chat/end", {"model": "wren"}, "web-a")
        self.assertEqual((status, body["ended"]), (202, a))
        recover.assert_called_once_with("wren")
        self.assertNotEqual(self.chat("web-a"), a)               # its next message starts a new conversation
        self.assertEqual(self.chat("web-b"), b)                  # the other is untouched
        with mock.patch.object(api, "spawn_recover"):
            self.assertEqual(self.post("/chat/end", {"model": "wren"}, "nobody")[0], 404)

    def test_end_is_refused_while_it_replies(self):
        a = self.chat("web-a")
        api.Handler.live.replies[a] = {"model": "wren", "talk_id": a, "held": [], "started": True}
        self.addCleanup(api.Handler.live.replies.pop, a, None)
        with mock.patch.object(api, "spawn_recover") as recover:
            status, body = self.post("/chat/end", {"model": "wren"}, "web-a")
        self.assertEqual(status, 409)
        self.assertIn("still replying", body["error"]["message"])
        recover.assert_not_called()

    def test_a_malformed_name_is_refused(self):
        status, _ = self.post("/chat/completions", {"model": "wren", "messages": [{"role": "user", "content": "x"}]},
                              "../../etc")
        self.assertEqual(status, 400)


class StreamHeardTest(unittest.TestCase):
    setUp = ApiTest.setUp

    def test_the_last_stream_chunk_says_how_many_interjections_were_heard(self):
        body = {"model": "wren", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        r = urllib.request.Request(self.base + "/chat/completions", data=json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}",
                                            "Tailscale-User-Login": ME})
        with urllib.request.urlopen(r, timeout=10) as resp:
            chunks = [json.loads(l[6:]) for l in resp.read().decode().splitlines()
                      if l.startswith("data: {")]
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(chunks[-1]["interjections_heard"], 0)


class OutsideTheGuiSessionTest(unittest.TestCase):
    """The Mac app's Tailscale CLI exits 0 and prints this on stdout outside the GUI session, where launchd jobs run."""
    REFUSED = ("The Tailscale GUI failed to start: The operation couldn’t be completed. "
               "(Tailscale.CLIError error 3.)\n")
    STATUS = json.dumps({"Self": {"DNSName": "mac.example.ts.net."}})

    def answers(self, *a):
        return subprocess.CompletedProcess(a, 0, stdout=self.REFUSED, stderr="")

    def test_api_on_outside_the_gui_session_does_not_crash_after_restarting_20261105(self):
        """The app's Restart API restarted it, then `url()` died parsing this message: a working restart read as failed."""
        out = io.StringIO()
        with mock.patch.object(api, "_tailscale", side_effect=self.answers), \
                mock.patch.object(api, "secrets_env", return_value={
                    "SHAFT_API_KEY": "k", "SHAFT_INBOX_KEY": "i", "SHAFT_API_LOGINS": "me"}), \
                mock.patch("supervisor.autonomy.service", return_value=0) as service, \
                mock.patch("supervisor.autonomy.running", return_value=True), \
                mock.patch.object(api, "log") as logged, contextlib.redirect_stdout(out):
            self.assertEqual(api.command("on"), 0)
        service.assert_called_once_with("api", "on")
        said = " ".join(str(c.args[0]) for c in logged.call_args_list)
        self.assertNotIn("api exposed", said)
        self.assertIn("left as it was", said)
        self.assertIn("API restarted", out.getvalue())

    def test_a_first_start_outside_the_gui_session_is_not_reported_as_exposed(self):
        err = io.StringIO()
        with mock.patch.object(api, "_tailscale", side_effect=self.answers), \
                mock.patch.object(api, "secrets_env", return_value={
                    "SHAFT_API_KEY": "k", "SHAFT_INBOX_KEY": "i", "SHAFT_API_LOGINS": "me"}), \
                mock.patch("supervisor.autonomy.running", return_value=False), \
                mock.patch("supervisor.autonomy.service", return_value=0) as service, \
                mock.patch.object(api, "log") as logged, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(api.command("on"), 1)
        service.assert_called_once_with("api", "on")
        self.assertIn("API started but is not exposed on the tailnet", err.getvalue())
        self.assertIn("CLI refuses to run outside the GUI session", err.getvalue())
        self.assertIn("shaft api on", err.getvalue())
        self.assertIn("terminal of the logged-in session", err.getvalue())
        logged.assert_called_once_with(err.getvalue().strip())

    def test_url_with_tailscales_refusal_says_so(self):
        with mock.patch.object(api, "_tailscale", side_effect=self.answers):
            self.assertEqual(api.url(), "(the tailnet address is not available from this session)")

    def test_a_normal_status_still_gives_the_address_and_exposes_it(self):
        def answers(*a):
            return subprocess.CompletedProcess(a, 0, stdout=self.STATUS if "status" in a else "", stderr="")
        with mock.patch.object(api, "_tailscale", side_effect=answers), \
                mock.patch.object(api, "secrets_env", return_value={
                    "SHAFT_API_KEY": "k", "SHAFT_INBOX_KEY": "i", "SHAFT_API_LOGINS": "me"}), \
                mock.patch("supervisor.autonomy.service", return_value=0), \
                mock.patch("supervisor.autonomy.running", return_value=False), \
                mock.patch.object(api, "allowed_logins", return_value={"me"}), \
                mock.patch.object(api, "log") as logged, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api.command("on"), 0)
            self.assertEqual(api.url(), f"https://mac.example.ts.net:{api.HTTPS_PORT}/v1")
        self.assertIn("api exposed", " ".join(str(c.args[0]) for c in logged.call_args_list))

    def test_garbage_that_is_not_the_refusal_is_not_swallowed(self):
        def answers(*a):
            return subprocess.CompletedProcess(a, 0, stdout="not json", stderr="")
        with mock.patch.object(api, "_tailscale", side_effect=answers), self.assertRaises(ValueError):
            api.url()


if __name__ == "__main__":
    unittest.main()
