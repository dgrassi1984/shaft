# PLAN — instances browse the web with a real browser

Goal: an instance can drive a headless Chromium. It opens pages that need JavaScript, clicks, types, submits forms
and logs in, and its logins carry over from one run to the next. The instance does that with its own code
(Playwright in its own `.venv`, its own plugin).

The supervisor provides four things, and only these:
1. one Chromium shared by every instance;
2. a private profile folder per instance;
3. the sandbox rules and environment that let Chromium start and reach the internet through the egress proxy;
4. one paragraph of documentation telling the instance all this.

**Status (2026-10-19): built and merged.** M4 did not establish persistent authenticated access.
Plugin, egress, replay isolation and process-cleanup checks passed.

## Decided (operator, 2026-10-18)

- **The sandbox becomes able to install, set up and run Chromium.** There is no supervisor browser tool.
- **Instances act, and keep their sessions.** Browsing means logging in, filling forms and clicking through, not
  only reading pages that need JavaScript.
- **Account boundary:** grant browser access only to explicitly authorized test accounts.
- **"Provide it a profile on a shared chromium, and leave all the rest to it."** One Chromium install for every
  instance, one profile per instance. Signing up, logging in, keeping its own passwords, its plugin and its
  Playwright code are all the instance's.
- **D1: a browser runs only in `run` and jobs**, never straight from the agent's sandbox (see 3). "Your
  recommendation."
- **D2: Playwright's fake keychain is enough.** "Fake keychain is fine." There is no keychain probe and no
  security-server deny (see 3).

## Why the instance builds it (the `.claude/CLAUDE.md` check)

Everything that is about browsing is in the instance's reach today:
- it installs packages with `uv` inside `run`;
- it reaches the public internet through the egress proxy;
- it writes plugins.

The only missing pieces are about the sandbox, and only the supervisor can change those:
- Chromium is ~300 MB per revision, so one shared copy is better than one per instance;
- no sandbox can read a shared copy yet;
- no folder is set aside for a profile that must stay out of git, out of trial copies and away from the coder.

**Ruled out:**
- **A shared running browser that instances connect to (CDP).** It would run outside every sandbox with the
  operator's permissions. `file:///Users/example/…` and localhost would then be reachable through it.
- **A broker tool that types a stored secret into a page.** The browser and the code driving it both run in the
  instance's sandbox, so any password typed there can be read back. It would only look safe.
- **An operator `browser-login` command.** Dropped with the "leave all the rest to it" decision. If the operator
  wants to sign an instance in themselves, the instance can open a visible window on its own profile (see Risks).

## Today (read in the code, 2026-10-18)

- **The instance's Python environment** is `.venv/` and `.uv/` in its folder (`pyenv.py`). `run` drives `uv`.
  The packages it declares are in `requirements.txt`, which is rebuilt at wake.
- **Network.** Every sandbox has `(deny network*)` plus outbound to listed localhost ports. One of those ports is
  the per-run egress proxy (`egress.py`):
  - it takes HTTP `CONNECT` with `Proxy-Authorization` for the user `shaft` and a per-run token;
  - it connects only to public addresses;
  - it logs every connection to `.supervisor/egress/<name>.jsonl`.
- **Where the environment comes from.**
  - `Broker.sandbox_env()` (`broker.py:643`) is "what every sandboxed process of this run gets": the agent
    (`runner.agent_command`) and jobs (`jobs.py:245`) receive it.
  - `run` (`broker.py:879`) builds its own environment from `self.egress.env()` and does *not* go through
    `sandbox_env()`. Anything added there would miss `run` today.
- **Temp files.**
  - `run` sets `HOME=TMPDIR=<root>/.tmp/home`; jobs use `.jobs/<plugin>/.home`; the agent uses `TMPDIR=<root>/.tmp`.
  - The per-user folder `/private/var/folders/<xx>/<id>/` and `/private/tmp` are closed
    (`sandbox._operator_places_closed`).
- **Reads.**
  - Each sandbox reads only its own folder in the home. `_env_reads` adds a read-only view of the real `.venv`/`.uv`
    for copies of an instance.
  - Nothing outside an instance's folder in the project is readable, so a shared Chromium would need a new read rule.
- **Writes.**
  - `run` writes only `RUN_WRITABLE` (`notes plugins skills tests shaft .tmp .venv .uv`) and the two declared files.
  - Jobs write only `.jobs/<plugin>/` and `.tmp/`.
  - The agent writes its whole folder, and plugins run inside the agent's sandbox.
- **Copies.**
  - Replays are rebuilt from `git archive` plus the mind store (`trial.py:255-262`), so an untracked folder never
    reaches one.
  - The copies made from the live folder are the trial gate's (`trial.py:788`) and the coder's scratch copy
    (`coder.py:184`). Both are filtered by `coder.HIDDEN` (`.git logs .tmp __pycache__ secrets .venv .uv`), and the
    coder model reads whatever is not hidden.
  - `trial.NOT_STATE` only filters the before/after diff the judge sees (`trial.py:335-339`).
- **Where instance writing travels.**
  - `notes/`, `memory/`, `self/` and `inbox/` (`core.MIND_PATHS`) go to the mind store, into replays and to the
    judges' model providers.
  - Anything outside `coder.HIDDEN` goes to the coder model.
  - `secrets/` cannot be read by any instance code.
  - Today nothing persistent is both private and readable by the instance.
- **Process lifetime.** Nothing guarantees a browser dies with its call:
  - `run` kills its process group only on timeout (`broker.py:901`), at most 300 s.
  - Plugin calls use `subprocess.run(timeout=120)` (`template/shaft/tools.py:25,331`), which kills only the plugin's
    Python.
  - At the end of a cycle the runner stops only the agent process (`runner.py:490-496`).
  - Playwright may also launch Chromium in its own process group (unverified).
- **Localhost from the agent's sandbox.**
  - The agent sandbox, which plugins inherit, allows outbound to the model port (8780), the embedding service (8782),
    the broker and the egress proxy (`runner.py:438`).
  - The model and embedding servers check no credentials.
  - Chromium skips the proxy for loopback by default.
- **The gate's sandbox has no network** since main 80c2589 (2026-10-18). Until then it allowed any localhost port
  (`any_localhost=True`), which was found while drafting this plan and fixed separately. That sandbox runs the
  instance's plugin check and its own tests.
- **Agent `HOME`** is the operator's real home (`agent_env` passes it through, `sandbox.py:198`). Reads and writes
  there are denied, but Chromium's `~/Library` paths behave differently in the agent's sandbox than in `run` and jobs
  (`HOME=.tmp/home`).
- **What instances read about their environment** is in their own code: `template/shaft/prompts.py:53` and
  `template/skills/writing-plugins.md`. It reaches existing instances through `shaft update`. There is no template
  README.
- **A Playwright install already exists** in the operator's `~/Library/Caches/ms-playwright/` (`chromium-1243`,
  `chromium_headless_shell-1243`). It is theirs, and it stays unused here.

## Design

### 1. One shared Chromium: `<project>/.browsers/`

- **Operator command** `shaft browser install [--playwright X.Y.Z]`. It runs `uvx --from playwright==X.Y.Z
  playwright install chromium chromium-headless-shell` with `PLAYWRIGHT_BROWSERS_PATH=<project>/.browsers`, and
  writes `.browsers/VERSION` (the Playwright version the revisions belong to).
  - The default version is a pinned constant in the supervisor, so updating it is a code change.
  - It runs as the operator, outside any sandbox, like `brew`. No instance can trigger it.
  - `.browsers/` goes in the supervisor repository's `.gitignore`.
- **Why Playwright's builds and not Chrome or Homebrew Chromium:**
  - Playwright's Python package finds them by revision with no configuration;
  - they are built for automation;
  - their default launch already uses `--use-mock-keychain` and no Chromium sandbox of its own (see 3).
- **Read-only from the sandboxes that may browse:** `run` and jobs (D1), including `run` in a replay, whose proxy
  refuses everything.
  - A new `_browser_reads(project)` rule allows `file-read*` on `.browsers/` and the metadata of its parents, as
    `_env_reads` does.
  - The agent's own profile and the gate's do not get it.
  - Nothing may write there, which `(deny file-write*)` already ensures.
- **The version contract.**
  - The environment carries `PLAYWRIGHT_BROWSERS_PATH=<project>/.browsers` and `SHAFT_PLAYWRIGHT=<VERSION>`.
  - An instance that installs `playwright==$SHAFT_PLAYWRIGHT` launches with no arguments.
  - An instance that picks another version gets Playwright's own error naming the missing executable. That failure
    is loud.
  - Such an instance may install its own browsers under `.uv/` by overriding `PLAYWRIGHT_BROWSERS_PATH` in `run`.
    That is its environment and its choice.
- **Missing install.**
  - At start-up the supervisor logs one line if `.browsers/VERSION` is missing, naming the command to run.
  - The documentation paragraph tells instances to ask the operator if the install is missing. No silent fallback
    to the operator's `~/Library/Caches` copy.
- **Panel:** one line next to the Python environment line, e.g. `browser: Playwright 1.5x · profile 14 MB`, or
  `browser: not installed (shaft browser install)`.

### 2. One profile per instance: `<root>/.browser/`

- **Creation.** `SHAFT_BROWSER_PROFILE=<root>/.browser` is in the environment. The supervisor creates the folder
  (`safe_mkdir`). The instance passes it to `launch_persistent_context`, and cookies, local storage and its sessions
  live there.
- **Writable by:** the agent (and so its plugins) as today; `run` (added to `RUN_WRITABLE`); jobs (added to
  `job_profile`'s writable paths).
  - `job_profile` keeps writes narrow so that nothing of a job's is mistaken for the cycle's *change*. The profile is
    never code and never committed, so that reason does not apply to it.
  - Two Chromiums cannot open the same profile at once: Chromium's `SingletonLock` refuses the second, loudly. A
    job that overlaps a cycle's browsing gets that error, and how to handle it is the instance's business.
- **Its passwords live here too** *(my call; the reviewer's top question)*.
  - The instance's site passwords go in `.browser/` (Chromium's own password store, or a file it keeps there). That
    is the one place an instance can write that is persistent, readable by it, and never travels.
  - The documentation paragraph says this plainly: a password in `notes/` or `memory/` reaches the mind store, the
    judges' model providers and the coder.
  - Nothing enforces it. An instance writing a password into its notes is its mistake, the same as any other secret
    it chooses to write there.
- **Never travels:**
  - `coder.HIDDEN`: the coder's scratch copy, the trial gate copy and the coder model never see it. This is the
    filter that matters.
  - `trial.NOT_STATE`: a profile created during a replay stays out of the judge's diff.
  - `.gitignore` (template plus existing instances): only for a clean `git status`. `commit_state` stages only
    gate-checked code paths (`core.py:459-472`), so a profile is never committed either way.
  - The mind store, which holds only `MIND_PATHS`.
- **Copies do not read it.** `_env_reads` stays limited to `.venv` and `.uv`. A copy sees no profile, so it cannot
  act as the instance on a live site. A replay could not anyway: its proxy refuses everything.
- **No browser outlives its cycle or job** *(my call)*.
  - When a cycle, a conversation or a job ends, the supervisor kills every process whose command line carries
    `--user-data-dir=<root>/.browser`, plus any stray process left in the plugin's or `run`'s group.
  - A leftover Chromium would otherwise hold `SingletonLock`, and the next launch would fail.
  - M0 checks whether Playwright detaches Chromium into its own group.
- **No cap on simultaneous browsers** *(my call)*. One profile per instance and `SingletonLock` already allow at most
  one Chromium per instance at a time. The panel shows which ones are running.

### 3. The sandbox and environment Chromium needs

**M0 results (2026-10-18).**

*Setup.* Playwright 1.64.0, Chromium revision 1248 (Chrome for Testing 156.0.8078.4), downloaded into a scratch
`.browsers/`. Launched with `launch_persistent_context` under the real `run_profile` and `job_profile`, with a live
`egress.Egress`. The only additions to each profile were a read rule for `.browsers/` and a write rule for
`.browser/`.

*What worked*, all with no other rule, flag or environment change, in both sandboxes:
- `example.com` loaded.
- A form on `httpbin.org/forms/post` was filled and submitted, and its echo came back.
- After a relaunch, a cookie with an expiry and a `localStorage` item were still there.
- The egress log recorded every host.

*End to end through the real broker (M1):* a scratch instance's own `run` ran `uv venv` and `uv pip install
playwright==$SHAFT_PLAYWRIGHT` and then the documented launch snippet. It printed "Example Domain" and created
`.browser/Default`. The same snippet in the agent's own sandbox failed: Chromium could not read its resources
(`icudtl.dat not found`, SIGTRAP), which is what D1 wants.

*What M0 showed:*
- **Session cookies** (no expiry) are dropped when the browser closes, as in any browser. Persistent cookies and
  storage survive. A site whose login is a session cookie therefore needs the instance to save and restore it
  (Playwright's `storage_state`). The documentation says so.
- **Loopback and the LAN already go through the proxy.** Playwright adds `<-loopback>` to the bypass list when a
  proxy is given. `page.goto` to `127.0.0.1:8780`, `localhost:8782` and `192.168.1.1` got the proxy's
  `refused: … non-public address`, and each refusal was logged. A public page's `fetch('http://127.0.0.1:8780/…')`
  failed. The sandbox would refuse a direct connection anyway, so there are two walls.
- **Process group.** Playwright puts Chromium in its own process group (pgid ≠ the `run` command's), so `killpg`
  does not reach it directly. But Chromium exited within 3 s in both cases: when the `run` group was killed, and when
  only the Python parent was killed. It quits when its driver's pipe closes. The end-of-cycle sweep stays as a guard
  against a browser the instance deliberately detaches.
- **Nothing outside the instance's folder.** After the runs there was nothing new under `~/Library` or the per-user
  folder matching chrom/playwright/headless/crashpad/google. Playwright's artifacts went to `$TMPDIR`
  (`.tmp/home/playwright-artifacts-*`). The mock keychain is Playwright's default, and nothing new appeared in
  `~/Library`.
- **Sandbox denial logs are not available.** `log show` shows no `sandbox-exec` denials on this macOS, even with
  `(debug deny)`; a known denial (`ls ~/Documents`) was not logged either. So M0 had to check by result, not by log.
  Any denial Chromium hit did not stop it from working.
- **The instance's Python must be uv-managed inside `.uv/`.** A `.venv` whose `python` links to
  `~/.local/share/uv/python` fails with `execvp … Operation not permitted`. `run` already sets
  `UV_PYTHON_INSTALL_DIR=.uv/python`, so a venv made in `run` is right. This was a spike setup slip, not a new rule.
- **Disk:** `chromium-1248` 366 MB, `chromium_headless_shell-1248` 203 MB, `ffmpeg` 3 MB. A profile after a few
  pages: 1.9 MB.
- **Not tested:** the agent's own sandbox (D1 keeps the browser out of it) and a visible window.

The design notes below were written before M0. They stand, with the results above.

- **Chromium's own sandbox.**
  - Its macOS sandbox (Seatbelt) cannot start inside `sandbox-exec`. Playwright already launches with
    `chromiumSandbox: false`.
  - Our profile is then the only wall around the renderer. That is the same wall around everything else the
    instance runs.
- **Keychain.** Without `--use-mock-keychain`, Chromium on macOS stores its cookie key in the *operator's* login
  keychain ("Chromium Safe Storage") through `securityd`, which runs outside our sandbox. That could prompt on their
  screen or write to their keychain.
  - Playwright passes the mock keychain by default, and that is the protection (D2). No probe and no
    security-server deny.
  - An instance that launches without it, through its own flags or a non-Playwright launch, could prompt on the
    operator's screen. The documentation paragraph says to keep the default.
  - M0 still checks directly, not through denial logs, that a normal Playwright launch leaves nothing new in
    `~/Library`: macOS services (cfprefsd, LaunchServices) write on the process's behalf without logging a denial.
- **Temp, crash and cache files.**
  - `TMPDIR` already points inside the instance's folder; M0 checks that Chromium honours it.
  - Crashpad's database goes under the profile.
  - Anything Chromium writes to the closed per-user folder shows up in M0 as a sandbox denial and gets redirected,
    never opened.
- **The proxy.**
  - Chromium on macOS ignores `HTTPS_PROXY`, so Playwright needs `proxy={"server": "http://127.0.0.1:<port>",
    "username": "shaft", "password": <token>}`. All of this is in `HTTPS_PROXY` already.
  - M0 checks Chromium's proxy authentication against `egress.py`: it answers 407 with `Proxy-Authenticate: Basic`,
    which Chromium expects.
  - Direct sockets (QUIC/UDP, WebRTC STUN, DNS prefetch) stay refused by `(deny network*)`. That is wanted: the
    proxy is the only way out, and it logs every host.
- **Where a browser may run: `run` and jobs only** *(D1, decided)*.
  - The agent's sandbox, which every plugin inherits, can reach the model and embedding ports (no credentials) and
    carries the broker token. A page's plain JavaScript could post to `localhost:8780`/`8782`, since Chromium
    bypasses the proxy for loopback. A renderer exploit could also read the broker token and call every broker tool,
    including the operator's granted mail.
  - `run`'s sandbox reaches only the egress proxy and carries no broker token.
  - So `_browser_reads` goes into `run_profile` and `job_profile` only, and the agent's own `profile` cannot read
    `.browsers`. A plugin browses by calling `broker.call("run", command=…)` (plugins may call supervisor tools).
  - A Chromium launched straight from a plugin fails to start, loudly.
  - Jobs keep the broker port and token their plugin needs. A hostile page reaches at most the job's restricted
    broker, which has `OFF_IN_JOBS` tools off and no model.
  - *Rejected alternative:* let plugins launch it in the agent's sandbox, accept the reach above, and document
    `--proxy-bypass-list=<-loopback>`. That is the instance's own flag, which the supervisor cannot enforce.
- **The gate never browses.** The gate's sandbox has no network and does not read `.browsers`, so an instance's tests
  mock the browser.
- **One environment for every sandboxed process.** `run` switches from `self.egress.env()` to
  `self.sandbox_env()`, so the agent, its plugins, `run` and jobs all carry the same `PLAYWRIGHT_BROWSERS_PATH`,
  `SHAFT_PLAYWRIGHT`, `SHAFT_BROWSER_PROFILE` and proxy. Today `run` alone builds its own.
- **Limits:** 300 s per `run` and plugin-call timeouts stay. A browser flow happens inside one call.
  - *Corrected while building (M1):* `run`'s per-file limit was 100 MB. Playwright's bundled `node` is 122 MB, so
    `uv pip install playwright` inside `run` died with SIGXFSZ (exit 153, no message). M0 missed this because it
    installed Playwright outside the sandbox.
  - The limit is now 1 GiB (`broker.RUN_MAX_FILE_BYTES`), which also covers the environment rebuild at wake and
    other big packages (torch).
  - `run` now names SIGXFSZ when a file passes the limit.
  - Regression: `test_browser.FileSizeTest`.

### 4. Telling the instance

- **One paragraph** in the `run` tool description (`broker.py:146`, next to the `.venv` text, which is the
  supervisor's and reaches every instance at once), and in `template/shaft/prompts.py:53` plus
  `template/skills/writing-plugins.md`, which reach instances through `shaft update`. It covers:
  - the shared Chromium and `playwright==$SHAFT_PLAYWRIGHT`;
  - its profile at `$SHAFT_BROWSER_PROFILE` and `launch_persistent_context`;
  - the proxy from `HTTPS_PROXY` passed as Playwright's `proxy=`;
  - that a flow lives inside one call, and a plugin browses through `broker.call("run", …)` (D1);
  - that its logins are its own: no supervisor secret is typed into pages;
  - that it keeps Playwright's default `--use-mock-keychain` (D2);
  - that its site passwords belong in `.browser/`, never in `notes/` or `memory/`, which travel;
  - that session cookies (no expiry) die with the browser, so a session-cookie login is kept with
    `storage_state` saved inside `.browser/`;
  - that replays and exams have no network.
- **No plugin and no skill ship in the template.** Writing them is the instance's work and part of what gets judged.

### 5. Trials, exams and the "never send" rule

- **Replays:** the `ReplayBroker`'s proxy refuses every connection, so a browser in a replay loads nothing and
  submits nothing. There is no profile in the copy either.
- **Exams:** `Broker(network=False)` behaves the same way.
- **Regression test:** a sandboxed fake "browser" (a plain HTTPS client through the proxy, so the test needs no
  Chromium) makes a request in a replay. The test asserts it is refused and recorded. The real Chromium is checked
  once in M0/M4.

## Milestones

| M | What | Acceptance |
|---|---|---|
| **M0** | Throwaway spike, nothing kept. Install the shared Chromium into a scratch `.browsers/`, launch it with a persistent profile under today's `run_profile` and `job_profile` with a live egress proxy, load a public page, submit a form on a public test site, then relaunch and read the cookie back. Record every sandbox denial (`log show --predicate 'sender=="Sandbox"'`) and check `~/Library` directly for anything new. Check whether Playwright detaches Chromium from the caller's process group, and that a `run` timeout kills it. Confirm the launch uses `--use-mock-keychain`. | A list of the rules and environment changes Chromium needs, each tied to an observed denial. A process-group verdict. The plan updated to match. |
| **M1** | `shaft browser install`, `.browsers/VERSION`, `_browser_reads` in `run_profile` and `job_profile`, `.browser/` writable in `run` and jobs, `run` on `sandbox_env()`, the env variables, the M0 sandbox changes, the browser sweep at the end of a cycle, conversation or job, the start-up log line and the panel line. | Tests: `run` and job profiles read `.browsers` and cannot write it; the agent and gate profiles cannot read it; `run` and jobs can write `.browser` and nothing new besides; `run`, a job and the agent carry the same env variables; a process with `--user-data-dir=<root>/.browser` left at the end of a cycle is killed; a missing install produces the log line. The full suite passes. |
| **M2** | `.browser` in `coder.HIDDEN`, `NOT_STATE` and `.gitignore` (template plus existing instances). | Tests against the copies that actually come from the live folder: the coder's scratch copy and the trial gate copy have no `.browser`, and the coder's `list_files`/`read_file` cannot see it. The replay fake-browser request is refused and recorded. |
| **M3** | Documentation: the `run` description, `template/shaft/prompts.py`, `template/skills/writing-plugins.md` and the supervisor README. | The paragraph is accurate against the built code (env names, versions, the `run` route, where passwords go). |
| **M4** | Integration verification with a scratch instance and a synthetic test site. | The instance builds a plugin that passes the gate; a test login persists across cycles; egress hosts are logged; replay refuses browser network activity. |

## Live steps (for the operator's go)

1. Wait until no instance is mid-cycle or replying.
2. Merge `claude/browser` into main and `template-browser` into template.
3. `shaft browser install`: about 570 MB from cdn.playwright.dev, into `.browsers/`.
4. Restart the API (`shaft api off && shaft api on`), since conversations run in its process, and the scheduler
   (`shaft autonomy`), so it logs the browser line and runs jobs on the new code. Restart Telegram too.
5. `shaft update --all`, so every instance gets the template half: `.gitignore`, the `list_files` change and the
   docs.
6. **M4:** message example-b that a browser is available and point it at `skills/writing-plugins.md`. Then watch:
   - it builds its plugin through `run`;
   - the plugin passes the gate;
   - it logs into a site with an account of its own;
   - the next cycle is still logged in;
   - its egress log shows the hosts;
   - a replay of that cycle shows the browser refused.

## M4 design findings (2026-10-19)

- Browser profiles can persist guest cookies without proving authentication. Verify access to a protected test page.
- A bot check can prevent sign-up; report the failure rather than claim a session.
- Replay must refuse browser network writes and leave the live profile unchanged.
- Check sandbox writes and process cleanup, including timeout cleanup.
- Concurrent job-state writes need a lock and a unique temporary filename per process.
  Regression test: `test_jobs.ConcurrentStateTest`.

**Found and fixed in the check:** a replay copy had no `.venv/`.
- `NOT_STATE` leaves `.venv/` out of the copy. Only `PATH` pointed at the real environment, so `.venv/bin/python …`
  failed with "no such file". That is how the 07:51 cycle drove its browser, so a replay of that cycle could never
  start Chromium at all.
- The replayed 08:35 cycle could also tell it was a replay: it printed `NO_VENV` and then tried `uv venv`.
- Now `trial.link_env` links the real `.venv/` and `.uv/` into the copy. They stay read-only: a write and a delete
  through the link are both denied.
- Regression test: `test_trial.ReplaySeesItsRealVenvTest`. It fails on the old code with `NO_VENV`.

**Attribution bug, outside M4:** An agent sent a browser follow-up on the operator's behalf.
The instance later incorrectly attributed that message directly to the operator, then
recorded a conflicting correction. Message provenance must distinguish the sender from
the person on whose behalf it acts. Private message text and archive identifiers are omitted.

## Risks

- **A session cookie is a credential the instance holds.** That is accepted for its own accounts. For an account of
  the operator's, signing in is their deliberate act, and the instance then holds their session in its folder. Two things
  still hold:
  - Nothing outside the instance's own sandbox can reach that session.
  - The mail rule (the operator's mail only through the broker's mail tools) is theirs to keep: signing an instance into
    their Gmail in its browser would go around it. The documentation and the panel line say so. Nothing in the code
    blocks it, since the operator decides.
- **Chromium has no sandbox of its own.** A renderer exploit from a hostile page reaches what the `run` sandbox
  allows: its working folders, the shared folder read-only, and the public internet. It never reaches the operator's
  files, the LAN or any localhost port but the egress proxy. In a job it also reaches the job's restricted broker.
  This is the same boundary as a malicious `pip` package, which is already accepted (instance-network-policy).- **Visible windows.** A headed launch is not blocked: WindowServer is reachable under `(allow default)`. An
  instance could open a window on the operator's screen at night. *My call:* it is not blocked, it is documented as
  "headless unless the operator asked", and M0 records whether it works at all.
- **CAPTCHAs and bot checks.** Some sites will block a headless browser. How the instance deals with that is its
  business. No supervisor code goes around them.
- **Disk.** About 300 MB once for `.browsers`, plus each profile (usually tens of MB, more if it caches heavily).
  The panel line shows profile size.
- **Version drift.** A newer Playwright in an instance's `.venv` with an older shared revision fails at launch, with
  Playwright's own message. `shaft browser install --playwright` moves the shared install forward, and the instance
  pins to `$SHAFT_PLAYWRIGHT`.

## Left to the builder

- The exact flags or env redirects M0 shows are needed.
- The format of the panel line.
- Whether `shaft browser install` keeps older revisions (simplest: remove them; instances pin to `VERSION`).
