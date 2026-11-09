"""Grok ACP backend for retained plan sessions, with a held MCP socket relay."""
from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import queue
import re
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import planchannel, sandbox
from .core import SUP_DIR, cloud_model, knob, positive_int


def serves(model):
    return isinstance(model, str) and model.startswith('grok/') and bool(model[5:])


def _models(value):
    names = [s.strip() for s in value.split(',') if s.strip()]
    if not names or not all(serves(s) for s in names):
        raise ValueError('must name grok/ models separated by commas')
    return names


HOME, _ = knob("SHAFT_GROK_HOME", str(SUP_DIR / 'grok-home'), Path,
    'private Grok configuration and login directory', group='Grok', services=('runs', 'api'))
BIN, _ = knob("SHAFT_GROK_BIN", str(Path.home() / '.grok/bin/grok'), str,
    'Grok CLI executable', group='Grok', services=('runs', 'api'))
MODELS, _ = knob("SHAFT_GROK_MODELS", 'grok/grok-4.7', _models,
    'Grok models offered to instances, comma separated', group='Grok', services=('runs', 'api'))
FALLBACK_MODEL, _ = knob("SHAFT_GROK_FALLBACK_MODEL", 'openrouter/deepseek/deepseek-v4.1-flash', cloud_model,
    'cloud model used for the run after a Grok failure', group='Grok', services=('runs', 'api'))
TIMEOUT_S, _ = knob("SHAFT_GROK_TIMEOUT_S", '900', positive_int,
    'seconds Grok may take per step; held calls are exempt', group='Grok', services=('runs', 'api'))
SESSION_HOURS, _ = knob("SHAFT_GROK_SESSION_HOURS", '24', positive_int,
    'hours of Grok session files to keep', group='Grok', services=('runs', 'api'))
LABEL, VENDOR, SEES_IMAGES = 'Grok', 'xai', True
STATE = planchannel.ProviderState('grok', LABEL)
BRIDGE = Path(__file__).with_name('planbridge.py').resolve()
RESERVED = 'shaft_reserved_no_tools'


def models():
    return list(MODELS)


def child_env(home, folder):
    env = dict(HOME=str(folder / 'home'), PATH='/usr/bin:/bin:/usr/sbin:/sbin', TMPDIR=str(folder),
               LANG='en_US.UTF-8', GROK_HOME=str(home), GROK_DISABLE_AUTOUPDATER='1', GROK_MEMORY='0')
    for vendor in ('CLAUDE', 'CURSOR', 'CODEX'):
        for feature in ('SKILLS', 'RULES', 'AGENTS', 'MCPS', 'HOOKS', 'SESSIONS'):
            env[f'GROK_{vendor}_{feature}_ENABLED'] = '0'
    return env


def _prune_sessions(home):
    try:
        with (home / 'sessions-pruned.stamp').open('a+') as stamp:
            try:
                fcntl.flock(stamp, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            now = time.time()
            if os.fstat(stamp.fileno()).st_size and now - os.fstat(stamp.fileno()).st_mtime < 600:
                return
            stamp.seek(0)
            stamp.truncate()
            stamp.write('pruned\n')
            stamp.flush()
            os.utime(stamp.fileno(), (now, now))
            root = home / 'sessions'
            if root.is_symlink():
                return
            for directory, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
                for name in files:
                    path = Path(directory) / name
                    if not path.is_symlink() and path.stat().st_mtime < now - SESSION_HOURS * 3600:
                        path.unlink()
    except OSError as exc:
        logging.getLogger(__name__).warning('Grok session retention: %s', type(exc).__name__)


def _profile(names):
    # Empty tools is UNRESTRICTED in Grok 1.0.70. Never serialize an empty list.
    entries = ['mcp__shaft__' + name for name in names] or ['mcp__shaft__' + RESERVED]
    return '---\nname: shaft\ndescription: SHAFT channel\ntools: ' + json.dumps(entries) + '\n---\nUse SHAFT tools.\n'


def _seatbelt(argv, folder, home):
    quote = json.dumps
    readers = {Path(BIN).expanduser().absolute().parent, Path(BIN).expanduser().resolve().parent,
               Path(sys.executable).resolve().parent, Path(sys.base_prefix).resolve()}
    allowed = (folder, home)
    literals = (BRIDGE, (home / 'auth.json').resolve())
    profile = ['(version 1)', '(allow default)', *sandbox._operator_places_closed(),
               f'(deny file-read* file-write* (subpath {quote(str(Path.home().resolve()))}))']
    profile += [f'(allow file-read* file-write* (subpath {quote(str(p))}))' for p in allowed]
    profile += [f'(allow file-read* (subpath {quote(str(p))}))' for p in readers]
    profile += [f'(allow file-read* (literal {quote(str(p))}))' for p in literals]
    ancestors = {p for item in (*allowed, *readers, *literals) for p in item.parents}
    profile.append('(allow file-read-metadata ' + ' '.join(f'(literal {quote(str(p))})' for p in sorted(ancestors)) + ')')
    path = folder / 'seatbelt.sb'
    path.write_text('\n'.join(profile) + '\n')
    return sandbox.wrap(path, argv)


class _Bridge:
    def __init__(self, channel, tools, folder):
        self.channel, self.folder = channel, folder
        folder.mkdir()
        self.address = str(folder / 'b.sock')
        self.tools_path = folder / 'tools.json'
        self.tools_path.write_text(json.dumps(tools))
        self.stopped = threading.Event()
        self.connections = set()
        self.last_ping = time.monotonic()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket.bind(self.address)
            self.socket.listen()
            self.socket.settimeout(.2)
        except BaseException:
            self.socket.close()
            raise
        self.worker = threading.Thread(target=self._accept, daemon=True)
        self.worker.start()

    def _accept(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                if time.monotonic() - self.last_ping > 3:
                    self.channel._fail(planchannel.ChannelFailure('Grok bridge died'))
                    return
                continue
            except OSError:
                return
            with self.channel._cv:
                self.connections.add(connection)
            threading.Thread(target=self._call, args=(connection,), daemon=True).start()

    def _call(self, connection):
        try:
            with connection, connection.makefile('rwb') as stream:
                value = json.loads(stream.readline())
                if value.get('type') == 'ping':
                    self.last_ping = time.monotonic()
                    result = {}
                else:
                    answer = queue.Queue()
                    self.channel._bridge_call(self, value, answer)
                    while not self.stopped.is_set():
                        try:
                            result = answer.get(timeout=.1)
                            break
                        except queue.Empty:
                            if select.select([connection], [], [], 0)[0] and not connection.recv(1, socket.MSG_PEEK):
                                raise ConnectionError('bridge died')
                    else:
                        return
                if not self.stopped.is_set():
                    stream.write((json.dumps(result) + '\n').encode())
                    stream.flush()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if not self.stopped.is_set():
                self.channel._fail(planchannel.ChannelFailure('Grok bridge failed: ' + type(exc).__name__))
        except planchannel.ChannelFailure as exc:
            self.channel._fail(exc)
        finally:
            with self.channel._cv:
                self.connections.discard(connection)

    def close(self):
        self.stopped.set()
        self.socket.close()
        with contextlib.suppress(FileNotFoundError):
            Path(self.address).unlink()
        with self.channel._cv:
            connections = list(self.connections)
        for connection in connections:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)


class Channel(planchannel.Channel):
    prefix, label = 'grok', LABEL
    calls_need_turn = True

    def __init__(self, broker_name, usage_name, log):
        super().__init__(broker_name, usage_name, log)
        self._bridge = None
        self._restarting = False
        self._launch = None
        self._turns = {}
        self._tool_names = set()
        self._native_calls = set()

    @property
    def timeout_s(self):
        return TIMEOUT_S

    def _failure(self, reason, cls=None):
        # Never append raw stderr: a CLI error may echo a prompt or image.
        return (cls or self.failure)(reason)

    def _prepare(self, model, effort):
        if self._process is not None and self._launch != (model, effort):
            with self._cv:
                if self._step:
                    self._record(self._step)
                    if self._step.turn and not self._step.turn_ended:
                        self._cancel_turn(self._step.turn)
            self._stop_process()
            self._session = self._signature = None

    def _start(self, model, effort):
        from .runner import RunRefused
        home = Path(HOME).expanduser().resolve()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        if not (home / 'auth.json').is_file():
            raise self.failure('Grok login missing; run shaft grok login')
        _prune_sessions(home)
        self._folder = Path(tempfile.mkdtemp(prefix='sg-', dir='/tmp')).resolve()
        (self._folder / 'home').mkdir()
        (self._folder / '.git').mkdir()  # Stop upward project discovery.
        profile = self._folder / 'agent.md'
        profile.write_text(_profile([]))
        argv = [str(Path(BIN).expanduser()), 'agent', '--no-leader', '--agent-profile', str(profile), '-m', model[5:]]
        if effort:
            argv += ['--reasoning-effort', effort]
        argv += ['stdio']
        try:
            self._process = subprocess.Popen(_seatbelt(argv, self._folder, home), cwd=self._folder,
                env=child_env(home, self._folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, RunRefused) as exc:
            shutil.rmtree(self._folder, ignore_errors=True)
            if isinstance(exc, RunRefused):
                from .runner import _refused
                _refused(self.broker_name, exc)
            raise self.failure('cannot start Grok: ' + (str(exc) if isinstance(exc, RunRefused) else type(exc).__name__)) from exc
        self._launch = (model, effort)
        self._start_workers()
        self._request('initialize', {'protocolVersion': 1, 'clientInfo': {'name': 'shaft', 'version': '1'},
            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}})
        self._request('authenticate', {'methodId': 'cached_token'})

    def _send(self, message):
        super()._send({'jsonrpc': '2.0', **message})

    def _result(self, method, reply):
        if 'error' in reply:
            # Log structure/code only: error messages can contain user content.
            error = reply.get('error')
            code = error.get('code') if isinstance(error, dict) else None
            code = code if isinstance(code, int) else 'unknown'
            self.log(f'Grok RPC error method={method} code={code}')
            raise self.failure(f'Grok RPC failed: {method} code={code}')
        result = reply.get('result')
        if not isinstance(result, dict):
            raise self.failure('malformed Grok RPC result')
        if method == 'session/new' and not isinstance(result.get('sessionId'), str):
            raise self.failure('malformed Grok session id')
        return result

    def _open_session(self, model, system, tools, effort):
        if self._bridge:
            self._bridge.close()
        self._native_calls.clear()
        self._tool_names = {t['function']['name'] for t in tools}
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+', n) or n == RESERVED for n in self._tool_names):
            raise self.failure('invalid Grok tool name')
        profile = _profile(sorted(self._tool_names))
        (self._folder / 'agent.md').write_text(profile)
        folder = self._folder / uuid.uuid4().hex[:8]
        try:
            self._bridge = _Bridge(self, [{'name': t['function']['name'],
                'description': t['function'].get('description', ''),
                'inputSchema': t['function'].get('parameters', {'type': 'object'})} for t in tools], folder)
        except OSError as exc:
            raise self.failure('cannot start Grok bridge: ' + type(exc).__name__) from exc
        return self._request('session/new', {'cwd': str(self._folder), 'mcpServers': [{
            'name': 'shaft', 'command': str(Path(sys.executable).resolve()), 'args': ['-I', '-B', str(BRIDGE), '--tools',
            str(self._bridge.tools_path), '--socket', self._bridge.address], 'env': []}],
            '_meta': {'systemPromptOverride': system, 'agentProfile': {'name': 'shaft',
                'tools': ['mcp__shaft__' + n for n in sorted(self._tool_names)] or ['mcp__shaft__' + RESERVED]},
                'rules': 'Find SHAFT tools with search_tool and call with use_tool: ' + ', '.join(sorted(self._tool_names))}})['sessionId']

    @staticmethod
    def _images(images):
        from . import vision
        content = []
        for url in images:
            try:
                media, data = vision.split_data_url(url)
                if vision.sniff(data) != media:
                    raise ValueError('image type mismatch')
                with tempfile.TemporaryDirectory(prefix='sgi-', dir='/tmp') as folder:
                    path = Path(folder) / 'image'
                    path.write_bytes(data)
                    result = vision._sips('-g', 'pixelWidth', '-g', 'pixelHeight', str(path))
                sizes = [int(line.split(':')[1]) for line in result.stdout.splitlines()
                         if 'pixel' in line and ':' in line and line.split(':')[1].strip().isdigit()]
                if result.returncode or len(sizes) != 2 or min(sizes) < 8:
                    raise ValueError('image must be at least 8x8')
                content.append({'type': 'image', 'mimeType': media, 'data': base64.b64encode(data).decode()})
            except (ValueError, OSError, subprocess.SubprocessError, vision.VisionError) as exc:
                raise planchannel.ChannelFailure('Grok image refused: invalid image or smaller than 8x8') from exc
        return content

    def _start_turn(self, text, images, effort):
        prompt = [{'type': 'text', 'text': text}, *self._images(images)]
        for previous in list(self._turns):
            if self._turns[previous]['step'].turn_ended:
                self._turns.pop(previous)
                self._responses.pop(previous, None)
        self._seq += 1
        ident = self._seq
        self._turns[ident] = {'step': self._step, 'input': 0, 'output': 0, 'cached': 0, 'reasoning': 0, 'ticks': 0}
        self._step.turn = ident
        self._send({'id': ident, 'method': 'session/prompt', 'params': {'sessionId': self._session, 'prompt': prompt}})
        return ident

    def _usage(self, turn, usage, cumulative=False):
        step = turn['step']
        cached = usage.get('cachedReadTokens', usage.get('cache_read_input_tokens', 0))
        values = {'input': usage.get('inputTokens', usage.get('input_tokens', 0) + cached),
                  'output': usage.get('outputTokens', usage.get('output_tokens', 0)), 'cached': cached,
                  'reasoning': usage.get('reasoningTokens', usage.get('reasoning_tokens', 0)),
                  'ticks': usage.get('costUsdTicks', 0)}
        for key, value in values.items():
            delta = max(0, value - turn[key]) if cumulative else value
            turn[key] += delta
            if key == 'input':
                step.usage['prompt_tokens'] += delta
            elif key == 'output':
                step.usage['completion_tokens'] += delta
            elif key == 'cached':
                step.cached += delta
            else:
                step.items[key] = step.items.get(key, 0) + delta
        step.usage_received = True

    def _response(self, message):
        turn = self._turns.get(message.get('id'))
        if turn is None:
            return super()._response(message)
        step = turn['step']
        try:
            result = self._result('session/prompt', message)
            if (usage := (result.get('_meta') or {}).get('usage')):
                self._usage(turn, usage, True)
            if result.get('stopReason') not in ('end_turn', 'cancelled'):
                raise self.failure('Grok turn did not complete')
        except planchannel.ChannelFailure as exc:
            if not step.event.is_set():
                step.error = exc
                self._finish(step)
        step.turn_ended = True
        step.items['_ended_at'] = time.monotonic()
        self._responses[message['id']] = message  # cancellation waits for the prompt reply

    def _check_tool(self, update):
        tool = (update.get('_meta') or {}).get('x.ai/tool') or {}
        name, kind = tool.get('name'), tool.get('kind')
        ident = update.get('toolCallId')
        # 2026-11-05, S4/S5/S8/S9/S11/S13: terminal status patches omit metadata.
        # Trust only an already checked declaration, never an unknown tool id.
        if (not tool and update.get('sessionUpdate') == 'tool_call_update'
                and update.get('status') in ('completed', 'failed') and ident in self._native_calls):
            return
        if name not in ('search_tool', 'use_tool') or kind != name:
            raise planchannel.ChannelSecurity(f'Grok native tool forbidden: tool={name} kind={kind}')
        raw = update.get('rawInput')
        if name == 'use_tool' and isinstance(raw, dict) and 'tool_name' in raw:
            target = raw['tool_name']
            if not isinstance(target, str) or not target.startswith('shaft__') or target[7:] not in self._tool_names:
                raise planchannel.ChannelSecurity('Grok native tool forbidden: tool=' + str(target))
        if isinstance(ident, str):
            self._native_calls.add(ident)

    def _dispatch(self, message):
        method, params = message['method'], message.get('params') or {}
        if 'id' in message:
            if method == 'session/request_permission':
                name = ((params.get('toolCall') or {}).get('rawInput') or {}).get('tool_name', '')
                security = None
                try:
                    self._check_tool(params.get('toolCall') or {})
                except planchannel.ChannelSecurity as exc:
                    security = exc
                allowed = (security is None and params.get('sessionId') == self._session and isinstance(name, str) and name.startswith('shaft__')
                           and name[7:] in self._tool_names)
                self._send({'id': message['id'], 'result': {'outcome': {
                    'outcome': 'selected', 'optionId': 'allow-once' if allowed else 'reject-once'}}})
                if security:
                    raise security
                if not allowed:
                    raise planchannel.ChannelSecurity('Grok refused permission for tool=' + str(name))
            else:
                self._send({'id': message['id'], 'error': {'code': -32601, 'message': 'Native client tools denied'}})
                raise planchannel.ChannelSecurity('Grok requested native client method=' + method)
            return
        update = params.get('update') or {}
        kind = update.get('sessionUpdate')
        if kind in ('tool_call', 'tool_call_update'):
            self._check_tool(update)
        if params.get('sessionId') != self._session:
            return
        if kind == 'image_dropped':
            raise self.failure('Grok dropped an image; image input was not delivered')
        step = self._step
        if step is None:
            return
        turn = self._turns.get(step.turn)
        if kind == 'response_completed' and turn:
            self._usage(turn, update['usage'])
        elif kind == 'turn_completed' and turn and update.get('usage'):
            self._usage(turn, update['usage'], True)
        elif kind == 'agent_message_chunk' and not step.event.is_set():
            content = update.get('content') or {}
            if content.get('type') == 'text':
                step.text += content['text']

    def _bridge_call(self, bridge, value, answer):
        with self._cv:
            if bridge is not self._bridge or self._closed or bridge.stopped.is_set():
                raise planchannel.ChannelClosed('Grok bridge closed')
            params = value['params']
            name, arguments = params.get('name'), params.get('arguments', {})
            if name not in self._tool_names or not isinstance(arguments, dict):
                raise planchannel.ChannelSecurity('Grok invalid bridge tool=' + str(name))
            step = self._step
            if step is None or step.turn is None or step.turn_ended:
                answer.put({'isError': True, 'content': [{'type': 'text', 'text': 'Grok call outside turn'}]})
                return
            if step.event.is_set():
                answer.put({'isError': True, 'content': [{'type': 'text', 'text': 'Repeat after other results arrive'}]})
                self.stats['late_calls'] += 1
                return
            ident = uuid.uuid4().hex
            step.calls.append({'id': ident, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(arguments)}})
            step.pending.append((ident, answer, bridge))
            step.last_call = time.monotonic()
            self._cv.notify_all()

    def _calls_ready(self, step, now):
        return (step.calls and now - step.last_call >= .15
                and (step.usage_received or now - step.last_call >= 1))

    def _answer_calls(self, pending, results, text, images):
        extra = ([{'type': 'text', 'text': text}] if text else []) + self._images(images)
        turn = self._turns[self._step.turn]
        if turn['step'].turn_ended or any(b.stopped.is_set() for _, _, b in pending):
            return False
        turn['step'] = self._step
        for i, (ident, answer, bridge) in enumerate(pending):
            content = [{'type': 'text', 'text': results.get(ident, 'not run')}]
            if i == len(pending) - 1:
                content += extra
            answer.put({'isError': ident not in results, 'content': content})
        return True

    def _cancel_turn(self, turn):
        self._send({'method': 'session/cancel', 'params': {'sessionId': self._session}})
        deadline = time.monotonic() + self.timeout_s
        with self._cv:
            while turn not in self._responses:
                if self._fatal:
                    raise self._fatal
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self.failure('Grok cancel timeout')
                self._cv.wait(remaining)
            reply = self._responses.pop(turn)
            result = self._result('session/cancel prompt', reply)
            if result.get('stopReason') not in ('cancelled', 'end_turn'):
                raise self.failure('Grok cancel did not end turn')
        if self._bridge:
            self._bridge.close()

    def _usage_cache(self, step):
        return {'cached_input_tokens': step.cached, 'reasoning_tokens': step.items.get('reasoning', 0),
                'api_equivalent_usd': step.items.get('ticks', 0) / 10**10}

    def _stop_process(self):
        # Reuse the shared transport lifecycle on an isolated reference, preserving channel state.
        if self._bridge:
            self._bridge.close()
            self._bridge = None
        with self._cv:
            self._restarting = True
            self._kill()
            self._outgoing.put(None)
            self._cv.notify_all()
        self._process.wait()
        for worker in self._workers:
            worker.join(timeout=1)
        for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
            pipe.close()
        shutil.rmtree(self._folder, ignore_errors=True)
        self._process = self._folder = None
        self._workers = []
        self._outgoing = queue.Queue()
        self._turns.clear()
        self._restarting = False

    def _read(self):
        try:
            for raw in self._process.stdout:
                message = json.loads(raw)
                with self._cv:
                    if self._restarting:
                        return
                    if 'method' in message:
                        self._dispatch(message)
                    else:
                        self._response(message)
                    self._cv.notify_all()
        except (ValueError, KeyError, TypeError):
            self._fail(self.failure('malformed Grok protocol'))
        except planchannel.ChannelFailure as exc:
            self._fail(exc)
        finally:
            if not self._closed and not self._restarting:
                self._fail(self.failure('Grok process died'))

    def _fail(self, exc):
        if not self._restarting:
            super()._fail(exc)

    def _watch(self):
        with self._cv:
            while not self._closed and not self._fatal and not self._restarting:
                step = self._step
                now = time.monotonic()
                if step and not step.event.is_set():
                    if step.turn_ended and now - step.items.get('_ended_at', now) >= .2:
                        self._finish(step, 'tool_calls' if step.calls else 'stop')
                    elif self._calls_ready(step, now):
                        self._finish(step, 'tool_calls')
                    elif not step.calls and not step.turn_ended and now - step.started >= self.timeout_s:
                        self._fail(self.failure('Grok step timeout'))
                        self._kill()
                self._cv.wait(.025)

    def close(self):
        with self._cv:
            if self._bridge:
                self._bridge.close()
        super().close()
