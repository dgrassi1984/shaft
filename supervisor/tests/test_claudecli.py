"""Claude stream-json mapping with a fake CLI and the production MCP bridge."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from supervisor import claudecli as claude, planchannel
from supervisor.tests.test_grokcli import assistant, png_image
from supervisor.tests.test_planbridge import require_sockets

FAKE = r'''
import json, os, queue, subprocess, sys, threading, time
from pathlib import Path
scenario = json.loads(Path(__file__).with_suffix('.json').read_text())
log = Path(__file__).with_suffix('.jsonl')
lock = threading.Lock()
cancel = threading.Event()
replies = queue.Queue()
worker = None
turn = 0
args = sys.argv[1:]
config = json.loads(Path(args[args.index('--mcp-config')+1]).read_text())
server = config['mcpServers']['shaft']
names = args[args.index('--allowedTools')+1:args.index('--permission-mode')]
names = [] if names == ['mcp__shaft__shaft_reserved_no_tools'] else names
def record(v):
 with lock:
  with log.open('a') as f: f.write(json.dumps(v)+'\n')
def send(v):
 with lock: print(json.dumps(v), flush=True)
record({'argv':args, 'pid':os.getpid(), 'env':dict(os.environ), 'server':server})
if scenario.get('bridge'):
 bridge = subprocess.Popen([server['command'],*server['args']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
 def read():
  for raw in bridge.stdout: replies.put(json.loads(raw))
 threading.Thread(target=read,daemon=True).start()
def usage():
 return {'input_tokens':7,'cache_read_input_tokens':3,'cache_creation_input_tokens':2,'output_tokens':5}
started = set()
def assistant(blocks, ident):
 if ident not in started:
  started.add(ident)
  send({'type':'stream_event','event':{'type':'message_start','message':{'id':ident,'usage':usage()}}})
 send({'type':'assistant','message':{'id':ident,'content':blocks,'usage':usage()}})
def delta():
 # A response's final usage: output is final here, unlike the assistant lines' snapshot.
 send({'type':'stream_event','event':{'type':'message_delta','usage':{**usage(),'output_tokens':40,
       'output_tokens_details':{'thinking_tokens':4}}}})
def finish(error=False, text='done', responses=1):
 send({'type':'result','subtype':'error_during_execution' if error else 'success','is_error':error,
       'result':text,'usage':{**usage(),'input_tokens':9*responses,'cache_read_input_tokens':3*responses,
                             'cache_creation_input_tokens':2*responses,'output_tokens':813,
                             'output_tokens_details':{'thinking_tokens':13}},'total_cost_usd':.0123})
def prompt(number):
 mode = scenario.get('mode','text')
 if mode == 'die': os._exit(3)
 if mode == 'malformed': print('not json private prompt',flush=True); return
 if mode == 'login': finish(True,'Not logged in: private prompt'); return
 send({'type':'system','subtype':'init','tools':names + (['Bash'] if mode == 'mismatch' else []),
       'permissionMode':'dontAsk','apiKeySource':'none','mcp_servers':[{'name':'shaft','status':'connected'}]})
 if mode == 'hang': return
 if mode in ('rate','rejected','warning'):
  send({'type':'rate_limit_event','rate_limit_info':{'status':{'rejected':'rejected','warning':'allowed_warning'}.get(mode,'allowed'),
        'unifiedWindows':{'seven_day':{'utilization':.5 if mode=='warning' else .87,'resetsAt':int(time.time())+86400}}}})
 if mode == 'limit': finish(True,'Claude AI usage limit reached|1795032000 private prompt'); return
 if mode == 'native': assistant([{'type':'tool_use','name':'Bash','input':{'command':'private prompt'}}],'a'); return
 if mode == 'error': finish(True,'private prompt'); return
 if mode in ('calls','parallel','cycle_end') and names:
  count = 2 if mode == 'parallel' else 1
  for n in range(count):
   assistant([{'type':'tool_use','id':str(n),'name':'mcp__shaft__echo','input':{'n':n}}],'a'+str(number))
  delta()
  for n in range(count):
   bridge.stdin.write(json.dumps({'id':n+10,'method':'tools/call','params':{'name':'echo','arguments':{'n':n}}})+'\n')
   bridge.stdin.flush()
   while not cancel.is_set():
    try: value = replies.get(timeout=.02); break
    except queue.Empty: pass
   if cancel.is_set(): return
   record({'bridge_result':value})
   if mode == 'cycle_end': return  # the cycle ends on this call: no later response, no result
  assistant([{'type':'text','text':'answered'}],'b'+str(number)); delta()
 else:
  assistant([{'type':'thinking','thinking':'ignored'},{'type':'text','text':'hello'}],'a'+str(number))
  if mode == 'duplicate': assistant([{'type':'thinking','thinking':'ignored'}],'a'+str(number))
  delta()
 finish(responses=2 if mode in ('calls','parallel') and names else 1)
for raw in sys.stdin:
 value = json.loads(raw); record(value)
 if value['type']=='user':
  cancel.clear(); turn += 1
  worker=threading.Thread(target=prompt,args=(turn,),daemon=True); worker.start()
 elif value['type']=='control_request':
  cancel.set()
  if worker: worker.join(1)
  send({'type':'control_response','response':{'subtype':'success','request_id':value['request_id']}})
  finish(True,'interrupted')
'''


def body(messages=None, **kw):
    return {'model': 'claudecode/claude-opus-5-5', 'messages': messages if messages is not None else [
        {'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'private prompt'}],
        'tools': [{'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}}], **kw}


class DummyBridge:
    def __init__(self, channel, tools, folder):
        self.address = 'unused'
        self.tools_path = folder / 'tools.json'
        self.stopped = threading.Event()
    def close(self):
        self.stopped.set()


REAL_BRIDGE = claude._Bridge


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='ct-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.fake = self.root / 'claude'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE)
        self.fake.chmod(0o700)
        self.enterContext(mock.patch.object(claude, 'BIN', str(self.fake)))
        self.enterContext(mock.patch.object(claude, 'HOME', self.root / 'home'))
        self.enterContext(mock.patch.object(claude, 'TIMEOUT_S', 3))
        self.enterContext(mock.patch.object(claude, 'WEEKLY_PCT', 85))
        self.enterContext(mock.patch.object(claude.STATE, 'rate', None))
        self.login = self.enterContext(mock.patch.object(claude, 'login_status', return_value={'loggedIn': True}))
        self.record = self.enterContext(mock.patch.object(planchannel.cloudllm, '_record'))
        self.enterContext(mock.patch.object(claude, '_Bridge', DummyBridge))
        self.enterContext(mock.patch.object(claude, '_seatbelt', side_effect=lambda argv, folder, home: argv))
        self.channel = claude.Channel('example', 'example', mock.Mock())
        self.addCleanup(self.channel.close)
        self.scenario()

    def scenario(self, **kw):
        self.fake.with_suffix('.json').write_text(json.dumps(kw))

    def wire(self):
        path = self.fake.with_suffix('.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def real_bridge(self, **kw):
        require_sockets(self)
        self.enterContext(mock.patch.object(claude, '_Bridge', REAL_BRIDGE))
        self.scenario(bridge=True, **kw)

    def test_text_repeat_extend_and_launch_isolation(self):
        first = self.channel.complete(body())
        self.assertEqual(assistant(first)['content'], 'hello')
        self.assertEqual(first['usage'], {'prompt_tokens': 14, 'completion_tokens': 813})
        self.assertEqual(self.record.call_args.kwargs['cache'], {'cached_input_tokens': 3,
            'cache_write_tokens': 2, 'reasoning_tokens': 13, 'api_equivalent_usd': .0123})
        self.assertIsNone(self.record.call_args.args[4])
        self.channel.complete(body())
        self.assertEqual(self.channel.stats['repeats'], 1)
        pid = self.channel._process.pid
        self.channel.complete(body(body()['messages'] + [assistant(first), {'role': 'user', 'content': 'next'}]))
        self.assertEqual(self.channel._process.pid, pid)
        self.assertEqual(self.channel.stats['extends'], 1)
        self.assertEqual(self.login.call_count, 1)
        launch = self.wire()[0]
        argv, env = launch['argv'], launch['env']
        for flag, value in (('--tools', ''), ('--setting-sources', ''), ('--permission-mode', 'dontAsk'),
                            ('--permission-prompts', 'none'), ('--model', 'claude-opus-5-5')):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        for flag in ('--strict-mcp-config', '--disable-slash-commands', '--no-session-persistence', '--system-prompt-file'):
            self.assertIn(flag, argv)
        self.assertNotIn('--bare', argv)
        self.assertEqual(env['HOME'], str(Path.home()))
        self.assertEqual(env['CLAUDE_CODE_TMPDIR'], str(self.channel._folder))
        self.assertEqual(env['CLAUDE_CONFIG_DIR'], str(self.root / 'home'))
        self.assertNotIn('ANTHROPIC_API_KEY', env)
        self.assertTrue((self.channel._folder / '.git').is_dir())
        self.assertEqual(launch['server']['command'], str(Path(sys.executable).resolve()))
        self.assertEqual(launch['server']['args'][:3], ['-I', '-B', str(claude.BRIDGE)])
        self.assertNotIn('private prompt', str(self.channel.log.call_args_list))

    def test_duplicate_message_inputs_and_final_output_reconciliation(self):
        self.scenario(mode='duplicate')
        reply = self.channel.complete(body())
        self.assertEqual(reply['usage'], {'prompt_tokens': 14, 'completion_tokens': 813})
        self.assertEqual(self.record.call_args.kwargs['cache']['cached_input_tokens'], 3)

    def test_effort_model_system_and_tools_changes_restart(self):
        self.channel.complete(body())
        for changes in ({'reasoning_effort': 'high'}, {'model': 'claudecode/other'},
                        {'messages': [{'role': 'system', 'content': 'new'}, {'role': 'user', 'content': 'next'}]},
                        {'tools': []}):
            pid = self.channel._process.pid
            self.channel.complete(body(**changes))
            self.assertNotEqual(self.channel._process.pid, pid)
        launches = [r['argv'] for r in self.wire() if 'argv' in r]
        self.assertEqual(launches[1][launches[1].index('--effort')+1], 'high')
        self.assertEqual(launches[-1][launches[-1].index('--allowedTools')+1], 'mcp__shaft__'+claude.RESERVED)

    def test_system_prompt_goes_by_file_without_a_cli_probe(self):
        # Regression: every session open used to run `claude --help` under the sandbox to find this flag.
        with mock.patch.object(claude.subprocess, 'run', side_effect=AssertionError('no CLI probe at launch')):
            self.channel.complete(body())
        args = self.wire()[0]['argv']
        self.assertNotIn('--system-prompt', args)
        self.assertEqual(Path(args[args.index('--system-prompt-file')+1]).read_text(), 'system')

    def test_allowed_warning_is_not_a_refusal(self):
        # Regression: any status other than 'allowed' failed the run, but Claude Code reports
        # 'allowed_warning' as a normal state near the limit.
        self.scenario(mode='warning')
        self.assertEqual(assistant(self.channel.complete(body()))['content'], 'hello')
        self.assertEqual(claude.STATE.rate['used_percent'], 50)

    def test_usage_limit_result_is_named_without_echoing_text(self):
        self.scenario(mode='limit')
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'Claude usage limit reached') as caught:
            self.channel.complete(body())
        self.assertNotIn('private prompt', str(caught.exception))

    def test_restart_never_leaves_the_channel_muted(self):
        # Regression: a slow worker drain raised inside _stop_process with _restarting still True,
        # which silenced every later failure on that channel.
        self.channel.complete(body())
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        straggler = threading.Thread(target=stuck.wait, daemon=True)
        straggler.start()
        self.channel._workers.append(straggler)  # a worker that outlives the drain deadline
        self.channel._stop_process()
        self.assertFalse(self.channel._restarting)
        self.channel.complete(body(reasoning_effort='high'))
        self.assertIsNotNone(self.channel._process)

    def test_missing_login_fails_before_launch(self):
        self.login.return_value = {'loggedIn': False}
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'Claude login missing; run shaft claude login'):
            self.channel.complete(body())
        self.assertEqual(self.wire(), [])

    def test_login_error_result(self):
        self.scenario(mode='login')
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'Claude login missing; run shaft claude login'):
            self.channel.complete(body())

    def test_security_failures_and_safe_errors(self):
        for mode, error, reason in (('native', planchannel.ChannelSecurity, 'native tool forbidden'),
                                    ('mismatch', planchannel.ChannelSecurity, 'init tools mismatch'),
                                    ('error', planchannel.ChannelFailure, 'turn failed'),
                                    ('malformed', planchannel.ChannelFailure, 'malformed Claude protocol'),
                                    ('die', planchannel.ChannelFailure, 'process died'),
                                    ('rejected', planchannel.ChannelFailure, 'rate limit rejected')):
            with self.subTest(mode=mode):
                self.scenario(mode=mode)
                c = claude.Channel('example', 'example', mock.Mock())
                try:
                    with self.assertRaisesRegex(error, reason) as caught:
                        c.complete(body())
                    self.assertNotIn('private prompt', str(caught.exception))
                    self.assertNotIn('private prompt', str(c.log.call_args_list))
                finally:
                    c.close()

    def test_weekly_cap_and_generic_persistence(self):
        self.scenario(mode='rate')
        reply = self.channel.complete(body())
        expected = {'used_percent': 87, 'window_mins': 10080, 'resets_at': claude.STATE.rate['resets_at']}
        self.assertGreater(expected['resets_at'], time.time())
        self.assertEqual(claude.STATE.rate, expected)
        with mock.patch.object(planchannel, 'SUP_DIR', self.root):
            planchannel.save_rate(None, 'claudecode')
            self.assertEqual(json.loads((self.root / 'claudecode_rate.json').read_text()), expected)
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'weekly cap'):
            self.channel.complete(body(body()['messages'] + [assistant(reply), {'role': 'user', 'content': 'next'}]))
        self.assertEqual(len([r for r in self.wire() if r.get('type') == 'user']), 1)

    def test_weekly_cap_past_its_reset_no_longer_blocks(self):
        # Review finding: an above-cap window whose reset had passed kept refusing every new session, and only a
        # new session can observe the new window.
        claude.STATE.rate = {'used_percent': 90, 'window_mins': 10080, 'resets_at': time.time() - 60}
        self.assertEqual(assistant(self.channel.complete(body()))['content'], 'hello')
        claude.STATE.rate = {'used_percent': 90, 'window_mins': 10080, 'resets_at': time.time() + 3600}
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'weekly cap'):
            claude.Channel('example', 'example', mock.Mock()).complete(body())

    def test_retired_bridge_never_fails_its_replacement(self):
        # Review finding: a late callback on a closed bridge called _fail on the channel and killed the new process.
        self.channel.complete(body())
        old = REAL_BRIDGE.__new__(REAL_BRIDGE)  # setUp swaps claude._Bridge for a dummy
        old.channel, old.stopped = self.channel, threading.Event()
        old.stopped.set()
        old._fail(planchannel.ChannelClosed('Claude bridge closed'))
        self.assertIsNone(self.channel._fatal)
        self.assertIsNone(self.channel._process.poll())

    def test_repeated_usage_report_does_not_mark_the_step(self):
        # Review finding: a repeated report with zero new usage set usage_received, flipping shaft_same_turn.
        self.channel.complete(body())
        self.channel._step.usage_received = False
        ident = next(iter(self.channel._turn['messages']))
        self.channel._account(ident, {'input_tokens': 7, 'cache_read_input_tokens': 3,
                                      'cache_creation_input_tokens': 2}, False)
        self.assertFalse(self.channel._step.usage_received)

    def test_late_usage_on_a_recorded_step_gets_its_own_row(self):
        # Review finding: a cancel or rebuild records the step before the interrupt lands; usage arriving after
        # that was added to a step that is never written again, so it was lost.
        self.channel.complete(body())
        step = self.channel._turn['step']
        self.assertTrue(step.recorded)
        before = self.record.call_count
        self.channel._account('late-response', {'input_tokens': 5, 'output_tokens': 30}, True)
        self.assertEqual(self.record.call_count, before + 1)
        row = self.record.call_args
        self.assertEqual(row.args[2:4], (5, 30))
        self.assertEqual(row.args[6], 'late_usage')

    def test_no_call_is_handed_out_before_init(self):
        # Review finding: a bridge call could complete as tool_calls while the turn's init (the tool check) had
        # not been read yet.
        self.channel.complete(body())
        step = self.channel._step
        self.channel._turn['initialized'] = False
        step.calls.append({'id': 'x'})
        step.last_call = 0
        step.usage_received = True
        self.assertFalse(self.channel._calls_ready(step, time.monotonic()))
        self.channel._turn['initialized'] = True
        self.assertTrue(self.channel._calls_ready(step, time.monotonic()))

    def test_tool_search_is_off_and_a_refused_tool_is_named(self):
        # Regression (bake-off 2026-11-12): Claude Code's server-side tool search failed a cycle as an unnamed
        # "native tool forbidden", then "bridge died" replaced that cause.
        self.channel.complete(body())
        self.assertEqual(self.wire()[0]['env']['ENABLE_TOOL_SEARCH'], 'false')
        self.scenario(mode='native')
        c = claude.Channel('example', 'example', mock.Mock())
        self.addCleanup(c.close)
        with self.assertRaisesRegex(planchannel.ChannelSecurity, 'native tool forbidden: tool_use Bash'):
            c.complete(body())
        c._fail(planchannel.ChannelFailure('Claude bridge died'))
        self.assertIn('native tool forbidden', c._fatal.reason)

    def test_step_timeout(self):
        self.scenario(mode='hang')
        with mock.patch.object(claude, 'TIMEOUT_S', .2):
            with self.assertRaisesRegex(planchannel.ChannelFailure, 'step timeout'):
                self.channel.complete(body())

    def test_images_and_malformed_image_refusal(self):
        url = png_image((8, 8))
        self.assertEqual(self.channel._images([url]), [{'type': 'image', 'source': {
            'type': 'base64', 'media_type': 'image/png', 'data': url.split(',')[1]}}])
        for bad in (png_image((7, 8)), 'data:image/png;base64,broken', url.replace('image/png', 'image/jpeg')):
            with self.assertRaisesRegex(planchannel.ChannelFailure, 'image refused'):
                self.channel._images([bad])
        self.channel.complete(body([{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': url}}]}]))
        user = next(r for r in self.wire() if r.get('type') == 'user')
        self.assertEqual(user['message']['content'][-1]['source']['data'], url.split(',')[1])

    def test_cycle_ending_on_a_tool_call_still_books_output_and_cost(self):
        # Regression (replay 2026-11-12): output was booked only from the turn's `result`, which never comes when
        # the cycle ends on a tool call, so every usage row had output 0 and API equivalent 0.
        self.real_bridge(mode='cycle_end')
        reply = self.channel.complete(body())
        self.assertEqual(len(assistant(reply)['tool_calls']), 1)
        self.channel.close()
        (row,) = self.record.call_args_list
        self.assertEqual(row.args[2:4], (12, 40))
        self.assertEqual(row.kwargs['cache']['reasoning_tokens'], 4)
        self.assertGreater(row.kwargs['cache']['api_equivalent_usd'], 0)
        self.assertAlmostEqual(row.kwargs['cache']['api_equivalent_usd'], places=6, second=
            planchannel.cloudllm.usd('claude-opus-5-5', planchannel.cloudllm.billed(
                {'input_tokens': 7, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 2, 'output_tokens': 40})))

    def test_parallel_calls_are_held_and_answered_one_at_a_time(self):
        self.real_bridge(mode='parallel')
        messages = body()['messages']
        first = self.channel.complete(body(messages))
        self.assertEqual(first['usage'], {'prompt_tokens': 12, 'completion_tokens': 40})
        pid = self.channel._process.pid
        for n, reply in enumerate([first, None]):
            if n:
                reply = self.channel.complete(body(messages))
                self.assertEqual(reply['usage'], {'prompt_tokens': 0, 'completion_tokens': 0})
                self.assertFalse(self.channel._step.usage_received)
                self.assertTrue(reply['shaft_same_turn'])
            calls = assistant(reply)['tool_calls']
            self.assertEqual(len(calls), 1)
            self.assertEqual(json.loads(calls[0]['function']['arguments']), {'n': n})
            self.assertFalse(self.channel._step.turn_ended)
            messages += [assistant(reply), {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': str(n)}]
        final = self.channel.complete(body(messages))
        self.assertEqual(assistant(final)['content'], 'answered')
        self.assertEqual(final['usage'], {'prompt_tokens': 16, 'completion_tokens': 773})
        self.assertEqual(self.channel._process.pid, pid)
        self.assertEqual([r['bridge_result']['result']['content'] for r in self.wire() if 'bridge_result' in r],
                         [[{'type': 'text', 'text': '0'}], [{'type': 'text', 'text': '1'}]])
        self.assertEqual(sum(c.args[2] for c in self.record.call_args_list), 28)
        self.assertEqual(sum(c.args[3] for c in self.record.call_args_list), 813)
        self.assertAlmostEqual(sum(c.kwargs['cache']['api_equivalent_usd'] for c in self.record.call_args_list), .0123)

    def test_held_call_later_answer_includes_user_text_and_mcp_image(self):
        self.real_bridge(mode='calls')
        first = self.channel.complete(body())
        time.sleep(.25)
        self.assertEqual([r for r in self.wire() if 'bridge_result' in r], [])
        call = assistant(first)['tool_calls'][0]
        url = png_image((8, 8))
        messages = body()['messages'] + [assistant(first), {'role': 'tool', 'tool_call_id': call['id'], 'content': 'ready'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'look'}, {'type': 'image_url', 'image_url': {'url': url}}]}]
        reply = self.channel.complete(body(messages))
        self.assertEqual(assistant(reply)['content'], 'answered')
        result = next(r['bridge_result']['result'] for r in self.wire() if 'bridge_result' in r)
        self.assertEqual(result['content'], [{'type': 'text', 'text': 'ready'}, {'type': 'text', 'text': '[message]\nlook'},
            {'type': 'image', 'mimeType': 'image/png', 'data': url.split(',')[1]}])

    def test_interrupt_held_call_and_continue_same_process(self):
        self.real_bridge(mode='calls')
        self.channel.complete(body())
        pid = self.channel._process.pid
        with self.channel._cv:
            self.channel._cancel_turn(self.channel._step.turn)
            self.assertTrue(self.channel._step.turn_ended)
            self.assertIsNone(self.channel._fatal)
            self.channel._step = planchannel.Step(body()['model'], 'next')
            self.channel._start_turn('next', [], None)
        self.assertTrue(self.channel._step.event.wait(2))
        self.assertEqual(len(self.channel._step.calls), 1)
        self.assertEqual(self.channel._process.pid, pid)
        self.assertTrue(any(r.get('type') == 'control_request' for r in self.wire()))

    def test_rebuild_interrupts_held_call_and_closes_old_bridge(self):
        self.real_bridge(mode='calls')
        self.channel.complete(body())
        bridge, pid = self.channel._bridge, self.channel._process.pid
        reply = self.channel.complete(body([{'role': 'user', 'content': 'different'}], tools=[]))
        self.assertEqual(assistant(reply)['content'], 'hello')
        self.assertTrue(bridge.stopped.is_set())
        self.assertNotEqual(self.channel._process.pid, pid)


class MappingTest(unittest.TestCase):
    """Protocol checks that also run when Unix sockets are unavailable."""
    def setUp(self):
        self.c = claude.Channel('example', 'example', mock.Mock())
        self.c._tool_names = {'echo'}
        self.c._bridge = DummyBridge(None, [], Path('/unused'))
        self.c._step = planchannel.Step(body()['model'], 'first')
        self.record = self.enterContext(mock.patch.object(planchannel.cloudllm, '_record'))
        self.c._start_turn('text', [], None)
        self.c._outgoing.get_nowait()
        self.dispatch({'type': 'system', 'subtype': 'init', 'tools': ['mcp__shaft__echo'],
                       'permissionMode': 'dontAsk', 'apiKeySource': 'none',
                       'mcp_servers': [{'name': 'shaft', 'status': 'connected'}]})

    def dispatch(self, message):
        with self.c._cv:
            self.c._dispatch(message)

    def response(self, ident='a', blocks=None):
        self.dispatch({'type': 'assistant', 'message': {'id': ident, 'content': blocks or [
            {'type': 'tool_use', 'name': 'mcp__shaft__echo'}], 'usage': {
            'input_tokens': 7, 'cache_read_input_tokens': 3, 'cache_creation_input_tokens': 2, 'output_tokens': 5}}})

    def call(self, n):
        answer = queue.Queue()
        self.c._bridge_call(self.c._bridge, {'params': {'name': 'echo', 'arguments': {'n': n}}}, answer)
        return answer

    def advance(self, answer, text):
        old = self.c._step
        with self.c._cv:
            self.c._finish(old, 'tool_calls')
            self.c._record(old)
            self.c._step = planchannel.Step(old.model, 'next', turn=old.turn)
            self.assertTrue(self.c._answer_calls(old.pending, {old.calls[0]['id']: text}, '', []))
        self.assertEqual(answer.get_nowait(), {'isError': False, 'content': [{'type': 'text', 'text': text}]})

    def test_sequential_calls_usage_and_duplicate_assistant_inputs(self):
        self.response()
        self.response()  # Separate tool blocks repeat the same message's usage.
        one = self.call(1)
        self.assertEqual(self.c._step.usage, {'prompt_tokens': 12, 'completion_tokens': 0})
        self.advance(one, 'one')
        two = self.call(2)
        self.assertTrue(self.c._calls_ready(self.c._step, self.c._step.last_call + .2))
        self.assertTrue(self.c._completion(self.c._step)['shaft_same_turn'])
        self.assertEqual(self.c._step.usage, {'prompt_tokens': 0, 'completion_tokens': 0})
        self.advance(two, 'two')
        self.response('b', [{'type': 'text', 'text': 'answer'}])
        self.dispatch({'type': 'result', 'subtype': 'success', 'is_error': False,
            'usage': {'input_tokens': 18, 'cache_read_input_tokens': 6, 'cache_creation_input_tokens': 4,
                      'output_tokens': 813, 'output_tokens_details': {'thinking_tokens': 13}}, 'total_cost_usd': .0123})
        self.assertEqual(self.c._step.usage, {'prompt_tokens': 16, 'completion_tokens': 813})
        self.assertEqual(self.c._usage_cache(self.c._step), {'cached_input_tokens': 3, 'cache_write_tokens': 2,
            'reasoning_tokens': 13, 'api_equivalent_usd': .0123})
        self.assertTrue(self.c._step.turn_ended)
        self.assertEqual(self.c._step.text, 'answer')
        with self.c._cv:
            self.c._finish(self.c._step, 'stop')
        self.assertEqual(sum(call.args[2] for call in self.record.call_args_list), 28)
        self.assertEqual(sum(call.args[3] for call in self.record.call_args_list), 813)

    def test_cancel_releases_held_answer_and_waits_for_correlated_ack_and_result(self):
        self.response()
        answer = self.call(1)
        errors = []
        def cancel():
            try:
                self.c._cancel_turn(self.c._step.turn)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=cancel)
        thread.start()
        try:
            interrupt = self.c._outgoing.get(timeout=1)
            self.assertEqual(interrupt['request'], {'subtype': 'interrupt'})
            self.assertTrue(answer.get(timeout=1)['isError'])
            self.dispatch({'type': 'control_response', 'response': {'request_id': 'unrelated', 'subtype': 'success'}})
            self.dispatch({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True})
            self.assertTrue(thread.is_alive())
            with self.c._cv:
                self.c._dispatch({'type': 'control_response', 'response': {
                    'request_id': interrupt['request_id'], 'subtype': 'success'}})
                self.c._cv.notify_all()
            thread.join(1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(self.c._bridge.stopped.is_set())
        finally:
            with self.c._cv:
                self.c._fatal = planchannel.ChannelClosed('test done')
                self.c._cv.notify_all()
            thread.join(1)

    def test_cancel_timeout_is_bounded(self):
        with mock.patch.object(claude, 'TIMEOUT_S', .02):
            with self.assertRaisesRegex(planchannel.ChannelFailure, 'cancel timeout'):
                self.c._cancel_turn(self.c._step.turn)

    def test_init_authentication_server_and_tool_checks(self):
        good = {'type': 'system', 'subtype': 'init', 'tools': ['mcp__shaft__echo'],
                'permissionMode': 'dontAsk', 'apiKeySource': 'none',
                'mcp_servers': [{'name': 'shaft', 'status': 'connected'}]}
        for change in ({'tools': []}, {'tools': ['mcp__shaft__echo', 'Bash']},
                       {'permissionMode': 'bypassPermissions'}, {'apiKeySource': 'env'}):
            with self.assertRaises(planchannel.ChannelSecurity):
                self.dispatch({**good, **change})
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'server unavailable'):
            self.dispatch({**good, 'mcp_servers': [{'name': 'shaft', 'status': 'failed'}]})
        for name in ('Bash', 'mcp__other__echo', 'mcp__shaft__unknown', 'private prompt'):
            with self.assertRaisesRegex(planchannel.ChannelSecurity, '^Claude native tool forbidden: tool_use ') as caught:
                self.response(blocks=[{'type': 'tool_use', 'name': name}])
            self.assertNotIn('private prompt', str(caught.exception))
        with self.assertRaises(planchannel.ChannelSecurity):
            self.dispatch({'type': 'control_request', 'request': {'subtype': 'can_use_tool'}})

    def test_no_stderr_or_cli_error_content_in_failure(self):
        self.c._stderr.extend(b'private prompt')
        self.assertEqual(str(self.c._failure('failed')), 'failed')
        with self.assertRaisesRegex(planchannel.ChannelFailure, '^Claude turn failed$'):
            self.dispatch({'type': 'result', 'is_error': True, 'result': 'private prompt'})
        self.assertNotIn('private prompt', str(self.c.log.call_args_list))

    def test_seatbelt_allows_only_literal_keychain_read_and_required_paths(self):
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='ct-') as folder:
            root = Path(folder).resolve()
            binary = root / 'versions' / 'claude'
            binary.parent.mkdir()
            binary.touch()
            link = root / 'claude'
            link.symlink_to(binary)
            with mock.patch.object(claude, 'BIN', str(link)), \
                    mock.patch.object(claude.sandbox, '_operator_places_closed', return_value=['; closed places']), \
                    mock.patch.object(claude.sandbox, 'wrap', side_effect=lambda path, argv: argv):
                claude._seatbelt(['cli'], root, root / 'config')
            profile = (root / 'seatbelt.sb').read_text()
            keychain = Path.home().resolve() / 'Library/Keychains/login.keychain-db'
            self.assertIn('(allow file-read* (literal '+json.dumps(str(keychain))+'))', profile)
            self.assertNotIn('(subpath '+json.dumps(str(keychain.parent)), profile)
            self.assertIn('(deny file-read* file-write* (subpath '+json.dumps(str(Path.home().resolve()))+'))', profile)
            for reader in (binary.parent, Path(sys.executable).resolve().parent, Path(sys.base_prefix).resolve()):
                self.assertIn('(allow file-read* (subpath '+json.dumps(str(reader))+'))', profile)
            self.assertIn('(allow file-read* (literal '+json.dumps(str(claude.BRIDGE))+'))', profile)
            self.assertIn('; closed places', profile)
            for path in (root, root / 'config'):
                self.assertIn('(allow file-read* file-write* (subpath '+json.dumps(str(path))+'))', profile)


class WiringTest(unittest.TestCase):
    def test_provider_models_and_bakeoff_redaction(self):
        from supervisor import bakeoff, core, settings
        model = 'claudecode/claude-opus-5-5'
        self.assertEqual(core.instance_model(model), model)
        with self.assertRaises(ValueError):
            core.cloud_model(model)
        self.assertIs(planchannel.provider(model), claude)
        self.assertEqual(planchannel.vendor(model), 'anthropic')
        self.assertIn('claudecli', settings.MODULES)
        self.assertEqual(bakeoff.parse_arm(model+'@high', ('high',))['model'], model)
        self.assertEqual(bakeoff._redactor({'arms': []})(model), '[model]')

    def test_costs_and_outage_are_provider_specific(self):
        from datetime import datetime, timedelta
        from supervisor import panel, trial, web_views
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='ct-') as folder:
            root = Path(folder)
            rows = [{'ts': datetime.now().isoformat(), 'model': 'claudecode/claude-opus-5-5',
                     'usd': None, 'api_equivalent_usd': .0123},
                    {'ts': (datetime.now()-timedelta(days=1)).isoformat(), 'model': 'claudecode/claude-opus-5-5',
                     'usd': None, 'api_equivalent_usd': 10},
                    {'ts': datetime.now().isoformat(), 'model': 'grok/grok-4.7', 'usd': None, 'api_equivalent_usd': .5}]
            (root / 'cloud_usage.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))
            (root / 'claudecode_outage.json').write_text('{"reason":"native tool forbidden","security":true}')
            (root / 'claudecode_rate.json').write_text('{"used_percent":7,"window_mins":10080,"resets_at":1795032000}')
            with mock.patch.object(panel, 'SUP_DIR', root), mock.patch.object(planchannel, 'SUP_DIR', root), \
                    mock.patch.object(trial, 'LEDGER', root/'trials.jsonl'), \
                    mock.patch.object(panel, 'instance_names', return_value=[]):
                costs = panel.costs()
                self.assertEqual(costs['claude_api_equivalent'], .0123)
                self.assertEqual(costs['grok_api_equivalent'], .5)
                self.assertEqual(costs['unpriced'], 0)
                self.assertEqual(costs['today']['total'], 0)
                self.assertIn('Claude plan: $0.012300 API equivalent (not money spent)',
                              str(web_views.costs_page(costs, [], 'csrf')))
                with mock.patch.object(claude.STATE, 'rate', None):  # the persisted window, as another process sees it
                    self.assertIn('Claude plan: 7% of weekly allowance used',
                                  str(web_views.costs_page(panel.costs(), [], 'csrf')))
                with mock.patch.object(panel, '_log_failures', return_value=[]), \
                        mock.patch.object(panel, 'provider_alerts', return_value=[]), \
                        mock.patch.object(panel.core, 'long_pauses', return_value=[]), \
                        mock.patch.object(panel, 'CORRECTION_ALERT_CYCLES', 0):
                    self.assertEqual(panel.alerts(cost=costs), [{'id': 'provider:claudecode:outage',
                        'text': 'Claude security event: native tool forbidden'}])
