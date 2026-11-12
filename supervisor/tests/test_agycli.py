"""Antigravity ACP contract exercised through a fake CLI and the production MCP relay."""
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from supervisor import agycli as agy, planchannel
from supervisor.tests.test_claudecli import DummyBridge
from supervisor.tests.test_grokcli import assistant
from supervisor.tests.test_planbridge import require_sockets

FAKE = r'''
import json, os, queue, subprocess, sys, threading
from pathlib import Path
scenario = json.loads(Path(__file__).with_suffix('.json').read_text())
log = Path(__file__).with_suffix('.jsonl')
lock = threading.Lock()
server = json.loads((Path.home()/'.gemini/config/mcp_config.json').read_text())['mcpServers']['shaft']
sid = 'conversation-'+str(os.getpid())
brain = Path.home()/'.gemini/antigravity-cli/brain'/sid
brain.mkdir(parents=True)
(brain/'test').write_text('session')
permissions = queue.Queue()
bridge_replies = queue.Queue()
turn = None
number = 0
cancel = threading.Event()
def record(v):
 with lock:
  with log.open('a') as f: f.write(json.dumps(v)+'\n')
def send(v):
 with lock: print(json.dumps({'jsonrpc':'2.0',**v}),flush=True)
def reply(v, result): send({'id':v['id'],'result':result})
def update(kind, **kw): send({'method':'session/update','params':{'sessionId':sid,'update':{'sessionUpdate':kind,**kw}}})
def finish(v, reason='end_turn'):
 reply(v,{'stopReason':reason,'usage':{'inputTokens':17,'outputTokens':31,'cachedReadTokens':3,
       'cachedWriteTokens':2,'thoughtTokens':7,'totalTokens':48}})
record({'argv':sys.argv[1:],'env':dict(os.environ),'server':server})
if scenario.get('bridge'):
 bridge = subprocess.Popen([server['command'],*server['args']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
 def read_bridge():
  for line in bridge.stdout: bridge_replies.put(json.loads(line))
 threading.Thread(target=read_bridge,daemon=True).start()
def prompt(v, n):
 mode = scenario.get('mode','text')
 if mode in ('quota','login','error'):
  send({'id':v['id'],'error':{'code':-1,'message':{'quota':'quota private prompt','login':'sign in private prompt','error':'private prompt'}[mode]}}); return
 if mode in ('fs/readTextFile','terminal/create','unknown'):
  send({'id':'bad','method':mode,'params':{'sessionId':sid}}); return
 if mode == 'native': update('tool_call',toolCallId='bad',name='run_command',rawInput={}); return
 if mode == 'inert':
  for name in ('list_resources','read_resource','manage_task'):
   update('tool_call',toolCallId=name,name=name)
   update('tool_call_update',toolCallId=name,status='completed')
 if mode in ('calls','server','tool') and n == 1:
  tool={'name':'call_mcp_tool','rawInput':{'ServerName':'other' if mode=='server' else 'shaft',
        'ToolName':'unknown' if mode=='tool' else 'echo','Arguments':{'n':1}}}
  send({'id':'permission','method':'session/request_permission','params':{'sessionId':sid,'toolCall':tool,
        'options':[{'optionId':'yes','kind':'allow_once'},{'optionId':'no','kind':'reject_once'}]}})
  permission=permissions.get(); record({'permission':permission})
  if permission['result']['outcome']['optionId']!='yes': return
  update('tool_call',toolCallId='call',**tool)
  bridge.stdin.write(json.dumps({'id':10,'method':'tools/call','params':{'name':'echo','arguments':{'n':1}}})+'\n'); bridge.stdin.flush()
  value=bridge_replies.get(); record({'bridge_result':value})
  if cancel.is_set() or n != number: return
  update('tool_call_update',toolCallId='call',status='completed')
 update('agent_thought_chunk',content={'type':'text','text':'ignored'})
 update('agent_message_chunk',content={'type':'text','text':
        '⚠ Agent execution terminated due to error.' if mode=='silent_error' else 'hello'})
 finish(v)
for line in sys.stdin:
 v=json.loads(line); record(v)
 assert v['jsonrpc']=='2.0'
 method=v.get('method')
 if method=='initialize': reply(v,{'protocolVersion':1})
 elif method=='session/new': reply(v,{'sessionId':sid,'models':{'availableModels':[
      {'modelId':m+'-'+level} for m in ('gemini-4-argon','gemini-3.8-flash') for level in ('low','medium','high')]},
      '_meta':{'antigravity':{'tier':{'name':'test tier','id':'test'}}}})
 elif method=='session/set_model': reply(v,{})
 elif method=='session/prompt':
  turn=v; number+=1; cancel.clear()
  threading.Thread(target=prompt,args=(v,number),daemon=True).start()
 elif method=='session/cancel': cancel.set(); finish(turn,'cancelled')
 elif v.get('id')=='permission': permissions.put(v)
'''


def body(messages=None, **kw):
    return {'model': 'antigravity/gemini-4-argon', 'messages': messages if messages is not None else [
        {'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'private prompt'}],
        'tools': [{'type': 'function', 'function': {'name': 'echo', 'description': 'Echo a number',
                   'parameters': {'type': 'object', 'properties': {'n': {'type': 'integer'}}}}}], **kw}


class ChannelTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/tmp', prefix='at-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fake = self.root / 'fake-agy'
        self.fake.write_text('#!' + sys.executable + '\n' + FAKE)
        self.fake.chmod(0o700)
        self.enterContext(mock.patch.object(agy, 'BIN', str(self.fake)))
        self.enterContext(mock.patch.object(agy, 'HOME', self.root / 'home'))
        self.enterContext(mock.patch.object(agy, 'TIMEOUT_S', 3))
        self.login = self.enterContext(mock.patch.object(agy, 'logged_in', return_value=True))
        self.record = self.enterContext(mock.patch.object(planchannel.cloudllm, '_record'))
        self.enterContext(mock.patch.object(agy, '_Bridge', DummyBridge))
        self.enterContext(mock.patch.object(agy, '_seatbelt', side_effect=lambda argv, folder, home: argv))
        self.channel = agy.Channel('example', 'example', mock.Mock())
        self.addCleanup(self.channel.close)
        self.scenario()

    def scenario(self, **kw):
        self.fake.with_suffix('.json').write_text(json.dumps(kw))

    def wire(self):
        path = self.fake.with_suffix('.jsonl')
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_text_usage_launch_and_configuration(self):
        config = agy.home() / '.gemini/config/mcp_config.json'
        config.parent.mkdir(parents=True)
        config.write_text('{"mcpServers":{"other":{}}}')
        reply = self.channel.complete(body())
        self.assertEqual(assistant(reply)['content'], 'hello')
        # Bug 2026-11-12: inputTokens excludes cache reads (a real cycle showed 69,891 beside 401,698 cached), so the
        # prompt was booked at a seventh of its size. 17 uncached + 3 read + 2 written.
        self.assertEqual(reply['usage'], {'prompt_tokens': 22, 'completion_tokens': 31})
        self.assertEqual(self.record.call_args.args[2:5], (22, 31, None))
        self.assertEqual(self.record.call_args.kwargs['cache'], {
            'cached_input_tokens': 3, 'cache_write_tokens': 2, 'reasoning_tokens': 7})
        launch = self.wire()[0]
        self.assertEqual(launch['argv'], ['acp', '--disable-telemetry', '--agent', 'shaft'])
        self.assertEqual(launch['env']['HOME'], str(self.root / 'home'))
        self.assertEqual(json.loads(config.read_text()), {'mcpServers': {'shaft': {
            'command': str(Path(sys.executable).resolve()),
            'args': ['-I', '-B', str(agy.BRIDGE), '--tools', 'bridge/tools.json', '--socket', 'bridge/b.sock'],
            'timeoutSeconds': 3603}}})
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        agent = (self.channel._folder / '.agents/agents/shaft/agent.md').read_text()
        self.assertTrue(agent.startswith('---\nname: shaft\ndescription: SHAFT instance\nexcludeDefaultComponents: true\n---\n\nsystem\n'))
        for tool in body()['tools']:
            f = tool['function']
            self.assertIn('## ' + f['name'], agent)
            self.assertIn(f['description'], agent)
            self.assertIn(json.dumps(f['parameters'], separators=(',', ':')), agent)
        initialize = next(r for r in self.wire() if r.get('method') == 'initialize')
        self.assertEqual(initialize['params'], {'protocolVersion': 1, 'clientCapabilities': {
            'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}})
        self.assertEqual(self.channel.tier, 'test tier')
        self.channel.complete(body())
        self.assertEqual(self.login.call_count, 1)
        self.assertEqual(self.record.call_count, 1)

    def test_known_api_price_is_recorded_without_charging_the_plan(self):
        with mock.patch.object(agy.cloudllm, 'usd', return_value=.04) as price:
            self.channel.complete(body())
        price.assert_called_once_with('gemini-4-argon', {'input_tokens': 17, 'output_tokens': 31,
            'cache_read_tokens': 3, 'cache_write_1h_tokens': 2})
        self.assertEqual(self.record.call_args.kwargs['cache']['api_equivalent_usd'], .04)
        self.assertIsNone(self.record.call_args.args[4])

    def test_efforts_and_model_change_restart(self):
        for effort, expected in [(None, 'medium'), ('low', 'low'), ('medium', 'medium'),
                                 ('high', 'high'), ('xhigh', 'high'), ('max', 'high')]:
            previous = self.channel._process.pid if self.channel._process else None
            self.channel.complete(body(reasoning_effort=effort))
            self.assertNotEqual(self.channel._process.pid, previous)
            selected = [r for r in self.wire() if r.get('method') == 'session/set_model'][-1]
            self.assertEqual(selected['params']['modelId'], 'gemini-4-argon-' + expected)
        pid = self.channel._process.pid
        self.channel.complete(body(model='antigravity/gemini-3.8-flash'))
        self.assertNotEqual(self.channel._process.pid, pid)

    def test_unavailable_model_refused(self):
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'model unavailable'):
            self.channel.complete(body(model='antigravity/unknown'))
        self.assertIsNone(self.channel._process)

    def test_missing_login_before_launch(self):
        self.login.return_value = False
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'Antigravity login missing; run shaft antigravity login'):
            self.channel.complete(body())
        self.assertEqual(self.wire(), [])

    def test_agent_execution_terminated_silent_error_is_not_a_reply(self):
        self.scenario(mode='silent_error')
        with self.assertRaisesRegex(planchannel.ChannelFailure, '^Antigravity turn failed$'):
            list(self.channel.stream(body()))
        self.assertEqual(self.record.call_args.args[2:4], (22, 31))

    def test_rpc_errors_classified_without_prompt(self):
        for mode, reason in [('quota', 'Antigravity quota reached'), ('error', 'Antigravity turn failed'),
                             ('login', 'Antigravity login missing; run shaft antigravity login')]:
            self.scenario(mode=mode)
            c = agy.Channel('example', 'example', mock.Mock())
            try:
                with self.assertRaisesRegex(planchannel.ChannelFailure, '^' + reason + '$'):
                    c.complete(body())
            finally:
                c.close()

    def test_native_and_client_requests_are_security_failures(self):
        # A refusal names what was refused (tool, server/tool or method), as the Claude channel does since a bake-off
        # failure read only "native tool forbidden" and could not be told apart from a slip (2026-11-12).
        named = {'native': 'native tool forbidden: run_command', 'fs/readTextFile': 'client request forbidden: fs/readTextFile',
                 'terminal/create': 'client request forbidden: terminal/create', 'unknown': 'client request forbidden: unknown',
                 'server': 'native tool forbidden: call_mcp_tool other/echo',
                 'tool': 'native tool forbidden: call_mcp_tool shaft/unknown'}
        for mode, reason in named.items():
            self.scenario(mode=mode)
            c = agy.Channel('example', 'example', mock.Mock())
            try:
                with self.subTest(mode=mode), self.assertRaises(planchannel.ChannelSecurity) as caught:
                    c.complete(body())
                self.assertIn('Antigravity ' + reason, str(caught.exception))
            finally:
                c.close()

    def test_permission_denial_is_written_before_security_failure(self):
        c = self.channel
        c._session, c._tool_names = 's', {'echo'}
        process = mock.Mock(stdin=io.BytesIO())
        c._process = process
        try:
            for server, tool in [('other', 'echo'), ('shaft', 'unknown')]:
                message = {'jsonrpc': '2.0', 'id': 9, 'method': 'session/request_permission', 'params': {
                    'sessionId': 's', 'toolCall': {'name': 'call_mcp_tool', 'rawInput': {
                        'ServerName': server, 'ToolName': tool, 'Arguments': {}}},
                    'options': [{'optionId': 'no', 'kind': 'reject_once'}]}}
                with self.assertRaises(planchannel.ChannelSecurity):
                    c._dispatch(message)
            self.assertEqual([json.loads(line) for line in process.stdin.getvalue().splitlines()], [
                {'jsonrpc': '2.0', 'id': 9, 'result': {'outcome': {'outcome': 'selected', 'optionId': 'no'}}}] * 2)
        finally:
            c._process = None

    def test_inert_tools_counted_once_not_forwarded(self):
        self.scenario(mode='inert')
        reply = self.channel.complete(body())
        self.assertEqual(self.channel.stats['inert_tools'], 3)
        self.assertNotIn('tool_calls', assistant(reply))

    def real_bridge(self):
        require_sockets(self)
        self.enterContext(mock.patch.object(agy, '_Bridge', agy.claudecli._Bridge))
        self.scenario(mode='calls', bridge=True)

    def test_permission_bridge_and_broker_result(self):
        self.real_bridge()
        first = self.channel.complete(body())
        call, = assistant(first)['tool_calls']
        self.assertEqual(call['function'], {'name': 'echo', 'arguments': '{"n": 1}'})
        self.assertEqual(first['usage'], {'prompt_tokens': 0, 'completion_tokens': 0})
        messages = body()['messages'] + [assistant(first), {'role': 'tool', 'tool_call_id': call['id'], 'content': 'ready'}]
        final = self.channel.complete(body(messages))
        self.assertEqual(assistant(final)['content'], 'hello')
        self.assertEqual(final['usage'], {'prompt_tokens': 22, 'completion_tokens': 31})
        result = next(r['bridge_result'] for r in self.wire() if 'bridge_result' in r)
        self.assertEqual(result['result']['content'], [{'type': 'text', 'text': 'ready'}])
        self.assertEqual(sum(c.args[2] for c in self.record.call_args_list), 22)

    def test_cancel_held_call_books_late_usage_and_next_prompt_works(self):
        self.real_bridge()
        first = self.channel.complete(body())
        pid = self.channel._process.pid
        with self.channel._cv:
            self.channel._record(self.channel._step)
            self.channel._cancel_turn(self.channel._step.turn)
        late = self.record.call_args
        self.assertEqual(late.args[6], 'late_usage')
        self.assertEqual(late.args[2:4], (22, 31))
        with self.channel._cv:
            self.channel._step = planchannel.Step(body()['model'], 'next')
            self.channel._start_turn('next', [], None)
        self.assertTrue(self.channel._step.event.wait(2))
        self.assertIsNone(self.channel._step.error)
        self.assertEqual(self.channel._step.text, 'hello')
        self.assertEqual(self.channel._process.pid, pid)

    def test_close_cancels_and_prunes_only_its_conversation(self):
        self.real_bridge()
        self.channel.complete(body())
        brain = agy.home() / '.gemini/antigravity-cli/brain'
        owned = brain / self.channel._conversation
        other = brain / 'other'
        other.mkdir()
        db = brain.parent / 'conversation_summaries.db'
        db.write_text('keep')
        # Bug 2026-11-12: only the brain folder was pruned; agy also keeps a ~384 KB database and an annotation per
        # conversation, so the private home grew with every session.
        conversation = self.channel._conversation
        (brain.parent / 'conversations').mkdir()
        (brain.parent / 'annotations').mkdir()
        owned_files = [brain.parent / 'conversations' / (conversation + suffix) for suffix in ('.db', '.db-wal', '.db-shm')]
        owned_files.append(brain.parent / 'annotations' / (conversation + '.pbtxt'))
        other_files = [brain.parent / 'conversations/other.db', brain.parent / 'annotations/other.pbtxt']
        for path in (*owned_files, *other_files):
            path.write_text('x')
        self.assertTrue(owned.is_dir())
        self.channel.close()
        self.assertFalse(owned.exists())
        self.assertTrue(other.is_dir())
        self.assertEqual(db.read_text(), 'keep')
        self.assertEqual([p for p in owned_files if p.exists()], [])
        self.assertTrue(all(p.exists() for p in other_files))
        self.assertEqual(self.record.call_args.args[6], 'late_usage')


class SameResponseTest(unittest.TestCase):
    """Bug 2026-11-12: ACP reports usage only when a turn ends, so every tool step read as shaft_same_turn and the
    instance's step budget stopped counting (up to SAME_TURN_LIMIT per run). Only the gap since the previous call
    completed tells a parallel sibling (same response, ~0.01 s) from a new model response (>= 2 s)."""

    def setUp(self):
        self.channel = agy.Channel('example', 'example', mock.Mock())
        self.channel._session = 's'
        self.channel._tool_names = {'echo'}
        self.now = 100.0
        self.enterContext(mock.patch.object(agy.time, 'monotonic', side_effect=lambda: self.now))

    def step(self):
        step = mock.Mock(usage_received=False)
        self.channel._turn = {'step': step}
        return step

    def call(self, ident, status='in_progress'):
        self.channel._dispatch({'jsonrpc': '2.0', 'method': 'session/update', 'params': {'sessionId': 's', 'update': {
            'sessionUpdate': 'tool_call' if status == 'in_progress' else 'tool_call_update', 'toolCallId': ident,
            'name': 'call_mcp_tool', 'status': status,
            'rawInput': {'ServerName': 'shaft', 'ToolName': 'echo', 'Arguments': {}}}}})

    def test_gap_separates_new_responses_from_parallel_siblings(self):
        first = self.step()
        self.call('a')
        self.assertTrue(first.usage_received, 'the first call of a turn opens a new response')
        self.now += 1
        self.call('a', 'completed')
        sibling = self.step()
        self.now += .01
        self.call('b')
        self.assertFalse(sibling.usage_received, 'a parallel sibling stays in the same response')
        self.call('b', 'completed')
        later = self.step()
        self.now += 2
        self.call('c')
        self.assertTrue(later.usage_received, 'a call after a model round trip is a new response')
        self.assertEqual(agy.SAME_RESPONSE_GAP_S, .5)


class WiringTest(unittest.TestCase):
    def test_provider_and_bakeoff(self):
        from supervisor import bakeoff, core
        model = 'antigravity/gemini-4-argon'
        self.assertEqual(core.instance_model(model), model)
        self.assertIs(planchannel.provider(model), agy)
        self.assertEqual(planchannel.vendor(model), 'google')
        self.assertEqual(bakeoff._redactor({'arms': []})(model), '[model]')

    def test_cost_views_distinguish_unpriced_tokens_and_api_equivalent(self):
        from datetime import datetime
        from supervisor import panel, trial, web_views
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='at-') as directory:
            root = Path(directory)
            rows = [{'ts': datetime.now().isoformat(), 'model': 'antigravity/gemini-4-argon',
                     'usd': None, 'input_tokens': 17, 'output_tokens': 31},
                    {'ts': datetime.now().isoformat(), 'model': 'antigravity/priced',
                     'usd': None, 'api_equivalent_usd': .04, 'input_tokens': 10, 'output_tokens': 20}]
            (root / 'cloud_usage.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            with mock.patch.object(panel, 'SUP_DIR', root), mock.patch.object(planchannel, 'SUP_DIR', root), \
                    mock.patch.object(trial, 'LEDGER', root / 'trials.jsonl'), \
                    mock.patch.object(panel, 'instance_names', return_value=[]):
                costs = panel.costs()
            self.assertEqual(costs['antigravity_tokens'], 48)
            self.assertEqual(costs['antigravity_api_equivalent'], .04)
            self.assertEqual(costs['today']['total'], 0)
            self.assertEqual(costs['unpriced'], 0)
            page = str(web_views.costs_page(costs, [], 'csrf'))
            self.assertIn('Antigravity plan: 48 tokens (no API price available)', page)
            self.assertIn('Antigravity plan: $0.040000 API equivalent (not money spent)', page)

    def test_status_prints_outage_without_launching_cli(self):
        from supervisor.__main__ import cmd_antigravity
        with mock.patch.object(agy, 'logged_in', return_value=True), \
                mock.patch.object(agy.STATE, 'outage', return_value={'reason': 'test', 'security': True}), \
                mock.patch('sys.stdout', new_callable=io.StringIO) as output, \
                mock.patch.object(agy.subprocess, 'run', side_effect=AssertionError('must not launch')):
            self.assertEqual(cmd_antigravity('status'), 0)
        self.assertIn('Antigravity security event: test', output.getvalue())

    def test_seatbelt_closes_operator_home_before_allowing_private_home(self):
        with tempfile.TemporaryDirectory(dir='/tmp', prefix='at-') as directory:
            folder = Path(directory).resolve()
            private = Path.home() / 'example-private-home'
            with mock.patch.object(agy.sandbox, '_operator_places_closed', return_value=['; closed']), \
                    mock.patch.object(agy.sandbox, 'wrap', side_effect=lambda path, argv: argv):
                agy._seatbelt(['fake'], folder, private)
            profile = (folder / 'seatbelt.sb').read_text()
            deny = '(deny file-read* file-write* (subpath ' + json.dumps(str(Path.home().resolve())) + '))'
            allow = '(allow file-read* file-write* (subpath ' + json.dumps(str(private)) + '))'
            self.assertLess(profile.index(deny), profile.index(allow))
            self.assertNotIn('keychain', profile)
            self.assertIn('(allow file-read* (literal ' + json.dumps(str(agy.BRIDGE)) + '))', profile)
