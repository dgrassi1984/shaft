# PLAN — instances own their Python and reach the internet

The operator permits network access and Python environment management inside the sandbox, provided
instances cannot read files outside it. A missing cryptography library illustrates why an instance
needs to install dependencies rather than reimplement signing in the standard library.

Done already (separate from this plan): `http_request` sends any method to any public host; the per-host write
allowlist (`shaft hosts`, `write_hosts`) is gone.

## Today

- Agent, plugins, gate tests and `run` all use the system Python 3.12 with `-E -s` (`core.PYTHON`); nothing can be
  installed.
- The agent sandbox may connect only to localhost model/embedding/broker ports; `run` has no network at all
  (`sandbox.py`).
- The only way out is the supervisor's `fetch_page`/`http_request`, which refuse non-public addresses
  (`broker.check_public`).

## Design

### 1. Egress through the supervisor, not open sockets

The obvious one-liner, `(allow network*)`, would also open **localhost and the LAN/tailnet**: the operator's own dev
servers, databases, Jupyter, NAS shares, the Mac's own services. macOS `sandbox-exec` cannot filter by IP (only
`localhost` or `*`), so the sandbox alone cannot keep that out.

Instead, every run gets an **egress proxy** (`supervisor/egress.py`, stdlib, started and stopped with the broker):

- It is a plain HTTP proxy handling `CONNECT` (HTTPS tunnels) and absolute-URI HTTP. It needs a per-run egress
  token (not the broker token) in `Proxy-Authorization`.
- It resolves the host itself and connects to the **resolved IP** (no DNS-rebinding race). It refuses any address
  that is not global, using the same rule as `check_public`: localhost, RFC1918, CGNAT/tailnet 100.64/10,
  link-local and metadata are all refused.
- It logs one line per connection (`.supervisor/egress/<name>.jsonl`: time, host:port, bytes up/down, refused
  reason). `record(kind='egress')` shows it to the instance; the panel shows the last hosts.
- Sandboxes: the agent profile and the `run` profile each allow outbound to `localhost:<egress port>`, and nothing
  else new.
- The gate's sandbox (plugin check, instance tests) gets no port at all. *Found 2026-10-18:* since M0 it allowed
  `network*` on `localhost:*`, so instance-written tests could reach every localhost service of the operator's. Dropped:
  fixture tests pass with and without that permission, and `run`, where instances write tests, never had it.
  Regression: `test_gate.GateLocalhostTest`.
- Environment variables:
  - `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`, upper and lower case, set to `http://shaft:<tok>@127.0.0.1:<port>`.
  - `NO_PROXY=127.0.0.1,localhost`, so the agent's own broker and model calls stay direct.
- uv, pip, urllib, requests, httpx, curl and git (https) all honour these variables. Code that opens raw sockets
  fails with a sandbox denial, loudly, rather than reaching the LAN.

Net effect: full public internet for instances, nothing of the operator's reachable.

### 2. A Python environment the instance owns

- **Where it lives:** `instances/<name>/.venv/` (the env) and `.uv/` (`UV_PYTHON_INSTALL_DIR=.uv/python`,
  `UV_CACHE_DIR=.uv/cache`, `UV_TOOL_DIR=.uv/tools`). Both are gitignored (template `.gitignore`), writable by `run`
  (`RUN_WRITABLE`), and left out of trial state (`trial.NOT_STATE`).
- **What it is declared by:** uv's own conventions, committed with the instance's code:
  - `.python-version`: any version uv can install, the instance's choice.
  - `requirements.txt`: the packages.
- **The instance drives it with `uv` inside `run`:** `uv python install 3.13`, `uv venv --python 3.13 .venv`,
  `uv pip install PyJWT[crypto]`. uv is found on the supervisor's PATH at start-up. If it is missing, `run` says so,
  and the error shows the PATH that was searched. Source distributions are fine: builds run inside the `run`
  sandbox, so wheels-only buys nothing.
- **PATH in `run`:** `.venv/bin` first when it exists, so `python3`/`pip` are the instance's. Otherwise the system
  Python, as today.
- **Plugins:** the runner passes `SHAFT_PYTHON` (`.venv/bin/python` if present, else system). The template's
  `tools.py` launches `plugin_runner` with it instead of `sys.executable`. The plugin path is unchanged otherwise.
- **Gate:** `plugin_runner --check` and the instance's own tests run with `SHAFT_PYTHON`. The supervisor
  invariants stay on the supervisor's Python, because that code is trusted. `.python-version` and
  `requirements.txt` are code paths (`gate.CODE_PATHS`): tested when changed, committed when kept.
  *As built:* the gate does **not** sync the env itself. The env is live state the instance changes through
  `run`, like any other run side effect; a sync during the gate would change the real env before the change was
  accepted. A plugin needing a new package is installed first (`run`), then tested.
- **Rebuild:** at wake, if `.venv` is missing but `requirements.txt` or `.python-version` exists, the supervisor
  rebuilds it, sandboxed as a `run` would be. This covers a new machine and a wiped folder. Logged; a failure is
  a journal note for the instance, never fatal.
- **Battery** (*as built*): exams stay offline (a refusing proxy, `Broker(network=False)`), so results stay
  comparable with earlier ones. The instance arm reads its env read-only; the control arm has none.
- **The agent process itself stays on the system Python.** This is not a restriction on the env: plugins and `run`
  use the instance's env. It means a broken env never stops the instance from waking up to fix it.

### 3. Trial replays

Rule: replays never send.

- With open egress, a replay's `run` could `curl -X POST`, and TLS hides the method from the proxy. So the
  **ReplayBroker's egress refuses every connection** (tagged in the trace, like other refused writes).
- Replays read the instance's real `.venv`/`.uv` **read-only** (sandbox allow-read). The replay copy lives in
  `/var/folders`, and the env is not in git. The copy links them at `.venv`/`.uv` (`trial.link_env`), so a
  `.venv/bin/python …` runs as it does live; the sandbox checks the resolved path, which a replay cannot write.
- Known divergence: a plugin that does its own network reads works in a real cycle but fails in a replay. We
  accept that for now. A later improvement could cache reads through the proxy, the way `fetch_page` does.

### 4. Words

- Update the `run` tool description in `broker.py` ("has NO network" → internet via proxy, `uv`, `.venv`,
  `.python-version`, `requirements.txt`, no localhost/LAN).
- Update the prompts in `template/shaft/prompts.py` where they mention the sandbox.
- Update the README sandbox section.
- The template change reaches instances through the normal template merge.

## Milestones

| M | What | Files | Accept when |
|---|---|---|---|
| M0 | Egress proxy | `supervisor/egress.py`, `broker.py` (start/stop, token), tests | Tests: CONNECT to a public host tunnels; to 127.0.0.1, 192.168.x, 100.64.x refused; DNS-rebind case refused; bad token 407; log line per connection |
| M1 | Sandboxes use it | `sandbox.py`, `runner.agent_command`, `broker._run`, tests | Sandboxed `curl https://pypi.org` works from `run` and from a plugin; `curl http://127.0.0.1:<api>` and a LAN IP fail; agent→broker still direct |
| M2 | Instance-owned env | `broker._run` (PATH, UV_* vars), `sandbox.RUN_WRITABLE`, `runner` (SHAFT_PYTHON, rebuild), `gate.py`, `template/shaft/tools.py`, template `.gitignore`, tests | In `run`: `uv python install 3.13 && uv venv --python 3.13 .venv && uv pip install PyJWT[crypto]` succeeds; a plugin importing `jwt` passes the gate; deleting `.venv` → rebuilt at next wake |
| M3 | Trials | `broker.ReplayBroker`, `trial.py`, `sandbox` (read-only env), tests | Replay with a plugin importing `jwt` works; a replay `curl -X POST` is refused and traced; `test_replay_has_no_side_effects` extended |
| M4 | Words + record | `broker.py` descriptions, `record(kind='egress')`, panel, README, prompts | Descriptions match behaviour; panel shows recent egress hosts |
| M5 | Live check | — | example-a, in a real cycle: installs PyJWT[crypto], signs an RS256 JWT that `openssl` verifies; egress log shows pypi + files.pythonhosted.org; full suite green |

## Open design question: supervisor-held signing keys

Signing the JWT needs the App's PEM. A key stored with `secret_set` is never readable by the instance, by design,
so an instance plugin cannot sign with it directly. Options:

- (a) The supervisor mints the token: http_request gains a key type `github_app` (PEM + app id), and the broker
  signs the JWT and exchanges it for an installation token.
- (b) An instance-readable key would break that secret boundary and require separate authorization.

(a) keeps the "instances never see secret values" rule.
