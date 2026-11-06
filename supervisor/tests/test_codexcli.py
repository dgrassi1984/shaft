"""Exercise the channel through a child process without Codex or Seatbelt."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from supervisor import codexcli as codex, planchannel

# Actions and all expected history are supplied by the parent, never inferred from its implementation.
FAKE = r'''
import json, os, sys, time
from pathlib import Path
scenario = json.loads(Path(__file__).with_suffix('.json').read_text())
log = Path(__file__).with_suffix('.jsonl')
thread = turn = None
threads = turns = stage = 0
calls = {}

def send(value):
    print(json.dumps(value), flush=True)

def note(method, params):
    send(dict(method=method, params=params))

def tagged(**values):
    return dict(threadId=thread, turnId=turn, **values)

def actions():
    global stage
    groups = scenario.get('stages', [[dict(text='hello')]])
    group = groups[stage] if stage < len(groups) else [dict(text='done')]
    stage += 1
    for action in group:
        if 'sleep' in action:
            time.sleep(action['sleep'])
        elif 'text' in action:
            text = action['text']
            for delta in action.get('deltas', [text]):
                note('item/agentMessage/delta', tagged(itemId='m'+str(stage), delta=delta))
                time.sleep(action.get('gap', 0))
            note('item/completed', tagged(item=dict(type='agentMessage', id='m'+str(stage), text=text)))
        elif 'call' in action:
            n = action['call']
            params = tagged(callId='call-'+str(n), tool='echo', arguments=action.get('arguments', {'n': n}))
            note('item/started', tagged(item=dict(type='dynamicToolCall', id='item-'+str(n))))
            calls[1000+n] = True
            send(dict(id=1000+n, method='item/tool/call', params=params))
        elif 'usage' in action:
            n = action['usage']
            note('thread/tokenUsage/updated', tagged(tokenUsage=dict(last=dict(inputTokens=n, outputTokens=2,
                                                               cachedInputTokens=3))))
        elif 'item' in action:
            note('item/started', tagged(item=action['item']))
        elif 'request' in action:
            send(dict(id=2000, method=action['request'], params=tagged()))
        elif 'notification' in action:
            note(action['notification'], tagged())
        elif 'stderr' in action:
            print(action['stderr'], file=sys.stderr, flush=True)
        elif 'die' in action:
            sys.exit(action['die'])
        elif 'raw' in action:
            print(action['raw'], flush=True)
        elif 'error' in action:
            note('error', tagged(error=dict(message=action['error'])))
        elif 'finish' in action:
            note('turn/completed', dict(threadId=thread, turn=dict(id=turn, status=action['finish'],
                                                                  items=action.get('items', []),
                                                                  error=dict(message='synthetic failure'))))
    if not calls and not any('finish' in a or 'die' in a or 'raw' in a for a in group) and not scenario.get('hang'):
        note('turn/completed', dict(threadId=thread, turn=dict(id=turn, status='completed', error=None)))

time.sleep(scenario.get('startup_delay', 0))
for line in sys.stdin:
    value = json.loads(line)
    with log.open('a') as f:
        f.write(json.dumps(value)+'\n')
    method = value.get('method')
    if method == 'initialize':
        send(dict(id=value['id'], result={}))
        with log.open('a') as f:
            f.write(json.dumps(dict(environment=dict(os.environ), argv=sys.argv))+'\n')
    elif method == 'account/rateLimits/read':
        send(dict(id=value['id'], result=dict(rateLimits=dict(primary=dict(usedPercent=scenario.get('weekly', 6),
                                                     windowDurationMins=scenario.get('window', 10080), resetsAt=12345)))))
    elif method == 'thread/start':
        threads += 1
        thread = 'thread-'+str(threads)
        send(dict(id=value['id'], result=dict(thread=dict(id=thread))))
        time.sleep(scenario.get('read_delay', 0))
    elif method == 'turn/interrupt':
        calls.clear()
        send(dict(id=value['id'], result={}))
        note('turn/completed', dict(threadId=thread, turn=dict(id=turn, status='interrupted')))
    elif method == 'turn/start':
        if scenario.get('hold_turn_start'):
            time.sleep(10)
        calls.clear()
        if 'rpc_result' in scenario:
            send(dict(id=value['id'], result=scenario['rpc_result']))
            continue
        if scenario.get('rpc_error'):
            send(dict(id=value['id'], error=dict(code=-32602, message='input_too_large')))
            continue
        turns += 1
        turn = 'turn-'+str(turns)
        send(dict(id=value['id'], result=dict(turn=dict(id=turn))))
        note('turn/started', dict(threadId=thread, turn=dict(id=turn)))
        actions()
    elif method == 'turn/steer':
        if scenario.get('steer_turn_ended'):
            note('turn/completed', dict(threadId=thread, turn=dict(id=turn, status='completed')))
        if 'steer_error' in scenario:
            send(dict(id=value['id'], error=scenario['steer_error']))
        else:
            send(dict(id=value['id'], result=dict(turnId=turn)))
            if scenario.get('wait_for_steer'):
                actions()
    elif method is None and value.get('id') in calls:
        del calls[value['id']]
        if not calls and not scenario.get('wait_for_steer'):
            actions()
'''


def body(messages=None, **extra):
    return {"model": "codex/gpt-6-astra", "messages": messages if messages is not None else [
        {"role": "system", "content": "system"}, {"role": "user", "content": "begin"}],
        "tools": [{"type": "function", "function": {"name": "echo", "description": "echo a value",
                  "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}}}}], **extra}


def assistant(reply):
    return reply["choices"][0]["message"]


class CodexLogRetentionTest(unittest.TestCase):
    """Codex's unbounded INFO log retains private instance tool results."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-codex-logs-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / "logs_2.sqlite"
        self.now = time.time()
        patch = mock.patch.object(codex, "LOG_HOURS", 24)
        patch.start()
        self.addCleanup(patch.stop)

    def database(self, count=1, text="private mail retention marker"):
        db = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        # No Codex-specific columns except ts are needed by retention.
        db.execute("CREATE TABLE logs (ts INTEGER, feedback_log_body TEXT)")
        db.executemany("INSERT INTO logs VALUES (?, ?)",
                       [(int(self.now - 90000), text)] * count)
        db.execute("INSERT INTO logs VALUES (?, ?)", (int(self.now), "recent"))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return db

    def test_unbounded_codex_log_old_rows_go_recent_stay_and_raw_text_is_erased(self):
        secret = b"private mail retention marker"
        db = self.database()
        self.assertIn(secret, self.path.read_bytes())
        codex._prune_logs(self.home)
        self.assertEqual(db.execute("SELECT feedback_log_body FROM logs").fetchall(), [("recent",)])
        self.assertFalse(secret in self.path.read_bytes(), "deleted mail remains in the database file")
        wal = self.path.with_name(self.path.name + "-wal")
        self.assertFalse(secret in wal.read_bytes(), "deleted mail remains in the WAL")
        self.assertEqual(wal.stat().st_size, 0)

    def test_unbounded_codex_log_prunes_more_than_one_batch(self):
        db = self.database(count=2005)
        codex._prune_logs(self.home)
        self.assertEqual(db.execute("SELECT feedback_log_body FROM logs").fetchall(), [("recent",)])

    def test_unbounded_codex_log_stamp_limits_frequency_across_processes(self):
        db = self.database()
        codex._prune_logs(self.home)
        db.execute("INSERT INTO logs VALUES (?, 'added after prune')", (int(self.now - 90000),))
        result = subprocess.run([sys.executable, "-B", "-c",
            "from pathlib import Path; from supervisor import codexcli; "
            "import sys; codexcli._prune_logs(Path(sys.argv[1]))", str(self.home)],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "SHAFT_SUPERVISOR_DIR": str(self.home)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM logs").fetchone(), (2,))
        stamp = self.home / "logs-pruned.stamp"
        os.utime(stamp, (self.now - 601, self.now - 601))
        codex._prune_logs(self.home)
        self.assertEqual(db.execute("SELECT feedback_log_body FROM logs").fetchall(), [("recent",)])

    def test_unbounded_codex_log_missing_table_is_logged(self):
        sqlite3.connect(self.path).close()
        with self.assertLogs("supervisor.codexcli", level="WARNING") as logs:
            codex._prune_logs(self.home)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("no such table: logs", logs.output[0])

    def test_unbounded_codex_log_missing_file_is_logged_without_creating_database(self):
        with self.assertLogs("supervisor.codexcli", level="WARNING") as logs:
            codex._prune_logs(self.home)
        self.assertIn("logs_2.sqlite is missing", logs.output[0])
        self.assertFalse(self.path.exists())

    def test_unbounded_codex_log_concurrent_reader_does_not_break_pruning(self):
        db = self.database()
        # A separate process keeps a real WAL read transaction open during deletion.
        child = subprocess.Popen([sys.executable, "-B", "-c",
            "import sqlite3, sys; db = sqlite3.connect(sys.argv[1]); "
            "db.execute('BEGIN'); db.execute('SELECT * FROM logs').fetchall(); "
            "print('ready', flush=True); sys.stdin.readline(); db.close()", str(self.path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertLogs("supervisor.codexcli", level="WARNING") as logs:
                codex._prune_logs(self.home)
            self.assertIn("wal_checkpoint(TRUNCATE) busy", logs.output[0])
            self.assertEqual(db.execute("SELECT feedback_log_body FROM logs").fetchall(), [("recent",)])
        finally:
            child.communicate("release\n", timeout=10)
        stamp = self.home / "logs-pruned.stamp"
        os.utime(stamp, (self.now - 601, self.now - 601))
        codex._prune_logs(self.home)
        self.assertEqual(self.path.with_name(self.path.name + "-wal").stat().st_size, 0)

    def test_unbounded_codex_log_vacuum_reclaims_large_free_space(self):
        db = self.database(count=12, text="old private mail" * 70000)
        before = self.path.stat().st_size
        self.assertGreater(before, 8 * 1024 * 1024)
        codex._prune_logs(self.home)
        self.assertLess(self.path.stat().st_size, before // 4)
        self.assertEqual(db.execute("SELECT feedback_log_body FROM logs").fetchall(), [("recent",)])


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-codex-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        # Synthetic login marker; no operator credentials are read by these tests.
        (self.home / "auth.json").write_text("{}")
        self.binary = self.root / "fake.py"
        self.binary.write_text("#!" + sys.executable + "\n" + FAKE)
        self.binary.chmod(0o700)
        self.scenario = self.binary.with_suffix(".json")
        self.scenario.write_text("{}")
        self.record = mock.Mock()
        self.logs = []
        for patch in (mock.patch.object(codex, "HOME", self.home),
                      mock.patch.object(codex, "BIN", str(self.binary)),
                      mock.patch.object(codex, "TIMEOUT_S", 3),
                      mock.patch.object(planchannel, "SUP_DIR", self.root),
                      mock.patch.object(codex.STATE, "rate", None),
                      mock.patch.object(codex, "_seatbelt", side_effect=lambda argv, *args: argv),
                      mock.patch.object(planchannel.cloudllm, "_record", self.record)):
            patch.start()
            self.addCleanup(patch.stop)
        self.channel = codex.Channel("broker", "usage-label", self.logs.append)
        self.addCleanup(self.channel.close)

    def use(self, stages=None, **extra):
        self.scenario.write_text(json.dumps({**extra, **({"stages": stages} if stages is not None else {})}))

    def rows(self):
        path = self.binary.with_suffix(".jsonl")
        return [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []

    def requests(self, method):
        return [r for r in self.rows() if r.get("method") == method]

    def test_unbounded_codex_log_locked_database_does_not_fail_channel(self):
        db = sqlite3.connect(self.home / "logs_2.sqlite", isolation_level=None)
        self.addCleanup(db.close)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE logs (ts INTEGER)")
        db.execute("BEGIN IMMEDIATE")
        try:
            with self.assertLogs("supervisor.codexcli", level="WARNING") as logs:
                reply = self.channel.complete(body())
            self.assertEqual(assistant(reply)["content"], "hello")
            self.assertEqual(len(logs.output), 1)
            self.assertIn("database is locked", logs.output[0])
        finally:
            db.execute("ROLLBACK")

    def extend(self, request, reply, results=(), users=()):
        new = copy.deepcopy(request)
        new["messages"] += [assistant(reply)] + [dict(role="tool", tool_call_id=call, content=text)
            for call, text in results] + [dict(role="user", content=text) for text in users]
        return new

    def test_new_thread_seed_and_effort(self):
        request = body(chat_template_kwargs={"reasoning_effort": "high"})
        request["messages"] += [{"role": "assistant", "content": "old", "tool_calls": [
            {"id": "older", "function": {"name": "echo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "older", "content": "verbatim\nresult"}]
        reply = self.channel.complete(request)
        self.assertEqual(assistant(reply)["content"], "hello")
        start = self.requests("thread/start")[0]["params"]
        self.assertEqual(start["baseInstructions"], "system")
        self.assertEqual(start["dynamicTools"], [{"type": "function", "name": "echo", "description": "echo a value",
            "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}}])
        params = self.requests("turn/start")[0]["params"]
        sent = params["input"][0]["text"]
        self.assertEqual(json.loads(sent.splitlines()[1]), request["messages"][1:])
        self.assertTrue(sent.endswith("Continue from the end of this transcript. Use your tools; don't write tool calls as text."))
        self.assertEqual(params["effort"], "high")
        self.assertEqual(self.channel.stats["rebuilds"], 1)

    def test_effort_change_extends_thread_and_is_sent_per_turn(self):
        self.use([[{"text": "first"}], [{"text": "second"}]])
        request = body(reasoning_effort="low")
        first = self.channel.complete(request)
        delta = self.extend(request, first, users=["continue"])
        delta["reasoning_effort"] = "high"
        self.channel.complete(delta)
        self.assertEqual(len(self.requests("thread/start")), 1)
        self.assertEqual([r["params"]["effort"] for r in self.requests("turn/start")], ["low", "high"])

    def test_parallel_results_answer_in_call_order_and_missing_is_failure(self):
        self.use([[{"call": 1}, {"call": 2}, {"usage": 10}], [{"text": "done"}, {"usage": 20}]])
        request = body()
        first = self.channel.complete(request)
        self.assertEqual([c["id"] for c in assistant(first)["tool_calls"]], ["call-1", "call-2"])
        second = self.channel.complete(self.extend(request, first, [("call-2", "second")]))
        self.assertEqual(assistant(second)["content"], "done")
        answers = [r for r in self.rows() if r.get("id") in (1001, 1002)]
        self.assertEqual(answers, [
            {"id": 1001, "result": {"success": False, "contentItems": [{"type": "inputText", "text": "not run"}]}},
            {"id": 1002, "result": {"success": True, "contentItems": [{"type": "inputText", "text": "second"}]}}])
        self.assertEqual(len(self.requests("thread/start")), 1)
        self.assertEqual(len(self.requests("turn/start")), 1)
        self.assertEqual(self.record.call_count, 2)
        self.assertEqual(self.record.call_args_list[0].args[:5], ("usage-label", "codex/gpt-6-astra", 10, 2, None))
        self.assertEqual(self.record.call_args_list[0].kwargs, {"cache": {"cached_input_tokens": 3}})

    def test_results_reversed_are_delivered_in_pending_order(self):
        self.use([[{"call": 1}, {"call": 2}], [{"text": "done"}]])
        request = body()
        first = self.channel.complete(request)
        self.channel.complete(self.extend(request, first, [("call-2", "two"), ("call-1", "one")]))
        answers = [r for r in self.rows() if r.get("id") in (1001, 1002)]
        self.assertEqual([r["result"]["contentItems"][0]["text"] for r in answers], ["one", "two"])

    def test_sequential_calls_are_separate_steps(self):
        self.use([[{"call": 1}], [{"call": 2}], [{"text": "done"}]])
        request = body()
        one = self.channel.complete(request)
        request = self.extend(request, one, [("call-1", "one")])
        two = self.channel.complete(request)
        self.assertEqual([c["id"] for c in assistant(two)["tool_calls"]], ["call-2"])
        final = self.channel.complete(self.extend(request, two, [("call-2", "two")]))
        self.assertEqual(assistant(final)["content"], "done")
        self.assertEqual(len(self.requests("turn/start")), 1)

    def test_turn_text_user_only_delta_is_plain_text(self):
        self.use([[{"text": "hello"}], [{"text": "next"}]])
        request = body()
        first = self.channel.complete(request)
        final = self.channel.complete(self.extend(request, first, users=["more", "and this"]))
        self.assertEqual(assistant(final)["content"], "next")
        self.assertEqual(len(self.requests("thread/start")), 1)
        self.assertEqual(self.requests("turn/start")[1]["params"]["input"][0]["text"], "more\nand this")

    def test_multiple_pending_calls_answer_before_user_delta_steer(self):
        self.use([[{"call": 1}, {"call": 2}], [{"text": "done"}]], wait_for_steer=True)
        request = body()
        first = self.channel.complete(request)
        self.channel.complete(self.extend(request, first, [("call-1", "one"), ("call-2", "two")], ["also this"]))
        answers = [r for r in self.rows() if r.get("id") in (1001, 1002)]
        self.assertEqual(answers[0]["result"]["contentItems"][0]["text"], "one")
        self.assertEqual(answers[1]["result"]["contentItems"][0]["text"], "two")
        steer = self.requests("turn/steer")[0]
        self.assertEqual(steer["params"], {"threadId": "thread-1", "expectedTurnId": "turn-1",
            "input": [{"type": "text", "text": "[message]\nalso this"}]})
        rows = self.rows()
        self.assertLess(rows.index(answers[0]), rows.index(answers[1]))
        self.assertLess(rows.index(answers[1]), rows.index(steer))

    def test_repeat_during_running_reuses_step_and_after_finish(self):
        self.use([[{"sleep": 0.3}, {"text": "once"}, {"usage": 7}]])
        request = body()
        step = self.channel._select(request)
        self.assertFalse(step.event.is_set())
        self.assertIs(self.channel._select(request), step)
        first = self.channel.complete(request)
        self.assertEqual(self.channel.complete(request), first)
        self.assertEqual(len(self.requests("turn/start")), 1)
        self.assertEqual(self.record.call_count, 1)
        self.assertEqual(self.channel.stats["repeats"], 3)

    def test_repeat_after_pause_ignores_tool_result_pacing_suffix(self):
        self.use([[{"call": 1}, {"usage": 10}], [{"text": "done"}]])
        request = body()
        first = self.channel.complete(request)
        delta = self.extend(request, first, [("call-1", "result")])
        second = self.channel.complete(delta)
        paused = copy.deepcopy(delta)
        paused["messages"][-1]["content"] += "\nPause before continuing."
        self.assertEqual(self.channel.complete(paused), second)
        self.assertEqual(self.channel.stats["repeats"], 1)
        self.assertEqual(len(self.requests("thread/start")), 1)
        self.assertEqual(len(self.requests("turn/start")), 1)
        changed_user = self.extend(request, first, [("call-1", "result")], ["new instruction"])
        self.assertNotEqual(planchannel._anchor(delta["messages"])[0], planchannel._anchor(changed_user["messages"])[0])
        changed_call = copy.deepcopy(delta)
        changed_call["messages"][-1]["tool_call_id"] = "other-call"
        self.assertNotEqual(planchannel._anchor(delta["messages"])[0], planchannel._anchor(changed_call["messages"])[0])

    def test_empty_step_fails_on_repeat(self):
        self.use([[{"finish": "completed"}]])
        for _ in range(2):
            with self.assertRaisesRegex(codex.CodexFailure, "empty step"):
                self.channel.complete(body())
        self.assertEqual(len(self.requests("turn/start")), 1)
        self.assertEqual(self.record.call_count, 1)

    def test_missing_anchor_rebuilds(self):
        self.use([[{"text": "hello"}], [{"text": "rebuilt"}]])
        self.channel.complete(body())
        final = self.channel.complete(body([{"role": "user", "content": "different"}]))
        self.assertEqual(assistant(final)["content"], "rebuilt")
        self.assertEqual(len(self.requests("thread/start")), 2)

    def test_tools_and_system_change_rebuild(self):
        for change in ("tools", "system"):
            with self.subTest(change=change):
                request = body()
                self.channel.complete(request)
                if change == "tools":
                    request["tools"] = []
                else:
                    request["messages"][0]["content"] = "new system"
                self.channel.complete(request)
        self.assertEqual(len(self.requests("thread/start")), 4)

    def test_rebuild_abandons_pending_call(self):
        self.use([[{"call": 1}], [{"text": "new"}]])
        self.channel.complete(body())
        self.assertEqual(assistant(self.channel.complete(body(tools=[])))["content"], "new")
        self.assertEqual(len(self.requests("turn/interrupt")), 1)
        self.assertFalse(any(r.get("id") == 1001 for r in self.rows()))

    def test_rebuild_interrupts_a_running_turn_and_releases_its_waiters(self):
        self.use([[{"sleep": 0.2}, {"text": "old"}], [{"text": "new"}]])
        old = self.channel._select(body())
        final = self.channel.complete(body(tools=[]))
        self.assertTrue(old.event.is_set())
        self.assertIn("abandoned", old.error.reason)
        self.assertEqual(assistant(final)["content"], "new")
        self.assertEqual(len(self.requests("turn/interrupt")), 1)

    def test_buffered_complete_rejects_failure_after_agent_deltas(self):
        self.use([[{"text": "partial reply"}, {"error": "failed after text"}]], hang=True)
        with self.assertRaisesRegex(codex.CodexFailure, "failed after text"):
            self.channel.complete(body())
        self.assertEqual(self.channel._step.text, "partial reply")

    def test_image_in_a_new_turn(self):
        url = "data:image/png;base64,bmV3"
        request = body([{"role": "user", "content": [
            {"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": url}}]}])
        self.channel.complete(request)
        inputs = self.requests("turn/start")[0]["params"]["input"]
        self.assertEqual(inputs[1:], [{"type": "image", "url": url}])
        self.assertNotIn(url, inputs[0]["text"])
        self.assertEqual(request["messages"][0]["content"][1]["image_url"]["url"], url)

    def test_image_in_user_only_delta_keeps_plain_text(self):
        self.use([[{"text": "hello"}], [{"text": "next"}]])
        request = body()
        first = self.channel.complete(request)
        self.channel.complete(self.extend(request, first, users=[[
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,bmV3"}}]]))
        self.assertEqual(self.requests("turn/start")[1]["params"]["input"], [
            {"type": "text", "text": "look"}, {"type": "image", "url": "data:image/png;base64,bmV3"}])

    def test_user_delta_with_pending_call_goes_by_turn_steer_not_in_result(self):
        self.use([[{"call": 1}, {"usage": 10}], [{"text": "done"}]], wait_for_steer=True)
        request = body()
        first = self.channel.complete(request)
        self.channel.complete(self.extend(request, first, [("call-1", "shown")], [[
            {"type": "text", "text": "[view_image: photo.png]"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,b25l"}},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,dHdv"}}]]))
        answer = next(r for r in self.rows() if r.get("id") == 1001)
        self.assertEqual(answer, {"id": 1001, "result": {"success": True, "contentItems": [
            {"type": "inputText", "text": "shown"}]}})
        steer = self.requests("turn/steer")[0]
        self.assertEqual(steer["params"], {"threadId": "thread-1", "expectedTurnId": "turn-1", "input": [
            {"type": "text", "text": "[message]\n[view_image: photo.png]"},
            {"type": "image", "url": "data:image/png;base64,b25l"},
            {"type": "image", "url": "data:image/png;base64,dHdv"}]})
        self.assertLess(self.rows().index(answer), self.rows().index(steer))
        self.assertEqual(len(self.requests("turn/start")), 1)

    def test_steer_no_active_turn_starts_new_turn_with_user_delta(self):
        self.use([[{"call": 1}, {"usage": 10}], [{"text": "continued"}]], wait_for_steer=True,
                 steer_turn_ended=True, steer_error={"code": -32600, "message": "no active turn to steer"})
        request = body()
        first = self.channel.complete(request)
        delta = self.extend(request, first, [("call-1", "shown")], [[
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,b25l"}}]])
        reply = self.channel.complete(delta)
        self.assertEqual(assistant(reply)["content"], "continued")
        starts = self.requests("turn/start")
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1]["params"]["input"], [
            {"type": "text", "text": "[message]\nlook"},
            {"type": "image", "url": "data:image/png;base64,b25l"}])
        self.assertEqual(self.channel._step.turn, "turn-2")
        self.assertTrue(self.channel._step.event.is_set())
        self.assertIsNone(self.channel._step.error)
        self.assertEqual(self.channel.complete(delta), reply)
        self.assertEqual(len(self.requests("thread/start")), 1)
        self.assertEqual(len([r for r in self.rows() if r.get("id") == 1001]), 1)

    def test_other_steer_error_marks_step_failed_and_repeat_fails(self):
        self.use([[{"call": 1}, {"usage": 10}]], wait_for_steer=True,
                 steer_error={"code": -32600, "message": "active turn cannot accept steering"})
        request = body()
        first = self.channel.complete(request)
        delta = self.extend(request, first, [("call-1", "shown")], ["look"])
        for _ in range(2):
            with self.assertRaisesRegex(codex.CodexFailure, "active turn cannot accept steering"):
                self.channel.complete(delta)
        self.assertTrue(self.channel._step.event.is_set())
        self.assertIsInstance(self.channel._step.error, codex.CodexFailure)
        self.assertTrue(self.channel._step.recorded)
        self.assertEqual(len(self.requests("turn/start")), 1)
        self.assertEqual(len(self.requests("turn/steer")), 1)

    def test_rebuild_carries_images_without_base64_in_text_and_numbers_markers(self):
        self.channel.complete(body())
        request = body([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,b25l"}},
            {"type": "text", "text": "first"}]},
            {"role": "assistant", "content": "old reply"},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,dHdv"}},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,dGhyZWU="}}]}])
        self.channel.complete(request)
        inputs = self.requests("turn/start")[1]["params"]["input"]
        self.assertNotIn("base64", inputs[0]["text"])
        transcript = json.loads(inputs[0]["text"].splitlines()[1])
        self.assertEqual(transcript[0]["content"], [
            {"type": "text", "text": "[image 1]"}, {"type": "text", "text": "first"}])
        self.assertEqual(transcript[2]["content"], [
            {"type": "text", "text": "[image 2]"}, {"type": "text", "text": "[image 3]"}])
        self.assertEqual(inputs[1:], [
            {"type": "image", "url": "data:image/png;base64,b25l"},
            {"type": "image", "url": "data:image/png;base64,dHdv"},
            {"type": "image", "url": "data:image/png;base64,dGhyZWU="}])
        self.assertEqual(len(self.requests("thread/start")), 2)

    def test_old_image_rewritten_to_text_does_not_rebuild(self):
        request = body([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,b25l"}}]}])
        first = self.channel.complete(request)
        delta = self.extend(request, first, users=["more"])
        delta["messages"][0]["content"] = "[image shown earlier, no longer in view: photo.png]"
        self.channel.complete(delta)
        self.assertEqual(len(self.requests("thread/start")), 1)

    def test_unsupported_part_type_is_named(self):
        with self.assertRaisesRegex(codex.CodexFailure, "input_audio"):
            self.channel.complete(body([{"role": "user", "content": [{"type": "input_audio"}]}]))
        self.assertIsNone(self.channel._process)

    def test_no_image_data_in_logs_or_usage_rows(self):
        self.channel.complete(body([{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,cHJpdmF0ZQ=="}}]}]))
        self.assertTrue(self.logs)
        self.record.assert_called_once()
        for output in (str(self.logs), str(self.record.call_args_list)):
            self.assertNotIn("cHJpdmF0ZQ==", output)
            self.assertNotIn("data:image", output)

    def test_benign_sleep_and_bookkeeping_items_do_not_trigger_security(self):
        items = [
            {"type": "sleep", "id": "s", "durationMs": 1000},
            {"type": "contextCompaction", "id": "c"},
            {"type": "plan", "id": "p", "text": "private plan"},
            {"type": "functionCallOutput", "id": "f", "name": "clock.sleep", "output": "done"},
            {"type": "userMessage"}, {"type": "agentMessage"},
            {"type": "reasoning"}, {"type": "dynamicToolCall"},
        ]
        for item in items:
            with self.subTest(kind=item["type"]):
                self.channel._check_item(item)
        self.use([[{"item": item} for item in items[:4]] + [{"text": "still running"}]])
        self.assertEqual(assistant(self.channel.complete(body()))["content"], "still running")

    def test_benign_item_allowlist_still_rejects_native_and_unknown_items(self):
        for kind in ("commandExecution", "fileChange", "webSearch", "imageView",
                     "imageGeneration", "collabAgentToolCall", "subAgentActivity",
                     "hookPrompt", "enteredReviewMode", "exitedReviewMode", "mcpToolCall",
                     "futureUnknownItem"):
            with self.subTest(kind=kind):
                with self.assertRaises(codex.CodexSecurity) as caught:
                    self.channel._check_item({"type": kind, "text": "private message"})
                self.assertEqual(caught.exception.reason, "Codex native item forbidden: " + kind)

    def test_forbidden_item_reason_keeps_type_and_tool_with_stderr(self):
        self.channel._stderr.extend(b"synthetic stderr")
        with self.assertRaises(codex.CodexSecurity) as caught:
            self.channel._check_item({"type": "mcpToolCall", "server": "codex", "tool": "exec",
                                      "arguments": {"text": "private message"}})
        self.assertEqual(caught.exception.reason,
                         "Codex native item forbidden: mcpToolCall; tool: exec; stderr: synthetic stderr")

    def test_native_item_fails_closed(self):
        self.use([[{"item": {"type": "commandExecution"}}]], hang=True)
        with self.assertRaises(codex.CodexSecurity):
            self.channel.complete(body())

    def test_mcp_resource_listing_allowed(self):
        self.use([[{"item": {"type": "mcpToolCall", "server": "codex", "tool": "list_mcp_resources"}},
                   {"text": "allowed"}]])
        self.assertEqual(assistant(self.channel.complete(body()))["content"], "allowed")

    def test_mcp_other_server_is_forbidden(self):
        self.use([[{"item": {"type": "mcpToolCall", "server": "other", "tool": "list_mcp_resources"}}]], hang=True)
        with self.assertRaises(codex.CodexSecurity):
            self.channel.complete(body())

    def test_approval_denied_and_security_failure(self):
        self.use([[{"request": "item/commandExecution/requestApproval"}, {"sleep": 0.1}]], hang=True)
        with self.assertRaises(codex.CodexSecurity):
            self.channel.complete(body())
        deadline = time.monotonic() + 1
        while not any(r.get("id") == 2000 for r in self.rows()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn({"id": 2000, "result": {"decision": "decline"}}, self.rows())

    def test_file_change_approval_denied(self):
        self.use([[{"request": "item/fileChange/requestApproval"}, {"sleep": 0.1}]], hang=True)
        with self.assertRaises(codex.CodexSecurity):
            self.channel.complete(body())
        deadline = time.monotonic() + 1
        while not any(r.get("id") == 2000 for r in self.rows()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn({"id": 2000, "result": {"decision": "decline"}}, self.rows())

    def test_unknown_notification_ignored(self):
        self.use([[{"notification": "future/status"}, {"text": "ok"}]])
        self.assertEqual(assistant(self.channel.complete(body()))["content"], "ok")

    def test_process_death_includes_stderr(self):
        self.use([[{"stderr": "synthetic tail"}, {"die": 4}]])
        with self.assertRaisesRegex(codex.CodexFailure, "process died.*synthetic tail"):
            self.channel.complete(body())

    def test_failed_turn(self):
        self.use([[{"finish": "failed"}]])
        with self.assertRaisesRegex(codex.CodexFailure, "turn failed.*synthetic failure"):
            self.channel.complete(body())

    def test_json_rpc_error(self):
        self.use(rpc_error=True)
        with self.assertRaisesRegex(codex.CodexFailure, "JSON-RPC.*input_too_large"):
            self.channel.complete(body())

    def test_malformed_rpc_result_fails(self):
        self.use(rpc_result={"turn": {}})
        with self.assertRaisesRegex(codex.CodexFailure, "malformed Codex RPC result"):
            self.channel.complete(body())

    def test_missing_auth_names_login(self):
        (self.home / "auth.json").unlink()
        with self.assertRaisesRegex(codex.CodexFailure, "shaft codex login"):
            self.channel.complete(body())
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.home / "config.toml").read_text(), "")

    def test_weekly_cap(self):
        self.use(weekly=85)
        with self.assertRaisesRegex(codex.CodexFailure, "weekly cap"):
            self.channel.complete(body())
        self.assertEqual(codex.rate_limit(), {"used_percent": 85, "window_mins": 10080, "resets_at": 12345})
        self.assertFalse(self.requests("thread/start"))

    def test_missing_weekly_window_fails_closed(self):
        self.use(window=300)
        with self.assertRaisesRegex(codex.CodexFailure, "weekly rate limit unavailable"):
            self.channel.complete(body())
        self.assertFalse(self.requests("thread/start"))

    def test_close_idempotent_removes_folder_and_process(self):
        self.channel.complete(body())
        folder = self.channel._folder
        process = self.channel._process
        self.channel.close()
        self.channel.close()
        self.assertFalse(folder.exists())
        self.assertIsNotNone(process.poll())
        with self.assertRaisesRegex(codex.CodexFailure, "closed"):
            self.channel.complete(body())

    def test_large_prompt_reaches_slow_child(self):
        self.use(read_delay=0.2)
        request = body([{"role": "user", "content": "x" * 300000}])
        self.channel.complete(request)
        sent = self.requests("turn/start")[0]["params"]["input"][0]["text"]
        self.assertEqual(json.loads(sent.splitlines()[1]), request["messages"])

    def test_environment_has_no_parent_secret(self):
        with mock.patch.dict(os.environ, {"PARENT_SECRET_CANARY": "never inherit"}):
            self.channel.complete(body())
        env = next(r["environment"] for r in self.rows() if "environment" in r)
        self.assertNotIn("PARENT_SECRET_CANARY", env)
        # macOS's Python adds __CF_USER_TEXT_ENCODING after exec, outside Popen's environment.
        self.assertEqual(set(env) - {"__CF_USER_TEXT_ENCODING"}, {"HOME", "PATH", "TMPDIR", "LANG", "CODEX_HOME"})
        self.assertEqual(env["CODEX_HOME"], str(self.home.resolve()))
        self.assertEqual(env["TMPDIR"], str(self.channel._folder))
        argv = next(r["argv"] for r in self.rows() if "argv" in r)
        self.assertNotIn("code_mode_host", argv)
        self.assertEqual([argv[i+1] for i, arg in enumerate(argv) if arg == "--disable"], list(codex.FEATURES))

    def test_read_only_prompt_suppresses_dynamic_tool_writes(self):
        self.channel.complete(body())
        argv = next(r["argv"] for r in self.rows() if "argv" in r)
        config = dict(argv[i+1].split("=", 1) for i, arg in enumerate(argv) if arg == "-c")
        for key in ("include_permissions_instructions", "include_environment_context",
                    "include_collaboration_mode_instructions"):
            with self.subTest(key=key):
                self.assertEqual(config.get(key), "false")
        self.assertEqual([argv[i+1] for i, arg in enumerate(argv) if arg == "--disable"], list(codex.FEATURES))
        self.assertEqual(config["web_search"], '"disabled"')
        self.assertEqual(config["agents.enabled"], "false")
        self.assertEqual(config["mcp_servers"], "{}")
        self.assertEqual(argv[-1], "app-server")
        self.assertEqual(self.requests("thread/start")[0]["params"]["sandbox"], "read-only")

    def test_usage_sums_last_and_records_once(self):
        self.use([[{"usage": 10}, {"usage": 20}, {"text": "answer"}]])
        reply = self.channel.complete(body())
        self.assertEqual(reply["usage"], {"prompt_tokens": 30, "completion_tokens": 4})
        self.channel.complete(body())
        self.assertEqual(self.record.call_count, 1)
        self.assertEqual(self.record.call_args.kwargs, {"cache": {"cached_input_tokens": 6}})
        self.assertEqual(self.channel.stats["tokens"], 34)
        self.assertTrue(all("answer" not in s for s in self.logs))

    def test_step_timeout_kills_child(self):
        self.use([[{"sleep": 10}]], hang=True)
        with mock.patch.object(codex, "TIMEOUT_S", 0.2):
            with self.assertRaisesRegex(codex.CodexFailure, "timeout"):
                self.channel.complete(body())
        self.channel._process.wait(timeout=1)

    def test_held_call_has_no_timeout_and_late_usage_is_recorded(self):
        self.use([[{"call": 1}, {"sleep": 0.25}, {"usage": 10}], [{"text": "done"}]])
        with mock.patch.object(codex, "TIMEOUT_S", 0.2):
            first = self.channel.complete(body())
            time.sleep(0.3)
            self.assertIsNone(self.channel._fatal)
            self.channel.complete(self.extend(body(), first, [("call-1", "ok")]))
        self.assertEqual(self.record.call_args_list[0].args[2:4], (10, 2))

    def test_zero_usage_reply_waits_for_usage_300ms_after_call(self):
        self.use([[{"call": 1}, {"sleep": 0.3}, {"usage": 10}]])
        reply = self.channel.complete(body())
        self.assertNotEqual(reply["usage"]["prompt_tokens"], 0)
        self.assertEqual(reply["usage"], {"prompt_tokens": 10, "completion_tokens": 2})

    def test_tool_step_without_usage_finishes_after_one_second(self):
        self.use([[{"call": 1}]])
        started = time.monotonic()
        reply = self.channel.complete(body())
        self.assertEqual(reply["choices"][0]["finish_reason"], "tool_calls")
        self.assertIs(reply["shaft_same_turn"], True)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)

    def test_model_polling_usage_during_hold_stays_on_held_step(self):
        self.use([[{"call": 1}, {"usage": 10}, {"sleep": 0.3}, {"usage": 20},
                   {"sleep": 0.1}, {"usage": 30}], [{"text": "done"}, {"usage": 40}]])
        first = self.channel.complete(body())
        held = self.channel._step
        with self.channel._cv:
            self.assertTrue(self.channel._cv.wait_for(lambda: held.usage["prompt_tokens"] == 60, timeout=2))
        self.assertEqual(self.record.call_count, 0)
        self.channel.complete(self.extend(body(), first, [("call-1", "ok")]))
        self.assertEqual([c.args[2:4] for c in self.record.call_args_list], [(60, 6), (40, 2)])
        self.assertEqual(self.channel.stats["tokens"], 108)

    def test_late_agent_text_cannot_mutate_reply_or_repeat_and_turn_still_ends(self):
        self.use([[{"call": 1}, {"usage": 10}, {"sleep": 0.3}, {"text": "final text"},
                   {"finish": "completed", "items": [{"type": "agentMessage", "id": "final",
                                                       "text": " from completion"}]}],
                  [{"text": "continued"}, {"usage": 20}]])
        request = body()
        first = self.channel.complete(request)
        saved = copy.deepcopy(first)
        held = self.channel._step
        with self.channel._cv:
            self.assertTrue(self.channel._cv.wait_for(lambda: held.turn_ended, timeout=2))
        self.assertEqual(held.text, "")
        self.assertEqual(first, saved)
        self.assertEqual(self.channel.complete(request), saved)
        delta = self.extend(request, first, [("call-1", "result")], ["continue"])
        reply = self.channel.complete(delta)
        self.assertEqual(assistant(reply)["content"], "continued")
        self.assertFalse(any(r.get("id") == 1001 for r in self.rows()))
        self.assertEqual(len(self.requests("thread/start")), 1)
        starts = self.requests("turn/start")
        self.assertEqual(len(starts), 2)
        self.assertEqual(json.loads(starts[1]["params"]["input"][0]["text"].splitlines()[1]),
                         delta["messages"][-2:])

    def test_late_tool_call_is_rejected_without_killing_channel(self):
        self.use([[{"call": 1}, {"usage": 10}, {"sleep": 0.3}, {"call": 2}], [{"text": "done"}]])
        first = self.channel.complete(body())
        with self.channel._cv:
            self.assertTrue(self.channel._cv.wait_for(lambda: self.channel.stats["late_calls"] == 1, timeout=2))
        reply = self.channel.complete(self.extend(body(), first, [("call-1", "ok")]))
        self.assertEqual(assistant(reply)["content"], "done")
        self.assertIsNone(self.channel._fatal)
        self.assertEqual([c["id"] for c in assistant(first)["tool_calls"]], ["call-1"])
        self.assertIn({"id": 1002, "result": {"success": False, "contentItems": [{"type": "inputText",
            "text": "not run: repeat this call after the other results arrive"}]}}, self.rows())
        self.assertTrue(any("late tool call" in line for line in self.logs))

    def test_non_object_arguments_fail(self):
        self.use([[{"call": 1, "arguments": "{}"}]], hang=True)
        with self.assertRaisesRegex(codex.CodexFailure, "invalid.*tool call"):
            self.channel.complete(body())

    def test_malformed_protocol_fails(self):
        self.use([[{"raw": "not JSON"}]], hang=True)
        with self.assertRaisesRegex(codex.CodexFailure, "malformed"):
            self.channel.complete(body())

    def test_seatbelt_closes_outside_home_and_limits_metadata_to_literal_ancestors(self):
        # Inspect the actual generator; sandbox-exec and the real Codex binary are not launched.
        personal_temp = self.root / "per-user-cache"
        with mock.patch.object(codex, "_seatbelt", new=self._real_seatbelt), \
                mock.patch.object(codex.sandbox, "user_folder", return_value=personal_temp):
            folder = self.root / "profile"
            folder.mkdir()
            auth = self.root / "synthetic-auth"
            argv = codex._seatbelt(["fake"], folder, self.home, auth)
        profile = (folder / "seatbelt.sb").read_text()
        lines = profile.splitlines()
        self.assertEqual(lines[:4], ["(version 1)", "(allow default)",
            '(deny file-read* file-write* (subpath ' + json.dumps(str(personal_temp)) + ') '
            '(subpath "/private/tmp") (subpath "/Users/Shared") (subpath "/Volumes"))',
            '(deny file-read* file-write* (subpath ' + json.dumps(str(Path.home().resolve())) + '))'])
        self.assertEqual(lines[4:7], [
            '(allow file-read* file-write* (subpath ' + json.dumps(str(self.home)) + '))',
            '(allow file-read* file-write* (subpath ' + json.dumps(str(folder)) + '))',
            '(allow file-read* file-write* (literal ' + json.dumps(str(auth)) + '))'])
        ancestors = sorted(set(self.home.parents) | set(folder.parents) | set(auth.parents))
        self.assertEqual(lines[7:], ['(allow file-read-metadata ' +
            ' '.join('(literal ' + json.dumps(str(parent)) + ')' for parent in ancestors) + ')'])
        self.assertEqual(argv, ["/usr/bin/sandbox-exec", "-f", str(folder / "seatbelt.sb"), "fake"])

    def test_close_during_rpc_wakes_waiter_immediately_as_codex_closed(self):
        self.use(hold_turn_start=True)
        sent = threading.Event()
        errors = []
        send = self.channel._send
        def track(message):
            send(message)
            if message.get("method") == "turn/start":
                sent.set()
        def complete():
            try:
                self.channel.complete(body())
            except Exception as exc:
                errors.append(exc)
        with mock.patch.object(self.channel, "_send", side_effect=track):
            worker = threading.Thread(target=complete)
            worker.start()
            self.assertTrue(sent.wait(2), "turn/start was not requested")
            self.channel.close()
            worker.join(timeout=1)
        self.assertFalse(worker.is_alive(), "close left the RPC waiting for its timeout")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], codex.CodexClosed)
        # A pipe error from a worker finishing after close must not turn shutdown into an outage.
        self.channel._fail(codex.CodexFailure("late pipe error"))
        self.assertIsInstance(self.channel._fatal, codex.CodexClosed)

    def test_select_rechecks_close_after_each_rpc(self):
        for target in ("turn/interrupt", "thread/start", "turn/start"):
            with self.subTest(rpc=target):
                channel = codex.Channel("broker", "usage", self.logs.append)
                channel._process = mock.Mock()
                if target == "turn/interrupt":
                    channel._session = "old-thread"
                    channel._step = planchannel.Step("codex/gpt-6-astra", "old-anchor", turn="old-turn", pending=[(1, "old")])
                    channel._step.event.set()
                def rpc(method, params):
                    if method == target:
                        channel.close()
                    return {"thread": {"id": "new-thread"}, "turn": {"id": "new-turn"}}
                with mock.patch.object(channel, "_kill"), mock.patch.object(channel, "_request", side_effect=rpc) as request:
                    with self.assertRaises(codex.CodexClosed):
                        channel.complete(body())
                self.assertEqual(request.call_args.args[0], target)
                self.assertIsInstance(channel._fatal, codex.CodexClosed)

    _real_seatbelt = staticmethod(codex._seatbelt)


if __name__ == "__main__":
    unittest.main()
