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
Sources: template branch `config.env:10`,
[port allowlist](../supervisor/core.py),
[sandbox](../supervisor/sandbox.py).

## Generation and service lifecycle

The server uses the pack's loader behind `mlx_lm`'s OpenAI-compatible interface.
It permits 16,384 output tokens. Temperature defaults to 0.6; top-p and top-k
come from the pack's generation config, with fallbacks of 0.95 and 20.
`SHAFT_THINK_BUDGET` defaults to 4,096 reasoning tokens, after which the server
forces the wrap-up phrase and closing think tag. Zero disables that limit.
Requests can lower the limit. The agent's `LLM_THINK_FOLLOWUP` defaults to 1,024
for tool follow-ups; `LLM_PRESENCE_PENALTY` is in tenths. The template's context
size setting is 262,144 tokens; it is a client configuration value.
Sources: [server setup](../supervisor/bonsai_server.py),
[thinking budget](../supervisor/bonsai_server.py),
[model knobs](../supervisor/localmodel.py), template `shaft/config.py:17`.

`SHAFT_PROMPT_CACHE_GB` defaults to 8; trimming occurs when the next request
starts. Cache bookkeeping arrays are evaluated each step to bound lazy graphs.
If the generation thread dies, the process exits with code 70 so launchd can
restart it. Sources: [generation failure](../supervisor/bonsai_server.py),
[cache graphs](../supervisor/bonsai_server.py),
[cache size](../supervisor/localmodel.py).

An instance pointing directly to this server keeps it running. Otherwise it
exits after 15 idle minutes, with no requests in flight. Zero disables idle exit.
Launchd restarts crashes, not normal idle exits; supervisor call paths start the
server on demand and wait up to 300 seconds by default. The panel distinguishes
an idle server from a failed start. Sources:
[pinning and idle checks](../supervisor/localmodel.py),
[service lifecycle](../supervisor/autonomy.py).

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
the Bonsai provider loads its adapter at startup only and ignores a per-request `adapter_path`, and on
2026-10-28 every adapter was DISCARDED. It needs per-request adapter loading in `bonsai_server.py` first. Steering is reserved for later. No model run was performed
while building this hook. Source: [perturbation.py](../supervisor/perturbation.py).

## Codex plan routing

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
Sources: [service command](../supervisor/autonomy.py),
[embedding port](../supervisor/core.py).
See [configuration](configuration.md) for all related knobs.
