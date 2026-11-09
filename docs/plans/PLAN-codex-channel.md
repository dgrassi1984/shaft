# PLAN: Codex CLI as a model channel in the broker

Status: on main since 2026-11-04. M4 bake-off was not part of this implementation record.

## What the test showed

A private cycle replay through `shaft trial replay` used `codex/gpt-6-astra`,
codex-cli 0.166.0-alpha.6, `SHAFT_SELF_HOSTED` and a local shim running `codex exec`.

| Measure | Run 1 | Run 2 |
|---|---|---|
| Codex calls, all reaching `finish_cycle` | 10 | 7 |
| Call errors | 0 | 0 |
| Seconds in Codex, total (median call) | 126 (11.9) | 95 (10.9) |
| Input tokens (cached) | 459,535 (27,264) | 319,648 (54,528) |
| Output tokens (reasoning) | 2,421 (91) | 1,796 (85) |
| Times Codex used its own tools | 0 | 0 |
| Replay record | lost: `--keep` crashed copying `.venv`, after the run | `completed True`, claimed `partial` |

The instance's real client also parsed a tool call and a text reply from the shim's streaming path. That was one
buffered reply per call, not token streaming, and it went straight to the shim, not through the broker.

The replays were not judged. They show the channel works, not that the model is good.

## M0 results

The transport measurements used `gpt-6-astra` and codex-cli 0.166.0-alpha.6.

### Transport

| Route | Status in OpenAI docs | Uncached per step | Cached share | Notes |
|---|---|---|---|---|
| `exec --ephemeral`, full history each call | supported for scripts and scheduled jobs | about 46,000+ | near 0 | `prompt_cache_key` had no effect |
| `exec resume`, new messages only | supported | about 25,000 | about 85% | context grows about 25,000 a step, sent instructions or not |
| `app-server`, one thread per cycle, `dynamicTools` | "isn't supported for production workloads" | about 1,400 | 99% | native tool calls; Codex owns the history, so SHAFT's shrinking cannot apply |

### Allowance measurement

Cached tokens still consume allowance. Measure retained-thread usage separately from stateless calls.
A coarse percentage meter cannot support precise capacity estimates; enforce a daily token cap.

### Security

| Test | Result |
|---|---|
| Canary read, `-s read-only`, shell on | **leaked** |
| Canary read by a spawned sub-agent, no lockdown | **leaked** |
| Full lockdown (below), direct and via sub-agent | not leaked, no shell or sub-agent items |
| Outer `sandbox-exec` denying the folder, shell on | not leaked; Codex still works under it |

Lockdown flags: `--disable` for `shell_tool`, `unified_exec`, `apps`, `plugins`, `multi_agent`, `view_image`,
`image_generation`, `memories`, `hooks`, `tool_suggest`, plus `web_search="disabled"`, `agents.enabled=false`,
`mcp_servers={}`. `--disable multi_agent` alone did not stop sub-agents. Two built-in `list_mcp_resources` tools remain.
This is one hostile prompt per case, and a GitHub issue reports code-mode tools surviving `--disable`, so **the outer
Seatbelt profile is required, not optional**. Codex also appends its own permissions/environment text (`include_permissions_instructions=false`, `include_environment_context=false`, `include_collaboration_mode_instructions=false`) that says the session is read-only, so instances stopped writing.

### Other findings

- Four parallel `exec` calls ran cleanly.
- Multiple tool calls in one reply worked in the example-a replay.
- Over-long input fails at 1,048,576 characters with stderr only and no `turn.failed`. SHAFT prompts are about 125,000.
- `turn.failed` and `error` carry text only. A real rate limit has not been observed.
- Truncation is still unseen: the long-answer probe was declined in 170 tokens.

### Plan terms

- Confirmed on the pages: "API keys are still the recommended default for automation", and scheduled `codex exec` is a
  documented use.
- OpenAI's terms page refused the checking lane (HTTP 403). Grok's quote of a ban on programmatic extraction of Output
  is unverified, and whether it applies here is a question for you, not one I can settle.

## Problems the channel must solve

1. **Secrets.** Lockdown plus an outer Seatbelt profile, as above. Any native tool item fails the call.
2. **Plan limits.** Measure allowance consumption before sizing a workload. A token cap per day is required on any route.
3. **Silent failures.** Prompts go through a file, because `Popen.communicate` stops writing stdin after its first
   timeout. `max_tokens` has no Codex equivalent. Unparseable output must be an error, never `stop`, because only
   `finish_reason == "length"` raises `Truncated` (`template/shaft/llm.py:121`).
4. **Dependencies.** Alpha CLI builds, and an `app-server` protocol that can change without notice. The binary sits
   inside ChatGPT.app and updates with it.

## Design (decided 2026-11-04, after the app-server spike)

The design chooses `app-server`, accepting the unsupported interface. `supervisor/codexcli.py`
implements the Codex backend; `supervisor/planchannel.py` owns the shared retained channel and provider registry.
Outside that channel this is one more provider: instance models named `codex/<model>`.

### Behaviour the spike established

- A dynamic tool call stays open until answered: held 300 s, then the turn continued.
- Parallel calls arrive together. Sequential calls arrive one at a time, after the earlier answer.
- `turn/interrupt`, or simply starting a new turn, abandons a pending call. The thread stays usable.
- App-server exits within 0.25 s when its parent dies.
- Notifications carry `threadId` and `turnId`. `thread/tokenUsage/updated` carries `last` for each model request.
- Over-long input is a JSON-RPC error (-32602, `input_too_large`) and the thread survives it.
- **Lockdown needs all three layers.** `--ignore-user-config` doesn't exist for app-server and `mcp_servers={}` did not
  remove the real config's MCP tools, so the channel needs a private `CODEX_HOME`. Dynamic tools run through
  `code_mode_host`, so that feature must stay on. An outer Seatbelt profile stopped a surviving JS tool.

### One session per Broker

`planchannel.Channel`, subclassed by `codexcli.Channel`, owns one app-server process and one current thread, created on the first request and closed by `Broker.stop()`.
Nothing crosses runs. Requests are serialised by a lock. Each model step is a `planchannel.Step` object (a future) that survives its
HTTP handler, so a client that has gone away cannot lose the reply.

### Mapping stateless requests onto a thread

The instance rewrites old messages on most steps (`_unsummarise`, `shrink`, pacing), so comparing history is useless. The
channel never reads history it has already sent. It anchors on its own last reply instead:

1. **New thread** when there is no thread, or the system text or tool set differs. Interrupt any open turn, `thread/start`
   with `baseInstructions` and `dynamicTools`, and seed the first turn with a text transcript of every non-system message.
   Images become numbered `[image N]` text markers in the transcript; their URLs follow as ordered `image` inputs.
   The transcript contains no base64. Count it as `rebuilds`.
2. **Repeat** when the request's anchor equals that of the request that produced the current `Step` (a pause, or a 502
   retry). Return that `Step`, waiting if it is still running. A finished empty `Step` is a failure, never returned twice.
3. **Extend** when the last assistant message carries the ids of the last reply's tool calls, or its text equals the
   last reply's text with no tool calls. The delta is everything after it.
   - Tool results answer the pending calls by `tool_call_id`, in order, with `success: true`.
   - A pending call with no result gets `success: false` and "not run" (the loop's `REPEAT_LIMIT` leaves some unanswered).
   - Tool results carry text only. After answering all pending calls, send any delta user text (marked `[message]`)
     and images via `turn/steer`, with `expectedTurnId` set to the held turn. If the RPC reports no active turn, start
     a new turn with that user delta; other steer errors fail the step. When no call is pending, start a new turn;
     user-only deltas keep plain text followed by `image` inputs.
4. **Rebuild** otherwise, as in 1. An anchor that can't be found is expected after `recovery` or a discarded turn.

User anchors include text and the SHA-256 of each image URL, so equal-length image replacements differ. Old images
replaced by `images.keep_newest` before the last assistant anchor do not trigger rebuilds.

Consequences to accept: Codex's thread keeps full tool results, so `shrink` saves nothing here. The final step's reduced
tools cost one seeded rebuild a cycle. Both are logged as counters.

### Reply

Assistant text arrives as `item/agentMessage/delta` and `item/completed`. A step ends at `turn/completed` (`stop`) or when
tool calls have arrived and none followed for 150 ms (`tool_calls`). Tool ids are Codex's `callId`, verbatim.
Once a step ends, later agent text is ignored; a late `turn/completed` still marks the turn as ended.
The broker waits for `complete()` before sending a reply, including when the instance asks for a stream.
It converts that complete reply with `cloudllm.as_sse`, so a Codex failure after internal text deltas can still
fall back before any reply bytes reach the instance. `max_tokens`, `presence_penalty`,
`thinking_budget` are ignored; `reasoning_effort` maps to `turn/start` `effort`. Prepared images use
`{"type":"image","url":"data:..."}` in `turn/start` or `turn/steer` input; dynamic tool results carry text only.
Unsupported message part types raise `CodexFailure`, naming the type.

### Failure and fallback

`CodexFailure(reason)` covers a dead process, protocol surprise, `error` or a failed turn, failed login, and the weekly
cap. `CodexSecurity(CodexFailure)` is any item outside the allow-list `agentMessage`, `reasoning`, `userMessage`,
`dynamicToolCall`, or `mcpToolCall` on server `codex` for `list_mcp_*`. The list fails closed; the real replay checks it.

`CodexClosed(CodexFailure)` means intentional shutdown. It wakes pending RPCs immediately; the broker returns
a shutdown error without reporting an outage or using the fallback.

On other failures the broker sends the full request to `SHAFT_CODEX_FALLBACK_MODEL` for the rest of that run.
If the fallback cannot see images (or its capability cannot be determined), each image becomes text with an explicit
note that it could not be shown on the fallback model. A vision fallback keeps the prepared images. Replies are labelled
`codex/x (fallback y)`, priced as usual. `ReplayBroker` never falls back: a trial fails instead (`local_fallback = False`).
The first failure of an outage calls `broker.notify` and writes `.supervisor/codex_outage.json`; `panel.alerts` shows it
until a Codex call succeeds, which clears it and notifies once. A security event says so, in both places.

### Limits, accounting, login

- `SHAFT_CODEX_WEEKLY_PCT` (default 85): at or above this `usedPercent`, requests fail as `CodexFailure("weekly cap")`.
  This replaces the planned daily token cap: it bounds the real resource for instances, trials and bake-offs together.
  The value comes from `account/rateLimits/read` at start and `account/rateLimits/updated` after.
- Usage rows go through `cloudllm._record` with `usd=None`, `cache={"cached_input_tokens": n}`. Skip `ledger.record_charge`
  for `codex/` as for self-hosted, and exclude `codex/` from `panel`'s unpriced count.
- `SHAFT_CODEX_HOME` (default `.supervisor/codex-home`): a private home, empty `config.toml`, mode 700.
  `shaft codex login` runs `codex login` with it; `shaft codex status` shows login, weekly %, models and any outage.
- `SHAFT_CODEX_BIN` (default: the ChatGPT.app path). `SHAFT_CODEX_MODELS` (default
  `codex/gpt-6-astra,codex/gpt-6.1-sol`).
- `SHAFT_CODEX_LOG_HOURS` (positive integer, default 24, Codex group, runs/api): before spawning
  the app-server, prune older `logs_2.sqlite` rows in batches of 1,000 with `secure_delete=ON`.
  A nonblocking lock on `logs-pruned.stamp` limits attempts across processes to once per ten
  minutes, including unsuccessful attempts. Use a three-second SQLite busy timeout and
  `wal_checkpoint(TRUNCATE)`; VACUUM when free pages exceed both 25% and 8 MiB. Missing files,
  missing tables and SQLite errors are logged and never fail the model call. Other readers
  may defer checkpointing or VACUUM. Startup retention does not run periodically within an
  existing channel. Only the log database, its SQLite sidecars and the stamp are involved.
  Secure deletion overwrites deleted cells but can leave historical slack bytes from before
  it was enabled until VACUUM qualifies. Checkpoint again after VACUUM to apply its WAL changes.

### Contract between the lanes

The broker and generic callers use `supervisor/planchannel.py`:

```python
class ChannelFailure(Exception): reason: str
class ChannelClosed(ChannelFailure): ...       # intentional shutdown, never an outage or fallback
class ChannelSecurity(ChannelFailure): ...
def serves(model: str | None) -> bool
def models() -> list[str]
def channel_for(model, name, usage_name, log) -> Channel
class Channel:
    def __init__(self, broker_name: str, usage_name: str, log: Callable[[str], None]): ...
    def complete(self, body: dict) -> dict      # OpenAI chat.completion; usage has prompt/completion tokens
    def close(self) -> None                    # idempotent; wakes waiters, kills process group
# Provider arguments accept a prefix or a prefixed model name.
def rate_limit(provider_name="codex") -> dict | None
def saved_rate(provider_name="codex") -> dict | None  # current process, else persisted observation
def outage(provider_name="codex") -> dict | None
def report_failure(exc, notify, provider_name="codex") -> None
def report_success(notify, provider_name="codex") -> None
```

Backends subclass `planchannel.Channel` and implement these protocol operations:

```python
_start(model, effort)                         # launch process and initialize transport
_open_session(model, system, tools, effort)    # return session ID
_start_turn(text, images, effort)              # return turn ID
_answer_calls(pending, results, text, images)  # True if consumed; False if delta needs a new turn
_cancel_turn(turn)
# close() supplies shared shutdown; a backend can extend it to close extra resources.
```

The base owns message splitting, anchors, transcripts, selection, steps, accounting, stdio workers, timeouts, and
provider outage/rate state. `_dispatch` and `_result` interpret protocol events and responses; `_response` can
consume an asynchronous turn response before normal RPC correlation. Pending-call handles
are opaque to the base. Answers include any user delta so another backend can deliver it through a tool bridge.
Session identity includes effort by default; Codex keeps effort per turn, preserving its existing thread reuse.
Codex also retains its wait for usage before completing held-call steps (150 ms quiet, up to 1 s without usage).

`codexcli` retains lockdown, Seatbelt, private home, app-server mappings, item checks, weekly cap and its knobs.
Its public `Channel`, `serves`, `models`, rate/outage helpers and `CodexFailure`/`CodexClosed`/`CodexSecurity` names
remain available. Those exceptions subclass the shared errors and retain their old class names in usage rows.
The Codex outage/rate file names, logs, notifications and `shaft codex login|status` wording stay unchanged.

## Milestones

### M1: the channel (Codex lane)
`supervisor/planchannel.py` and `supervisor/codexcli.py`; shared tests in `supervisor/tests/test_planchannel.py`,
Codex protocol/lifecycle tests in `supervisor/tests/test_codexcli.py` with a fake app-server over stdio.
**Done when:** tests pass for every mapping case above, including repeat during a pause, unanswered calls, a rebuild, a
security item, process death and the weekly cap.

### M2: wiring (separate lane, disjoint files)
- `supervisor/broker.py`: route `codex/` in `_llm` before the self-hosted branch, fallback, `Broker.stop()` closes the
  channel, handle `BrokenPipeError` on the codex path. `_llm_stream` wraps generators in `contextlib.closing`.
- `supervisor/core.py`: `instance_model` validator. `cloud_model` stays as it is, so judges, describe and the trial
  fallback can't be set to `codex/`.
- `supervisor/opconfig.py:75`, its checks, and `supervisor/settings.py` choices and `MODULES`.
- `supervisor/runner.py:676`: `_sees_images` honours `extra["LLM_MODEL"]`; `:101` and `supervisor/localmodel.py:73`.
- `supervisor/vision.py:79`, `supervisor/judge.py:46`, `supervisor/consultstats.py:70`, `supervisor/bakeoff.py:92` and
  `:207`, `supervisor/panel.py`, `supervisor/version.py`.
- `supervisor/__main__.py`: `shaft codex login|status`. Docs for the knobs.
**Done when:** the supervisor suite passes on the integrated branch.

### M3: real verification (main session)
`shaft trial replay` of the same example-a cycle on `codex/gpt-6-astra`; a live conversation through the app; a forced failure
that shows the fallback, the notification and the panel alert; the planted-canary probe through the real channel.
**Done when:** each renders in the real system, then the operator signs in with `shaft codex login`.

### M4: bake-off
Per `docs/bakeoffs/README.md`. No instance moves without a ledger verdict and the operator's go-ahead.

## Regression tests to carry over
- A prompt larger than a pipe reaches a slow child.
- Native tool item fails the call.
- Non-zero exit, missing `turn.completed`, unparsable answer and non-object `arguments` each return an error.
- Child killed with its process group on timeout and on disconnect.
- Secrets absent from the child's environment.
- Transcript verbatim, including tool results.

## Verified on the real system, 2026-11-04

On branch `feature/codex-channel`, codex-cli 0.166.0-alpha.6, `gpt-6-astra`, full lockdown.

| Check | Result |
|---|---|
| `shaft trial replay` of example-a `2026-11-04T12:55:05` | `completed True`, 121.5 s, 15 steps, 1 rebuild, 241,629 input tokens, almost all cached |
| Hostile prompts through `Channel`: read canaries in home, `/private/tmp`, temp folder; write; network | nothing leaked, written or fetched |
| Real `answer` in the instance sandbox, two turns | correct replies, history kept |
| Broken binary | reply through DeepSeek, labelled and priced, alert raised, macOS notification sent |
| Recovery | next call on Codex, alert cleared |
| Web app | model picker, `/costs` weekly %, overview alert, Settings section |

Measured limits of this test:

- Usage can lag a step: a step's `usage` may be 0 and the next one larger. Totals are right.
- A second sequential tool call from one model reply is a real step with zero tokens.
- During a long tool call Codex polls about every 14 s after the first 30 s, which uses allowance.
- Replies are buffered, so conversations don't show text word by word.
- Real-binary measurement: tool-result user text/images reached the code host and caused 2–3 `view_image` retries; `turn/steer` delivered the image immediately on both tested Codex models.
- Image-bearing cloud fallback calls have not been measured in this test.
- Not observed: a real rate limit, a real login expiry, phone push, and a real security event.

## Decision

App-server channel (design decision, 2026-11-04). The alternatives are kept in git history of this plan.
