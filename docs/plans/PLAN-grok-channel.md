# PLAN: Grok Build CLI as a model channel

Status: on main since 2026-11-05. Verification covered CLI replay, hostile prompts, a conversation, forced failure, images and Codex regression checks.

The Codex channel (`docs/plans/PLAN-codex-channel.md`) is the model. Outside the channel code this is one more provider:
instance models named `grok/<model>`.

## What the spike established

Run on `grok` 1.0.70, `grok agent stdio`, inside an outer Seatbelt profile, 2026-11-05.

| Question | Result |
|---|---|
| Protocol | ACP JSON-RPC: `initialize`, `authenticate` (`cached_token`), `session/new`, `session/prompt`, `session/update` |
| CLI flags on `agent` or `stdio` | `--tools`, `--deny`, `--disallowed-tools`, `--sandbox`, `--no-subagents` are all refused there |
| Flags before `agent` | Accepted, but all 33 native tools stayed callable |
| **Lockdown** | `grok agent --agent-profile <file>` with a **non-empty** `tools` list leaves only `search_tool` and `use_tool` |
| Empty `tools: []` | **No restriction at all**: fails open, so the list must never be empty |
| Client tools | Local stdio MCP server, reached by the model through `search_tool` then `use_tool`, named `shaft__<tool>` |
| Permission | Each MCP call raises `session/request_permission`; `allow-once` runs it, `reject-once` refuses it |
| Hostile prompts | Reading canaries, writing, shell, sub-agents: nothing leaked, written or run |
| Held call | Held 120 s, then the turn continued, with no model calls during the hold |
| Parallel calls | Arrive together, can be answered in any order |
| Next turn | `session/prompt` on one session remembers, cached about 98% from the third turn |
| Tools change | No live update (`-32601`); a new session on the same process works, seeded with a text transcript |
| Cancel | `session/cancel` with a call pending ends the turn; the session stays usable |
| Parent death | Agent and bridge exit in under 3 s with `--no-leader`; leader mode fails inside the sandbox |
| Instructions | `_meta.systemPromptOverride` works. Input about 5,500 tokens. A short default rule about affirmative wording and the bundled skill list remain |
| Images | An MCP image result reaches the model; an image in a prompt must be at least 8×8 |
| Usage | `session/update` usage and `_meta.usage`: input, cached, output, reasoning, `costUsdTicks` |
| Weekly pool | **No programmatic read**: `x.ai/usage` is `-32601` |
| Very long prompts | A 3M-character prompt was handled through extra model calls and an omitted-text mechanism: not silent, but a rebuild must stay well under this |
| Local storage | `sessions/` holds transcripts in SQLite (18 MB after the spike): needs retention like Codex's log |

## Design

### One shared channel

`supervisor/planchannel.py` holds what Codex and Grok share: message splitting, anchors, transcripts, `Step`, the
new/repeat/extend/rebuild choice, `complete`, outage and rate state, and the failure classes. A channel supplies a small
backend: start, new session, start turn, answer calls, cancel, close. `supervisor/codexcli.py` and
`supervisor/grokcli.py` are those backends. The broker and every wiring seam ask `planchannel`, not a provider: one code
path, no copied logic, and one migration for Codex rather than a second parallel system.

Names: `ChannelFailure`, `ChannelSecurity`, `ChannelClosed`, `planchannel.serves(model)`, `planchannel.models()`,
`planchannel.channel_for(model, name, usage_name, log)`. Outage and rate files are per provider:
`SUP_DIR/<provider>_outage.json`, `<provider>_rate.json`. The Codex channel's behaviour, tests and existing files stay
the same.

### Grok backend

- One process per Broker: `<bin> agent --no-leader --agent-profile <file> -m <model>
  --reasoning-effort <e> stdio`, wrapped in the same style of Seatbelt profile as Codex, with the binary's folder, the
  bridge script and the Python it runs readable.
- The environment is explicit: `HOME`, `PATH`, `TMPDIR`, `LANG`, `GROK_HOME`, `GROK_DISABLE_AUTOUPDATER=1`, `GROK_MEMORY=0`,
  and every `GROK_{CLAUDE,CURSOR,CODEX}_{SKILLS,RULES,AGENTS,MCPS,HOOKS,SESSIONS}_ENABLED=0`.
- `SHAFT_GROK_HOME` (default `.supervisor/grok-home`): private, mode 700. No `auth.json` is a `ChannelFailure` naming
  `shaft grok login`.
- The agent profile is generated per session and lists every `mcp__shaft__<tool>`. **It always holds at least one
  entry**: with no tools, the name of a reserved tool the bridge doesn't serve. An empty list fails open.
- `supervisor/planbridge.py`, a small MCP stdio server started by Grok: serves the session's tool list and relays each
  `tools/call` over a Unix socket in the channel's temp folder, blocking until the channel answers. Stdlib only, imports
  nothing from the supervisor.
- `session/request_permission`: `allow-once` only when `rawInput.tool_name` starts with `shaft__` and the tool is in this
  session's list. Anything else is `reject-once` plus `ChannelSecurity`.
- `tool_call` updates are checked by `_meta["x.ai/tool"].name`: allow-list `search_tool`, `use_tool`. Anything else is
  `ChannelSecurity`, fail closed, with the name in the reason. A tool whose `kind` is a harmless bookkeeping one found in
  real runs goes on the list with a dated comment, as `sleep` did for Codex.
- The model has to find a tool's schema through `search_tool`. `_meta.rules` lists the tool names and says to call them
  with `use_tool`, which limits the extra model calls.
- A step ends when calls are pending and quiet for 150 ms, or at the `session/prompt` response (`stop`). Usage comes from
  `session/update` usage events, attributed like Codex's.
- Answers go back through the bridge. A user delta during a turn goes in the last answered result, as text and MCP image
  content: unlike Codex, an MCP result reaches the model directly. If a turn has ended, a new `session/prompt` carries it.
- Rebuild: new session on the same process, new bridge, new profile. A changed model or effort restarts the process.
- Images: `sees_images` True for `grok/`. A prompt image needs a size of at least 8×8; a smaller one is `ChannelFailure`,
  never silently dropped.

### Limits, accounting, login

- The weekly pool can't be read, so there's no percentage cap. A limit reaches the channel as a failed turn or error,
  which becomes `ChannelFailure` and the fallback, with the notification. Failure events for a real limit are unobserved:
  log them raw, without message content.
- `costUsdTicks` (10^10 ticks per dollar: verify against `usage` in tests) is logged as the API-equivalent cost and shown
  in `/costs` as "API equivalent", clearly not money spent. `usd=None`, as for Codex.
- `SHAFT_GROK_HOME`, `SHAFT_GROK_BIN` (default `~/.grok/bin/grok`), `SHAFT_GROK_MODELS` (default `grok/grok-4.7`),
  `SHAFT_GROK_FALLBACK_MODEL` (default as Codex), `SHAFT_GROK_TIMEOUT_S` (900), `SHAFT_GROK_SESSION_HOURS` (24).
- Retention: at channel start, at most every 10 minutes, delete files under `sessions/` older than
  `SHAFT_GROK_SESSION_HOURS`. Never fail a call.
- `shaft grok login` runs the CLI's login with `GROK_HOME` set; `shaft grok status` shows login, models, outage.

## Milestones

### G1: shared channel
Extract `supervisor/planchannel.py`; `supervisor/codexcli.py` becomes a backend. Migrate the broker and every seam to
`planchannel`. No behaviour change.
**Done when:** all existing Codex tests pass, moved where needed, with no assertion weakened.

### G2: Grok backend and wiring
`supervisor/grokcli.py`, `supervisor/planbridge.py`, tests with a fake agent, the seams, `shaft grok`, docs.
**Done when:** tests cover every mapping case, the empty-tools guard, permission refusal, an unknown tool kind, process
death and retention.

### G3: review
Another model reviews the diff.

### G4: real verification (main session)
A private cycle replay on `grok/grok-4.7`, a test conversation with an image, hostile prompts through the real
channel, a forced failure, the web app, and one replay on `codex/gpt-6.1-sol` to prove Codex still works.
**Done when:** each works in the real system and the full suite passes.

## Open for the operator

- Run `shaft grok login` once, after merging.
- Check Grok's data setting: its docs say a first-launch choice and `/privacy` control code retention. It couldn't be
  set from a script here.
