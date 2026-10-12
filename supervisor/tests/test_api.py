"""The OpenAI-compatible API: who may use it, and that its output is what OpenAI clients expect."""
from __future__ import annotations

import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

from supervisor import api

KEY, ME = "sk-shaft-test", "daniele@example.com"
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
                   mock.patch.dict("os.environ", {"SHAFT_API_LOGINS": ""})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        api.Handler.conversations = api.Conversations()
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

    def test_only_the_owner_with_the_key_gets_in(self):
        for key, login, code in ((None, ME, 401), ("wrong", ME, 401), (KEY, "someone@else.com", 403), (KEY, None, 403)):
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

    def test_plain_chat_and_same_conversation_keeps_its_id(self):
        body = {"model": "shaft", "messages": [{"role": "user", "content": "hi"}]}
        out = json.load(self.req("/chat/completions", body))
        self.assertEqual(out["choices"][0]["message"]["content"], "Teal is blue-green.")
        first = fake_run_agent.request["talk_id"]
        body["messages"] += [{"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}]
        self.req("/chat/completions", body).read()
        self.assertEqual(fake_run_agent.request["talk_id"], first)

    def test_request_whose_client_left_while_waiting_never_runs(self):
        # Regression: my test request waited behind Daniele's conversation; after the client gave up,
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


if __name__ == "__main__":
    unittest.main()
