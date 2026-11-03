# Configuration reference

## Supervisor settings (`shaft settings`)

Supervisor settings cover the judge pool, cloud models and caps, trial limits, timeouts,
and the local model server. These knobs (`core.knob`) are listed below. Each process reads them when it starts: from its **environment**,
else **`.supervisor/settings.env`**, else the knob's **default** (env > file > default). The launchd services run with
`PATH` alone in their environment, so the file is what sets a knob for them; an environment variable still wins for
a command you run from a shell that sets it.

- `shaft settings` lists every knob, grouped: its value, where it comes from (`env`, `file`, `default`), the default,
  what it does and which services read it.
- `shaft settings set SHAFT_JUDGE_MODELS=claude-sonnet-5-5,openrouter/xiaomi/mimo-v2.6-pro [--expect KEY=OLD]` and
  `shaft settings unset KEY [--expect-unset KEY]` write the file, several keys in one write, under a lock. Each value
  is checked by its knob's own parser first (for example, a negative timeout or JSON that is not an object): a value it refuses is never written. `--expect` refuses if the file's value changed since you looked.
  Each change is logged in `supervisor.log`.
- **Applying a change.** Runs (cycles, sleeps, trials, batteries, CLI commands) are fresh processes: they read the
  file when they start. A long-running service (api, scheduler, telegram, web, model) holds what it read at its start;
  each records the knobs it read (`.supervisor/services/<service>.json`), so `shaft settings` and the web UI's
  Settings page say "not live yet: restart api (`shaft api on`), …" until the services holding another value
  restart. A service holds the knobs of every module it imported, used or not, so the list errs towards a restart.
  A service started before it recorded its knobs is taken to hold the defaults of the knobs it is declared to read.
- Every `knob()` call registers its description, Settings group and readers; `test_settings` fails on a knob that is
  not registered, and on a `SHAFT_` variable read straight from the environment.
- Ports, interpreter paths, `SHAFT_WEB_HOSTS` and `SHAFT_TELEGRAM_API` are set from a terminal only: a wrong value there
  cuts the web UI off or breaks a service. Secrets stay in `.env` (`shaft secret` for an instance's own keys).

Sources: [settings registry and writes](../supervisor/settings.py),
[knob precedence](../supervisor/core.py).

## Supervisor secrets and identity

Root `.env` is read by the supervisor, not passed to instances. Its parser accepts
`KEY=value` and strips outer quotes; it does not evaluate shell syntax or expand
variables. Keep comments on separate lines. Start with [.env.example](../.env.example).
Source: [supervisor/core.py](../supervisor/core.py).

| Key | Purpose | Source |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude instance models, judges, coding, recaps, image descriptions | [cloudllm.py](../supervisor/cloudllm.py) |
| `OPENROUTER_API_KEY` | OpenRouter chat, decision models, judges | [cloudllm.py](../supervisor/cloudllm.py), [judge.py](../supervisor/judge.py) |
| `SCRAPEBADGER_API_KEY` | Web search | [broker.py](../supervisor/broker.py) |
| `SHAFT_API_KEY` | API bearer key, created by `shaft api on` | [api.py](../supervisor/api.py) |
| `SHAFT_INBOX_KEY` | Separate delivery-only inbox bearer key | [api.py](../supervisor/api.py) |
| `SHAFT_API_LOGINS` | Allowed tailnet logins; identity policy, not a knob | [api.py](../supervisor/api.py) |
| `TELEGRAM_TOKEN_<NAME>` | One bot token per instance; hyphens become underscores | [telegram.py](../supervisor/telegram.py) |
| `GMAIL_<CLIENT>_CLIENT_ID` / `GMAIL_<CLIENT>_CLIENT_SECRET` | Named Google Desktop OAuth clients | [google.py](../supervisor/google.py) |

`SHAFT_API_LOGINS` can also come from the process environment. Integration refresh
tokens and instance-owned keys are stored by their setup commands, not in instance
`config.env`. See [integrations](integrations.md).

## Every supervisor knob

`SHAFT_ACTION_INSTANCES` enables the conservative [operator action classes](safety.md#operator-action-classes-142)
for a comma-separated list of instance names. Its default is `thelos`; an empty value selects nobody.
It does not add grants. Other instances retain their current behavior. The selection is read at process start;
class demotion and restoration are read from supervisor-owned state on each dispatch.
Calendar proposals retain `SHAFT_CALENDAR_WRITES_PER_DAY` and `SHAFT_CALENDAR_PROPOSAL_HOURS`.

`SHAFT_GATE_TIMEOUT_S` defaults to 300 seconds. The supervisor's tests run in `SHAFT_GATE_WORKERS` processes, and
`shaft gate budget` keeps each part within half of that limit (docs/plans/PLAN-gate-5min.md).

This table is extracted statically from production `knob()` calls in `supervisor/*.py`.
Defaults below are source defaults, not values read from this checkout's private state.
`PROJECT` means the clone root. Paths are resolved under that root at runtime.

| Name | Default | Meaning | Declaration |
|---|---|---|---|
| `SHAFT_ACTION_INSTANCES` | `thelos` | Instances using conservative operator action classes; comma-separated, empty disables | [actions.py](../supervisor/actions.py) |
| `SHAFT_API_PORT` | `8781` | the API's local port (OpenAI-compatible; the web UI and Telegram chat through it) | [api.py](../supervisor/api.py) |
| `SHAFT_INBOX_PER_HOUR` | `30` | messages per hour the API's inbox (doorbell) accepts for one instance | [api.py](../supervisor/api.py) |
| `SHAFT_INBOX_WAKES_PER_HOUR` | `4` | wakes per hour inbox messages may start for one instance | [api.py](../supervisor/api.py) |
| `SHAFT_API_HTTPS_PORT` | `8443` | the API's https port on the tailnet (tailscale serve) | [api.py](../supervisor/api.py) |
| `SHAFT_PAUSE_ALERT_HOURS` | `3` | hours an operator pause may last before an alert; 0 = off. Read by CLI runs and web. | [core.py](../supervisor/core.py) |
| `SHAFT_BATTERY_VERSION` | `v1` | the battery item set in use (.supervisor/battery/items/<version>) | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_EVERY_DAYS` | `5` | nights between an instance's scheduled capability batteries (0: never scheduled) | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_ITEM_TIMEOUT_S` | `1800` | seconds one battery item may run | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_CONTROL_REF` | `template` | the git branch a battery control instance is made from | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_RECALL_MIN_AGE_H` | `48` | hours old a journal entry must be to make a battery recall item | [battery.py](../supervisor/battery.py) |
| `SHAFT_OPERATOR_PER_DAY` | `0` | messages per day one instance may send the operator (0: no limit) | [broker.py](../supervisor/broker.py) |
| `SHAFT_TAILNET_GRANTS` | `{}` | tailnet hosts an instance may call through http_request with one of its keys (JSON: {"<instance>": ["<host>:<port>"]}); its sandbox still reaches none | [broker.py](../supervisor/broker.py) |
| `SHAFT_PUSH_HOLD` | `1` | hold non-urgent device notifications (0: off) | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_QUIET_HOURS` | `22-07` | local quiet hours, may wrap midnight; empty disables | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_DIGEST_AT` | `07:15` | local digest time, or quiet hours' end if later | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_PUSH_BUDGET` | `6` | operator pushes per instance per day before holding for a digest (0: unlimited, count hidden) | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_DESCRIBE_MODEL` | `claude-haiku-5-5` | the model describe_image uses for an instance whose own model cannot see images | [broker.py](../supervisor/broker.py) |
| `SHAFT_CLOUD_MODELS` | `claude-haiku-5-5,claude-opus-5-5,openrouter/xiaomi/mimo-v2.6-pro` | the cloud models an instance may run on (through the broker) | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_OPENROUTER_PROVIDER` | `{"only": ["novita", "deepinfra"], "order": ["novita", "deepinfra"], "allow_fallbacks": false, "data_collection": "deny", "zdr": true, "require_parameters": true}` | OpenRouter's provider routing for every openrouter/ call (JSON): which providers, and the data policy | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_CLOUD_DAILY_USD` | `8` | dollars per day an instance may spend on its cloud model (shaft limits can set one instance's own cap) | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_DECISION_MODELS` | `typesafe/jev-1.13,~typesafe/jev-latest` | the models the broker's decide tool may call (OpenRouter's decisions API); the first is its default | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_DECISION_PROVIDER` | `{"data_collection": "deny", "zdr": true, "allow_fallbacks": false}` | the decide tool's provider routing and data policy (JSON) | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_CONSULT_MODELS` | `openrouter/anthropic/claude-opus-5.5,openrouter/anthropic/claude-sonnet-5.5,openrouter/moonshotai/kimi-k3.1-pro,openrouter/google/gemini-4-argon,openrouter/openai/gpt-6.1-sol,openrouter/openai/gpt-6.1-terra,openrouter/meta/muse-spark-1.3,openrouter/z-ai/glm-5.3,openrouter/minimax/minimax-m3.1-flash` | models on the instance's consult menu | [consult.py](../supervisor/consult.py) |
| `SHAFT_CONSULT_DAILY_USD` | `3` | dollars per day per instance for model consultations; `shaft limits <name> consult-cap <usd>` overrides this | [consult.py](../supervisor/consult.py) |
| `SHAFT_CONSULT_PROVIDER` | `{"data_collection": "deny", "zdr": true, "require_parameters": true}` | OpenRouter consult provider routing and data policy; instances cannot override it | [consult.py](../supervisor/consult.py) |
| `SHAFT_CONSULT_MAX_TOKENS` | `8000` | default and maximum output tokens per consultation | [consult.py](../supervisor/consult.py) |
| `SHAFT_CODER_MODEL` | `claude-opus-5-5` | the model an instance hands a coding task to (delegate) | [coder.py](../supervisor/coder.py) |
| `SHAFT_CODER_DAILY_USD` | `5` | dollars per day per instance its coding hand-offs may spend | [coder.py](../supervisor/coder.py) |
| `SHAFT_CYCLE_TIMEOUT_S` | `2400` | seconds a cycle (and a trial's replayed cycle) may run before it is stopped | [core.py](../supervisor/core.py) |
| `SHAFT_SLEEP_TIMEOUT_S` | `5400` | seconds the nightly sleep (consolidation) may run; nobody is waiting for it | [core.py](../supervisor/core.py) |
| `SHAFT_LLM_PORTS` | `8780` | the local model servers' ports a run's sandbox may reach (the first is SHAFT's own server) | [core.py](../supervisor/core.py) |
| `SHAFT_CORE_MIN_HOURS` | `72` | hours an instance's core personality must stay unchanged before it may change it again | [core.py](../supervisor/core.py) |
| `SHAFT_EMBED_PORT` | `8782` | the embedding service's port (memory search), which every run may reach | [core.py](../supervisor/core.py) |
| `SHAFT_BROKER_MAX_CALLS` | `60` | supervisor tool calls (broker) one run may make | [core.py](../supervisor/core.py) |
| `SHAFT_IDLE_MINUTES` | `30` | minutes a conversation (terminal, web, Telegram, API) may stay quiet before it is closed and written up into the journal | [core.py](../supervisor/core.py) |
| `SHAFT_FILE_READ_MAX_MB` | `512` | megabytes the supervisor reads from one file in an instance's folder (a bigger one is refused) | [core.py](../supervisor/core.py) |
| `SHAFT_SNAPSHOT_MAX_MB` | `512` | megabytes of an instance's code the gate reads to check a change (more, and the change is not kept) | [core.py](../supervisor/core.py) |
| `SHAFT_COPY_MAX_GB` | `8` | gigabytes the supervisor copies of an instance's folder for a battery exam or a gate's scratch copy (more, and the exam or the check fails) | [core.py](../supervisor/core.py) |
| `SHAFT_TREE_MAX_FILES` | `200000` | files and folders the supervisor reads or copies in one walk of an instance's folder (the gate's code snapshot, a battery exam's copy) | [core.py](../supervisor/core.py) |
| `SHAFT_TREE_MAX_DEPTH` | `64` | folders deep the supervisor walks in an instance's folder (the gate's code snapshot, a battery exam's copy) | [core.py](../supervisor/core.py) |
| `SHAFT_FT_PYTHON` | `str(PROJECT / ".venv-ft" / "bin" / "python")` | the interpreter fine-tuning runs with | [finetune.py](../supervisor/finetune.py) |
| `SHAFT_FT_BASE_MODEL` | `mlx-community/Qwen3.8-27B-4bit` | the base model fine-tuning starts from when it does not train Bonsai itself | [finetune.py](../supervisor/finetune.py) |
| `SHAFT_BONSAI_PYTHON` | `str(PROJECT / ".venv-bonsai" / "bin" / "python")` | the interpreter Bonsai LoRA training runs with | [finetune.py](../supervisor/finetune.py) |
| `SHAFT_GATE_TIMEOUT_S` | `300` | seconds each gate test stage may run before it times out; positive integer | [gate.py](../supervisor/gate.py) |
| `SHAFT_GATE_WORKERS` | `4` | processes each part of the supervisor's tests runs in at a gate; positive integer | [gate.py](../supervisor/gate.py) |
| `SHAFT_IDLE_WAKE_MINUTES` | `30` | minutes an instance may go without a cycle before a timed wake starts one with nothing new for it (no unread message, commitment coming due or job result) | [autonomy.py](../supervisor/autonomy.py) |
| `SHAFT_JOB_MIN_MINUTES` | `5` | the shortest interval an instance's own job (a plugin on a timer) may run at | [jobs.py](../supervisor/jobs.py) |
| `SHAFT_JOBS_MAX` | `12` | scheduled plugins (jobs) an instance may run; beyond it, the rest by name are not run; positive integer | [jobs.py](../supervisor/jobs.py) |
| `SHAFT_WATCH` | `on` | the supervisor's mail and calendar watch, for instances that opt in with WATCH_MAIL or WATCH_CALENDAR in their config.env (on or off) | [watch.py](../supervisor/watch.py) |
| `SHAFT_WATCH_WAKE_MIN` | `15` | minutes between two wakes the mail and calendar watch gives one instance; a change sooner waits in its inbox | [watch.py](../supervisor/watch.py) |
| `SHAFT_WATCH_EVERY_S` | `60` | seconds between two passes of the mail and calendar watch (the scheduler looks every 30 seconds) | [watch.py](../supervisor/watch.py) |
| `SHAFT_JOB_LONG_TIMEOUT_S` | `900` | seconds one run of a long scheduled job (`"long": true` in its schedule) may take before it is stopped (and resumed if it wrote its checkpoint); positive integer | [jobs.py](../supervisor/jobs.py) |
| `SHAFT_JOB_LONG_CALLS` | `200` | supervisor tool calls one run of a long scheduled job may make; positive integer | [jobs.py](../supervisor/jobs.py) |
| `SHAFT_JOB_LONG_RESUMES` | `4` | times a long scheduled job that timed out after writing its checkpoint is resumed before it gives up (0: never resumed) | [jobs.py](../supervisor/jobs.py) |
| `SHAFT_JOB_RUN_CONFINED` | `0` | 1: the broker's `run` inside a scheduled job writes only where the job may (`.jobs/<plugin>/`, `.tmp/`, `.browser/`); 0: where a cycle's `run` may (notes/, code). Turn it on once every instance's scheduled plugins publish through `publishes` | [jobs.py](../supervisor/jobs.py) |
| `SHAFT_JUDGE_MODELS` | `openrouter/xiaomi/mimo-v2.6-pro,openrouter/z-ai/glm-5.3-flash` | the judge pool: picks at random, excluding the instance's model and the second judge | [judge.py](../supervisor/judge.py) |
| `SHAFT_JUDGE_EVERY` | `2` | the main judge grades one live run in this many (1: every run) | [judge.py](../supervisor/judge.py) |
| `SHAFT_JUDGE2_MODEL` | `openrouter/deepseek/deepseek-v4.1-flash` | the second judge, which grades a sample of cycles again to measure the judges' agreement | [judge.py](../supervisor/judge.py) |
| `SHAFT_JUDGE2_EVERY` | `4` | the second judge grades one cycle in this many (0: never) | [judge.py](../supervisor/judge.py) |
| `SHAFT_MODEL_IDLE_MINUTES` | `15` | minutes SHAFT's own model server stays up unused before it exits (it starts again on demand); 0 = never exits | [localmodel.py](../supervisor/localmodel.py) |
| `SHAFT_MODEL_START_TIMEOUT_S` | `300` | seconds to wait for the model server to start on demand | [localmodel.py](../supervisor/localmodel.py) |
| `SHAFT_THINK_BUDGET` | `4096` | reasoning tokens SHAFT's own model may think per reply before it is made to answer (0: no limit) | [localmodel.py](../supervisor/localmodel.py) |
| `SHAFT_MODEL_TEMP` | `0.6` | SHAFT's own model's sampling temperature | [localmodel.py](../supervisor/localmodel.py) |
| `SHAFT_PROMPT_CACHE_GB` | `8` | gigabytes of prompt cache SHAFT's own model server keeps | [localmodel.py](../supervisor/localmodel.py) |
| `SHAFT_PROBATION_CYCLES` | `6` | judged cycles a kept self-change stays on probation (reverted on a clear drop) | [metrics.py](../supervisor/metrics.py) |
| `SHAFT_CORRECTION_ALERT_CYCLES` | `6` | alert when an operator correction remains open for more than this many live cycles; 0 disables | [panel.py](../supervisor/panel.py) |
| `SHAFT_JUDGE_ALERT_USD` | `3` | judge spend per day that raises an alert (the judge has no cap) | [panel.py](../supervisor/panel.py) |
| `SHAFT_RECAP_MODEL` | `claude-haiku-5-5` | the model that writes the overview's one-line recap of each instance | [recap.py](../supervisor/recap.py) |
| `SHAFT_SELF_HOSTED` | `{}` | self-hosted model servers instances may run on through the broker (JSON: name -> url, sampling, think_budget) | [selfhosted.py](../supervisor/selfhosted.py) |
| `SHAFT_SELF_HOSTED_FALLBACK` | `f"http://127.0.0.1:{LLM_PORTS[0]}/v1"` | the server a self-hosted model's calls fall back to when it is unreachable (empty: none) | [selfhosted.py](../supervisor/selfhosted.py) |
| `SHAFT_SELF_HOSTED_TIMEOUT_S` | `1800` | seconds a self-hosted server may take to answer one call | [selfhosted.py](../supervisor/selfhosted.py) |
| `SHAFT_TELEGRAM_API` | `https://api.telegram.org` | the Telegram Bot API endpoint | [telegram.py](../supervisor/telegram.py) |
| `SHAFT_TRIALS_PER_DAY` | `2` | trials an instance may ask for per day (the operator's are not counted) | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_DAILY_USD` | `3` | dollars per day an instance's own trials may spend on cloud models | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_LOCAL_UNITS_PER_DAY` | `24` | replayed cycles per day an instance's own trials may run on the local model; past it they run on the fallback model | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_N_MAX` | `10` | the most past cycles one trial replays | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_LOCAL_WAIT_H` | `6` | hours a trial waits for the local model's idle time before it runs on the fallback model | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_FALLBACK_MODEL` | `openrouter/xiaomi/mimo-v2.6-pro` | the cloud model a local-model trial runs on when the local model stays busy | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_PARALLEL` | `3` | replayed cycles a cloud-model trial runs at once | [trial.py](../supervisor/trial.py) |
| `SHAFT_MAX_IMAGES` | `4` | images one model call may carry | [vision.py](../supervisor/vision.py) |
| `SHAFT_WEB_PORT` | `8785` | the web UI's local port (tailscale serve forwards to it) | [web.py](../supervisor/web.py) |
| `SHAFT_WEB_HTTPS_PORT` | `8444` | the web UI's https port on the tailnet (tailscale serve) | [web.py](../supervisor/web.py) |
| `SHAFT_WEB_HOSTS` | empty | the host names the web UI answers to, comma-separated (empty: the tailnet name `shaft web on` recorded) | [web.py](../supervisor/web.py) |

Special runtime variables are not settings: `SHAFT_PYTHON` selects the launcher
interpreter ([bin/shaft](../bin/shaft)); `SHAFT_SUPERVISOR_DIR` redirects state
for tests ([core.py](../supervisor/core.py)). Run-injected values such as broker
tokens, proxy variables, `SHAFT_VISION`, and browser paths are supervisor-owned.
Do not copy them into `.env` as operator settings.

## Instance config.env

Edit `instances/<name>/config.env`, or use `shaft config <name> set KEY=VALUE`.
The latter validates and commits the edit; `--expect KEY=OLD` prevents overwriting
a concurrent edit. Instances use `set_config`; direct writes are sandbox-denied.
Sources: [opconfig.py](../supervisor/opconfig.py),
[sandbox.py](../supervisor/sandbox.py).

The template loader uses environment > nonempty config value > built-in default. Under SHAFT the
environment holds none of these: the sandbox passes on only `PATH`, `HOME`, `USER`, `LANG`, `TERM`,
`COLUMNS`, `LINES`, `TZ` and `LC_*` (`sandbox.ENV_ALLOW`), so an `LLM_*` variable exported in your shell or
launchd never reaches an instance; `config.env` is what sets them (a trial names its own model).
An empty `OPERATOR_NAME` resolves to `the operator`; setting your name is recommended,
not required. `LLM_BASE_URL` and `LLM_MODEL` are required. Leave `LLM_API_KEY` empty:
cloud credentials belong to the broker. Config is versioned with instance code.

The following defaults are from the template branch's `shaft/config.py:17`;
the shipped `config.env:10` supplies the local URL and model. Inspect them without
creating an instance with `git show template:shaft/config.py` and
`git show template:config.env` after setup. During publication preparation the
reviewed ref was `refs/heads/publish-prep-template`.

| Key | Built-in default | Shipped config override |
|---|---|---|
| `NAME` | `''` | uses built-in default |
| `LLM_BASE_URL` | `required` | `http://127.0.0.1:8780/v1` |
| `LLM_MODEL` | `required` | `ternary-bonsai-2-27b-mlx` |
| `LLM_API_KEY` | `''` | empty |
| `LLM_TIMEOUT_S` | `900` | `900` |
| `MAX_TOKENS_REFLECT` | `16384` | `16384` |
| `MAX_TOKENS_TALK` | `16384` | `16384` |
| `WAKE_INTERVAL_MINUTES` | `120` | `120` |
| `LLM_CONTEXT_TOKENS` | `262144` | `262144` |
| `LLM_REASONING_EFFORT` | `'medium'` | `medium` |
| `LLM_THINK_FOLLOWUP` | `1024` | uses built-in default |
| `LLM_PRESENCE_PENALTY` | `15` | `15` |
| `MAX_TOKENS_STEP` | `16384` | `16384` |
| `MAX_STEPS_LIVE` | `12` | `12` |
| `MAX_STEPS_SLEEP` | `30` | `30` |
| `SLEEP_VERDICT_CHARS` | `120000` | `120000` |
| `TALK_REPLY_CALLS` | `3` | uses built-in default |
| `TALK_REPLY_S` | `45` | uses built-in default |
| `MAX_STEPS_TALK` | `0` | `0` |
| `LOOP_GUARD` | `'on'` | `on` |
| `LOOP_EFFECT_ROUNDS` | `3` | `3` |
| `CONTEXT_JOURNAL_CHARS` | `16000` | `16000` |
| `STALE_LINE_HOURS` | `24` | uses built-in default |
| `STALE_GOAL_HOURS` | `48` | uses built-in default |
| `CRITERION_CHARS` | `600` | uses built-in default |
| `CONFIDENCE_MODE` | `'confirm'` | uses built-in default |
| `GOALS_CHARS` | `12000` | uses built-in default |
| `BRIEFING` | `'notes/briefing.md'` | `notes/briefing.md` |
| `BRIEFING_CHARS` | `6000` | uses built-in default |
| `HANDOFF` | `'on'` | `on` |
| `CURIOSITY_EVERY_CYCLES` | `5` | `5` |
| `OUTCOME_CARDS` | `'on'` | uses built-in default |
| `WORKSPACE` | `'competitive'` | uses built-in default |
| `WORKSPACE_K` | `3` | uses built-in default |
| `ATTENTION_SCHEMA` | `'on'` | uses built-in default |
| `COMMITMENTS` | `'on'` | uses built-in default |
| `FORECAST` | `'on'` | uses built-in default |
| `OPERATOR_NAME` | `'the operator'` | empty |

`STALE_LINE_HOURS` flags dates older than this many hours in self/goals.md and self/self.md
at sleep. `STALE_GOAL_HOURS` flags Active goal bullets with no live touch in that window,
only if the operator sent a conversation turn during it. Set either to 0 to disable that check.
Sleep asks for a fix, retirement or reason to keep each item, recorded in its journal.

`TALK_REPLY_CALLS` and `TALK_REPLY_S` bound work before a conversation reply. When either budget
is used, the next model call is offered only `queue_for_next_cycle` and must answer with its next steps. A running
call finishes normally. Set either knob to 0 to disable that budget.
`queue_for_next_cycle(task, why)` puts unfinished work in the instance's own inbox for its next live cycle.
`CRITERION_CHARS` limits `declare_intent.success_criterion` in characters. An overlong criterion is refused
with its length, the limit and how many characters to cut, asking for the deliverable's check rather than ritual
clauses, and ends with the draft so the instance can trim it in one retry (review I8; never trimmed silently).

`SLEEP_VERDICT_CHARS` bounds sleep feedback reads, in the sleep feedback group in `template/config.env`.
Sleep reads judgment pages (up to 24,000 characters each) until caught up or until the next page would take the
verdict text past this positive character budget; the block is re-sent on every sleep step, so the default
(~30k tokens) leaves the rest of a 262,144-token window free. At the budget it tells the model and stderr that
feedback was cut short; the next finished sleep resumes from the last page it read.

`LOOP_GUARD` controls the effect-loop guard (#103): `shadow` records would-fire events in the episode's
`loop_shadow` array without changing model input; `on` also appends advice after the call executes;
`off` records nothing. The existing exact-call repeat guard stays active in all three modes.
`LOOP_EFFECT_ROUNDS` is a positive integer, default 3. A first failed result counts as round one;
new successful observations reset the count. A successful observation seen earlier in this run counts as
no new evidence. Counts require consecutive calls in the same tool/target family.
See [effect-loop measurement](architecture.md#effect-loop-measurement) for target rules and replay reporting.
`CONFIDENCE_MODE` accepts `confirm` (default), `clauses`, or `noconfirm`; the live prompt names the resolved mode.
The gate's scripted cycles declare in a form every mode accepts, and the supervisor's own suite runs them once
per mode, so an instance that changes the mode is judged on its code, not on tests pinned to `confirm`.
`confirm` keeps the existing declare and base-rate confirmation flow. `clauses` asks for 1-4 success-criterion
clauses, each with an integer confidence 0-100. The template records them with a null combined confidence;
the supervisor multiplies their probabilities when it writes the judgment, assuming independence because
all clauses must hold. This v4 confidence stays apart from v1-v3 in calibration, AUROC and base rates.
Both `clauses` and `noconfirm` omit the base rate and `confirm_confidence`; `noconfirm` records a single v2 confidence.
These arms are available for a later trial. No instance configuration changes automatically. If the Thelos trial
selects `noconfirm`, the operator sets it in Thelos's `config.env`. Invalid values follow the usual warning and fallback.

`LLM_PRESENCE_PENALTY` uses tenths (15 means 1.5). `MAX_STEPS_TALK=0` removes the
conversation step limit. `WAKE_INTERVAL_MINUTES=0` disables periodic wakes (and event wakes); positive
values are bounded by the operator's wake floor (default 10 minutes), and a wake at that interval runs only with
something to do or after `SHAFT_IDLE_WAKE_MINUTES` without a cycle ([architecture](architecture.md#wakes)). `CURIOSITY_EVERY_CYCLES=5` makes one live
cycle in five, on average, a curiosity cycle while the instance has outward open surprises and no operator message waits
(0 = never; [PLAN-surprise](plans/PLAN-surprise.md)).
A drawn cycle follows one outward surprise, weighted by the content words it shares with the briefing, goals and
inbox; surprises about the instance's own tools, plugins, jobs, ledgers, gate or code are never drawn, and with none
outward the prompt says so and the goal is free (review I7 in [architecture](architecture.md)).
The six knobs after it switch the mechanisms of [PLAN-consciousness](plans/PLAN-consciousness.md), each on by default:
`OUTCOME_CARDS` (on, off: before a kind of action that has been failing, the first such call in a
cycle is held while the instance sees what comparable ones led to), `WORKSPACE` (competitive, relevance, off: what wins
`WORKSPACE_K` places at the front of a live cycle, in automatic recall's place, broadcast after every step),
`ATTENTION_SCHEMA` (declare_intent asks where it expects to look), `COMMITMENTS` (commit_to for promises to
Daniele (`operator`) or peers from `list_instances`, settled in sleep against the record; own plans belong in goals/notes)
and `FORECAST` (on, reserve, off: what comparable goals cost, or a fixed reserve rule). A value outside
these keeps the default with a warning. `shaft experiment` flips a knob's default in code to run its kill test, and
refuses an instance whose config.env sets that knob.
Outcome-card shell families and inspection exclusions are described in [architecture](architecture.md).
Under the same COMMITMENTS setting, an operator promise needs the conversation open now or a message delivered
to the operator in the same live cycle; unsourced ones become self records with a note. Self records expire 24 hours after due and remain in history
(review I10, [architecture](architecture.md)).
Sources: template `shaft/config.py:17`, [autonomy.py](../supervisor/autonomy.py).

`WATCH_MAIL` and `WATCH_CALENDAR` (not in the template's defaults: unset is `off`) ask the supervisor's watch to report
new mail under the instance's granted labels and changed events on its granted calendars: `inbox` puts one message
per pass in its inbox, `wake` also wakes it (at most once per `SHAFT_WATCH_WAKE_MIN` minutes); any other value is
`off`, logged once. Only what the operator granted is watched; see [integrations](integrations.md#mail-shaft-mail-).

`BRIEFING` names a file inside the instance's repository, or `off` to hide the briefing. The instance's own
watchers write it as a scheduled job with the next 48 hours of calendar, actionable mail, open commitments to the
operator and his last corrections, each dated. Prompt construction only reads the file. A first line
`generated: <ISO time>` dates the briefing; otherwise its mtime does. The prompt shows its age in hours, marks it
stale after 24 hours, and shows it like goals.md under `BRIEFING_CHARS` (default 6,000): whole `## ` sections, a section
whose heading names corrections first, then the rest in file order, and one line per section left out with the
`read_file` offset where it starts. Missing, empty or
unreadable files render a line explaining their state.

`HANDOFF=on` shows the last two live episodes' completed calls, unfinished work and next action with its revision
condition. Other episode kinds do not age the handoff. `raw` shows those episodes' tool logs, cut to the character
budget of their structured blocks; a shorter log stays shorter. `off` hides the block. Invalid modes keep the
next source's value with a warning. These switches affect prompt display; live episodes still record handoffs.

Cloud routing uses `LLM_BASE_URL="broker"` and a model in `SHAFT_CLOUD_MODELS`.
Self-hosted routing uses the same base URL and `<server>/<model>`, where the server
is configured in `SHAFT_SELF_HOSTED`. Its fallback defaults to `:8780`; set
`SHAFT_SELF_HOSTED_FALLBACK` to an empty value to fail instead of using local Bonsai.
Trial replays disable fallback. Sources: [cloudllm.py](../supervisor/cloudllm.py),
[selfhosted.py](../supervisor/selfhosted.py),
[selfhosted.py](../supervisor/selfhosted.py).
