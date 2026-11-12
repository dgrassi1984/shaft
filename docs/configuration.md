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
  file when they start. A long-running service (api, scheduler, telegram, web, model, embed) holds what it read at its start;
  each records the knobs it read (`.supervisor/services/<service>.json`), so `shaft settings` and the web UI's
  Settings page say "not live yet: restart api (`shaft api on`), …" until the services holding another value
  restart. A service holds the knobs of every module it imported, used or not, so the list errs towards a restart.
  A service started before it recorded its knobs is taken to hold the defaults of the knobs it is declared to read.
- Every `knob()` call registers its description, Settings group and readers; `test_settings` fails on a knob that is
  not registered, and on a `SHAFT_` variable read straight from the environment.
- Ports, interpreter paths, `SHAFT_WEB_HOSTS`, `SHAFT_WEB_LOCAL` and `SHAFT_TELEGRAM_API` are set from a terminal only: a wrong value there
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

Native push needs all three `SHAFT_APNS_*` settings and the operator-provided key at
`.supervisor/web/apns/AuthKey_<KEY_ID>.p8` (mode `600`). A missing setting or unreadable key disables native push;
APNs status reports the missing items and the actual key id, team, topic and key path. The key stays outside
instance sandboxes. These settings use the same environment > settings file > default precedence as other knobs;
restart the web service after changing them.

## Every supervisor knob

`SHAFT_ACTION_INSTANCES` enables the conservative [operator action classes](safety.md#operator-action-classes)
for a comma-separated list of instance names. Empty, the default, selects nobody.
It does not add grants. Other instances retain their current behavior. The selection is read at process start;
class demotion and restoration are read from supervisor-owned state on each dispatch.
Calendar proposals retain `SHAFT_CALENDAR_WRITES_PER_DAY` and `SHAFT_CALENDAR_PROPOSAL_HOURS`.

`SHAFT_GATE_TIMEOUT_S` defaults to 300 seconds. The supervisor's tests run in `SHAFT_GATE_WORKERS` processes, and
`shaft gate budget` keeps each part within half of that limit.

This table is extracted statically from production `knob()` calls in `supervisor/*.py`.
Defaults below are source defaults, not values read from this checkout's private state.
`PROJECT` means the clone root. Paths are resolved under that root at runtime.

| Name | Default | Meaning | Declaration |
|---|---|---|---|
| `SHAFT_ACTION_INSTANCES` | empty | Instances using conservative operator action classes; comma-separated, empty disables | [actions.py](../supervisor/actions.py) |
| `SHAFT_APNS_KEY_ID` | empty | APNs signing key id; empty disables native push | [apns.py](../supervisor/apns.py) |
| `SHAFT_APNS_TEAM_ID` | empty | Apple developer team id for native push | [apns.py](../supervisor/apns.py) |
| `SHAFT_APNS_TOPIC` | empty | Native app bundle id used as the APNs topic | [apns.py](../supervisor/apns.py) |
| `SHAFT_API_PORT` | `8781` | the API's local port (OpenAI-compatible; the web UI and Telegram chat through it) | [api.py](../supervisor/api.py) |
| `SHAFT_INBOX_PER_HOUR` | `30` | messages per hour the API's inbox (doorbell) accepts for one instance | [api.py](../supervisor/api.py) |
| `SHAFT_INBOX_WAKES_PER_HOUR` | `4` | wakes per hour inbox messages may start for one instance | [api.py](../supervisor/api.py) |
| `SHAFT_VOICE_SESSION_IDLE_S` | `300` | seconds a voice conversation keeps its instance's process up between spoken turns (its model session and memory stay warm); 0: none, every spoken turn starts its own process | [api.py](../supervisor/api.py) |
| `SHAFT_API_HTTPS_PORT` | `8443` | the API's https port on the tailnet (tailscale serve) | [api.py](../supervisor/api.py) |
| `SHAFT_PAUSE_ALERT_HOURS` | `3` | hours an operator pause may last before an alert; 0 = off. Read by CLI runs and web. | [core.py](../supervisor/core.py) |
| `SHAFT_BATTERY_VERSION` | `v1` | the battery item set in use (.supervisor/battery/items/<version>) | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_EVERY_DAYS` | `5` | nights between an instance's scheduled capability batteries (0: never scheduled) | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_ITEM_TIMEOUT_S` | `1800` | seconds one battery item may run | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_CONTROL_REF` | `template` | the git branch a battery control instance is made from | [battery.py](../supervisor/battery.py) |
| `SHAFT_BATTERY_RECALL_MIN_AGE_H` | `48` | hours old a journal entry must be to make a battery recall item | [battery.py](../supervisor/battery.py) |
| `SHAFT_CALENDAR_WRITES_PER_DAY` | `20` | calendar write attempts per instance per day; 0 disables writes | [broker.py](../supervisor/broker.py) |
| `SHAFT_CALENDAR_PROPOSAL_HOURS` | `24` | calendar proposal lifetime in hours | [calendar_proposals.py](../supervisor/calendar_proposals.py) |
| `SHAFT_OPERATOR_PER_DAY` | `0` | messages per day one instance may send the operator (0: no limit) | [broker.py](../supervisor/broker.py) |
| `SHAFT_TAILNET_GRANTS` | `{}` | tailnet hosts an instance may call through http_request with one of its keys (JSON: {"<instance>": ["<host>:<port>"]}); its sandbox still reaches none | [broker.py](../supervisor/broker.py) |
| `SHAFT_PUSH_HOLD` | `1` | hold non-urgent device notifications (0: off) | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_QUIET_HOURS` | `22-07` | local quiet hours, may wrap midnight; empty disables | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_DIGEST_AT` | `07:15` | local digest time, or quiet hours' end if later | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_PUSH_BUDGET` | `6` | operator pushes per instance per day before holding for a digest (0: unlimited, count hidden) | [pushes.py](../supervisor/pushes.py) |
| `SHAFT_DESCRIBE_MODEL` | `claude-haiku-5-5` | the model describe_image uses for an instance whose own model cannot see images (a custom/ model named here is taken to see them) | [broker.py](../supervisor/broker.py) |
| `SHAFT_CODEX_HOME` | `.supervisor/codex-home` | private Codex login and configuration directory, mode 700 | [codexcli.py](../supervisor/codexcli.py) |
| `SHAFT_CODEX_BIN` | `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex` | Codex CLI executable | [codexcli.py](../supervisor/codexcli.py) |
| `SHAFT_CODEX_MODELS` | `codex/gpt-6-astra,codex/gpt-6.1-sol` | Codex models offered to instances, comma separated | [codexcli.py](../supervisor/codexcli.py) |
| `SHAFT_CODEX_WEEKLY_PCT` | `85` | stop Codex calls at this percentage of the weekly allowance | [codexcli.py](../supervisor/codexcli.py) |
| `SHAFT_CODEX_FALLBACK_MODEL` | `openrouter/deepseek/deepseek-v4.1-flash` | cloud model used for the rest of a run after a Codex failure | [codexcli.py](../supervisor/codexcli.py) |
| `SHAFT_CODEX_TIMEOUT_S` | `900` | seconds allowed for a Codex model step; held tool calls are exempt | [codexcli.py](../supervisor/codexcli.py) |
| `SHAFT_CODEX_LOG_HOURS` | `24` | positive integer hours of Codex's own SQLite logs to keep; read by runs and api | [codexcli.py](../supervisor/codexcli.py) |
| `SHAFT_GROK_HOME` | `.supervisor/grok-home` | Private login and configuration directory, mode 700 | [grokcli.py](../supervisor/grokcli.py) |
| `SHAFT_GROK_BIN` | `~/.grok/bin/grok` | Grok CLI executable | [grokcli.py](../supervisor/grokcli.py) |
| `SHAFT_GROK_MODELS` | `grok/grok-4.7` | Offered instance models, comma separated | [grokcli.py](../supervisor/grokcli.py) |
| `SHAFT_GROK_FALLBACK_MODEL` | `openrouter/deepseek/deepseek-v4.1-flash` | Cloud fallback for the remainder of a live run | [grokcli.py](../supervisor/grokcli.py) |
| `SHAFT_GROK_TIMEOUT_S` | `900` | Model step timeout; held calls are exempt | [grokcli.py](../supervisor/grokcli.py) |
| `SHAFT_GROK_SESSION_HOURS` | `24` | Hours of Grok session files to retain | [grokcli.py](../supervisor/grokcli.py) |
| `SHAFT_CLOUD_MODELS` | `claude-haiku-5-5,claude-opus-5-5,openrouter/xiaomi/mimo-v2.6-pro` | the cloud models an instance may run on (through the broker) | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_OPENROUTER_PROVIDER` | `{"only": ["novita", "deepinfra"], "order": ["novita", "deepinfra"], "allow_fallbacks": false, "data_collection": "deny", "zdr": true, "require_parameters": true}` | OpenRouter's provider routing for every openrouter/ call (JSON): which providers, and the data policy | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_CLOUD_DAILY_USD` | `8` | dollars per day an instance may spend on its cloud model (shaft limits can set one instance's own cap) | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_CUSTOM_BASE_URL` | _(empty)_ | the OpenAI-compatible endpoint (an http(s) URL, e.g. https://host/v1) that custom/<model> is called on: <url>/chat/completions, with the key SHAFT_CUSTOM_API_KEY (a secret in the project `.env`, never in an instance) | [cloudllm.py](../supervisor/cloudllm.py) |
| `SHAFT_CUSTOM_PRICES` | `{}` | what each custom/<model> costs, in dollars per million tokens, as JSON `{"<model>": [input, output]}` (0 is allowed): a custom model with no price is refused, so the daily cap counts every call; a reply without usage is booked at the worst case (prompt characters / 3 in, `max_tokens` out) | [cloudllm.py](../supervisor/cloudllm.py) |
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
| `SHAFT_WATCH` | `on` | the supervisor's mail, calendar, and Google Chat watch, for instances that opt in with WATCH_MAIL, WATCH_CALENDAR, or WATCH_GCHAT in their config.env (on or off) | [watch.py](../supervisor/watch.py) |
| `SHAFT_WATCH_WAKE_MIN` | `15` | minutes between two wakes the mail, calendar, and Google Chat watch gives one instance; a change sooner waits in its inbox | [watch.py](../supervisor/watch.py) |
| `SHAFT_WATCH_EVERY_S` | `60` | seconds between two passes of the mail, calendar, and Google Chat watch (the scheduler looks every 30 seconds) | [watch.py](../supervisor/watch.py) |
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
| `SHAFT_CORRECTION_PASSES` | `3` | messages to the operator that must pass the order check against a correction before it stops being checked (0: corrections are not checked) | [orders.py](../supervisor/orders.py) |
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
| `SHAFT_TRIAL_N_MAX` | `10` | maximum replay units per arm across selected past states, counting live cycles and sleeps | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_LOCAL_WAIT_H` | `6` | hours a trial waits for the local model's idle time before it runs on the fallback model | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_FALLBACK_MODEL` | `openrouter/xiaomi/mimo-v2.6-pro` | the cloud model a local-model trial runs on when the local model stays busy | [trial.py](../supervisor/trial.py) |
| `SHAFT_TRIAL_PARALLEL` | `3` | replayed cycles a cloud-model trial runs at once | [trial.py](../supervisor/trial.py) |
| `SHAFT_MAX_IMAGES` | `4` | images one model call may carry | [vision.py](../supervisor/vision.py) |
| `SHAFT_WEB_PORT` | `8785` | the web UI's local port (tailscale serve forwards to it) | [web.py](../supervisor/web.py) |
| `SHAFT_WEB_HTTPS_PORT` | `8444` | the web UI's https port on the tailnet (tailscale serve) | [web.py](../supervisor/web.py) |
| `SHAFT_WEB_HOSTS` | empty | the host names the web UI answers to, comma-separated (empty: the tailnet name `shaft web on` recorded) | [web.py](../supervisor/web.py) |
| `SHAFT_WEB_LOCAL` | `off` | on: the Mac app on this Mac reaches /app/v1 at 127.0.0.1 with a paired device's bearer, no Tailscale needed; the pages stay tailnet-only | [web.py](../supervisor/web.py) |

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

The following defaults are from the template branch's `shaft/config.py`;
the shipped `config.env` supplies the local URL and model. Inspect them without
creating an instance with `git show template:shaft/config.py` and
`git show template:config.env` after setup.

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
| `TALK_REPLY_CALLS` | `6` | uses built-in default |
| `TALK_REPLY_S` | `90` | uses built-in default |
| `TALK_REPLY_MAX_CALLS` | `20` | uses built-in default |
| `TALK_REPLY_MAX_S` | `300` | uses built-in default |
| `MAX_STEPS_TALK` | `0` | `0` |
| `VOICE_MODE` | `'off'` | `off` |
| `VOICE_REASONING_EFFORT` | `''` | empty |
| `LOOP_GUARD` | `'on'` | `on` |
| `LOOP_EFFECT_ROUNDS` | `3` | `3` |
| `CONTEXT_JOURNAL_CHARS` | `16000` | `16000` |
| `CONTEXT_CHARS` | `48000` | `48000` |
| `INBOX_CHARS` | `4000` | `4000` |
| `RESULTS_KEEP_TURNS` | `4` | `4` |
| `STALE_LINE_HOURS` | `24` | uses built-in default |
| `STALE_GOAL_HOURS` | `48` | uses built-in default |
| `CRITERION_CHARS` | `600` | uses built-in default |
| `CONFIDENCE_MODE` | `'confirm'` | uses built-in default |
| `DOUBTS` | `'on'` | uses built-in default |
| `LOOK_AHEAD` | `'off'` | uses built-in default |
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
| `GOAL_SLATE` | `'on'` | uses built-in default |
| `FIRST_ERROR` | `'on'` | uses built-in default |
| `OPERATOR_NAME` | `'the operator'` | empty |

### Stale records

`STALE_LINE_HOURS` flags dates older than this many hours in self/goals.md and self/self.md
at sleep. `STALE_GOAL_HOURS` flags Active goal bullets with no live touch in that window,
only if the operator sent a conversation turn during it. Set either to 0 to disable that check.
Sleep asks for a fix, retirement or reason to keep each item, recorded in its journal.

### Conversation reply budgets

`TALK_REPLY_CALLS` (6) and `TALK_REPLY_S` (90) set the default conversation reply budget.
`plan_reply(calls, seconds, why)` changes it for that reply, counting from the reply's start.
Plans above `TALK_REPLY_MAX_CALLS` (20) or `TALK_REPLY_MAX_S` (300) are refused; the previous budget stays.
Set a ceiling to 0 to disable it. The plan is shown to the operator and recorded in the talk episode.
The two conversation controls do not spend the calls budget. Work expected to exceed a ceiling is queued up front.
An operator request to take your time means queue the full task for the next cycle and reply briefly. When either budget
is used, the next model call is offered only `queue_for_next_cycle` and must answer with its next steps. A running
call finishes normally. Set either knob to 0 to disable that budget.
`queue_for_next_cycle(task, why)` puts unfinished work in the instance's own inbox for its next live cycle.
`CRITERION_CHARS` limits `declare_intent.success_criterion` in characters. An overlong criterion is refused
with its length, the limit and how many characters to cut, asking for the deliverable's check rather than ritual
clauses, and ends with the draft so the instance can trim it in one retry (review I8; never trimmed silently).

### Voice turns

`VOICE_MODE` (`on`/`off`, default `off`) offers voice mode for the instance: the app shows its Talk button only when it
is `on`. `VOICE_REASONING_EFFORT` is the reasoning effort of spoken turns only: empty (the default) means
`LLM_REASONING_EFFORT`, otherwise one of the values `LLM_REASONING_EFFORT` accepts; any other value is refused with a
warning and stays empty. A spoken turn is a chat request with `"voice": true`
([API](web-and-api.md#v1-wire-contract)): the instance adds `prompts.VOICE_NOTE` to that reply's prompt, makes every
model call of the reply with this effort, and marks the turn's talk episodes `voice`. Typed turns are unchanged.
The spoken turns of one conversation share one warm process (supervisor setting `SHAFT_VOICE_SESSION_IDLE_S`, default 300 s
between turns; [API](web-and-api.md#v1-wire-contract)); a change to `config.env` starts a new one at the next turn, so a
changed setting is never answered with the old one.

### Sleep feedback

`SLEEP_VERDICT_CHARS` bounds sleep feedback reads, in the sleep feedback group in `template/config.env`.
Sleep reads judgment pages (up to 24,000 characters each) until caught up or until the next page would take the
verdict text past this positive character budget; the block is re-sent on every sleep step, so the default
(~30k tokens) leaves the rest of a 262,144-token window free. At the budget it tells the model and stderr that
feedback was cut short; the next finished sleep resumes from the last page it read.

### Loop guards and confidence

`LOOP_GUARD` controls the effect-loop guard (#103): `shadow` records would-fire events in the episode's
`loop_shadow` array without changing model input; `on` also appends advice after the call executes;
`off` records nothing. The existing exact-call repeat guard stays active in all three modes.
`LOOP_EFFECT_ROUNDS` is a positive integer, default 3. A first failed result counts as round one;
new successful observations reset the count. A successful observation seen earlier in this run counts as
no new evidence. Counts require consecutive calls in the same tool/target family.
See [effect-loop measurement](architecture.md#repeated-actions-and-effect-measurement) for target rules and replay reporting.
`CONFIDENCE_MODE` accepts `confirm` (default), `clauses`, or `noconfirm`; the live prompt names the resolved mode.
The gate's scripted cycles declare in a form every mode accepts, and the supervisor's own suite runs them once
per mode, so an instance that changes the mode is judged on its code, not on tests pinned to `confirm`.
`confirm` keeps the existing declare and base-rate confirmation flow. `clauses` asks for 1-4 success-criterion
clauses, each with an integer confidence 0-100. The template records them with a null combined confidence;
the supervisor multiplies their probabilities when it writes the judgment, assuming independence because
all clauses must hold. This v4 confidence stays apart from v1-v3 in calibration, AUROC and base rates.
Both `clauses` and `noconfirm` omit the base rate and `confirm_confidence`; `noconfirm` records a single v2 confidence.
These arms are available for a later trial. No instance configuration changes automatically.
The operator can set `noconfirm` in an instance's `config.env`. Invalid values follow the usual warning and fallback.

### Model turns and wake behavior

`LLM_PRESENCE_PENALTY` uses tenths (15 means 1.5). `MAX_STEPS_TALK=0` removes the
conversation step limit. `MAX_STEPS_*` count model turns. Plan-channel tool replies marked `shaft_same_turn`
do not spend a turn; after 10 consecutive such replies, the next one spends a turn to bound missing end signals.
Other replies count even without usage. `WAKE_INTERVAL_MINUTES=0` disables periodic wakes (and event wakes); positive
values use the instance's own interval unless the operator sets a wake floor, and a wake at that interval runs only with
something to do or after `SHAFT_IDLE_WAKE_MINUTES` without a cycle ([architecture](architecture.md#wake-sources-and-background-work)). `CURIOSITY_EVERY_CYCLES=5` makes one live
cycle in five, on average, a curiosity cycle while the instance has outward open surprises and no operator message waits
(0 = never).
A drawn cycle follows one outward surprise, weighted by the content words it shares with the briefing, goals and
inbox; surprises about the instance's own tools, plugins, jobs, ledgers, gate or code are never drawn, and with none
outward the prompt says so and the goal is free (review I7 in [architecture](architecture.md)).
`DOUBTS` accepts `on` (default) or `off`. Off removes the optional `doubts` parameter from `finish_cycle`;
no prompt asks for doubtful steps. `LOOK_AHEAD` accepts `on` or `off` (default). On adds a question to live step 1
about what the operator will need within 7 days, asking the instance to prepare it and offer it in one line.
Off leaves the live instructions unchanged.

### Experimental mechanisms

These settings switch the self-knowledge mechanisms, each on by default:
`OUTCOME_CARDS` (on, off: before a kind of action that has been failing, the first such call in a
cycle is held while the instance sees what comparable ones led to), `WORKSPACE` (competitive, relevance, off: what wins
`WORKSPACE_K` places at the front of a live cycle, in automatic recall's place, broadcast after every step),
`ATTENTION_SCHEMA` (declare_intent asks where it expects to look), `COMMITMENTS` (commit_to for promises to
the configured operator (`operator`) or peers from `list_instances`, settled in live cycles or sleep against the record; own plans belong in goals/notes)
and `FORECAST` (on, reserve, off: what comparable goals cost, or a fixed reserve rule). A value outside
these keeps the default with a warning. `shaft experiment` flips a knob's default in code to run its kill test, and
refuses an instance whose config.env sets that knob.
Outcome-card shell families and inspection exclusions are described in [architecture](architecture.md).
Under the same COMMITMENTS setting, an operator promise needs the conversation open now or a message delivered
to the operator in the same live cycle; unsourced ones become self records with a note. Self records expire 24 hours after due and remain in history
(review I10, [architecture](architecture.md)).
In conversations, orders for later work must be recorded with `commit_to`, a due date and a checkable criterion.
With `COMMITMENTS=off`, `queue_for_next_cycle` carries the exact order marked ORDER and its criterion instead.
A blocked order gets a one-line reply. The conversation write-up lists orders with neither a commitment nor a result,
using the conversation tool results as evidence.
Sources: template `shaft/config.py`, [autonomy.py](../supervisor/autonomy.py).

### Mail, calendar, and Google Chat watches

`WATCH_MAIL`, `WATCH_CALENDAR`, and `WATCH_GCHAT` default to `off` when unset. They ask the supervisor's watch to report
new mail under granted labels, changed events on granted calendars, and new messages on granted Google Chat accounts.
The web Config page offers them as a choice of `off`, `inbox` or `wake`, listed even when unset; the app API's settings
carry the same choices (`watch_mail`, `watch_calendar`, `watch_gchat`), which the native apps do not show yet.
`inbox` puts one message per kind per pass in the inbox. `wake` also wakes the instance, at most once per
`SHAFT_WATCH_WAKE_MIN` minutes. Any other value is `off`, logged once. Chat includes message text cut at 500 characters
and at most 40 message lines per inbox message. The operator's own messages land but do not wake the instance on
their own. Only what the operator granted is watched; see [mail](integrations.md#mail-shaft-mail-) and
[Google Chat](integrations.md#the-operators-google-chat-shaft-gchat).

### Briefing

`BRIEFING` names a file inside the instance's repository, or `off` to hide the briefing. The template prompt asks the instance to create its own
watchers as a scheduled job. No briefing watcher is shipped. The requested briefing contains the next 48 hours
of calendar, actionable mail, open commitments to the operator, and the operator's last corrections, each dated. Prompt construction only reads the file. A first line
`generated: <ISO time>` dates the briefing; otherwise its mtime does. The prompt shows its age in hours, marks it
stale after 24 hours, and shows it like goals.md under `BRIEFING_CHARS` (default 6,000): whole `## ` sections, a section
whose heading names corrections first, then the rest in file order, and one line per section left out with the
`read_file` offset where it starts. Missing, empty or
unreadable files render a line explaining their state.

### Standing context budgets

Every section of the standing context (the system prompt of every run) has a budget: the self-model 10,000
characters, personality 4,000, the skill list 3,000, facts 6,000, digests 3,500, goals `GOALS_CHARS`, journal
`CONTEXT_JOURNAL_CHARS`, the last sleep 3,000, the improvement plan 2,500, open commitments, projects and corrections
3,000 each, stats 5,500 (template `shaft/prompts.py` `SECTIONS`). `CONTEXT_CHARS` (default 48,000, about what the
standing slices came to before the self-model and the rest had any budget; 0 = no total) caps them together: when the
sections want more, a section wanting less than its share keeps all of it and the others share the rest in proportion
to their budgets. A cut section keeps its head (or, for facts and digests, its newest tail) and ends with a line saying
how many characters were left out and how to read them (`read_file <file>`, `list_files skills`, `search_memory`).
The open surprises keep their own cap (1,200 characters) and count against the total first; when they leave no room, every section shows only its cut line (the journal a pointer to `search_memory`), never everything. Sections run stable first: self-model, personality, skills,
facts, digests, goals, journal, then the last sleep, improvement plan, surprises with commitments, projects,
corrections, stats and the time, so a prompt cache keeps the longest prefix it can from one run to the next.

### Inbox budget

`INBOX_CHARS` (default 4,000; 0 = no limit) bounds a live cycle's inbox: the operator's messages whole and first (never cut), every other message as one line
and then by time, with its header but `To` (`Trust` cut at 300 characters, other values at 100, the header at 600
together) and the first 200 characters of its body; `read_file inbox/<file>` shows one whole. The budget keeps 200
characters for the line that counts the messages past it (`list_files inbox` names them). Only the messages listed,
and any the cycle read whole with `read_file`, are archived to `memory/messages/` and named under **Messages read**;
the rest stay in the inbox for a later cycle, are named under **Left in my inbox** in the journal and in the episode's
`inbox_deferred` (its `inbox` still lists every message found at the start, which a trial replay restores). Retrieval
reads every message whole; attached images and the goal slate come from the listed ones.

### Tool result retention

`RESULTS_KEEP_TURNS` (default 4; 0 = never) shrinks the tool results of model turns older than that many, in live
cycles, sleeps and conversations, to one line: the tool, its arguments (120 characters), the result's first line, its
length and `read_file .tmp/results/<pid>/<n>.txt`, where the whole result is kept. Results of 400 characters or less
stay whole. The call ledger and the workspace's focus line stand under the newest step's last result only; the
ledger's file writes are cut from their oldest end at 800 characters, each one left out still in its own result.

### Goal selection and error feedback

`GOAL_SLATE` (on, off) shows a live cycle, before `declare_intent`, up to six candidate goals from the instance's own
records: inbox messages (the operator's first), overdue commitments, projects at risk or past a boundary, its last live
cycle's `next_action`, and its top open surprise — one of each kind first, in that order, then the rest — each with the
judged value of the newest similar earlier goal (word overlap at least 0.2) that has one. In a trial replay whose
monitor probe removes or replaces the open surprises (`x_probes`), the slate's surprise follows that arm. The model still chooses;
how often similar goals were met is never shown, since declare_intent's base rate is what the confirm step measures.
`FIRST_ERROR` (on, off) adds to `declare_intent`'s result at most two lines: the judge's `first_error` on the newest
similar earlier goal judged `no` or `partial`. Both read `memory/verdicts.jsonl`, which the sleep fills from
`record(kind='judgments', format='json')` (it carries `value` and `value_for` when the judge gave them).

### Live handoffs

`HANDOFF=on` shows the last two live episodes' completed calls, unfinished work and next action with its revision
condition, each field cut at 600 characters and the block at 3,000, saying which episode file holds the whole.
Other episode kinds do not age the handoff. `raw` shows those episodes' tool logs, cut to the character
budget of their structured blocks; a shorter log stays shorter. `off` hides the block. Invalid modes keep the
next source's value with a warning. These switches affect prompt display; live episodes still record handoffs.

### Cloud and self-hosted routing

Cloud routing uses `LLM_BASE_URL="broker"` and a model in `SHAFT_CLOUD_MODELS`.
Self-hosted routing uses the same base URL and `<server>/<model>`, where the server
is configured in `SHAFT_SELF_HOSTED`. Its fallback defaults to `:8780`; set
`SHAFT_SELF_HOSTED_FALLBACK` to an empty value to fail instead of using local Bonsai.
Trial replays disable fallback. Sources: [cloudllm.py](../supervisor/cloudllm.py),
[selfhosted.py](../supervisor/selfhosted.py).

### Codex plan channel

Codex plan routing also uses `LLM_BASE_URL="broker"`, with `LLM_MODEL="codex/gpt-6-astra"`
or another model in `SHAFT_CODEX_MODELS`. Run `shaft codex login` first; `shaft codex status`
shows whether login is present, the last observed weekly usage, offered models and any outage.
The allowance is shared with other Codex use. Calls use no API dollars; after a failure,
the run uses `SHAFT_CODEX_FALLBACK_MODEL`, charged as usual and labelled
`codex/x (fallback y)`. Trial replays and bake-offs fail instead of falling back.
Judges, image descriptions, consults, coder hand-offs and recaps still require their API models.
Source: [broker.py](../supervisor/broker.py).

### Codex log retention

Before starting a channel, retention deletes rows older than `SHAFT_CODEX_LOG_HOURS` from
`SHAFT_CODEX_HOME/logs_2.sqlite` in batches of at most 1,000. A locked stamp file limits attempts
to once per ten minutes across processes. SQLite secure deletion overwrites deleted text; a
TRUNCATE checkpoint clears the WAL when other readers permit it. VACUUM reclaims free pages
when they exceed both a quarter of the database and 8 MiB. SQLite waits at most three seconds
per lock attempt. Missing files, missing tables and SQLite errors are logged without failing
the model call. Active readers can defer WAL truncation and space reclamation until a later attempt.
Secure deletion does not scrub historical slack bytes left before it was enabled; those can
remain until the database qualifies for VACUUM.
Retention runs on channel startup, so an already running channel does not prune periodically.
Source: [codexcli.py](../supervisor/codexcli.py).

### Grok plan channel

Instance models in `SHAFT_GROK_MODELS` use `LLM_BASE_URL="broker"`.
Run `shaft grok login` first. These models are not accepted for supervisor cloud
jobs (judging, describing images, consult or recap). Login and channel children
receive an explicit environment, with imported skills, rules, hooks and MCPs disabled.

| Setting | Default | Purpose |
|---|---|---|
| `SHAFT_GROK_HOME` | `.supervisor/grok-home` | Private login and configuration directory, mode 700 |
| `SHAFT_GROK_BIN` | `~/.grok/bin/grok` | Grok CLI executable |
| `SHAFT_GROK_MODELS` | `grok/grok-4.7` | Offered instance models, comma separated |
| `SHAFT_GROK_FALLBACK_MODEL` | `openrouter/deepseek/deepseek-v4.1-flash` | Cloud fallback for the remainder of a live run |
| `SHAFT_GROK_TIMEOUT_S` | `900` | Model step timeout; held calls are exempt |
| `SHAFT_GROK_SESSION_HOURS` | `24` | Hours of Grok session files to retain |

Retention runs at channel start, at most once every ten minutes across processes.
It removes only old regular files under `SHAFT_GROK_HOME/sessions/`, skips symlinks,
and logs failures without failing a call. No weekly percentage is available.
Usage rows keep token counts and API equivalent cost, with `usd=null`; message
content and image data are not logged. A live failure uses the configured fallback;
trial replays and bake-offs fail without fallback. Outages persist separately in
`.supervisor/grok_outage.json`. Source: [grokcli.py](../supervisor/grokcli.py).
