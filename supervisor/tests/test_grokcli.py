"""Grok ACP mapping with a fake stdio agent and the production MCP bridge."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from supervisor import grokcli as grok, planchannel
from supervisor.tests.test_planbridge import require_sockets

FAKE = r'''
import json, os, queue, subprocess, sys, threading, time
from pathlib import Path
scenario = json.loads(Path(__file__).with_suffix('.json').read_text())
log = Path(__file__).with_suffix('.jsonl')
lock = threading.Lock()
session = 0
has_tools = True
bridge = None
turn = None
cancel = threading.Event()
replies = queue.Queue()
def send(v):
 with lock:
  print(json.dumps(v), flush=True)
def note(kind, **kw):
 send({'method':'session/update','params':{'sessionId':str(session),'update':{'sessionUpdate':kind,**kw}}})
def permission(name):
 send({'id':900,'method':'session/request_permission','params':{'sessionId':str(session),
  'toolCall':{'rawInput':{'tool_name':name},'_meta':{'x.ai/tool':{'name':'use_tool','kind':'use_tool'}}}}})
def usage():
 send({'method':'_x.ai/session_notification','params':{'sessionId':str(session),'update':{
  'sessionUpdate':'response_completed','usage':{'input_tokens':7,'output_tokens':2,'cache_read_input_tokens':3,'reasoning_tokens':1}}}})
def finish(ident, reason='end_turn'):
 send({'id':ident,'result':{'stopReason':reason,'_meta':{'usage':{'inputTokens':10,'outputTokens':2,
  'cachedReadTokens':3,'reasoningTokens':1,'costUsdTicks':12300000}}}})
def prompt(ident):
 mode=scenario.get('mode','text') if has_tools else 'text'
 if mode=='hang': return
 if mode=='die': os._exit(3)
 if mode=='permission': permission(scenario['name']); return
 if mode=='native':
  note('tool_call',_meta={'x.ai/tool':{'name':'bash','kind':'bash'}}); return
 if mode in ('calls','parallel','ended'):
  count=2 if mode=='parallel' else 1
  for n in range(count):
   permission('shaft__echo')
   bridge.stdin.write(json.dumps({'id':n+10,'method':'tools/call','params':{'name':'echo','arguments':{'n':n}}})+'\n')
  bridge.stdin.flush()
  time.sleep(scenario.get('usage_delay',.01)); usage()
  if mode=='ended': finish(ident); return
  for n in range(count):
   while not cancel.is_set():
    try: value=replies.get(timeout=.05); break
    except queue.Empty: pass
   if cancel.is_set(): return
   with lock:
    with log.open('a') as f: f.write(json.dumps({'bridge_result':value})+'\n')
  note('agent_message_chunk',content={'type':'text','text':'answered'})
 else:
  note('agent_message_chunk',content={'type':'text','text':'hello'})
  usage()
 finish(ident)
for line in sys.stdin:
 v=json.loads(line)
 with lock:
  with log.open('a') as f: f.write(json.dumps(v)+'\n')
 method=v.get('method')
 if method=='initialize':
  if scenario.get('startup_hang'): continue
  send({'id':v['id'],'result':{}})
 elif method=='authenticate': send({'id':v['id'],'result':{}})
 elif method=='session/new':
  session+=1
  has_tools = v['params']['_meta']['agentProfile']['tools'] != ['mcp__shaft__shaft_reserved_no_tools']
  if scenario.get('bridge'):
   if bridge: bridge.kill(); bridge.wait()
   server=v['params']['mcpServers'][0]
   bridge=subprocess.Popen([server['command'],*server['args']],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
   def read(b):
    for raw in b.stdout: replies.put(json.loads(raw))
   threading.Thread(target=read,args=(bridge,),daemon=True).start()
  send({'id':v['id'],'result':{'sessionId':str(session)}})
 elif method=='session/prompt':
  turn=v['id']; cancel.clear()
  threading.Thread(target=prompt,args=(turn,),daemon=True).start()
 elif method=='session/cancel':
  cancel.set(); finish(turn,'cancelled')
'''


def body(messages=None, **kw):
    return {'model': 'grok/grok-4.7', 'messages': messages if messages is not None else [
        {'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'begin'}],
        'tools': [{'type': 'function', 'function': {'name': 'echo', 'parameters': {'type': 'object'}}}], **kw}


def assistant(reply):
    return reply['choices'][0]['message']


class DummyBridge:
    def __init__(self, channel, tools, folder):
        self.address = 'unused'
        self.tools_path = folder / 'tools.json'
        self.stopped = threading.Event()
    def close(self):
        self.stopped.set()


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp', prefix='gt-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = (self.root / 'home').resolve()
        self.home.mkdir()
        # Fake login presence only; no credential file is opened or read.
        self.presence = self.enterContext(mock.patch.object(Path, 'is_file', autospec=True,
            side_effect=lambda p: True if p == self.home / 'auth.json' else os.path.isfile(p)))
        self.enterContext(mock.patch.object(grok, 'HOME', self.home))
        self.enterContext(mock.patch.object(grok, 'TIMEOUT_S', 3))
        self.record = self.enterContext(mock.patch.object(planchannel.cloudllm, '_record'))
        self.enterContext(mock.patch.object(grok, '_Bridge', DummyBridge))
        self.enterContext(mock.patch.object(grok, '_seatbelt', side_effect=lambda argv, folder, home: argv))
        self.fake = self.root / 'fake.py'
        self.fake.write_text(FAKE)
        self.enterContext(mock.patch.object(grok, 'BIN', str(self.fake)))
        self.popen = subprocess.Popen
        def launch(argv, **kwargs):
            if argv[0] != grok.BIN:
                return self.popen(argv, **kwargs)
            self.argv, self.env = argv, kwargs['env']
            return self.popen([sys.executable, '-B', str(self.fake)], **kwargs)
        self.enterContext(mock.patch.object(grok.subprocess, 'Popen', side_effect=launch))
        self.channel = grok.Channel('bird', 'bird', mock.Mock())
        self.addCleanup(self.channel.close)
        self.scenario()

    def scenario(self, **kw):
        self.fake.with_suffix('.json').write_text(json.dumps(kw))

    def wire(self):
        p = self.fake.with_suffix('.jsonl')
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_text_repeat_extend_rebuild_restart_and_explicit_environment(self):
        first = self.channel.complete(body())
        self.assertEqual(assistant(first)['content'], 'hello')
        self.assertEqual(first['usage'], {'prompt_tokens': 10, 'completion_tokens': 2})
        self.assertEqual(self.record.call_args.kwargs['cache'], {'cached_input_tokens': 3,
            'reasoning_tokens': 1, 'api_equivalent_usd': .00123})
        self.assertIsNone(self.record.call_args.args[4])
        self.channel.complete(body())
        self.assertEqual(self.channel.stats['repeats'], 1)
        messages = body()['messages'] + [assistant(first), {'role': 'user', 'content': 'next'}]
        self.channel.complete(body(messages))
        self.assertEqual(self.channel.stats['extends'], 1)
        self.channel.complete(body(messages, tools=[]))
        self.assertEqual(self.channel.stats['rebuilds'], 2)
        rows = self.wire()
        session = [r for r in rows if r.get('method') == 'session/new'][-1]
        self.assertEqual(session['params']['_meta']['agentProfile']['tools'], ['mcp__shaft__shaft_reserved_no_tools'])
        self.assertIn('Prior messages (transcript)', [r for r in rows if r.get('method') == 'session/prompt'][-1]['params']['prompt'][0]['text'])
        self.assertNotIn('begin', str(self.channel.log.call_args_list))
        self.assertEqual(self.env['GROK_CODEX_SKILLS_ENABLED'], '0')
        self.assertNotIn('USER', self.env)
        self.assertEqual(self.argv[self.argv.index('-m')+1], 'grok-4.7')
        pid = self.channel._process.pid
        self.channel.complete(body(model='grok/other', reasoning_effort='high'))
        self.assertNotEqual(self.channel._process.pid, pid)
        self.assertEqual(self.argv[self.argv.index('-m')+1], 'other')
        self.assertIn('--reasoning-effort', self.argv)
        self.assertLess(len(str(self.channel._folder / '12345678/b.sock').encode()), 104)

    def test_permission_refusal_and_forbidden_kind(self):
        for name in ('bash', 'shaft__unknown'):
            with self.subTest(name=name):
                c = grok.Channel('bird', 'bird', mock.Mock())
                c._session, c._tool_names = 's', {'echo'}
                with self.assertRaises(planchannel.ChannelSecurity):
                    c._dispatch({'id': 2, 'method': 'session/request_permission', 'params': {'sessionId': 's',
                        'toolCall': {'rawInput': {'tool_name': name}, '_meta': {'x.ai/tool': {'name': 'use_tool', 'kind': 'use_tool'}}}}})
                self.assertEqual(c._outgoing.get()['result']['outcome']['optionId'], 'reject-once')
        for meta in ({'name': 'bash', 'kind': 'bash'}, {'name': 'use_tool', 'kind': 'unknown'}, {}):
            with self.assertRaisesRegex(planchannel.ChannelSecurity, 'tool=.*kind='):
                self.channel._check_tool({'_meta': {'x.ai/tool': meta}})

    def test_native_tool_and_process_death_fail_closed(self):
        self.scenario(mode='native')
        with self.assertRaisesRegex(planchannel.ChannelSecurity, 'tool=bash kind=bash'):
            self.channel.complete(body())

    def test_process_death(self):
        self.scenario(mode='die')
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'process died'):
            self.channel.complete(body())

    def test_close_during_rpc(self):
        self.scenario(startup_hang=True)
        errors = []
        def complete():
            try: self.channel.complete(body())
            except Exception as exc: errors.append(exc)
        worker = threading.Thread(target=complete)
        worker.start()
        deadline = time.monotonic()+2
        while not self.wire() and time.monotonic()<deadline: time.sleep(.01)
        started = time.monotonic()
        self.channel.close()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(errors[0], planchannel.ChannelClosed)
        self.assertLess(time.monotonic()-started, 1)

    def test_missing_login(self):
        self.presence.side_effect = lambda p: False
        with self.assertRaisesRegex(planchannel.ChannelFailure, 'shaft grok login'):
            self.channel.complete(body())

    def test_prompt_image_size_refusal_and_mapping(self):
        import struct, zlib
        def image(size):
            def chunk(kind, data):
                return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data))
            width,height=size
            raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\0'*(width*3))*height))+chunk(b'IEND',b'')
            return 'data:image/png;base64,'+base64.b64encode(raw).decode()
        with self.assertRaisesRegex(planchannel.ChannelFailure, '8x8'):
            self.channel._images([image((7,8))])
        url=image((8,8))
        self.assertEqual(self.channel._images([url]), [{'type':'image','mimeType':'image/png','data':url.split(',')[1]}])

    def test_empty_profile_never_unrestricted(self):
        for names in ([], ['echo']):
            profile = grok._profile(names)
            tools = json.loads(profile.split('tools: ', 1)[1].splitlines()[0])
            self.assertTrue(tools)
            self.assertEqual(tools, ['mcp__shaft__echo'] if names else ['mcp__shaft__shaft_reserved_no_tools'])

    def real_bridge(self, **scenario):
        require_sockets(self)
        # Undo only this test's dummy bridge; use the real standalone MCP relay.
        self.enterContext(mock.patch.object(grok, '_Bridge', REAL_BRIDGE))
        self.scenario(bridge=True, **scenario)

    def test_parallel_calls_late_usage_images_and_results(self):
        self.real_bridge(mode='parallel', usage_delay=.3)
        first = self.channel.complete(body())
        calls = assistant(first)['tool_calls']
        self.assertEqual(sorted(json.loads(c['function']['arguments'])['n'] for c in calls), [0,1])
        self.assertEqual(first['usage']['prompt_tokens'],10)
        self.assertTrue(self.channel._step.usage_received)
        messages = body()['messages'] + [assistant(first)] + [
            {'role':'tool','tool_call_id':c['id'],'content':'result '+str(i)} for i,c in enumerate(calls)]
        messages += [{'role':'user','content':'delta'}]
        reply = self.channel.complete(body(messages))
        self.assertEqual(assistant(reply)['content'],'answered')
        self.assertEqual(reply['usage'], {'prompt_tokens':0,'completion_tokens':0})
        results = [r['bridge_result'] for r in self.wire() if 'bridge_result' in r]
        self.assertEqual(len(results),2)
        self.assertIn({'type':'text','text':'[message]\ndelta'}, [p for r in results for p in r['result']['content']])

    def test_cancel_pending_on_rebuild(self):
        self.real_bridge(mode='calls')
        first = self.channel.complete(body())
        old = self.channel._bridge
        self.channel.complete(body([{'role':'user','content':'different'}], tools=[]))
        self.assertTrue(old.stopped.is_set())
        self.assertTrue(any(r.get('method')=='session/cancel' for r in self.wire()))

    def test_turn_ended_with_call_outstanding(self):
        self.real_bridge(mode='ended')
        first = self.channel.complete(body())
        self.assertEqual(first['choices'][0]['finish_reason'],'tool_calls')
        old = self.channel._bridge
        messages=body()['messages']+[assistant(first),{'role':'tool','tool_call_id':assistant(first)['tool_calls'][0]['id'],'content':'late answer'}]
        self.channel.complete(body(messages))
        self.assertTrue(old.stopped.is_set())
        self.assertFalse(any('bridge_result' in r for r in self.wire()))

    def test_bridge_death_with_held_call(self):
        self.real_bridge(mode='calls')
        self.channel.complete(body())
        # Kill the bridge by its watchdog path without ps or knowing its PID.
        Path(self.channel._bridge.address).unlink()
        deadline=time.monotonic()+3
        while not self.channel._fatal and time.monotonic()<deadline: time.sleep(.02)
        self.assertIsInstance(self.channel._fatal, planchannel.ChannelFailure)


REAL_BRIDGE = grok._Bridge


class RetentionTest(unittest.TestCase):
    def test_only_old_session_files_and_ten_minute_stamp(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder); sessions=home/'sessions'; sessions.mkdir()
            outside=home/'keep'; outside.write_text('private')
            old=sessions/'old'; old.write_text('old')
            fresh=sessions/'fresh'; fresh.write_text('fresh')
            link=sessions/'link'; link.symlink_to(outside)
            os.utime(old,(0,0)); os.utime(outside,(0,0))
            grok._prune_sessions(home)
            self.assertFalse(old.exists()); self.assertTrue(outside.exists()); self.assertTrue(link.is_symlink()); self.assertTrue(fresh.exists())
            os.utime(fresh,(0,0)); grok._prune_sessions(home); self.assertTrue(fresh.exists())
            stamp=home/'sessions-pruned.stamp'; os.utime(stamp,(0,0)); grok._prune_sessions(home); self.assertFalse(fresh.exists())

    def test_retention_failure_never_raises(self):
        with mock.patch.object(Path,'open',side_effect=PermissionError()):
            with self.assertLogs('supervisor.grokcli',level='WARNING'):
                grok._prune_sessions(Path('/unused'))


class WiringTest(unittest.TestCase):
    def test_provider_and_generic_model_seams(self):
        from supervisor import bakeoff, consultstats, core, judge, localmodel, opconfig, runner, settings, vision
        model='grok/grok-4.7'
        self.assertEqual(core.instance_model(model),model)
        with self.assertRaises(ValueError): core.cloud_model(model)
        self.assertEqual(planchannel.vendor(model),'xai')
        self.assertEqual(consultstats.vendor(model),'xai')
        self.assertTrue(vision.sees_images(model))
        self.assertTrue(judge.same_model(model,'openrouter/x-ai/grok-4.7'))
        self.assertEqual(opconfig.base_url_for(model),'broker')
        with mock.patch.object(opconfig,'_template_text',return_value=''):
            self.assertIn({'model':model,'base_url':'broker','label':model+' (Grok plan)'},opconfig.model_choices())
        self.assertIn('grokcli',settings.MODULES)
        self.assertEqual(bakeoff.parse_arm(model+'@high',('high',))['model'],model)
        self.assertEqual(bakeoff._redactor({'arms':[]})('Grok grok/grok-4.7'), '[model] [model]')
        cfg={'LLM_BASE_URL':'broker','LLM_MODEL':model}
        with mock.patch.object(runner,'instance_config',return_value=cfg), mock.patch('supervisor.selfhosted.SERVERS',{'grok':{'url':'http://127.0.0.1:8780/v1'}}):
            self.assertIsNone(runner.model_server('bird'))
            self.assertEqual(localmodel.pinned_by(names=['bird'],config_of=lambda _:cfg),[])

    def test_login_and_status(self):
        from contextlib import redirect_stdout
        from supervisor import __main__ as cli
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)/'private'
            with mock.patch.object(grok,'HOME',home), mock.patch.object(cli.subprocess,'run',return_value=mock.Mock(returncode=0)) as run:
                self.assertEqual(cli.main(['grok','login']),0)
                self.assertEqual(run.call_args.args,([grok.BIN,'login'],))
                self.assertEqual(set(run.call_args.kwargs),{'env'})
                self.assertEqual(run.call_args.kwargs['env']['GROK_HOME'],str(home.resolve()))
                self.assertNotIn('USER',run.call_args.kwargs['env'])
                self.assertEqual(home.stat().st_mode&0o777,0o700)
                with mock.patch.object(planchannel,'SUP_DIR',Path(folder)),redirect_stdout(io.StringIO()) as out:
                    (Path(folder)/'grok_outage.json').write_text('{"reason":"offline"}')
                    self.assertEqual(cli.main(['grok','status']),0)
                self.assertIn('login: missing',out.getvalue())
                self.assertIn('Grok unavailable: offline',out.getvalue())
                self.assertIn('grok/grok-4.7',out.getvalue())
                self.assertEqual(run.call_count,1)

    def test_costs_and_provider_alerts(self):
        from datetime import datetime
        from supervisor import panel, trial, web_views
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            row={'ts':datetime.now().isoformat(),'model':'grok/grok-4.7','usd':None,'api_equivalent_usd':.00123}
            (root/'cloud_usage.jsonl').write_text(json.dumps(row)+'\n')
            (root/'grok_outage.json').write_text('{"reason":"tool=bash kind=bash","security":true}')
            with mock.patch.object(panel,'SUP_DIR',root),mock.patch.object(planchannel,'SUP_DIR',root),mock.patch.object(trial,'LEDGER',root/'trials.jsonl'),mock.patch.object(panel,'instance_names',return_value=[]):
                costs=panel.costs()
                self.assertEqual(costs['grok_api_equivalent'],.00123)
                self.assertEqual(costs['unpriced'],0)
                self.assertEqual(costs['today']['total'],0)
                self.assertIn('API equivalent (not money spent)',str(web_views.costs_page(costs,[],'csrf')))
                with mock.patch.object(panel,'_log_failures',return_value=[]),mock.patch.object(panel,'provider_alerts',return_value=[]),mock.patch.object(panel.core,'long_pauses',return_value=[]),mock.patch.object(panel,'CORRECTION_ALERT_CYCLES',0):
                    self.assertEqual(panel.alerts(cost=costs),[{'id':'provider:grok:outage','text':'Grok security event: tool=bash kind=bash'}])


class MappingTest(unittest.TestCase):
    """Protocol edge cases that require no socket permission."""
    def setUp(self):
        self.channel = grok.Channel('bird','bird',mock.Mock())
        self.channel._session='s'
        self.channel._tool_names={'echo'}
        self.channel._bridge=DummyBridge(None,[],Path('/unused'))
        self.channel._step=planchannel.Step('grok/grok-4.7','anchor',turn=1)
        self.channel._turns[1]={'step':self.channel._step,'input':0,'output':0,'cached':0,'reasoning':0,'ticks':0}
        self.enterContext(mock.patch.object(planchannel.cloudllm,'_record'))

    def call(self,n):
        answer=__import__('queue').Queue()
        self.channel._bridge_call(self.channel._bridge,{'params':{'name':'echo','arguments':{'n':n}}},answer)
        return answer

    def update(self,kind,**kw):
        with self.channel._cv:
            self.channel._dispatch({'method':'_x.ai/session_notification','params':{
                'sessionId':'s','update':{'sessionUpdate':kind,**kw}}})

    def test_parallel_late_usage_and_user_image_result_mapping(self):
        one,two=self.call(1),self.call(2)
        old=self.channel._step
        self.assertFalse(self.channel._calls_ready(old,old.last_call+.2))
        self.update('response_completed',usage={'input_tokens':7,'cache_read_input_tokens':3,'output_tokens':2})
        self.assertTrue(self.channel._calls_ready(old,old.last_call+.2))
        with self.channel._cv:
            self.channel._finish(old,'tool_calls')
        # Usage after a held step attaches to it until its answers are supplied.
        self.update('response_completed',usage={'input_tokens':4,'output_tokens':1})
        self.assertEqual(old.usage,{'prompt_tokens':14,'completion_tokens':3})
        self.channel._step=planchannel.Step('grok/grok-4.7','next',turn=1)
        image={'type':'image','mimeType':'image/png','data':'synthetic'}
        with mock.patch.object(grok.Channel,'_images',return_value=[image]):
            self.assertTrue(self.channel._answer_calls(old.pending,{c['id']:str(i) for i,c in enumerate(old.calls)},'delta',['image']))
        self.assertEqual(one.get_nowait(),{'isError':False,'content':[{'type':'text','text':'0'}]})
        self.assertEqual(two.get_nowait(),{'isError':False,'content':[{'type':'text','text':'1'},{'type':'text','text':'delta'},image]})
        self.assertEqual(self.channel._step.usage,{'prompt_tokens':0,'completion_tokens':0})

    def test_turn_ended_does_not_deliver_late_answer(self):
        answer=self.call(1)
        old=self.channel._step
        old.turn_ended=True
        self.channel._step=planchannel.Step('grok/grok-4.7','next',turn=1)
        self.assertFalse(self.channel._answer_calls(old.pending,{},'late',[]))
        self.assertTrue(answer.empty())

    def test_unknown_permission_kind_is_rejected_before_security_failure(self):
        with self.assertRaisesRegex(planchannel.ChannelSecurity,'tool=use_tool kind=unknown'):
            self.channel._dispatch({'id':5,'method':'session/request_permission','params':{'sessionId':'s','toolCall':{
                'rawInput':{'tool_name':'shaft__echo'},'_meta':{'x.ai/tool':{'name':'use_tool','kind':'unknown'}}}}})
        self.assertEqual(self.channel._outgoing.get()['result']['outcome']['optionId'],'reject-once')

    def test_final_usage_reconciliation_and_duplicate_totals(self):
        self.update('response_completed',usage={'input_tokens':7,'cache_read_input_tokens':3,'output_tokens':2})
        total={'inputTokens':10,'outputTokens':2,'cachedReadTokens':3,'costUsdTicks':12300000}
        self.update('turn_completed',usage=total)
        with self.channel._cv:
            self.channel._response({'id':1,'result':{'stopReason':'end_turn','_meta':{'usage':total}}})
        step=self.channel._step
        self.assertEqual(step.usage,{'prompt_tokens':10,'completion_tokens':2})
        self.assertEqual(self.channel._usage_cache(step)['api_equivalent_usd'],.00123)
        self.assertTrue(step.turn_ended)

    def test_no_empty_session_tools_and_socket_start_failure_is_channel_failure(self):
        self.channel._folder=Path('/tmp')
        with mock.patch.object(Path,'write_text'),mock.patch.object(grok,'_Bridge',side_effect=PermissionError()):
            with self.assertRaisesRegex(planchannel.ChannelFailure,'cannot start Grok bridge'):
                self.channel._open_session('grok/grok-4.7','system',[],None)

    def test_rpc_error_does_not_log_message_content(self):
        with self.assertRaises(planchannel.ChannelFailure) as error:
            self.channel._result('session/prompt',{'error':{'code':-32000,'message':'private prompt data'}})
        self.assertNotIn('private',str(error.exception))
        self.assertEqual(self.channel.log.call_args.args,('Grok RPC error method=session/prompt code=-32000',))

    def test_metadata_free_completion_requires_a_known_safe_tool_id(self):
        self.channel._check_tool({'sessionUpdate':'tool_call','toolCallId':'safe',
            '_meta':{'x.ai/tool':{'name':'search_tool','kind':'search_tool'}}})
        self.channel._check_tool({'sessionUpdate':'tool_call_update','toolCallId':'safe','status':'completed'})
        for update in ({'sessionUpdate':'tool_call_update','toolCallId':'unknown','status':'completed'},
                       {'sessionUpdate':'tool_call','toolCallId':'safe'},
                       {'sessionUpdate':'tool_call_update','toolCallId':'safe','status':'completed',
                        '_meta':{'x.ai/tool':{'name':'bash','kind':'bash'}}}):
            with self.assertRaises(planchannel.ChannelSecurity):
                self.channel._check_tool(update)
