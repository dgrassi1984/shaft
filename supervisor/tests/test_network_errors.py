"""A reply cut short upstream is retried, and the broker always answers (wren's sleep, 2026-10-15 04:34).

OpenRouter stopped MiMo's chunked reply after 220 bytes. http.client raised IncompleteRead — an HTTPException, not an
OSError — so cloudllm's retry on (URLError, OSError) let it escape, the broker's model handler crashed without
answering, and the instance saw "Remote end closed connection without response": its sleep failed with nothing of the
digest saved. The same killed its 06:15 wake.
"""
from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from supervisor import broker, cloudllm, core

REPLY = {"id": "gen-1", "model": "xiaomi/mimo-v2.6-pro", "choices": [{"index": 0, "finish_reason": "stop",
         "message": {"role": "assistant", "content": "Consolidated."}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.0001}}


class CutShort:
    """An HTTP server whose first `cut` answers stop mid-body (chunked, then the socket closes); then whole answers."""

    def __init__(self, cut: int = 1, stream: bool = False):
        self.requests, self.cut, self.stream = 0, cut, stream
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/api/v1/chat/completions"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(65536)
                head, _, rest = data.partition(b"\r\n\r\n")
                length = int(next((l.split(b":")[1] for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")), b"0"))
                while len(rest) < length:
                    rest += conn.recv(65536)
                self.requests += 1
                ctype = b"text/event-stream" if self.stream else b"application/json"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: " + ctype + b"\r\nTransfer-Encoding: chunked\r\n\r\n")
                if self.stream:
                    first = b'data: {"choices": [{"index": 0, "delta": {"content": "Consol"}}]}\n\n'
                    conn.sendall(b"%x\r\n%s\r\n" % (len(first), first))
                    if self.requests <= self.cut:
                        conn.sendall(b"400\r\npartial")       # a chunk promised, never delivered
                        continue
                    conn.sendall(b"0\r\n\r\n")
                    continue
                body = json.dumps(REPLY).encode()
                if self.requests <= self.cut:
                    conn.sendall(b"%x\r\n%s" % (len(body), body[:220]))   # 220 bytes, then the socket closes
                    continue
                conn.sendall(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))

    def close(self):
        self.sock.close()


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-net-"))
        for p in (mock.patch.object(cloudllm, "USAGE", self.tmp / "cloud_usage.jsonl"),
                  mock.patch.object(cloudllm, "CLOUD_MODELS", ["openrouter/xiaomi/mimo-v2.6-pro"]),
                  mock.patch.object(cloudllm.time, "sleep", lambda s: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_incomplete_read_is_a_network_error(self):
        self.assertTrue(issubclass(http.client.IncompleteRead, core.NETWORK_ERRORS))
        self.assertTrue(issubclass(http.client.RemoteDisconnected, core.NETWORK_ERRORS))

    def test_a_reply_cut_short_is_asked_again(self):
        up = CutShort(cut=1)
        self.addCleanup(up.close)
        with mock.patch.object(cloudllm, "OPENROUTER_API", up.url):
            out = cloudllm.complete("wren", {"model": "openrouter/xiaomi/mimo-v2.6-pro",
                                             "messages": [{"role": "user", "content": "sleep"}]},
                                    {"OPENROUTER_API_KEY": "k"}, {})
        self.assertEqual(out["choices"][0]["message"]["content"], "Consolidated.")
        self.assertEqual(up.requests, 2)

    def test_cut_short_every_time_is_a_clean_error_not_a_crash(self):
        up = CutShort(cut=99)
        self.addCleanup(up.close)
        with mock.patch.object(cloudllm, "OPENROUTER_API", up.url), self.assertRaises(cloudllm.CloudError) as e:
            cloudllm.complete("wren", {"model": "openrouter/xiaomi/mimo-v2.6-pro",
                                       "messages": [{"role": "user", "content": "sleep"}]}, {"OPENROUTER_API_KEY": "k"}, {})
        self.assertEqual(e.exception.status, 502)
        self.assertIn("IncompleteRead", str(e.exception))
        self.assertEqual(up.requests, 3)


class BrokerAnswersTest(unittest.TestCase):
    """The instance must always get an HTTP answer it can act on: its llm.py retries a 502, a dropped socket is fatal."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-net-")).resolve()
        (self.tmp / "instances" / "wren").mkdir(parents=True)
        p = mock.patch.object(core, "SUP_DIR", self.tmp / "sup")
        p.start()
        self.addCleanup(p.stop)
        self.b = broker.Broker("wren", self.tmp, {"OPENROUTER_API_KEY": "k"}, self.tmp / "broker.log")
        self.url = self.b.start()
        self.addCleanup(self.b.stop)

    def post(self, body: dict):
        req = urllib.request.Request(f"{self.url}/llm/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"X-Shaft-Token": self.b.token, "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=20)

    def test_an_unexpected_failure_is_a_502_not_a_dropped_connection(self):
        with mock.patch.object(cloudllm, "complete", side_effect=http.client.IncompleteRead(b"x" * 220)), \
                self.assertRaises(urllib.error.HTTPError) as e:
            self.post({"model": "openrouter/xiaomi/mimo-v2.6-pro", "messages": []})
        self.assertEqual(e.exception.code, 502)
        self.assertIn("IncompleteRead", json.load(e.exception)["error"]["message"])
        self.assertIn("-> ERROR IncompleteRead", (self.tmp / "broker.log").read_text())

    def test_a_stream_broken_upstream_ends_with_an_error_event(self):
        up = CutShort(cut=1, stream=True)
        self.addCleanup(up.close)
        for p in (mock.patch.object(cloudllm, "OPENROUTER_API", up.url),
                  mock.patch.object(cloudllm, "CLOUD_MODELS", ["openrouter/xiaomi/mimo-v2.6-pro"]),
                  mock.patch.object(cloudllm, "USAGE", self.tmp / "cloud_usage.jsonl")):
            p.start()
            self.addCleanup(p.stop)
        raw = self.post({"model": "openrouter/xiaomi/mimo-v2.6-pro", "stream": True, "messages": []}).read().decode()
        events = [json.loads(l[6:]) for l in raw.splitlines() if l.startswith("data: {")]
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "Consol")
        self.assertIn("stream broke", events[-1]["error"]["message"])           # the instance's llm.py raises on it
        self.assertIn("upstream stream broke", (self.tmp / "broker.log").read_text())


if __name__ == "__main__":
    unittest.main()
