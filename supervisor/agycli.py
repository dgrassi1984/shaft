"""Antigravity (`agy`) backend for retained plan sessions: Gemini on the operator's own Antigravity subscription.

The unmodified `agy` binary does all of the authentication: `shaft antigravity login` runs agy's own Google sign-in with
a private home, and SHAFT never reads, copies or stores the resulting credential. Nothing here calls Google's API
directly with subscription credentials.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from . import claudecli, cloudllm, planchannel, sandbox
from .core import SUP_DIR, cloud_model, knob, positive_int

PREFIX = 'antigravity/'


def serves(model):
    return isinstance(model, str) and model.startswith(PREFIX) and bool(model[len(PREFIX):])


def _models(value):
    names = [s.strip() for s in value.split(',') if s.strip()]
    if not all(serves(s) for s in names):
        raise ValueError('must name antigravity/ models separated by commas')
    return names


HOME, _ = knob("SHAFT_ANTIGRAVITY_HOME", str(SUP_DIR / 'agy-home'), Path,
    'private Antigravity home: its login and configuration, never the operator\'s ~/.gemini', group='Antigravity',
    services=('runs', 'api'))
BIN, _ = knob("SHAFT_ANTIGRAVITY_BIN", str(Path.home() / '.local/bin/agy'), str,
    'Antigravity CLI executable', group='Antigravity', services=('runs', 'api'))
MODELS, _ = knob("SHAFT_ANTIGRAVITY_MODELS", "", _models,
    "only these of the models Antigravity lists, comma separated; empty: every model Antigravity lists", group='Antigravity',
    services=('runs', 'api'))
FALLBACK_MODEL, _ = knob("SHAFT_ANTIGRAVITY_FALLBACK_MODEL", 'openrouter/deepseek/deepseek-v4.1-flash', cloud_model,
    'cloud model used for the run after an Antigravity plan failure', group='Antigravity', services=('runs', 'api'))
TIMEOUT_S, _ = knob("SHAFT_ANTIGRAVITY_TIMEOUT_S", '300', positive_int,
    'seconds Antigravity may take per step, then the step is asked once more on a new session; held calls are exempt',
    group='Antigravity', services=('runs', 'api'))
LABEL, VENDOR, SEES_IMAGES = 'Antigravity', 'google', True
# agy runs a response's parallel calls one after another: the next one starts ~0.01 s after the previous completes,
# while a new model response needs a model round trip (>= 2 s measured, 2026-11-12). ACP reports usage only per turn,
# so this gap is the only sign that a tool call opens a new model response (shaft_same_turn false).
SAME_RESPONSE_GAP_S = .5
CLOSE_CANCEL_S = 30
# Where agy 1.5.6 keeps the login under its home (probe 2026-11-12). Only its presence is checked, never its content.
TOKEN_PATH = ('.gemini', 'antigravity-cli', 'antigravity-oauth-token')


def models(refresh: bool = True) -> list[str]:
    from . import effortcatalog
    listed = effortcatalog.listed("antigravity", _model_listing, refresh=refresh)
    if listed is None:
        return list(MODELS)
    return ["antigravity/" + m for m in listed if not MODELS or "antigravity/" + m in MODELS]


def home() -> Path:
    path = Path(HOME).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def child_env(home_dir: Path, folder: Path) -> dict:
    """The environment of every `agy` child. HOME is the private home, so agy's ~/.gemini (login, settings, MCP
    servers, plugins, conversations) is SHAFT's own and never the operator's."""
    return dict(HOME=str(home_dir), USER=os.environ.get('USER', ''), PATH='/usr/bin:/bin:/usr/sbin:/sbin',
                TMPDIR=str(folder), LANG='en_US.UTF-8')


def _model_listing() -> dict:
    from . import effortcatalog
    if not logged_in():                  # a signed-out list is not the plan's: never cache it
        raise RuntimeError('Antigravity is not signed in: run shaft antigravity login')
    home_dir = home()
    with tempfile.TemporaryDirectory(prefix='sa-', dir='/tmp') as temporary:
        folder = Path(temporary).resolve()
        result = subprocess.run([str(Path(BIN).expanduser()), 'models'], cwd=folder, env=child_env(home_dir, folder),
                                capture_output=True, text=True, timeout=20, check=True)
    models = {}
    for line in result.stdout.splitlines():
        ident, sep, _ = line.strip().partition('\t')
        base, _, level = ident.rpartition('-')
        if sep and base.startswith('gemini-') and level in ('low', 'medium', 'high', 'xhigh', 'max'):
            models.setdefault(base, []).append(level)
    return {base: effortcatalog.entry(levels, 'medium' if 'medium' in levels else None)
            for base, levels in models.items() if {'low', 'medium', 'high'}.issubset(levels)}


def logged_in() -> bool:
    return home().joinpath(*TOKEN_PATH).is_file()


STATE = planchannel.ProviderState('antigravity', LABEL)
BRIDGE = claudecli.BRIDGE
_Bridge = claudecli._Bridge


def _seatbelt(argv, folder, home_dir):
    quote = json.dumps
    readers = {Path(BIN).expanduser().absolute().parent, Path(BIN).expanduser().resolve().parent,
               Path(sys.executable).resolve().parent, Path(sys.base_prefix).resolve()}
    allowed = (folder, home_dir)
    profile = ['(version 1)', '(allow default)', *sandbox._operator_places_closed(),
               f'(deny file-read* file-write* (subpath {quote(str(Path.home().resolve()))}))']
    profile += [f'(allow file-read* file-write* (subpath {quote(str(p))}))' for p in allowed]
    profile += [f'(allow file-read* (subpath {quote(str(p))}))' for p in readers]
    profile += [f'(allow file-read* (literal {quote(str(BRIDGE))}))']
    ancestors = {p for item in (*allowed, *readers, BRIDGE) for p in item.parents}
    profile.append('(allow file-read-metadata ' + ' '.join(f'(literal {quote(str(p))})' for p in sorted(ancestors)) + ')')
    path = folder / 'seatbelt.sb'
    path.write_text('\n'.join(profile) + '\n')
    return sandbox.wrap(path, argv)


class Channel(claudecli.Channel):
    """ACP protocol with Claude's process-bound workers and held MCP call lifecycle."""
    prefix, label = 'antigravity', LABEL
    calls_need_turn = True

    def __init__(self, broker_name, usage_name, log):
        super().__init__(broker_name, usage_name, log)
        self._write_lock = threading.Lock()
        self.tier = None
        self._private_home = None
        self._conversation = None
        self._inert = set()
        self._tool_updates = {}
        self._last_completed = None
        self.stats['inert_tools'] = 0

    @property
    def timeout_s(self):
        return TIMEOUT_S

    def _failure(self, reason, cls=None):
        return (cls or self.failure)(reason.replace('Claude', 'Antigravity'))

    def _fail(self, exc):
        super()._fail(type(exc)(str(exc).replace('Claude', 'Antigravity')))

    def _check_rate(self):
        pass  # ACP exposes no allowance percentage.

    def _start(self, model, effort):
        if self._login_checked is None or time.monotonic() - self._login_checked >= 30:
            if not logged_in():
                raise self.failure('Antigravity login missing; run shaft antigravity login')
            self._login_checked = time.monotonic()

    def _send(self, message):
        super()._send({'jsonrpc': '2.0', **message})

    def _write(self):
        process, outgoing = self._process, self._outgoing
        try:
            while (message := outgoing.get()) is not None:
                with self._cv:
                    if self._process is not process:
                        return
                with self._write_lock:
                    process.stdin.write(json.dumps(message).encode() + b'\n')
                    process.stdin.flush()
        except (OSError, ValueError):
            with self._cv:
                if self._process is process and not self._restarting:
                    self._fail(self.failure('Antigravity stdin closed'))

    def _result(self, method, reply):
        if 'error' in reply:
            detail = str((reply.get('error') or {}).get('message', '')).lower()
            reason = ('Antigravity login missing; run shaft antigravity login'
                      if any(s in detail for s in ('not logged', 'sign in')) else
                      'Antigravity quota reached'
                      if any(s in detail for s in ('quota', 'resource_exhausted', 'rate limit', 'limit reached'))
                      else 'Antigravity turn failed')
            raise self.failure(reason)
        if not isinstance(reply.get('result'), dict):
            raise self.failure('malformed Antigravity RPC result')
        return reply['result']

    def _open_session(self, model, system, tools, effort):
        from .runner import RunRefused
        if self._process is not None:
            self._stop_process()
        self._start(model, effort)
        levels = {None: 'medium', 'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'high', 'max': 'high'}
        if effort not in levels:
            raise self.failure('invalid Antigravity effort')
        self._tool_names = {t['function']['name'] for t in tools}
        if any(not re.fullmatch(r'[A-Za-z0-9_-]+', n) for n in self._tool_names):
            raise self.failure('invalid Antigravity tool name')
        self._private_home = home()
        self._folder = Path(tempfile.mkdtemp(prefix='sa-', dir='/tmp')).resolve()
        try:
            catalogue = ['Your tools are on the MCP server "shaft". Call each one with call_mcp_tool, '
                         'ServerName "shaft", ToolName the tool\'s name, Arguments a JSON object matching its schema. '
                         'You have no other tools.']
            bridge_tools = []
            for tool in tools:
                f = tool['function']
                schema = f.get('parameters', {'type': 'object'})
                catalogue += [f"## {f['name']}", f.get('description', ''),
                              'Arguments schema: ' + json.dumps(schema, separators=(',', ':'))]
                bridge_tools.append({'name': f['name'], 'description': f.get('description', ''), 'inputSchema': schema})
            agent = self._folder / '.agents/agents/shaft/agent.md'
            agent.parent.mkdir(parents=True)
            agent.write_text('---\nname: shaft\ndescription: SHAFT instance\nexcludeDefaultComponents: true\n---\n\n'
                             + system + '\n\n' + '\n\n'.join(catalogue) + '\n')
            self._bridge = _Bridge(self, bridge_tools, self._folder / 'bridge')
            config = self._private_home / '.gemini/config/mcp_config.json'
            config.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replacement also keeps concurrent session starts from reading a partial configuration.
            fd, temporary = tempfile.mkstemp(dir=config.parent, prefix='mcp-')
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'mcpServers': {'shaft': {'command': str(Path(sys.executable).resolve()),
                        'args': ['-I', '-B', str(BRIDGE), '--tools', 'bridge/tools.json', '--socket', 'bridge/b.sock'],
                        'timeoutSeconds': TIMEOUT_S + 3600}}}, stream)
                os.replace(temporary, config)
            finally:
                Path(temporary).unlink(missing_ok=True)
            argv = [str(Path(BIN).expanduser().resolve()), 'acp', '--disable-telemetry', '--agent', 'shaft']
            self._process = subprocess.Popen(_seatbelt(argv, self._folder, self._private_home), cwd=self._folder,
                env=child_env(self._private_home, self._folder), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True)
        except (OSError, subprocess.SubprocessError, RunRefused, planchannel.ChannelFailure) as exc:
            if self._bridge:
                self._bridge.close()
                self._bridge = None
            shutil.rmtree(self._folder, ignore_errors=True)
            self._folder = None
            if isinstance(exc, RunRefused):
                from .runner import _refused
                _refused(self.broker_name, exc)
            raise self.failure('cannot start Antigravity: ' + type(exc).__name__) from exc
        self._launch = (model, effort)
        self._start_workers()
        try:
            self._request('initialize', {'protocolVersion': 1, 'clientCapabilities': {
                'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}})
            result = self._request('session/new', {'cwd': str(self._folder), 'mcpServers': []})
            ident = result['sessionId']
            if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', ident):
                raise self.failure('invalid Antigravity session id')
            self._conversation = ident
            self.tier = ((result.get('_meta') or {}).get('antigravity') or {}).get('tier', {}).get('name')
            if self.tier:
                self.log('Antigravity tier: ' + str(self.tier))
            model_id = model[len(PREFIX):] + '-' + levels[effort]
            if model_id not in {m['modelId'] for m in result['models']['availableModels']}:
                raise self.failure('Antigravity model unavailable')
            self._request('session/set_model', {'sessionId': ident, 'modelId': model_id})
            return ident
        except (planchannel.ChannelFailure, KeyError, TypeError) as exc:
            self._stop_process()
            if isinstance(exc, planchannel.ChannelFailure):
                raise
            raise self.failure('malformed Antigravity session') from exc

    def _start_turn(self, text, images, effort):
        content = [{'type': 'text', 'text': text}] + [
            {'type': 'image', 'mimeType': b['source']['media_type'], 'data': b['source']['data']}
            for b in self._images(images)]
        self._seq += 1
        self._turn = {'id': self._seq, 'step': self._step, 'initialized': True, 'cancelling': False,
                      'text': '', **dict.fromkeys(claudecli.USAGE_KEYS, 0)}
        self._inert.clear()
        self._tool_updates.clear()
        self._last_completed = None
        self._step.turn = self._seq
        self._send({'id': self._seq, 'method': 'session/prompt',
                    'params': {'sessionId': self._session, 'prompt': content}})
        return self._seq

    def _calls_ready(self, step, now):
        return step.calls and now - step.last_call >= .15

    def _book_turn(self, usage):
        values = {key: usage.get(source, 0) or 0 for key, source in (
            ('input', 'inputTokens'), ('output', 'outputTokens'), ('cached', 'cachedReadTokens'),
            ('cache_write', 'cachedWriteTokens'), ('reasoning', 'thoughtTokens'))}
        if not all(isinstance(v, int) and v >= 0 for v in values.values()):
            raise self.failure('malformed Antigravity usage')
        # inputTokens counts only the uncached prompt (a real cycle, 2026-11-12: input 69,891 beside 401,698 cache
        # reads), so the prompt's size is the sum, as for Claude.
        values['input'] += values['cached'] + values['cache_write']
        cost = cloudllm.usd(planchannel.unprefix(self._launch[0]), {
            'input_tokens': max(0, values['input'] - values['cached'] - values['cache_write']),
            'output_tokens': values['output'], 'cache_read_tokens': values['cached'],
            'cache_write_1h_tokens': values['cache_write']})
        step = self._turn['step']
        cache = {'cached_input_tokens': values['cached'], 'cache_write_tokens': values['cache_write'],
                 'reasoning_tokens': values['reasoning']}
        if cost is not None:
            values['cost'] = cost
            cache['api_equivalent_usd'] = round(cost, 6)
        if step.recorded:
            cloudllm._record(self.usage_name, step.model, values['input'], values['output'], None,
                            time.monotonic(), 'late_usage', None, cache=cache)
        else:
            for key, value in values.items():
                self._add(key, value)
            step.usage_received = True

    def _usage_cache(self, step):
        cache = {'cached_input_tokens': step.cached, 'cache_write_tokens': step.items.get('cache_write', 0),
                 'reasoning_tokens': step.items.get('reasoning', 0)}
        if 'cost' in step.items:
            cache['api_equivalent_usd'] = round(step.items['cost'], 6)
        return cache

    def _shaft_tool(self, tool):
        raw = tool.get('rawInput') or {}
        return (tool.get('name') == 'call_mcp_tool' and isinstance(raw, dict)
                and raw.get('ServerName') == 'shaft' and raw.get('ToolName') in self._tool_names
                and isinstance(raw.get('Arguments'), dict))

    @staticmethod
    def _shown(value):
        # A tool or method name is not message content: name it, so an event can be told apart from a slip.
        return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_./-]{1,64}', value) else '<invalid name>'

    def _refused(self, tool):
        raw = tool.get('rawInput') if isinstance(tool.get('rawInput'), dict) else {}
        name = self._shown(tool.get('name'))
        if tool.get('name') == 'call_mcp_tool':
            name += f" {self._shown(raw.get('ServerName'))}/{self._shown(raw.get('ToolName'))}"
        return planchannel.ChannelSecurity('Antigravity native tool forbidden: ' + name)

    def _dispatch(self, message):
        if message.get('jsonrpc') != '2.0':
            raise self.failure('malformed Antigravity protocol')
        if 'method' not in message:
            turn = self._turn
            if not turn or message.get('id') != turn['id']:
                self._responses[message['id']] = message
                return
            step = turn['step']
            if step.turn_ended:
                return
            result = self._result('session/prompt', message)
            self._book_turn(result.get('usage') or {})
            step.turn_ended = True
            step.items['_ended_at'] = time.monotonic()
            if 'Agent execution terminated due to error' in turn['text'] or (
                    result.get('stopReason') != 'end_turn'
                    and not (turn['cancelling'] and result.get('stopReason') == 'cancelled')):
                raise self.failure('Antigravity turn failed')
            return
        method, params = message['method'], message.get('params') or {}
        if 'id' in message:
            if method != 'session/request_permission':
                raise planchannel.ChannelSecurity('Antigravity client request forbidden: ' + self._shown(method))
            allowed = params.get('sessionId') == self._session and self._shaft_tool(params.get('toolCall') or {})
            option = next((o['optionId'] for o in params.get('options', [])
                           if o.get('kind') == ('allow_once' if allowed else 'reject_once')), None)
            if option is None:
                raise planchannel.ChannelSecurity('Antigravity permission option missing')
            # Write the denial before _fail kills the child; the queued writer cannot race that kill.
            response = {'jsonrpc': '2.0', 'id': message['id'],
                        'result': {'outcome': {'outcome': 'selected', 'optionId': option}}}
            if allowed:
                self._send(response)
            else:
                with self._write_lock:
                    self._process.stdin.write(json.dumps(response).encode() + b'\n')
                    self._process.stdin.flush()
                raise self._refused(params.get('toolCall') or {})
            return
        if method != 'session/update' or params.get('sessionId') != self._session:
            return
        update = params['update']
        kind = update.get('sessionUpdate')
        if kind in ('tool_call', 'tool_call_update'):
            ident = update['toolCallId']
            first = ident not in self._tool_updates
            tool = self._tool_updates.setdefault(ident, {})
            tool.update(update)
            name = tool.get('name')
            if name in ('list_resources', 'read_resource', 'manage_task'):
                if ident not in self._inert:
                    self.stats['inert_tools'] += 1
                    self._inert.add(ident)
            elif not self._shaft_tool(tool):
                raise self._refused(tool)
            else:
                now = time.monotonic()
                if first and self._turn and (self._last_completed is None
                                             or now - self._last_completed >= SAME_RESPONSE_GAP_S):
                    # A new model response: its calls count against the instance's step budget.
                    self._turn['step'].usage_received = True
                if update.get('status') in ('completed', 'failed'):
                    self._last_completed = now
        elif kind == 'agent_message_chunk' and self._turn:
            content = update['content']
            if content.get('type') == 'text':
                self._turn['text'] += content['text']
                step = self._turn['step']
                if not step.event.is_set():
                    # Buffer until the end so a silent model error never becomes a streamed reply.
                    step.text += content['text']

    def _cancel_turn(self, turn, wait_s=None):
        if not self._turn or self._turn['id'] != turn:
            return
        self._turn['cancelling'] = True
        self._send({'method': 'session/cancel', 'params': {'sessionId': self._session}})
        step = self._turn['step']
        for _, answer, _ in step.pending:
            answer.put({'isError': True, 'content': [{'type': 'text', 'text': 'Interrupted'}]})
        deadline = time.monotonic() + (self.timeout_s if wait_s is None else wait_s)
        with self._cv:
            while not step.turn_ended:
                if self._closed or self._fatal:
                    raise self._fatal or self.closed_error('Antigravity channel closed')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self.failure('Antigravity cancel timeout')
                self._cv.wait(remaining)

    def _prune(self):
        """Delete this conversation's state from the private home (agy 1.5.6 layout): its brain folder, its
        conversation database (~384 KB each) and its annotation. Never the login, the config or shared databases."""
        if self._conversation and self._private_home:
            data = self._private_home / '.gemini/antigravity-cli'
            shutil.rmtree(data / 'brain' / self._conversation, ignore_errors=True)
            for path in (*(data / 'conversations').glob(self._conversation + '.db*'),
                         data / 'annotations' / (self._conversation + '.pbtxt')):
                path.unlink(missing_ok=True)
            self._conversation = None

    def _stop_process(self):
        super()._stop_process()
        self._prune()

    def close(self):
        with self._cv:
            if not self._closed and self._turn and not self._turn['step'].turn_ended and not self._fatal:
                self._record(self._turn['step'])
                try:
                    # Only to collect the turn's usage; a hung agy must not hold the close for the step timeout.
                    self._cancel_turn(self._turn['id'], CLOSE_CANCEL_S)
                except planchannel.ChannelFailure:
                    pass
        super().close()
        self._prune()


# Provider reasoning levels, probed 2026-11-13.
EFFORTS = (('low', 'medium', 'high'), 'medium')


def efforts(model: str) -> tuple[tuple[str, ...], str | None] | None:
    return EFFORTS if serves(model) else None
