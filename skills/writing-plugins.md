# Writing my own plugins (tools)

A plugin is a new tool I give myself. It lives in `plugins/<name>/`:

1. `plugin.json` — `{"name": "<name>", "description": "...", "parameters": {"type": "object", "properties": {...}, "required": [...]}}`. The name must equal the folder name and must not clash with a built-in tool.
2. `main.py` — defines `run(**params) -> str`. Whatever it returns is what I see.

To use the supervisor's tools from a plugin: `from shaft import broker` then `broker.call("web_search", query=...)` or `broker.call("fetch_page", url=...)`. A plugin also reaches the public internet directly, through the supervisor's proxy (HTTPS_PROXY is set). For pages that need a real browser, see the last section.

## Building and testing one in the same run

With the `run` tool (shell commands in my folder; the public internet through the proxy):

- check every plugin the way the supervisor will: `python3 -m shaft.plugin_runner --check`
- try mine with real arguments: `echo '{"text": "hello"}' | python3 -m shaft.plugin_runner <name>`
  (a plugin that calls `broker.call` cannot reach the broker from `run`; test its own logic instead —
  e.g. put that logic in a function and call it from a small script or a test in `tests/`)
- tests I write in `tests/` (`test_*.py`, unittest) also run in the supervisor's gate after my run.

When I have `delegate_coding` (the operator can switch it off for me), anything beyond a few lines
goes to it: an Opus-class coder writes it faster and more reliably than I do on the local model. I
decide what the plugin should do and why, name the files and how to tell it works; the supervisor
applies its change only if all tests pass, and I check the result with `run`. Small edits I still
make myself.

After my run the supervisor checks every plugin (it must load, have a valid plugin.json and a `run`). A broken plugin is reverted and I am told why in my journal. A plugin I write becomes available as a tool in my *next* run, not the current one.

Example: `plugins/web_research/`.

## Running a plugin on a timer (a scheduled job)

Add a `schedule` to its `plugin.json` and the supervisor's scheduler runs it for me, between my cycles:

```json
"schedule": {"every_minutes": 15, "args": {"label": "AssistantAccess"}, "can_wake": true}
```

- `every_minutes`: at least 5. `args`: what `run()` gets. `can_wake`: whether a run may wake me (default false).
- What `run()` returns reaches my inbox as a message from "job <name>"; returning `""` delivers nothing (a quiet
  check stays quiet). To wake me as well, return `{"message": "...", "wake": true}` — only a job with
  `can_wake: true` can; a run that should not wake me returns plain text or `"wake": false`.
- It runs in the `run` sandbox with its own broker: supervisor tools (mail, calendar, http_request with my keys…),
  the internet through the proxy, my own Python environment. No model, and no delegate_coding, trial or
  send_message — the cycle it wakes does those.
- It may write only `.jobs/<name>/` (its own state between runs, e.g. the last id it saw: `SHAFT_JOB_STATE` holds
  the path), `.tmp/` and my browser profile `.browser/`. 120 s and 30 tool calls per run. A run still going when the next comes due means the
  next is skipped. A direct write to `notes/` fails (EPERM) — and a dry run through `run` hides that, because `run`
  may write `notes/`: write only where the job may, and check its first real run in `record(kind='jobs')`.
- **New mail or calendar changes need no polling job:** set `WATCH_MAIL` / `WATCH_CALENDAR` to `inbox` or `wake`
  with `set_config`, and the supervisor puts one line per change in my inbox (and wakes me, at most every 15 min).
- **Handing a file to the cycle:** return `{"message": "...", "files": ["brief.md"]}` (paths in `.jobs/<name>/`,
  at most 5, 256 KB each). They arrive whole beside the message, under `inbox/<message>.files/`. A text longer than
  4,000 characters is cut in the message, and the whole of it is kept there too; the cut says where.
- **Publishing to notes/:** `"publishes": {"notes/briefing.md": "briefing.md"}` in the schedule copies
  `.jobs/<name>/briefing.md` onto `notes/briefing.md` after each successful run in which it changed (targets under
  `notes/` only). This is how a job keeps a note up to date; never route around the write limit with `run` + `cp`,
  which the operator can switch off for jobs (`SHAFT_JOB_RUN_CONFINED`).
- **Long work:** `"long": true` gives a run 15 minutes and 200 tool calls (one long job at a time). Write progress to
  `SHAFT_JOB_CHECKPOINT`; a run that times out after writing it is resumed at the next tick with `SHAFT_JOB_RESUME=1`,
  up to 4 times.
- `record(kind='jobs')` shows my jobs, their runs and errors. After 3 failures in a row I get a message and the job
  backs off until it works again; the operator can switch my jobs off.
- The schedule takes effect once the cycle that wrote it ends and its tests pass.

## Browsing the web with a real browser

For sites that need JavaScript, a login, forms or clicks, I have Chromium. Driving it is my own code: Playwright in
my `.venv`, my own plugin.

- **What the supervisor gives me:**
  - one Chromium shared by every instance (`PLAYWRIGHT_BROWSERS_PATH`, read-only);
  - the Playwright version it belongs to (`SHAFT_PLAYWRIGHT`);
  - my own profile folder, `.browser/` (`SHAFT_BROWSER_PROFILE`).
  If `SHAFT_PLAYWRIGHT` is empty, nothing is installed yet: I ask the operator to run `shaft browser install`.
- **Install, in `run`:** `uv pip install playwright==$SHAFT_PLAYWRIGHT`, and add the same line to `requirements.txt`.
  I never run `playwright install`: the shared copy is read-only, and a different Playwright version cannot find
  its browser.
- **Where it runs:** only in `run` and in my scheduled jobs. A plugin called as a tool runs in my own sandbox, which
  cannot even read the browser, so a launch there fails. A plugin browses by putting the flow in a script and
  calling `broker.call("run", command="python3 plugins/<name>/flow.py ...", timeout_s=100)`. A tool call is stopped
  after 120 s, so a flow must finish well within that.
- **Launching it:**

  ```python
  import os
  from urllib.parse import urlsplit
  from playwright.sync_api import sync_playwright

  u = urlsplit(os.environ["HTTPS_PROXY"])
  with sync_playwright() as p:
      ctx = p.chromium.launch_persistent_context(
          os.environ["SHAFT_BROWSER_PROFILE"], headless=True,
          proxy={"server": f"http://{u.hostname}:{u.port}", "username": u.username, "password": u.password})
      page = ctx.new_page()
      page.goto("https://example.com")
      print(page.title())
      ctx.close()
  ```

  Chromium ignores `HTTPS_PROXY` on its own, so without `proxy=` it reaches nothing. Localhost and the LAN are
  refused by the proxy, as everywhere. Every host I reach shows in `record(kind='egress')`.
- **My profile, `.browser/`, keeps my sessions between runs.**
  - Cookies with an expiry and site storage survive a restart. Session cookies (no expiry) die with the browser:
    for a site that logs in that way, I save `ctx.cookies()` to a file in `.browser/` and put them back with
    `ctx.add_cookies(...)` next time.
  - The folder is never committed, never copied to my coder or to trials, and never read by the judges.
- **Passwords for my own accounts** go in `.browser/` too (e.g. `.browser/accounts.json`), never in `notes/` or
  `memory/`: those go to my mind store and are read by the judges' and the coder's models. The supervisor's keys
  are never typed into a page; they stay with `http_request`.
- **One browser per profile at a time:** a second launch while one is open (my job and my cycle at once) fails with
  the profile in use.
- **Keep Playwright's defaults:**
  - headless;
  - its fake keychain (`--use-mock-keychain`), so nothing touches the operator's keychain;
  - a visible window only when the operator asks for one.
  A browser left running after my run is killed.
- **Trials, exams and my tests:** trial replays and exams have no network, so the browser loads nothing there. My
  tests in `tests/` run in the gate with no network and no browser, so I test the logic around the page with the
  page mocked.
