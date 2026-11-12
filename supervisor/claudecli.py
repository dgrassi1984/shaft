"""Claude Code (`claude -p`) backend for retained plan sessions, on the operator's own Claude subscription.

The unmodified `claude` binary does all of the authentication: `shaft claude login` runs `claude auth login`
with a private config directory, and SHAFT never reads, copies or stores the resulting credential. Nothing
here calls Anthropic's API directly with subscription credentials.
"""
from __future__ import annotations

import base64
import contextlib
import json
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

from . import cloudllm, planchannel, sandbox
from .core import SUP_DIR, cloud_model, knob, positive_int

PREFIX = 'claudecode/'


def serves(model):
    return isinstance(model, str) and model.startswith(PREFIX) and bool(model[len(PREFIX):])


def _models(value):
    names = [s.strip() for s in value.split(',') if s.strip()]
    if not names or not all(serves(s) for s in names):
        raise ValueError('must name claudecode/ models separated by commas')
    return names


def _percent(value):
    n = int(value)
    if not 0 <= n <= 100:
        raise ValueError('must be between 0 and 100')
    return n


HOME, _ = knob("SHAFT_CLAUDE_HOME", str(SUP_DIR / 'claude-home'), Path,
    'private Claude Code configuration and login directory', group='Claude', services=('runs', 'api'))
BIN, _ = knob("SHAFT_CLAUDE_BIN", str(Path.home() / '.local/bin/claude'), str,
    'Claude Code CLI executable', group='Claude', services=('runs', 'api'))
MODELS, _ = knob("SHAFT_CLAUDE_MODELS", 'claudecode/claude-opus-5-5,claudecode/claude-sonnet-5-5', _models,
    'Claude models offered to instances through the Claude plan, comma separated', group='Claude', services=('runs', 'api'))
WEEKLY_PCT, _ = knob("SHAFT_CLAUDE_WEEKLY_PCT", '85', _percent,
    'stop Claude plan calls at this percentage of the weekly allowance', group='Claude', services=('runs', 'api'))
FALLBACK_MODEL, _ = knob("SHAFT_CLAUDE_FALLBACK_MODEL", 'openrouter/deepseek/deepseek-v4.1-flash', cloud_model,
    'cloud model used for the run after a Claude plan failure', group='Claude', services=('runs', 'api'))
TIMEOUT_S, _ = knob("SHAFT_CLAUDE_TIMEOUT_S", '900', positive_int,
    'seconds Claude Code may take per step; held calls are exempt', group='Claude', services=('runs', 'api'))
LABEL, VENDOR, SEES_IMAGES = 'Claude', 'anthropic', True
STATE = planchannel.ProviderState('claudecode', LABEL)


def models():
    return list(MODELS)


def home() -> Path:
    path = Path(HOME).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def child_env(config_dir: Path, folder: Path) -> dict:
    """The environment of every `claude` child: private config dir, no updater, no telemetry.

    HOME stays the operator's: Claude Code finds its login in the login keychain through HOME (probe 2026-11-12:
    HOME=<run folder> reads as "Not logged in"). The sandbox still closes the home folder except that keychain file.
    """
    return dict(HOME=str(Path.home()), USER=os.environ.get('USER', ''), PATH='/usr/bin:/bin:/usr/sbin:/sbin',
                # Claude Code keeps its own temp root (/tmp/claude-<uid>) apart from TMPDIR; the sandbox closes /tmp.
                TMPDIR=str(folder), CLAUDE_CODE_TMPDIR=str(folder), LANG='en_US.UTF-8', CLAUDE_CONFIG_DIR=str(config_dir),
                DISABLE_AUTOUPDATER='1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                # Held SHAFT calls can outlast the default MCP tool timeout; the channel's own timeout governs.
                MCP_TOOL_TIMEOUT=str((TIMEOUT_S + 3600) * 1000),
                # With many long MCP tool definitions Claude Code defers them behind a server-side tool search
                # (bake-off 2026-11-12: a server_tool_use failed a cycle). Every tool stays loaded, as on the other paths.
                ENABLE_TOOL_SEARCH='false')


def login_status() -> dict:
    """`claude auth status` for the private config dir. Never contains the credential itself."""
    config_dir = home()
    env = child_env(config_dir, config_dir)
    try:
        out = subprocess.run([str(Path(BIN).expanduser()), 'auth', 'status'], env=env,
                             capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {'loggedIn': False, 'error': type(exc).__name__}


BRIDGE = Path(__file__).with_name('planbridge.py').resolve()
USAGE_KEYS = ('input', 'output', 'cached', 'cache_write', 'reasoning', 'cost')
RESERVED = 'shaft_reserved_no_tools'


def _seatbelt(argv, folder, home):
    quote = json.dumps
    readers = {Path(BIN).expanduser().absolute().parent, Path(BIN).expanduser().resolve().parent,
               Path(sys.executable).resolve().parent, Path(sys.base_prefix).resolve()}
    allowed = (folder, home)
    literals = (BRIDGE, Path.home().resolve() / 'Library/Keychains/login.keychain-db')
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
        # No ping deadline until the relay has started: a cold CLI start can take longer than the 3 s liveness gap.
        self.created, self.last_ping = time.monotonic(), None
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
                now = time.monotonic()
                silent = (now - self.last_ping > 3 if self.last_ping is not None
                          else now - self.created > self.channel.timeout_s)
                if silent:
                    self._fail(planchannel.ChannelFailure('Claude bridge died'))
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
            self._fail(planchannel.ChannelFailure('Claude bridge failed: ' + type(exc).__name__))
        except planchannel.ChannelFailure as exc:
            self._fail(exc)
        finally:
            with self.channel._cv:
                self.connections.discard(connection)

    def _fail(self, exc):
        # A retired bridge's late callback must never fail (and kill) the session that replaced it.
        with self.channel._cv:
            if not self.stopped.is_set() and self.channel._bridge is self:
                self.channel._fail(exc)

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
    prefix, label = 'claudecode', LABEL
    calls_need_turn = True

    def __init__(self, broker_name, usage_name, log):
        super().__init__(broker_name, usage_name, log)
        self._bridge = None
        self._restarting = False
        self._launch = None
        self._turn = None
        self._tool_names = set()
        self._login_checked = None

    @property
    def timeout_s(self):
        return TIMEOUT_S

    def _failure(self, reason, cls=None):
        # CLI diagnostics may echo prompt text, including tool names and images.
        return (cls or self.failure)(reason)

    def _check_rate(self):
        rate = STATE.rate
        if rate is None or rate['used_percent'] < WEEKLY_PCT:
            return
        reset = rate.get('resets_at')
        # A window past its reset says nothing about now; only a new session can observe the new one.
        if isinstance(reset, (int, float)) and time.time() >= reset:
            STATE.rate = None
            return
        raise self.failure('weekly cap')

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
        # The shared selector calls this before system/tools are available. Launch
        # in _open_session, where all immutable CLI configuration is known.
        if self._login_checked is None or time.monotonic() - self._login_checked >= 30:
            status = login_status()
            if not isinstance(status, dict) or status.get('loggedIn') is not True:
                raise self.failure('Claude login missing; run shaft claude login')
            self._login_checked = time.monotonic()

    def _open_session(self, model, system, tools, effort):
        from .runner import RunRefused
        if self._process is not None:
            self._stop_process()
        self._start(model, effort)
        self._tool_names = {t['function']['name'] for t in tools}
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+', n) or n == RESERVED for n in self._tool_names):
            raise self.failure('invalid Claude tool name')
        if effort and effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
            raise self.failure('invalid Claude effort')
        config_dir = home()
        self._folder = Path(tempfile.mkdtemp(prefix='sc-', dir='/tmp')).resolve()
        (self._folder / '.git').mkdir()  # Stop upward CLAUDE.md discovery.
        binary = str(Path(BIN).expanduser().resolve())
        env = child_env(config_dir, self._folder)
        try:
            prompt = self._folder / 'system.md'
            prompt.write_text(system)
            # Partial messages carry each response's final usage (message_delta); the assistant lines do not.
            argv = [binary, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
                    '--verbose', '--include-partial-messages', '--model', model[len(PREFIX):]]
            if effort:
                argv += ['--effort', effort]
            argv += ['--tools', '', '--strict-mcp-config', '--mcp-config', str(self._folder / 'mcp.json'),
                     '--allowedTools', *(['mcp__shaft__' + n for n in sorted(self._tool_names)]
                                        or ['mcp__shaft__' + RESERVED]),
                     '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
                     '--setting-sources', '', '--disable-slash-commands', '--no-session-persistence',
                     # Accepted though `claude --help` does not list it (2.1.294); keeps the prompt off the argv.
                     '--system-prompt-file', str(prompt)]
            self._bridge = _Bridge(self, [{'name': t['function']['name'],
                'description': t['function'].get('description', ''),
                'inputSchema': t['function'].get('parameters', {'type': 'object'})} for t in tools],
                self._folder / 'bridge')
            (self._folder / 'mcp.json').write_text(json.dumps({'mcpServers': {'shaft': {
                'command': str(Path(sys.executable).resolve()),
                'args': ['-I', '-B', str(BRIDGE), '--tools', str(self._bridge.tools_path),
                         '--socket', self._bridge.address]}}}))
            self._process = subprocess.Popen(_seatbelt(argv, self._folder, config_dir), cwd=self._folder,
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
        except (OSError, subprocess.SubprocessError, RunRefused, planchannel.ChannelFailure) as exc:
            if self._bridge:
                self._bridge.close()
                self._bridge = None
            shutil.rmtree(self._folder, ignore_errors=True)
            self._folder = None
            if isinstance(exc, RunRefused):
                from .runner import _refused
                _refused(self.broker_name, exc)
            raise self.failure('cannot start Claude: ' + type(exc).__name__) from exc
        self._launch = (model, effort)
        self._start_workers()
        return uuid.uuid4().hex

    @staticmethod
    def _images(images):
        from . import vision
        content = []
        for url in images:
            try:
                media, data = vision.split_data_url(url)
                if vision.sniff(data) != media:
                    raise ValueError('image type mismatch')
                with tempfile.TemporaryDirectory(prefix='sci-', dir='/tmp') as folder:
                    path = Path(folder) / 'image'
                    path.write_bytes(data)
                    result = vision._sips('-g', 'pixelWidth', '-g', 'pixelHeight', str(path))
                sizes = [int(line.split(':')[1]) for line in result.stdout.splitlines()
                         if 'pixel' in line and ':' in line and line.split(':')[1].strip().isdigit()]
                if result.returncode or len(sizes) != 2 or min(sizes) < 8:
                    raise ValueError('image must be at least 8x8')
                content.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media,
                                                          'data': base64.b64encode(data).decode()}})
            except (ValueError, OSError, subprocess.SubprocessError, vision.VisionError) as exc:
                raise planchannel.ChannelFailure('Claude image refused: invalid image or smaller than 8x8') from exc
        return content

    def _start_turn(self, text, images, effort):
        content = [{'type': 'text', 'text': text}, *self._images(images)]
        self._seq += 1
        self._turn = {'id': self._seq, 'step': self._step, 'messages': {}, 'current': None, 'initialized': False,
                      'cancelling': False, **dict.fromkeys(USAGE_KEYS, 0)}
        self._step.turn = self._seq
        self._send({'type': 'user', 'message': {'role': 'user', 'content': content}})
        return self._seq

    def _add(self, key, delta):
        turn = self._turn
        step = turn['step']
        turn[key] += delta
        if key == 'input':
            step.usage['prompt_tokens'] += delta
        elif key == 'output':
            step.usage['completion_tokens'] += delta
        elif key == 'cached':
            step.cached += delta
        else:
            step.items[key] = step.items.get(key, 0) + delta

    @staticmethod
    def _figures(usage, final, model):
        cached = usage.get('cache_read_input_tokens') or 0
        write = usage.get('cache_creation_input_tokens') or 0
        values = {'input': (usage.get('input_tokens') or 0) + cached + write, 'cached': cached, 'cache_write': write}
        if final:
            values['output'] = usage.get('output_tokens') or 0
            values['reasoning'] = (usage.get('output_tokens_details') or {}).get('thinking_tokens') or 0
            values['cost'] = (cloudllm.usd(model, cloudllm.billed(usage)) or 0) if model else 0
        if not all(isinstance(v, (int, float)) and v >= 0 for v in values.values()):
            raise planchannel.ChannelFailure('malformed Claude usage')
        return values

    def _account(self, ident, usage, final):
        """One model response's usage, counted once. A cycle is a single Claude Code turn whose `result` never
        comes when the cycle ends on a tool call (replay 2026-11-12: every row had output 0), so each response's
        final figures (stream message_delta) are booked as they arrive; earlier reports only add the difference."""
        turn = self._turn
        seen = turn['messages'].setdefault(ident, dict.fromkeys(USAGE_KEYS, 0))
        model = planchannel.unprefix(self._launch[0]) if self._launch else None
        deltas = {}
        for key, value in self._figures(usage, final, model).items():
            deltas[key] = max(0, value - seen[key])
            seen[key] += deltas[key]
        self._book(deltas)

    def _reconcile(self, usage, cost):
        """The turn's aggregate, when Claude Code sends it, tops up anything the per-response reports missed."""
        values = self._figures(usage, True, None)
        values['cost'] = cost if isinstance(cost, (int, float)) and cost >= 0 else 0
        self._book({key: max(0, value - self._turn[key]) for key, value in values.items()})

    def _book(self, deltas):
        """Credit usage to the step that owns the turn now. A step already written up (a cancel or rebuild records
        it before the interrupt lands) gets the late remainder as a row of its own, never silently dropped."""
        if not any(deltas.values()):
            return  # a repeated report: it must not mark the step as carrying usage (shaft_same_turn)
        turn = self._turn
        step = turn['step']
        if step.recorded:
            for key, delta in deltas.items():
                turn[key] += delta
            cloudllm._record(self.usage_name, step.model, deltas['input'], deltas.get('output', 0), None,
                             time.monotonic(), 'late_usage', None,
                             cache={'cached_input_tokens': deltas['cached'], 'cache_write_tokens': deltas['cache_write'],
                                    'reasoning_tokens': deltas.get('reasoning', 0),
                                    'api_equivalent_usd': round(deltas.get('cost', 0), 6)})
            return
        for key, delta in deltas.items():
            self._add(key, delta)
        step.usage_received = True

    def _dispatch(self, message):
        kind = message['type']
        if kind == 'control_response':
            response = message['response']
            self._responses[response['request_id']] = response
            return
        if kind == 'rate_limit_event':
            info = message['rate_limit_info']
            # 'allowed_warning' is a normal state near the limit; only a refusal stops the run.
            if info.get('status') == 'rejected':
                raise self.failure('Claude rate limit rejected')
            weekly = (info.get('unifiedWindows') or {}).get('seven_day')
            if weekly is not None:
                utilization = weekly['utilization']
                if not isinstance(utilization, (int, float)) or not 0 <= utilization <= 1:
                    raise self.failure('malformed Claude weekly rate')
                STATE.rate = {'used_percent': round(utilization * 100), 'window_mins': 10080,
                              'resets_at': weekly.get('resetsAt', info.get('resetsAt'))}
            return
        turn = self._turn
        if kind == 'system' and message.get('subtype') == 'init':
            expected = {'mcp__shaft__' + n for n in self._tool_names}
            tools = message.get('tools')
            if not isinstance(tools, list) or len(tools) != len(expected) or set(tools) != expected:
                raise planchannel.ChannelSecurity('Claude init tools mismatch')
            if message.get('permissionMode') != 'dontAsk' or message.get('apiKeySource') != 'none':
                raise planchannel.ChannelSecurity('Claude init permissions or authentication mismatch')
            if not any(s.get('name') == 'shaft' and s.get('status') == 'connected'
                       for s in message.get('mcp_servers', [])):
                raise self.failure('Claude shaft MCP server unavailable')
            if turn:
                turn['initialized'] = True
            return
        if kind == 'assistant':
            response = message['message']
            blocks = response['content']
            for block in blocks:
                if block['type'] in ('tool_use', 'server_tool_use'):
                    name = block.get('name')
                    if (block['type'] != 'tool_use' or not isinstance(name, str)
                            or not name.startswith('mcp__shaft__') or name[12:] not in self._tool_names):
                        # A tool name is not message content: name it, so an event can be told apart from a slip.
                        shown = name if isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', name) else '<invalid name>'
                        raise planchannel.ChannelSecurity(f'Claude native tool forbidden: {block["type"]} {shown}')
            if not turn or not turn['initialized']:
                raise self.failure('Claude assistant before init')
            step = turn['step']
            ident = response['id']
            if not isinstance(ident, str):
                raise self.failure('malformed Claude message id')
            self._account(ident, response.get('usage') or {}, False)
            for block in blocks:
                if block['type'] == 'text' and not step.event.is_set():
                    self._append_text(step, block['text'])
            return
        if kind == 'stream_event':
            event = message['event']
            if event['type'] not in ('message_start', 'message_delta'):
                return  # text and tool deltas: the full assistant lines carry the same content
            if not turn or not turn['initialized']:
                raise self.failure('Claude response before init')
            if event['type'] == 'message_start':
                ident = event['message']['id']
                if not isinstance(ident, str):
                    raise self.failure('malformed Claude message id')
                turn['current'] = ident
                self._account(ident, event['message'].get('usage') or {}, False)
            elif turn['current'] is not None:
                self._account(turn['current'], event.get('usage') or {}, True)
            return
        if kind == 'result':
            if not turn:
                raise self.failure('Claude result outside turn')
            step = turn['step']
            if step.turn_ended:
                return
            self._reconcile(message.get('usage') or {}, message.get('total_cost_usd', 0))
            step.turn_ended = True
            step.items['_ended_at'] = time.monotonic()
            if message.get('is_error') or message.get('subtype') != 'success':
                if not (turn['cancelling'] and message.get('subtype') == 'error_during_execution'):
                    # Classify only; never echo the result text (it can quote the prompt).
                    text = str(message.get('result', '')).lower()
                    reason = ('Claude login missing; run shaft claude login' if 'not logged in' in text
                              else 'Claude usage limit reached' if 'usage limit' in text or 'limit reached' in text
                              else 'Claude turn failed')
                    raise self.failure(reason)
            elif not turn['initialized']:
                raise self.failure('Claude result before init')
            return
        if kind == 'control_request':
            raise planchannel.ChannelSecurity('Claude requested native permissions')
        # tool_progress heartbeats and other system notifications carry no reply.

    def _bridge_call(self, bridge, value, answer):
        with self._cv:
            if bridge is not self._bridge or self._closed or bridge.stopped.is_set():
                raise planchannel.ChannelClosed('Claude bridge closed')
            params = value['params']
            name, arguments = params.get('name'), params.get('arguments', {})
            if name not in self._tool_names or not isinstance(arguments, dict):
                raise planchannel.ChannelSecurity('Claude invalid bridge tool')
            step = self._step
            if (step is None or step.turn is None or step.turn_ended
                    or not self._turn or self._turn['cancelling']):
                answer.put({'isError': True, 'content': [{'type': 'text', 'text': 'Claude call outside turn'}]})
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
        # Usage precedes the response's first call. Later sequential calls are
        # ready too, but keep usage_received false so shaft_same_turn stays true.
        # Never hand out a call before this turn's init passed the tool and permission check.
        return (step.calls and now - step.last_call >= .15 and bool(self._turn and self._turn['initialized'])
                and (step.usage_received or bool(self._turn['messages']) or now - step.last_call >= 1))

    def _answer_calls(self, pending, results, text, images):
        # MCP results use MCP image blocks, unlike Claude's user-input blocks.
        extra = ([{'type': 'text', 'text': text}] if text else []) + [
            {'type': 'image', 'mimeType': b['source']['media_type'], 'data': b['source']['data']}
            for b in self._images(images)]
        if self._turn['step'].turn_ended or any(b.stopped.is_set() for _, _, b in pending):
            return False
        self._turn['step'] = self._step
        for i, (ident, answer, bridge) in enumerate(pending):
            content = [{'type': 'text', 'text': results.get(ident, 'not run')}]
            if i == len(pending) - 1:
                content += extra
            answer.put({'isError': ident not in results, 'content': content})
        return True

    def _cancel_turn(self, turn):
        if not self._turn or self._turn['id'] != turn:
            return
        ident = uuid.uuid4().hex
        self._turn['cancelling'] = True
        self._send({'type': 'control_request', 'request_id': ident, 'request': {'subtype': 'interrupt'}})
        step = self._turn['step']
        for _, answer, _ in step.pending:
            answer.put({'isError': True, 'content': [{'type': 'text', 'text': 'Interrupted'}]})
        deadline = time.monotonic() + self.timeout_s
        with self._cv:
            while not step.turn_ended or ident not in self._responses:
                if self._closed or self._fatal:
                    raise self._fatal or self.closed_error('Claude channel closed')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self.failure('Claude cancel timeout')
                self._cv.wait(remaining)
            if self._responses.pop(ident).get('subtype') != 'success':
                raise self.failure('Claude interrupt failed')

    def _usage_cache(self, step):
        return {'cached_input_tokens': step.cached, 'cache_write_tokens': step.items.get('cache_write', 0),
                'reasoning_tokens': step.items.get('reasoning', 0), 'api_equivalent_usd': round(step.items.get('cost', 0), 6)}

    def _stop_process(self):
        with self._cv:
            self._restarting = True
            try:
                if self._bridge:
                    self._bridge.close()
                    self._bridge = None
                self._kill()
                self._outgoing.put(None)
                self._cv.notify_all()
                # _open_session runs under the selector's condition. Release it while
                # workers drain; old workers return on _restarting even if late.
                deadline = time.monotonic() + 3
                while any(worker.is_alive() for worker in self._workers) and time.monotonic() < deadline:
                    self._cv.wait(.025)
                if any(worker.is_alive() for worker in self._workers):
                    self.log('Claude workers still draining after restart')
                self._process.wait()
                for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                    pipe.close()
                shutil.rmtree(self._folder, ignore_errors=True)
                self._process = self._folder = None
                self._workers = []
                self._outgoing = queue.Queue()
                self._responses.clear()
                self._turn = None
            finally:
                # A stuck flag would mute every later failure and leave the channel dead.
                self._restarting = False

    def _write(self):
        # Bound like the reader: a writer that outlives a restart must not deliver an old line to the new process.
        process, outgoing = self._process, self._outgoing
        try:
            while (message := outgoing.get()) is not None:
                with self._cv:
                    if self._process is not process:
                        return
                process.stdin.write(json.dumps(message).encode() + b'\n')
                process.stdin.flush()
        except (OSError, ValueError):
            with self._cv:
                if self._process is process and not self._restarting:
                    self._fail(self.failure('Claude stdin closed'))

    def _read(self):
        # A reader serves one process: after a restart, a late one must never fail the new process.
        process = self._process
        current = lambda: self._process is process and not self._restarting
        try:
            for raw in process.stdout:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    raise self.failure('malformed Claude protocol')
                with self._cv:
                    if not current():
                        return
                    self._dispatch(message)
                    self._cv.notify_all()
        except (ValueError, KeyError, TypeError, AttributeError):
            with self._cv:
                if current():
                    self._fail(self.failure('malformed Claude protocol'))
        except planchannel.ChannelFailure as exc:
            with self._cv:
                if current():
                    self._fail(exc)
        finally:
            with self._cv:
                if not self._closed and current() and not self._fatal:
                    self._fail(self.failure('Claude process died'))

    def _fail(self, exc):
        # Keep the first cause: the dead process's bridge stops pinging and would replace a security event with
        # "bridge died" (bake-off 2026-11-12), and every new reason notifies the operator again.
        if self._restarting or self._fatal is not None:
            return
        super()._fail(exc)
        if self._bridge:
            self._bridge.close()
        self._kill()

    def _watch(self):
        process = self._process  # one watcher per process; a restart retires it
        with self._cv:
            while not self._closed and not self._fatal and not self._restarting and self._process is process:
                step = self._step
                now = time.monotonic()
                if step and not step.event.is_set():
                    if step.turn_ended and now - step.items.get('_ended_at', now) >= .2:
                        self._finish(step, 'tool_calls' if step.calls else 'stop')
                    elif self._calls_ready(step, now):
                        self._finish(step, 'tool_calls')
                    elif not step.turn_ended and now - step.started >= self.timeout_s and (
                            not step.calls or not (self._turn and self._turn['initialized'])):
                        self._fail(self.failure('Claude step timeout'))
                self._cv.wait(.025)

    def close(self):
        with self._cv:
            if self._bridge:
                self._bridge.close()
        super().close()
