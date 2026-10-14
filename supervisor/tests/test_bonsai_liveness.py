"""The model server exits when its generation thread dies, instead of serving a dead backend.

Regression (2026-10-14 01:48): mlx_lm's generation thread raised `[metal::malloc] Resource limit (499000) exceeded`
and died; the HTTP server went on accepting requests that waited forever. Every agent call timed out after 900 s
for 6.5 hours and launchd (KeepAlive) never restarted the process, which was alive at 0% CPU."""
import contextlib
import io
import socket
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace as NS

try:
    import mlx_lm.server   # noqa: F401
except ImportError:          # the supervisor's own test run uses the system Python, without MLX
    mlx_lm = None

ROOT = Path(__file__).resolve().parents[2]
METAL_ERROR = "[metal::malloc] Resource limit (499000) exceeded."

# mlx_lm's real HTTP server and ResponseGenerator, with a stub model provider; the generation thread raises the
# production error when it starts a batch (in production it was the next decode step, on the same thread).
SERVER = f"""
import sys
from types import SimpleNamespace as NS
import mlx_lm.server as S
from supervisor import bonsai_server

class Boom:
    def __init__(self, *a, **k):
        raise RuntimeError({METAL_ERROR!r})

class Provider:
    cli_args = NS(prompt_cache_size=1, prompt_cache_bytes=None, num_draft_tokens=3, max_tokens=16, temp=0.0,
                  top_p=1.0, top_k=0, min_p=0.0, decode_concurrency=1, prompt_concurrency=1, prefill_step_size=512)
    model_key, is_batchable = ("stub", None, None), True
    def load_default(self): pass
    def load(self, *a, **k): return object(), object()

S.BatchGenerator = Boom
if sys.argv[2] == "guarded":
    bonsai_server.exit_if_generation_dies(S)
S.run("127.0.0.1", int(sys.argv[1]), Provider())
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def chat(port: int, timeout: float):
    body = b'{"model": "x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}'
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout).read()


@unittest.skipIf(mlx_lm is None, "needs mlx_lm (.venv-bonsai/bin/python -m unittest supervisor.tests.test_bonsai_liveness)")
class GenerationThreadDiesTest(unittest.TestCase):
    def serve(self, mode: str):
        port = free_port()
        proc = subprocess.Popen([sys.executable, "-c", SERVER, str(port), mode], cwd=ROOT,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                return proc, port
            except OSError:
                if proc.poll() is not None:
                    self.fail(f"server did not start: {proc.stdout.read()}")
                time.sleep(0.1)
        self.fail("server did not start listening")

    def test_process_exits_and_the_waiting_request_fails_fast(self):
        proc, port = self.serve("guarded")
        started = time.monotonic()
        with self.assertRaises(OSError):   # connection reset / remote end closed — not a 900 s hang
            chat(port, timeout=30)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(proc.wait(timeout=10), 70)   # non-zero: launchd's KeepAlive restarts it
        log = proc.stdout.read()
        self.assertIn(METAL_ERROR, log)
        self.assertIn("the generation thread died", log)

    def test_control_without_the_guard_the_request_hangs_on_a_live_process(self):
        # The bug this guards against: the same failure leaves a listening process that never answers.
        proc, port = self.serve("unguarded")
        with self.assertRaises(TimeoutError):
            chat(port, timeout=3)
        self.assertIsNone(proc.poll())


class GuardTest(unittest.TestCase):   # plain Python: a stand-in for mlx_lm's ResponseGenerator
    def guarded(self, generate, stop=False):
        from supervisor import bonsai_server
        exits = []

        class Generator:
            _generate = generate
        S = NS(ResponseGenerator=Generator)
        bonsai_server.exit_if_generation_dies(S, exit=exits.append)
        with contextlib.redirect_stderr(io.StringIO()):
            S.ResponseGenerator._generate(NS(_stop=stop))
        return exits

    def test_any_exception_exits_non_zero(self):
        def boom(self):
            raise RuntimeError(METAL_ERROR)
        self.assertEqual(self.guarded(boom), [70])

    def test_returning_unasked_exits_too(self):
        self.assertEqual(self.guarded(lambda self: None), [70])

    def test_a_requested_stop_is_a_clean_end(self):
        self.assertEqual(self.guarded(lambda self: None, stop=True), [])


if __name__ == "__main__":
    unittest.main()
