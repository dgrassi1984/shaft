"""A conversation pauses the instance's autonomous run at once (the operator, 2026-10-13: "when I start a chat, its
background cycle should be immediately paused"), and the run resumes when the conversation ends."""
from __future__ import annotations

import json
import os
import signal
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

TEMPLATE = Path(__file__).resolve().parents[2] / "template"
sys.path.insert(0, str(TEMPLATE))


class SlowModel:
    """Answers its first request only after `delay` seconds (or not at all if the client hangs up)."""

    def __init__(self, delay: float):
        self.requests, self.cut_off = [], []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if len(fake.requests) == 1:
                    time.sleep(delay)
                body = json.dumps({"choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    fake.cut_off.append(True)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class AgentSideTest(unittest.TestCase):
    def test_a_model_call_is_cut_off_and_the_step_redone_after_the_pause(self):
        from shaft import agent, pausing
        from shaft.llm import Client
        from shaft.tools import Toolbox
        model = SlowModel(delay=8)
        self.addCleanup(model.close)
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".tmp").mkdir()
        old = signal.getsignal(signal.SIGUSR1)
        self.addCleanup(signal.signal, signal.SIGUSR1, old)
        self.addCleanup(pausing._state.update, {"installed": False, "requested": False, "in_call": False})
        with mock.patch.dict(os.environ, {"SHAFT_ROOT": str(root)}):
            pausing.install()

            def supervisor():
                time.sleep(1)                                   # the cycle is waiting on the model
                pid = os.getpid()
                (root / ".tmp" / f"pause-{pid}").write_text("now")
                os.kill(pid, signal.SIGUSR1)
                for _ in range(50):
                    if (root / ".tmp" / f"paused-{pid}").exists():   # it acknowledged: the conversation happens
                        break
                    time.sleep(0.1)
                seen["acked"] = (root / ".tmp" / f"paused-{pid}").exists()
                time.sleep(1)
                (root / ".tmp" / f"pause-{pid}").unlink()           # the conversation ended
            seen = {}
            t = threading.Thread(target=supervisor)
            t.start()
            started = time.monotonic()
            out = agent.run(Client(model.url, "m", "", 30, root / "u.jsonl"), [{"role": "user", "content": "go"}],
                            Toolbox(root), max_tokens=10, purpose="test", max_steps=3)
            t.join()
        self.assertTrue(seen["acked"])
        self.assertEqual(out.ended_by, "answer")
        self.assertEqual(out.steps, 1)                              # the cut-off step does not count
        self.assertEqual(len(model.requests), 2)                    # cut off, then redone
        self.assertLess(time.monotonic() - started, 7)              # not after the slow reply's 8 s

    def test_the_deadline_moves_by_the_paused_time(self):
        from shaft import agent
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".tmp").mkdir()
        (root / ".tmp" / f"deadline-{os.getpid()}").write_text(f"{time.time() + 3600:.0f}")
        with mock.patch.dict(os.environ, {"SHAFT_ROOT": str(root), "SHAFT_DEADLINE": f"{time.time() + 60:.0f}"}):
            self.assertGreater(agent.time_left(), 3000)


FAKE_CYCLE = r"""
import os, signal, sys, time
from pathlib import Path
tmp = Path(".tmp"); tmp.mkdir(exist_ok=True)
me = os.getpid()
(tmp / f"pausable-{me}").write_text(str(me))
paused = {"now": False}
signal.signal(signal.SIGUSR1, lambda *a: paused.update(now=True))
end = time.time() + 6
while time.time() < end:
    if paused["now"]:
        (tmp / f"paused-{me}").write_text("1")
        while (tmp / f"pause-{me}").exists():
            time.sleep(0.2)
        (tmp / f"paused-{me}").unlink()
        paused["now"] = False
        end += 1
    time.sleep(0.1)
"""


class SupervisorSideTest(unittest.TestCase):
    def test_a_reply_takes_the_instance_from_a_running_cycle_and_gives_it_back(self):
        from supervisor import runner
        project = Path(tempfile.mkdtemp(prefix="shaft-proj-")).resolve()
        self.addCleanup(shutil.rmtree, project, True)
        sup, root = project / ".supervisor", project / "instances" / "shaft"
        root.mkdir(parents=True)
        settles = []
        broker = mock.Mock(handoffs=[], config_writes=[], counts={})
        cmd = [sys.executable, "-c", FAKE_CYCLE]
        env = {"SHAFT_DEADLINE": f"{time.time() + 600:.0f}"}
        result, during = {}, {}
        with mock.patch.object(runner, "SUP_DIR", sup), mock.patch.object(runner, "API_TALKS", sup / "api.json"), \
             mock.patch.object(runner, "_launch", return_value=(broker, cmd, env)), \
             mock.patch.object(runner, "_snapshot", side_effect=lambda r, *a: {"t": time.monotonic()}), \
             mock.patch.object(runner, "_reconcile"), \
             mock.patch.object(runner, "_settle", side_effect=lambda *a, **k: settles.append(a[3]) or True), \
             mock.patch.object(runner, "commit_state", return_value=True), \
             mock.patch.object(runner, "log"), mock.patch.object(runner, "instance_root", return_value=root), \
             mock.patch.object(runner, "PROJECT", Path(__file__).resolve().parents[2]), \
             mock.patch("supervisor.metrics.after_cycle"):                   # the judge is not part of this
            t = threading.Thread(target=lambda: result.update(code=runner.run_agent("shaft", "live", ["live"], False)))
            t.start()
            time.sleep(1)
            self.assertTrue(runner.is_busy("shaft"))
            with runner.Presence("shaft", "chat1"):              # the operator opens `shaft talk` and types nothing yet
                time.sleep(2.5)
                during["open_only"] = runner.is_busy("shaft")    # an idle open conversation does not pause it
                with runner.Reply("shaft", "talk"):              # he sends a message: a reply wants the instance
                    for _ in range(40):
                        if not runner.is_busy("shaft"):
                            break
                        time.sleep(0.25)
                    during["free"] = not runner.is_busy("shaft")     # the reply can have the instance
                    during["settled"] = len(settles)
                    runner._clean_tmp(root)                           # the conversation's write-up starts meanwhile
                    time.sleep(1)
                    during["still_paused"] = bool(list((root / ".tmp").glob("pause-*")))
                t.join(timeout=30)                               # reply done: it resumes with the talk still open
                during["finished_while_open"] = not t.is_alive()
            t.join(timeout=30)
        self.assertTrue(during["open_only"])                      # the operator, 2026-10-13: "pause only while replying"
        self.assertTrue(during["free"])
        self.assertTrue(during["finished_while_open"])
        # Regression (2026-10-13): another run's start-up cleanup deleted the paused cycle's pause file and resumed
        # it while the conversation still had the instance.
        self.assertTrue(during["still_paused"])
        self.assertEqual(during["settled"], 1)                    # the cycle's work so far was checked first
        self.assertEqual(result.get("code"), 0)                   # then it resumed and finished
        self.assertEqual(len(settles), 2)                         # and its own work after the pause was checked
        self.assertIsNot(settles[0], settles[1])                  # from a fresh baseline: the chat's work is not its
        self.assertEqual(list((root / ".tmp").glob("pause-*")), [])


class CleanupTest(unittest.TestCase):
    def test_only_files_of_runs_that_are_gone_are_removed(self):
        from supervisor import runner
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, root, True)
        tmp = root / ".tmp"
        tmp.mkdir()
        gone, alive = 2 ** 22 + 4321, os.getpid()
        for name in ("pause", "paused", "pausable", "deadline"):
            (tmp / f"{name}-{gone}").write_text("x")
            (tmp / f"{name}-{alive}").write_text("x")
        (tmp / "activity.json").write_text(json.dumps({"pid": gone}))
        (tmp / "memory-index.json").write_text("{}")
        runner._clean_tmp(root)
        left = sorted(p.name for p in tmp.iterdir())
        self.assertEqual(left, sorted([f"{n}-{alive}" for n in ("pause", "paused", "pausable", "deadline")]
                                      + ["memory-index.json"]))


class OldCodeTest(unittest.TestCase):
    def test_a_run_that_cannot_be_paused_is_asked_to_wrap_up_not_signalled(self):
        from supervisor import runner
        root = Path(tempfile.mkdtemp(prefix="shaft-inst-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / ".tmp").mkdir()
        proc = mock.Mock(pid=999999)
        run = {}
        with mock.patch.object(runner, "request_yield") as yielded, mock.patch("os.kill") as kill:
            self.assertEqual(runner._pause("shaft", root, "live", proc, mock.Mock(), run, mock.Mock()), 0.0)
            runner._pause("shaft", root, "live", proc, mock.Mock(), run, mock.Mock())
        kill.assert_not_called()                     # SIGUSR1 would have killed it
        yielded.assert_called_once_with(["shaft"])   # asked once, not every 2 seconds



class YieldOnlyForTheSameModelTest(unittest.TestCase):
    """Bug, 2026-10-13: `shaft talk` asked every other running instance to wrap up "so you get the model" — wren on
    MiMo and kant on Opus were cut short for every conversation with shaft, on local Bonsai, though a cloud model
    is shared with nobody. Only an instance on the same local model server is asked."""
    CONFIGS = {"shaft": {"LLM_BASE_URL": "http://127.0.0.1:8780/v1"},
               "twin": {"LLM_BASE_URL": "http://localhost:8780/v1"},       # same server, other spelling
               "other": {"LLM_BASE_URL": "http://127.0.0.1:9999/v1"},      # another local server
               "wren": {"LLM_BASE_URL": "broker"}, "kant": {"LLM_BASE_URL": "broker"}}

    def setUp(self):
        from supervisor import runner
        p = mock.patch.object(runner, "instance_config", lambda n: self.CONFIGS.get(n, {}))
        p.start()
        self.addCleanup(p.stop)
        self.runner = runner

    def test_model_server(self):
        self.assertEqual(self.runner.model_server("shaft"), "127.0.0.1:8780")
        self.assertEqual(self.runner.model_server("twin"), "127.0.0.1:8780")
        self.assertIsNone(self.runner.model_server("wren"))

    def test_only_the_same_local_server_is_shared(self):
        self.assertEqual(self.runner.sharing_model("shaft", ["wren", "kant", "twin", "other"]), ["twin"])
        self.assertEqual(self.runner.sharing_model("wren", ["shaft", "kant", "twin"]), [])   # cloud waits for nobody

    def test_talking_to_shaft_leaves_cloud_cycles_alone(self):
        from supervisor import __main__ as cli
        busy = {"wren": "live since 20:59", "kant": "live since 21:00", "twin": "live since 21:01"}
        with mock.patch.object(cli, "busy_with", lambda n: busy.get(n)), \
                mock.patch.object(cli, "instance_names", lambda: ["shaft", "wren", "kant", "twin"]), \
                mock.patch.object(cli, "request_yield") as yielded, \
                mock.patch.object(cli, "run_agent", return_value=0), \
                mock.patch.object(cli, "spawn_recover"), \
                mock.patch("sys.stderr", new_callable=__import__("io").StringIO) as err:
            self.assertEqual(cli.main(["talk", "shaft", "--plain"]), 0)
        yielded.assert_called_once_with(["twin"])
        self.assertNotIn("wren", err.getvalue())
        self.assertIn("twin (live since 21:01) is using the model shaft needs (127.0.0.1:8780)", err.getvalue())
        with mock.patch.object(cli, "busy_with", lambda n: busy.get(n)), \
                mock.patch.object(cli, "instance_names", lambda: ["shaft", "wren", "kant", "twin"]), \
                mock.patch.object(cli, "request_yield") as yielded, \
                mock.patch.object(cli, "run_agent", return_value=0), \
                mock.patch.object(cli, "spawn_recover"):
            cli.main(["talk", "wren", "--plain"])
        yielded.assert_not_called()                  # talking to a cloud instance takes nothing from anyone


if __name__ == "__main__":
    unittest.main()
