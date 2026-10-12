"""A scripted OpenAI-compatible server for black-box tests."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLM:
    """Each queued reply is a dict: content, finish_reason ('stop'), reasoning (''),
    tool_calls ([(name, args_dict), ...]) — tool calls imply finish_reason 'tool_calls'."""

    def __init__(self, replies: list[dict]):
        self.replies = list(replies)
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                r = fake.replies.pop(0) if fake.replies else {"content": "(fake: out of replies)"}
                content, reasoning = r.get("content", ""), r.get("reasoning", "")
                calls = [{"id": f"call_{i}", "type": "function",
                          "function": {"name": n, "arguments": json.dumps(a)}}
                         for i, (n, a) in enumerate(r.get("tool_calls", []))]
                finish = r.get("finish_reason", "tool_calls" if calls else "stop")
                usage = {"prompt_tokens": 10, "completion_tokens": 5}
                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [{"reasoning_content": reasoning}] if reasoning else []
                    chunks += [{"content": content[i:i + 7]} for i in range(0, len(content), 7)]
                    chunks += [{"tool_calls": [{"index": i, **c}]} for i, c in enumerate(calls)]
                    for delta in chunks:
                        self._sse({"choices": [{"delta": delta, "finish_reason": None}]})
                        time.sleep(r.get("delay", 0))   # slow streaming, to test interrupts
                    self._sse({"choices": [{"delta": {}, "finish_reason": finish}]})
                    self._sse({"choices": [], "usage": usage})
                    self.wfile.write(b"data: [DONE]\n\n")
                else:
                    payload = json.dumps({"choices": [{"message": {
                        "role": "assistant", "content": content, "reasoning_content": reasoning,
                        **({"tool_calls": calls} if calls else {})},
                        "finish_reason": finish}], "usage": usage}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            def _sse(self, obj):
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True   # a client that disconnects mid-stream must not keep a test alive
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
