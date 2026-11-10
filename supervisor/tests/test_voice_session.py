"""Lane L6: the warm voice session (§7). A spoken turn of an API conversation is answered by one
agent process kept for the whole conversation (runner.Warm, api.VoiceSessions), which holds the instance only while
it replies, never answers on stale code, and ends when it is no longer wanted — idle, ended, paused, killed, updated,
deleted, or with the API. The agent here is a real process speaking `answer --loop`'s protocol; no model, no network."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from supervisor import api, kill, runner
from supervisor.kill import Proc
from supervisor.tests import test_api as base
from supervisor.tests import test_voice_api as voice_api

# A stand-in for `python -m shaft answer --loop` (template talk.answer_loop): one request per line, its events, then
# request_done. Its content names its pid and how many requests it has taken, so a test sees which process answered.
AGENT = r'''
import json, os, signal, sys, time
from pathlib import Path
pid, n = os.getpid(), 0
def emit(**e):
    sys.stdout.write(json.dumps(e) + "\n"); sys.stdout.flush()
while True:
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        continue
    if not line:
        break
    req = json.loads(line)
    if req.get("type") == "end":
        break
    n += 1
    text = req["messages"][-1]["content"]
    try:
        emit(type="ready", name="wisp", pid=pid)
        script = Path(".tmp/script.json")
        if script.exists():
            for e in json.loads(script.read_text()):
                emit(**e)
            emit(type="request_done")
            continue
        if text in ("slow", "deaf"):
            if text == "deaf":
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            emit(type="content", text="thinking ")
            Path(".tmp/working").write_text(str(pid))
            while True:
                time.sleep(0.05)
        if text == "listen":
            path = Path(".tmp") / ("interject-" + req["talk_id"] + ".jsonl")
            Path(".tmp/working").write_text(str(pid))
            for _ in range(200):
                if path.exists() and path.read_text().strip():
                    break
                time.sleep(0.05)
            for l in path.read_text().splitlines():
                emit(type="heard", text=json.loads(l)["text"])
        emit(type="content", text="pid %d turn %d: %s" % (pid, n, text))
        emit(type="turn_end", steps=1, ended_by="answer")
    except KeyboardInterrupt:
        emit(type="interrupted")
    emit(type="request_done")
'''


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:   # a zombie still answers kill(0): ask ps
        import subprocess
        state = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
        return bool(state.strip()) and not state.strip().startswith("Z")
    except OSError:
        return True


def wait_for(cond, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


class WarmFixture:
    """runner's run machinery around a real fake agent: lock, Reply presence and Turn are real; the launch, the
    reconcile/snapshot (the approved code is self.code) and the settle are stand-ins."""

    name = "wisp"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="shaft-warm-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sup = self.tmp / "sup"
        self.root = self.tmp / "instances" / self.name
        (self.root / ".tmp").mkdir(parents=True)
        (self.root / "config.env").write_text("LLM_MODEL=fake\n")
        self.code = {"shaft/talk.py": b"v1"}
        self.launched: list = []
        self.settled = mock.Mock(return_value=True)
        self.logged: list[str] = []

        def launch(name, root, kind, agent_args, budget, approved=None):
            broker = mock.Mock(handoffs=[], config_writes=[], counts={}, calls=0)
            self.launched.append({"broker": broker, "args": list(agent_args), "code": approved})
            return broker, [sys.executable, "-c", AGENT], {"PATH": "/usr/bin:/bin"}

        for target, attr, value in ((runner, "SUP_DIR", self.sup), (runner, "API_TALKS", self.sup / "api_talks.json"),
                                    (runner, "instance_root", lambda n: self.tmp / "instances" / n),
                                    (runner, "_launch", launch),
                                    (runner, "_reconcile", lambda name, root: dict(self.code)),
                                    (runner, "_snapshot", lambda root, code=None: {"code": code}),
                                    (runner, "_settle", self.settled),
                                    (runner, "log", self.logged.append),
                                    (kill, "end_left_in", lambda *a, **k: [])):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.sessions: list[runner.Warm] = []

        def close_all():
            for s in self.sessions:
                s.close("test over")
        self.addCleanup(close_all)

    def warm(self) -> runner.Warm:
        s = runner.Warm(self.name, "api", ["answer", "--loop"], "api-t1")
        self.sessions.append(s)
        return s

    def ask(self, s: runner.Warm, text: str, on_event=None, on_start=None) -> tuple[object, list[dict]]:
        got: list[dict] = []

        def keep(ev):
            got.append(ev)
            if on_event:
                on_event(ev)
        code = s.reply({"talk_id": s.talk_id, "voice": True, "messages": [{"role": "user", "content": text}]}, keep,
                       on_start)
        return code, got

    @staticmethod
    def said(events: list[dict]) -> str:
        return "".join(e["text"] for e in events if e["type"] == "content")


class WarmRunTest(WarmFixture, unittest.TestCase):
    def test_two_replies_use_one_process_and_one_broker(self):
        """Bug guarded (M0): every API reply started a fresh sandboxed agent (~1.8 s), re-read Tundra's memory (~4 s)
        and, its broker being per run, re-opened the Codex session (5.8-9.8 s): a voice turn must reuse all three."""
        s = self.warm()
        code1, first = self.ask(s, "hello")
        pid = s.proc.pid
        code2, second = self.ask(s, "and again")
        self.assertEqual((code1, code2), (None, None))
        self.assertEqual(self.said(first), f"pid {pid} turn 1: hello")
        self.assertEqual(self.said(second), f"pid {pid} turn 2: and again")    # the same process, its second request
        self.assertEqual([e["type"] for e in second], ["ready", "content", "turn_end"])   # no marker leaks out
        self.assertEqual(len(self.launched), 1)                                  # one broker, one launch
        self.assertEqual(self.launched[0]["args"], ["answer", "--loop"])
        self.assertIs(s.broker, self.launched[0]["broker"])
        self.assertEqual(self.settled.call_count, 2)                             # each reply checked and committed
        self.assertTrue(alive(pid))

    def test_the_instance_is_held_only_while_a_reply_runs(self):
        """Bug guarded: a warm process holding the run lock (or a Reply presence) for the whole conversation, so no
        cycle ran while the operator was quiet, or a reply written without the lock."""
        s = self.warm()
        during = []

        def look(ev):
            if ev["type"] == "content":
                during.append((runner.busy_with(self.name), runner.replying(self.name)))
        self.ask(s, "hello", look)
        self.assertTrue(during[0][0].startswith("api"), during)
        self.assertEqual(len(during[0][1]), 1)
        self.assertTrue(s.alive())
        self.assertFalse(runner.is_busy(self.name))          # quiet: a cycle could take it now
        self.assertEqual(runner.replying(self.name), [])
        with runner.run_lock(self.name, wait=False, kind="live"):   # and does: the process is not in its way
            self.assertTrue(s.alive())
        during.clear()
        self.ask(s, "after the cycle", look)
        self.assertTrue(during[0][0].startswith("api"))
        self.assertFalse(runner.is_busy(self.name))

    def test_a_code_change_in_the_pause_starts_a_new_process_for_the_next_reply(self):
        """Bug guarded: a reply answered by a process started before a cycle kept a code change (or `shaft update`
        merged the template, or the operator changed config.env): stale code answering the operator."""
        s = self.warm()
        self.ask(s, "hello")
        old = s.proc.pid
        self.code = {"shaft/talk.py": b"v2"}                   # a cycle in the pause: approved new code
        _, got = self.ask(s, "after the change")
        self.assertNotEqual(s.proc.pid, old)
        self.assertEqual(self.said(got), f"pid {s.proc.pid} turn 1: after the change")
        self.assertEqual(self.launched[1]["code"], {"shaft/talk.py": b"v2"})
        self.assertTrue(wait_for(lambda: not alive(old)))
        again = s.proc.pid
        (self.root / "config.env").write_text("LLM_MODEL=fake\nVOICE_REASONING_EFFORT=low\n")
        self.ask(s, "after a setting")
        self.assertNotEqual(s.proc.pid, again)
        self.assertEqual(len(self.launched), 3)
        kept = s.proc.pid
        self.ask(s, "nothing changed")
        self.assertEqual(s.proc.pid, kept)

    def test_close_ends_the_process_and_its_registration(self):
        """Bug guarded: an ended session leaving its agent, broker or registration behind."""
        s = self.warm()
        self.ask(s, "hello")
        pid, broker = s.proc.pid, s.broker
        self.assertEqual(runner.warm_pids(self.name), {"api-t1": pid})
        self.assertTrue(s.close("its conversation ended"))
        self.assertFalse(alive(pid))
        broker.stop.assert_called_once()
        self.assertEqual(runner.warm_pids(self.name), {})
        self.assertEqual(self.ask(s, "too late")[0], runner.Warm.CLOSED)

    def test_end_warm_from_another_process_ends_a_quiet_one(self):
        """Bug guarded: `shaft update` / `shaft delete` (other processes) unable to reach the API's quiet voice session,
        so the update left it running the old code and the delete was refused as busy, or deleted its folder under
        it."""
        s = self.warm()
        self.ask(s, "hello")
        pid = s.proc.pid
        self.assertEqual(runner.end_warm(self.name), [pid])
        self.assertFalse(alive(pid))
        self.assertTrue(wait_for(lambda: runner.warm_pids(self.name) == {} and s.proc is None))   # the keeper noticed
        _, got = self.ask(s, "next")                          # the next reply starts a new one
        self.assertNotEqual(s.proc.pid, pid)
        self.assertIn("turn 1: next", self.said(got))

    def test_a_stop_interrupts_the_reply_and_keeps_the_process(self):
        """Bug guarded: the client's Stop killing the warm process (the next turn then cold), or never reaching the
        reply; and a Stop sent after its reply ended interrupting the next one."""
        s = self.warm()
        stops = []
        threading.Thread(target=lambda: (wait_for(lambda: (self.root / ".tmp" / "working").exists()), stops[0]()),
                         daemon=True).start()
        code, got = self.ask(s, "slow", on_start=stops.append)
        self.assertIsNone(code)
        self.assertEqual([e["type"] for e in got], ["ready", "content", "interrupted"])
        pid = s.proc.pid
        stops[0]()                                             # late: nothing happens to the next reply
        _, got = self.ask(s, "go on")
        self.assertEqual(self.said(got), f"pid {pid} turn 2: go on")

    def test_a_reply_that_ignores_its_stop_ends_its_process(self):
        """Bug guarded: a stopped reply that never says it is done holding the instance (the run lock) for ever."""
        s = self.warm()
        stops = []
        threading.Thread(target=lambda: (wait_for(lambda: (self.root / ".tmp" / "working").exists()), stops[0]()),
                         daemon=True).start()
        with mock.patch.object(runner, "WARM_STOP_GRACE_S", 0.5):
            code, _ = self.ask(s, "deaf", on_start=stops.append)
        self.assertIsNotNone(code)
        self.assertIsNone(s.proc)
        self.assertFalse(runner.is_busy(self.name))
        self.assertIn("turn 1: next", self.said(self.ask(s, "next")[1]))

    def test_the_api_going_away_ends_a_quiet_warm_agent(self):
        """Bug guarded: a stopped API service leaving the agents of its voice sessions running (they are between
        turns, held by no run)."""
        s = self.warm()
        self.ask(s, "hello")
        pid = s.proc.pid
        runner.end_all_runs()                                  # what _trap_runs does on SIGTERM
        self.assertFalse(alive(pid))


class WarmTargetsTest(unittest.TestCase):
    def test_kill_stops_a_quiet_voice_sessions_agent(self):
        """Bug guarded: `shaft kill` missing the API's warm agent (its parent is the API, its command no `-m shaft`:
        the protected-code launch), so it stayed up through a kill."""
        root = Path("/p/instances/kdx")
        py = sys.executable
        procs = {30: Proc(30, 1, f"{py} -E -s -m supervisor api serve", "/p"),
                 31: Proc(31, 30, f"{py} -I -c import runpy, sys; runpy.run_module('shaft') /p/.supervisor/protected/"
                                  "30-x/kdx answer --loop", str(root))}
        self.assertEqual(kill.targets("kdx", procs, root), ([31], []))


class VoiceApiSessionTest(WarmFixture, unittest.TestCase):
    """Through the HTTP API: spoken, streamed turns with X-Shaft-Conversation go to the conversation's session."""

    def setUp(self):
        WarmFixture.setUp(self)
        base.ApiTest.setUp(self)
        self.roots = self.tmp / "instances"      # the API's instance_root and runner's agree
        (self.roots / "shaft").mkdir(exist_ok=True)
        self.paused = False
        for target, attr, value in ((api, "instance_root", lambda n: self.roots / n),
                                    (api, "paused", lambda n: self.paused)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        api.Handler.voice = api.VoiceSessions(api.Handler.conversations.is_open)
        self.addCleanup(setattr, api.Handler, "voice", None)

        def close_all():
            for s in list(api.Handler.voice.sessions.values()):
                s.close("test over")
        self.addCleanup(close_all)
        base.fake_run_agent.args = None

    def post(self, path: str, body: dict, cid: str = "chat-1"):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {base.KEY}",
                   "Tailscale-User-Login": base.ME, "X-Shaft-Conversation": cid, "X-Shaft-Channel": "web"}
        return urllib.request.urlopen(urllib.request.Request(self.base + path, data=json.dumps(body).encode(),
                                                             headers=headers), timeout=30)

    def turn(self, messages: list[dict], voice=True, cid="chat-1") -> list:
        body = {"model": self.name, "stream": True, "messages": messages, **({"voice": True} if voice else {})}
        with self.post("/chat/completions", body, cid) as r:
            raw = r.read().decode()
        out = []
        for block in raw.split("\n\n"):
            if block.startswith("data: {"):
                chunk = json.loads(block[6:])
                choice = chunk.pop("choices")[0]
                for k in ("id", "object", "created", "model"):
                    chunk.pop(k)
                out.append([choice["delta"], choice["finish_reason"], chunk])
            elif block.startswith("data: "):
                out.append(block[6:])
        return out

    @staticmethod
    def content(chunks) -> str:
        return "".join(c[0].get("content", "") for c in chunks if isinstance(c, list))

    def session(self) -> runner.Warm:
        (s,) = api.Handler.voice.sessions.values()
        return s

    def test_two_spoken_turns_of_a_conversation_are_answered_by_one_process(self):
        """Bug guarded (M0): each spoken turn a fresh process, memory read and Codex session — 6 s a turn."""
        first = self.content(self.turn([{"role": "user", "content": "hello"}]))
        pid = self.session().proc.pid
        self.assertEqual(first, f"pid {pid} turn 1: hello")
        second = self.content(self.turn([{"role": "user", "content": "hello"}, {"role": "assistant", "content": first},
                                         {"role": "user", "content": "and the weather?"}]))
        self.assertEqual(second, f"pid {pid} turn 2: and the weather?")
        self.assertEqual(len(self.launched), 1)
        self.assertIsNone(base.fake_run_agent.args)              # nothing ran cold
        self.assertFalse(runner.is_busy(self.name))
        self.assertEqual(runner.replying(self.name), [])

    def test_a_warm_voice_stream_is_the_one_a_cold_voice_turn_writes(self):
        """Bug guarded: the warm path dropping or reordering what L2's voice stream promises (drafts, step ends,
        content, the typed chunks around them)."""
        (self.root / ".tmp" / "script.json").write_text(json.dumps(voice_api.WEATHER))
        got = self.turn([{"role": "user", "content": "weather in Rome?"}])
        self.assertEqual(got, voice_api.VOICE)
        self.assertEqual(len(self.launched), 1)

    def test_a_typed_turn_in_a_voice_conversation_runs_as_before(self):
        """Bug guarded: a typed request routed to the warm process (its prompt differs: the Codex session would be
        rebuilt, and the typed path's own guarantees lost), or its stream changed."""
        self.turn([{"role": "user", "content": "hello"}])
        pid = self.session().proc.pid
        with mock.patch.object(base, "EVENTS", voice_api.WEATHER):
            typed = self.turn([{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"},
                               {"role": "user", "content": "weather in Rome?"}], voice=False)
        # the stream captured before voice, with the status events only the web chat's typed stream gets (threads)
        self.assertEqual(typed, voice_api.TYPED_STATUS)
        self.assertEqual(base.fake_run_agent.args, ["answer"])   # in a process of its own
        self.assertNotIn("voice", base.fake_run_agent.request)
        spoken = self.content(self.turn([{"role": "user", "content": "back to voice"}]))
        self.assertEqual(spoken, f"pid {pid} turn 2: back to voice")   # the warm process never saw the typed turn

    def test_ending_the_conversation_ends_its_session(self):
        """Bug guarded: /v1/chat/end writing the conversation up while its warm process stayed up."""
        self.turn([{"role": "user", "content": "hello"}])
        pid = self.session().proc.pid
        with mock.patch.object(api, "spawn_recover"):
            with self.post("/chat/end", {"model": self.name, "messages": []}) as r:
                self.assertEqual(r.status, 202)
        self.assertFalse(alive(pid))
        self.assertEqual(api.Handler.voice.sessions, {})

    def test_a_quiet_session_ends_after_its_idle_time(self):
        """Bug guarded: a voice session kept up for ever after the operator stopped talking (a process, a broker and
        a Codex session per forgotten conversation)."""
        self.turn([{"role": "user", "content": "hello"}])
        s = self.session()
        pid = s.proc.pid
        api.Handler.voice.sweep()
        self.assertTrue(alive(pid))                              # 300 s: not yet
        with mock.patch.object(api, "VOICE_SESSION_IDLE_S", 0.2):
            time.sleep(0.3)
            api.Handler.voice.sweep()
        self.assertFalse(alive(pid))
        self.assertTrue(s.closed)
        self.assertEqual(api.Handler.voice.sessions, {})

    def test_a_paused_instance_keeps_no_warm_process_and_answers_cold(self):
        """Bug guarded: a pause (and a kill, which pauses first) leaving the voice session's process up between turns,
        or a paused instance still given a warm process."""
        self.turn([{"role": "user", "content": "hello"}])
        pid = self.session().proc.pid
        self.paused = True
        api.Handler.voice.sweep()
        self.assertFalse(alive(pid))
        self.assertEqual(api.Handler.voice.sessions, {})
        with mock.patch.object(base, "EVENTS", voice_api.WEATHER):
            got = self.turn([{"role": "user", "content": "still there?"}])
        self.assertEqual(got, voice_api.VOICE)                   # a spoken turn, answered in its own process
        self.assertEqual(base.fake_run_agent.args, ["answer"])
        self.assertEqual(api.Handler.voice.sessions, {})

    def test_a_killed_agent_ends_its_session_and_the_next_turn_starts_again(self):
        """Bug guarded: a voice session whose agent `shaft kill` stopped (it signals the instance's processes) left
        registered with a dead process, or the next turn failing on it."""
        self.turn([{"role": "user", "content": "hello"}])
        pid = self.session().proc.pid
        os.killpg(pid, signal.SIGTERM)
        self.assertTrue(wait_for(lambda: runner.warm_pids(self.name) == {}))
        api.Handler.voice.sweep()
        self.assertEqual(api.Handler.voice.sessions, {})
        self.assertIn("turn 1: again", self.content(self.turn([{"role": "user", "content": "again"}])))

    def test_the_sweeper_neither_sleeps_on_time_sleep_nor_outlives_its_sessions(self):
        """Bug guarded (found by the full suite): the sweeper looped on time.sleep for ever once started, so a later
        test's patched time.sleep (the scheduler's loop, test_wake_gate) ran out under it — in the API it would be a
        thread for life after the last voice session."""
        self.turn([{"role": "user", "content": "hello"}])
        voice = api.Handler.voice
        with mock.patch("time.sleep") as slept:
            self.assertTrue(wait_for(lambda: voice.sweeper is not None and voice.sweeper.is_alive(), 2))
            threading.Event().wait(voice.SWEEP_S * 1.5)
            self.assertEqual(slept.call_count, 0)
        voice.end(self.session().talk_id, "test")
        self.assertTrue(wait_for(lambda: voice.sweeper is None, 5))

    def test_a_message_sent_during_a_warm_turn_reaches_it(self):
        """Bug guarded: interjections lost on the warm path (the request's Live entry never started, or its held
        messages written after the running agent had already looked)."""
        out = []
        t = threading.Thread(target=lambda: out.append(self.turn([{"role": "user", "content": "listen"}])))
        t.start()
        self.assertTrue(wait_for(lambda: (self.root / ".tmp" / "working").exists()))
        body = {"model": self.name, "voice": True, "messages": [{"role": "user", "content": "listen"},
                                                                {"role": "user", "content": "and Milan"}]}
        with self.post("/chat/interject", body) as r:
            self.assertEqual(r.status, 202)
        t.join(30)
        notes = "".join(c[0].get("reasoning_content", "") for c in out[0] if isinstance(c, list))
        self.assertIn("read your new message: and Milan", notes)
        self.assertEqual(out[0][-2][2]["interjections_heard"], 1)

    def test_a_client_that_leaves_mid_turn_stops_the_reply_not_the_process(self):
        """Bug guarded: Stop during a warm turn ending the whole session (the next turn cold), or not stopping the
        reply (the instance held until it finished for nobody)."""
        self.turn([{"role": "user", "content": "hello"}])
        pid = self.session().proc.pid
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.request("POST", "/v1/chat/completions", json.dumps(
            {"model": self.name, "stream": True, "voice": True, "messages": [{"role": "user", "content": "slow"}]}),
            {"Authorization": f"Bearer {base.KEY}", "Tailscale-User-Login": base.ME,
             "Content-Type": "application/json", "X-Shaft-Conversation": "chat-1"})
        resp = conn.getresponse()
        self.assertTrue(wait_for(lambda: (self.root / ".tmp" / "working").exists()))
        resp.close()
        conn.close()                                             # Stop
        self.assertTrue(wait_for(lambda: not runner.is_busy(self.name) and not api.Handler.live.replies, 15))
        self.assertTrue(alive(pid))
        (self.root / ".tmp" / "working").unlink()
        self.assertEqual(self.content(self.turn([{"role": "user", "content": "next"}])), f"pid {pid} turn 3: next")


class DeleteAndUpdateTest(WarmFixture, unittest.TestCase):
    def test_delete_ends_a_quiet_voice_session_instead_of_refusing(self):
        """Bug guarded: `shaft delete` refused as busy ("1 process(es) in its folder") by a voice session merely
        waiting for the operator's next sentence."""
        from supervisor import delete
        s = self.warm()
        self.ask(s, "hello")
        pid = s.proc.pid

        class Past(Exception):
            pass
        found = mock.Mock()
        found.anything.return_value = True
        with mock.patch.object(delete, "find", return_value=found), \
                mock.patch.object(kill, "running", lambda n: ["1 process(es) in its folder"] if alive(pid) else []), \
                mock.patch.object(delete, "set_paused", side_effect=Past):
            with self.assertRaises(Past):                       # past the busy check: not refused
                delete.delete(self.name, say=lambda *_: None)
        self.assertFalse(alive(pid))


class UpdateEndsWarmTest(unittest.TestCase):
    def test_update_ends_a_quiet_voice_session(self):
        """Bug guarded: `shaft update` merging new code while a voice session's process kept running the old code
        between turns (it holds no lock, so the update did not wait for it)."""
        from supervisor.tests.test_commit import UpdateCase
        case = UpdateCase("template_change")
        case.setUp()
        self.addCleanup(case.doCleanups)
        sup = case.project / ".supervisor"
        p = mock.patch.object(runner, "SUP_DIR", sup)
        p.start()
        self.addCleanup(p.stop)
        import subprocess
        agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        self.addCleanup(agent.kill)
        reaper = threading.Thread(target=agent.wait, daemon=True)   # its keeper reaps it, as Warm._read does
        reaper.start()
        with runner.Presence("x", "api-t1", str(agent.pid), folder="warm"):
            case.update()                                       # already up to date: nothing to end
            self.assertIsNone(agent.poll())
            case.template_change("template: voice tweak")
            code, out = case.update()
            self.assertEqual(code, 0, out)
            reaper.join(10)
            self.assertIsNotNone(agent.poll())


if __name__ == "__main__":
    unittest.main()
