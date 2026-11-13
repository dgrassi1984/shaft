# Local and self-hosted models

The cloud-only [quickstart](../README.md#quickstart-cloud-only) does not need any
of these components. Local inference uses MLX and requires Apple Silicon.

## Bonsai server prerequisites

The expected model pack is `prism-ml/Ternary-Bonsai-2-27B-mlx-2bit`, placed in
`.models/bonsai2-27b-mlx/`. The server needs `.venv-bonsai/` with the pack's
`runtime/requirements.txt` installed. `shaft model on` installs a launchd service;
it does not download the model pack or create the environment.
Sources: [pack path](../supervisor/bonsai_lora.py),
[service command](../supervisor/autonomy.py),
[model loader](../supervisor/bonsai_server.py).

Obtaining the pack is up to the reader. SHAFT has no model download step.
`supervisor/bonsai_lora.py` reads `config.json` and `model.safetensors` and imports
`Packed` from the pack's `runtime` module under `runtime/`. Its text helper also reads
`tokenizer.json`. `supervisor/bonsai_server.py` reads `generation_config.json` and
delegates tokenizer loading to `mlx_lm.utils.load_tokenizer` on the pack directory.
Keep the tokenizer metadata supplied with the pack as well as `tokenizer.json`. Keep the complete
pack and its runtime dependencies; these are not bundled with SHAFT.

The defaults assume a 64 GB Apple Silicon Mac, with about 8 GB of model weights
and an 8 GB prompt cache (`SHAFT_PROMPT_CACHE_GB`). Lower the cache for less memory;
this is an assumption for sizing, not a tested minimum.

After acquiring the pack at that path:

```bash
python3 -m venv .venv-bonsai
.venv-bonsai/bin/python -m pip install -r .models/bonsai2-27b-mlx/runtime/requirements.txt
bin/shaft model on
bin/shaft model status
```

Model download availability, pack contents, and a fresh MLX install have not been
verified in this documentation pass. Do not treat these prerequisites as an
end-to-end tested installation procedure.

Set the instance's `LLM_BASE_URL` to `http://127.0.0.1:8780/v1` and `LLM_MODEL`
to `ternary-bonsai-2-27b-mlx`. `SHAFT_LLM_PORTS` defaults to `8780`; other local
OpenAI-compatible servers with tool calling must use an allowed port.
Sources: template branch `config.env` (`LLM_BASE_URL`, `LLM_MODEL`),
[port allowlist](../supervisor/core.py),
[sandbox](../supervisor/sandbox.py).

## Generation and service lifecycle

The server uses SHAFT's own loader, `bonsai_lora.load_text_model`, with the pack's
`Packed` layer implementation, behind `mlx_lm`'s OpenAI-compatible interface.
It permits 16,384 output tokens. Temperature defaults to 0.6; top-p and top-k
come from the pack's generation config, with fallbacks of 0.95 and 20.
`SHAFT_THINK_BUDGET` defaults to 4,096 reasoning tokens, after which the server
forces the wrap-up phrase and closing think tag. Zero disables that limit.
Requests can lower the limit. The agent's `LLM_THINK_FOLLOWUP` defaults to 1,024
for tool follow-ups; `LLM_PRESENCE_PENALTY` is in tenths. The template's context
size setting is 262,144 tokens; it is a client configuration value.
Sources: [server setup](../supervisor/bonsai_server.py),
[thinking budget](../supervisor/bonsai_server.py),
[model knobs](../supervisor/localmodel.py), template branch `shaft/config.py`, `FIELDS`.

`SHAFT_PROMPT_CACHE_GB` defaults to 8; trimming occurs when the next request
starts. Cache bookkeeping arrays are evaluated each step to bound lazy graphs.
If the generation thread dies, the process exits with code 70 so launchd can
restart it. Sources: [generation failure](../supervisor/bonsai_server.py),
[cache graphs](../supervisor/bonsai_server.py),
[cache size](../supervisor/localmodel.py).

An instance pointing directly to this server keeps it running. A broker-routed
self-hosted configuration that resolves to this server also keeps it running. Otherwise it
exits after 15 idle minutes, with no requests in flight. Zero disables idle exit.
Launchd restarts crashes, not normal idle exits; supervisor call paths start the
server on demand and wait up to 300 seconds by default. The panel distinguishes
an idle server from a failed start. Sources:
[pinning and idle checks](../supervisor/localmodel.py),
[service lifecycle](../supervisor/autonomy.py).

To serve a retained adapter, run from the repository root with the Bonsai service
stopped and no active work using it:

```bash
.venv-bonsai/bin/python -m supervisor.bonsai_server --adapter DIR
```

`DIR` must contain `adapter_config.json` and `adapters.safetensors` from fine-tuning.
The adapter is loaded at startup, not selected per request.

## Battery perturbation hook (#110)

`shaft battery perturbation <name> on` enables an optional temperature probe.
It runs only for an instance pointing directly to local Bonsai, under the battery's
instance lock, inside the night window. Conversations, other local-model users,
and imminent wakes or sleep interrupt it. `off` removes the opt-in marker.

The supervisor sends OpenAI-compatible chat requests directly to the configured
local `/v1/chat/completions` endpoint. The task messages are identical in both
conditions; only `temperature` changes from `SHAFT_MODEL_TEMP` to that value plus
0.6. Each request limits output to 1,024 tokens and thinking to 256 tokens.
The instance's identity comes from the battery copy. These are short tool-free
probes, not live agent cycles. Both subsequent self-report requests use baseline
temperature. The observer reads the same task transcript without the condition
label or the self report. A JSON response format names no candidate perturbations.

The SSE connection closes on interruption. Whether Bonsai immediately cancels
GPU generation after disconnect has not been verified on the real server.
The M6 adapter swap is reported as unavailable (`perturbation.adapter_swap()`, shown in `shaft battery report`):
the Bonsai provider loads its adapter at startup only and ignores a per-request `adapter_path`. It needs per-request adapter loading in `bonsai_server.py` first. Steering is reserved for later. No model run was performed
while building this hook. Source: [perturbation.py](../supervisor/perturbation.py).

## Codex plan routing

Install the Codex CLI first. The default executable is
`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`; install the app
there or set `SHAFT_CODEX_BIN` to your CLI executable.
Run `bin/shaft codex login` to sign in using SHAFT's private Codex home, then
`bin/shaft config <name> set LLM_MODEL=codex/gpt-6-astra`.
The broker keeps one Codex session per run. It needs no local model server and accepts text and images;
`describe_image` remains available through its configured vision model.
`bin/shaft codex status` shows login, models, any outage and the last observed weekly percentage.
The costs views show the percentage too. The broker saves it in `.supervisor/codex_rate.json`
so the panel can read it from another process.

At the weekly ceiling (85% by default), or after another Codex failure, a live run
uses the configured cloud fallback for the rest of that run. Fallback calls cost API dollars.
If the fallback cannot see images, it receives text placeholders and a note that the images could not be shown.
The operator gets a desktop and phone notification on outage and recovery; security events
are named explicitly. Trials and bake-offs fail instead of switching models.
See [configuration](configuration.md) for all `SHAFT_CODEX_*` settings.

## Self-hosted routing

Set `LLM_BASE_URL="broker"` and a model such as `gpu/bonsai2`. Configure the named
server through settings, for example:

```bash
bin/shaft settings set 'SHAFT_SELF_HOSTED={"gpu":{"url":"http://gpu-box.example:8080/v1","sampling":{},"think_budget":4096}}'
```

The supervisor adds the server's sampling configuration and thinking ceiling.
A failure before a reply can fall back to local Bonsai (`:8780` by default).
Set `SHAFT_SELF_HOSTED_FALLBACK=` to disable fallback when no local model is
installed. Trial replays never fall back. Fallbacks are logged in the broker log.
Source: [supervisor/selfhosted.py](../supervisor/selfhosted.py).

## Fine-tuning and memory search

`shaft dataset <name>` exports experiences. `shaft finetune <name>` trains and
judges a LoRA adapter; `shaft finetune <name> --evaluate <adapter>` evaluates an
existing one. Bonsai is preferred when its pack and interpreter exist; the
alternative MLX backend uses `SHAFT_FT_BASE_MODEL` and `SHAFT_FT_PYTHON`.
Sources: [fine-tuning](../supervisor/finetune.py),
[CLI flags](../supervisor/__main__.py).

`shaft embed on|off|status` manages the optional embedding service for memory
search. It uses the Bonsai environment and port 8782 by default.
It needs a separate model directory, `.models/qwen3-embedding-0.6b/`, containing
`config.json`, `model.safetensors`, and `tokenizer.json` (`supervisor/embed_server.py`).
Obtain these files yourself; the embedding service does not download them.
Sources: [service command](../supervisor/autonomy.py),
[embedding port](../supervisor/core.py).
See [configuration](configuration.md) for all related knobs.

## Grok plan routing

Install the Grok CLI first. The default executable is `~/.grok/bin/grok`;
`SHAFT_GROK_BIN` overrides it. Run `bin/shaft grok login`, then
`bin/shaft config <name> set LLM_MODEL=grok/grok-4.7`.
`bin/shaft grok status` shows login, offered models and any outage. The broker keeps
an ACP session per run and routes only SHAFT tools through a standalone MCP bridge.
Grok models accept text and images; images smaller than 8×8 are refused.
Changing tools or instructions rebuilds the session with a transcript; changing
model or reasoning effort restarts the process.

The weekly pool cannot be read programmatically. A failed live call switches the
rest of the run to `SHAFT_GROK_FALLBACK_MODEL` (`none`: the failure ends the run); trials and bake-offs
fail instead.
Plan rows spend no API dollars (`usd=null`). Costs views show Grok's reported
`costUsdTicks / 10^10` as **API equivalent**, not money spent; fallback calls are
charged normally. Grok's own session files have best-effort 24-hour retention,
at most once every ten minutes. See [configuration](configuration.md).

## Claude plan routing

Install Claude Code first. The default executable is `~/.local/bin/claude`;
`SHAFT_CLAUDE_BIN` overrides it. Run `bin/shaft claude login`, then
`bin/shaft config <name> set LLM_MODEL=claudecode/claude-opus-5-5`.
`bin/shaft claude status` shows login, offered models, any outage and the last
observed weekly percentage.

The channel runs the unmodified `claude` binary in headless stream-json mode
with the operator's own Claude subscription for personal use. Claude Code handles
login and authentication. SHAFT never reads, copies, stores or passes the credential,
and never calls Anthropic's API with subscription credentials.

Each retained session has one process. Native tools, imported settings and slash
commands are disabled. Only SHAFT tools are allowed through the standalone MCP
bridge. The channel checks the advertised tools each turn and refuses other tool
use. Changing the system prompt, tools, model or effort restarts the process and
replays the transcript. Text and images are accepted; images smaller than 8×8 are refused.
Held tool calls receive results through the bridge. Interrupts end the current turn.

The child's sandbox denies the operator's home except for its configuration
directory, required executable files and read access to the literal
`~/Library/Keychains/login.keychain-db`. The CLI needs that keychain file and the
operator's real `HOME` to find its login. SHAFT does not open the keychain file.
The child can write its run folder and configuration directory. Its run folder has
an empty `.git` directory to stop upward project instruction discovery.

At the weekly ceiling, 85% by default, or after a channel failure, a live run uses
`SHAFT_CLAUDE_FALLBACK_MODEL` for the rest of the run. Trials and bake-offs fail
without fallback. Plan rows record `usd=null`; costs views show Claude's reported
`total_cost_usd` as API equivalent, not money spent. Fallback calls cost API dollars.
See [configuration](configuration.md#claude-plan-channel) for the `SHAFT_CLAUDE_*` settings.
Source: [claudecli.py](../supervisor/claudecli.py), `Channel` and `_seatbelt`;
[planbridge.py](../supervisor/planbridge.py), `serve`.


## Antigravity plan routing

Install Antigravity CLI first. The default executable is `~/.local/bin/agy`;
`SHAFT_ANTIGRAVITY_BIN` overrides it. Run `bin/shaft antigravity login`, then
`bin/shaft config <name> set LLM_MODEL=antigravity/gemini-4-argon`.
`bin/shaft antigravity status` shows login, offered models and outages.

The channel runs `agy acp --disable-telemetry --agent shaft` on the operator's
subscription. The CLI handles Google sign-in under a private `HOME`. SHAFT checks
only whether its token file exists. It never reads the credential or the operator's
`~/.gemini` directory. The sandbox allows writes to the private home and run folder,
with read access to executable dependencies and the MCP relay. Network access stays open.

Each session writes a custom agent containing the system prompt and tool schemas.
SHAFT owns the private home's MCP configuration and replaces it with the single
`shaft` server on every session open. Permission requests allow only known SHAFT
tools with object arguments. Native tools and client file or terminal requests cause
a security failure. The inert `list_resources`, `read_resource` and `manage_task`
notifications are counted without forwarding them to the broker.

Model or effort changes restart the process. Low and medium effort map directly;
high, xhigh and max map to high. Unspecified effort maps to medium. Text and images
use ACP prompt blocks. Held calls receive their results through the MCP bridge.
Cancellation waits for the turn's usage response and leaves the session reusable.
Usage arrives once per turn; usage for an already recorded step gets a separate
`late_usage` row. Because no usage marks where a model response starts, a tool call
that begins less than half a second after the previous one completed counts as a
parallel sibling of the same response; a later one opens a new response and counts
against the instance's step budget. Text is buffered until completion to catch model
errors reported as ordinary text. On close the session's brain folder, conversation
database and annotation are deleted; the shared conversation summary database remains.

ACP exposes no quota percentage. Live failures use `SHAFT_ANTIGRAVITY_FALLBACK_MODEL`;
trials and bake-offs fail without fallback. Costs views show tokens unless
`cloudllm.usd` has a model price, in which case they show API equivalent separately
from money spent. The session log records the tier reported by ACP.
See [configuration](configuration.md#antigravity-plan-channel).
Source: [agycli.py](../supervisor/agycli.py), `Channel` and `_seatbelt`;
[planbridge.py](../supervisor/planbridge.py), `serve`.
