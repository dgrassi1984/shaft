# Moving SHAFT to a new Mac

You cannot recreate a SHAFT setup from GitHub. GitHub holds `main` and `template`.
Everything that makes your instances what they are exists only in the project
folder on this Mac, plus one folder in `~/Library`. To move a setup:

1. Stop the setup on the old Mac.
2. Copy the folder to the new Mac.
3. Rebuild the parts tied to the machine.
4. Start the setup again.

Never run both Macs at once. Two schedulers would wake the same instances on
both machines, and their branches and minds would diverge. Two Telegram services
would also compete for the same bots.

## Two commands walk you through it

The sections below are the manual route. Two commands run it for you:

```bash
bin/shaft migrate source --to <new-mac> [--check]   # on the old Mac
bin/shaft migrate target [--check]                  # on the new Mac, from the copied folder
```

`source` stops the old Mac, freezes the environments and pushes the copy over
SSH, so turn on **Remote Login** on the *new* Mac first (the reverse of the
manual route, which pulls from the old one). `target` repairs the paths, rebuilds
the environments and starts the services. Before starting, it asks you to confirm
that the old Mac is stopped and stays stopped. It removes instance environments;
the first `talk` rebuilds them on this Mac. Run `target` in **Terminal on the new
Mac**, logged in at its screen — not over SSH — because the services load into the
logged-in session and `on` records that terminal's `python3`. Run `--check` first
on either: it runs the read-only checks and changes nothing. Each step prints what
it will do and why, and waits — Enter runs, `q` stops, and a rerun resumes at the
first unfinished step. If `source` stops partway it prints how to bring the old
Mac back (safe only while `target` has not started). External disks and Migration
Assistant stay manual (below).

## What lives where

| What | Where | On the new Mac |
|---|---|---|
| Supervisor code and the template | `main` and `template` branches (also on GitHub) | copied with the folder |
| Instance code | `instance/<name>` branches in `.git/`, **never pushed** | copied with the folder |
| Instance minds (self-model, memory, notes, inbox history) | `.supervisor/minds/<name>.git`, **never pushed** | copied with the folder |
| Instance working files: `memory/`, `notes/`, `logs/`, `self/`, `secrets/` (its own keys), `.browser/` (its browser sessions), `attachments/` | `instances/<name>/`, gitignored | copied with the folder |
| Instance Python environment | `instances/<name>/.venv`, `.uv` | copied; rebuilt at the next wake if you delete it |
| Supervisor credentials | `.env` (mode 600) | copied with the folder |
| Supervisor state: settings, ledgers, judgments, schedule, jobs, trials, Google OAuth tokens (`google/`), paired browsers and native apps, push tokens and APNs signing key (`web/`), Telegram pairing | `.supervisor/` | copied with the folder |
| Capability battery's held-out backup | `~/Library/Application Support/SHAFT/` | **copy separately** |
| Push guard for `instance/*` | `.git/hooks/pre-push` | copied; `bin/shaft setup` reinstalls it |
| launchd services | `~/Library/LaunchAgents/com.shaft.*.plist` | **reinstall** with each service's `on` command |
| Tailnet exposure of the API and web UI | Tailscale's `serve` configuration on the old Mac | **reinstall** with `api on` and `web on` |
| Terminal client, Bonsai, and fine-tuning environments | `.venv-ui`, `.venv-bonsai`, `.venv-ft` | **recreate** |
| Local model packs | `.models/` | copy, or download again |
| Codex CLI | `/Applications/ChatGPT.app` (`SHAFT_CODEX_BIN`) | **install** ChatGPT if any instance uses a `codex/*` model; its login in `.supervisor/codex-home/` is copied with the folder |
| Shared Chromium | `.browsers/` | **reinstall** with `bin/shaft browser install` |
| Folders shared with an instance | your own folders, referenced by absolute path | **copy separately**, to the same path |
| Development worktrees | `.claude/worktrees/`, `../shaft-*` | push their branches first |

The plists and the virtual environments contain absolute paths: the Python
interpreter that installed them and the project folder. Copying them works only
when both paths are the same on the new Mac. Regenerating them is always safe,
so do that instead.

## 1. Prepare the new Mac

- Use the same macOS user name if you can, and the same folder path
  (`~/Development/shaft` here). Every step then works unchanged. If a path
  changes, follow [the path changed](#if-the-project-path-changed).
- Install the requirements from the [README](../README.md#requirements):
  Python 3.12 or later as `python3`, git, and `uv`.
  `uv` and `git` must be in `/opt/homebrew/bin` or `/usr/local/bin`.
  Homebrew does this. Services run under launchd with
  `PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`. Installing `uv` in
  `~/.local/bin` works in a terminal but fails under launchd.
  Check `python3 --version` first. `bin/shaft` runs whichever `python3` is on
  your PATH, and the `on` commands record that interpreter in the plists.
  Homebrew's plain `python3` may be newer than the one you used before.
  To use `python@3.12`, put `/opt/homebrew/opt/python@3.12/libexec/bin` first
  on your PATH.
- On the old Mac, run `brew bundle dump --file=Brewfile` in the project
  folder to record your Homebrew packages. The file travels with the copy.
  On the new Mac, `brew bundle --file=Brewfile` installs them. This matters
  when instances use tools such as ffmpeg or pandoc.
- Set git `user.name` and `user.email`. The supervisor commits as itself, but
  `new` and your own commits need an identity.
- Install Tailscale and log in to the **same tailnet**. `SHAFT_API_LOGINS` in
  `.env` lists the tailnet logins allowed to use the API and web UI, so those
  logins must keep working. Install it at `/Applications/Tailscale.app`, or
  put `tailscale` on the same launchd PATH above.
- If any instance uses a `codex/*` model, install ChatGPT in `/Applications`.
  The Codex CLI lives inside it. `SHAFT_CODEX_BIN` overrides this location.
- Local models need Apple Silicon (MLX). The model server's defaults assume
  a 64 GB Mac: `SHAFT_PROMPT_CACHE_GB=8` on top of about 8 GB of weights.
  On a Mac with less memory, lower it with
  `bin/shaft settings set SHAFT_PROMPT_CACHE_GB=<gb>`.
  Instances that use cloud, Codex or self-hosted models need none of this.

### If the new Mac is a laptop

Turn on FileVault. The folder stores `.env`, Google tokens and instance keys
unencrypted.

SHAFT never keeps the Mac awake. It has no `caffeinate` or power handling.
While the Mac sleeps, nothing runs: no cycles, Telegram replies, web UI or API.
On waking, each instance gets at most one catch-up wake, however many it missed.
The nightly sleep catches up only later on the same day. Its time is 04:00
(`SLEEP_AT` in `supervisor/autonomy.py`). If the Mac sleeps through that time
and wakes after midnight, the next nightly sleep runs at that day's 04:00.

Keep it on power, with `sudo pmset -c sleep 0`. Closing the lid still sleeps
it unless it is in clamshell mode, with power and an external display.
On battery it sleeps as normal.

## 2. Stop the old Mac

From the project folder on the old Mac:

1. Pause first, so that nothing new starts: `bin/shaft pause`. With several
   instances, their cycles can overlap, so waiting for none to be awake may
   never work. A pause starts no wake, sleep, job, battery, warm-up or trial,
   and lets running ones finish. Finish any conversation in progress, then
   wait until nothing is left in the scheduler's process group. This must
   print nothing:

   ```bash
   pgrep -lf -g "$(ps -o pgid= -p "$(pgrep -f 'supervisor autonomy serve')" | tr -d ' ')" | grep -v 'autonomy serve'
   ```

   The pause flag is copied with the folder. The new Mac deliberately starts
   paused until `bin/shaft resume` in section 5.
2. Commit and push any development branch you want to keep (`.claude/worktrees/`
   and `../shaft-*`). Copying the project folder does not include worktrees
   outside it, such as `../shaft-*`. Their commits are in `.git/` and move
   with the folder; only uncommitted work in those worktrees is lost.
3. Record the environments you will recreate, because they may not have a
   requirements file to reinstall from. Run each command only if that
   environment exists:

   ```bash
   .venv-bonsai/bin/python -m pip freeze > venv-bonsai.txt
   .venv-ft/bin/python -m pip freeze > venv-ft.txt
   ```

   These files go in the project folder and travel with the copy.
4. Stop every service, and take it off the tailnet:

   ```bash
   bin/shaft autonomy off
   bin/shaft telegram off
   bin/shaft web off
   bin/shaft api off
   bin/shaft embed off
   bin/shaft model off
   ```

   `off` stops the service and deletes its plist, so nothing starts again at
   login. `autonomy off`, `api off`, `embed off` and `model off` refuse while
   a run needs them: they print `not now: …`, exit 1, and stop nothing.
   Adding `--force` stops the service anyway and leaves the runs unfinished.
   A service you never installed is not an error to worry about, but a
   `not now:` line is. That service is still running. Do not continue until
   it is stopped. The next step catches this.
5. Check that nothing is left:

   ```bash
   launchctl list | grep com.shaft
   ```

   This should print nothing.

Do not delete anything on the old Mac yet. It is your backup until the new one
has run for a few days.

## 3. Copy the folder

Copy the whole project folder, including the hidden folders. Use `rsync -a`
because it keeps permissions, which matters for `.env` and
`.supervisor/minds/`. Over the network, enable Remote Login on the old Mac
(System Settings → General → Sharing), then run this on the new Mac:

```bash
mkdir -p ~/Development
rsync -a old-mac.local:Development/shaft/ ~/Development/shaft/
rsync -a 'old-mac.local:Library/Application\ Support/SHAFT/' ~/Library/Application\ Support/SHAFT/
rsync -a 'old-mac.local:Documents/Shared/' ~/Documents/Shared/
```

The third command is an example. Copy each folder you shared with an instance
to the same path. `bin/shaft status` lists each instance's "may read" folder.
On first read, macOS may ask permission for folders such as `~/Documents`.

Replace `old-mac.local` with the old Mac's name; its tailnet name also works.
The macOS `rsync` (openrsync) passes a remote path through the old Mac's shell,
so the space in `Application Support` is escaped inside the quotes. To use an
external disk, run the same `rsync -a` commands through the disk. The trailing
`/` on each source matters.

To save time and space, skip the parts the new Mac rebuilds:

```bash
rsync -a --exclude /.venv-ui --exclude /.venv-bonsai --exclude /.venv-ft --exclude /.browsers \
  old-mac.local:Development/shaft/ ~/Development/shaft/
```

Keep `.models/` unless you would rather download the packs again.

Migration Assistant also works, because it copies the folder and the
`~/Library` backup. You still need steps 4–6. Step 2 deleted the plists, so
Migration Assistant finds none to copy. If you skipped step 2, the copied
services start at your first login with the old Mac's paths. In that case, run
`launchctl list | grep com.shaft` on the new Mac and run `off` for each service
it lists before you continue.

## 4. Rebuild on the new Mac

From the project folder on the new Mac:

```bash
cd ~/Development/shaft
bin/shaft setup
```

Every line should say `ok`, followed by `guarded …`. A `created` line means a
worktree was missing, so check the copy before you continue.

Then remove git's records of development worktrees you did not copy, even if
the project path stayed the same. Their branches stay in `.git/`:

```bash
git worktree prune
```

Recreate the terminal client's environment. Use `--clear`, because a copied
environment keeps links to the old Mac's Python:

```bash
python3 -m venv --clear .venv-ui
.venv-ui/bin/python -m pip install -r requirements-ui.txt
```

Optionally, put the launcher on your PATH:

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/bin/shaft" ~/.local/bin/shaft
```

If you used local models, the shared browser, or memory search, rebuild them too.
See [local models](local-model.md) and [the browser](integrations.md#a-real-browser-shaft-browser-).

```bash
python3 -m venv --clear .venv-bonsai
.venv-bonsai/bin/python -m pip install -r venv-bonsai.txt
bin/shaft browser install
```

Use the freeze from section 2. The embedding service also runs on
`.venv-bonsai`, so you need it for memory search even with no local model.
The pack's `runtime/requirements.txt` works only when `.models/bonsai2-27b-mlx/`
exists, so do not rely on it.

If you fine-tune, recreate `.venv-ft` from its freeze too:

```bash
python3 -m venv --clear .venv-ft
.venv-ft/bin/python -m pip install -r venv-ft.txt
```

Check the saved supervisor settings for paths and names that belong to the old
Mac. They override the defaults, so the steps below cannot correct them:

```bash
bin/shaft settings
```

Look at `SHAFT_WEB_HOSTS`: if it is set, the web UI answers only those names,
whatever `web on` records. Also look at `SHAFT_FT_PYTHON`, `SHAFT_BONSAI_PYTHON`,
`SHAFT_CODEX_HOME`, `SHAFT_CODEX_BIN`, `SHAFT_FT_BASE_MODEL`, and any other
setting whose value is a path. Fix each one with
`bin/shaft settings set KEY=VALUE`, or remove it with `bin/shaft settings unset KEY`.

Check that the instances and their minds arrived intact:

```bash
bin/shaft mind status
bin/shaft status
```

`mind status` should show each instance's commit count and date of its last
commit. Compare these with the old Mac.

## 5. Start the services

Run the `on` command for each service you used before, in this order. Each
command regenerates the service's plist with this Mac's Python and paths. Start
the model services first: the API and Telegram can start instance work as soon
as they run. Telegram immediately answers any message it had taken but not
answered when the old Mac stopped. The API writes up idle conversations.
Run `model on` only if the model pack is present at `.models/bonsai2-27b-mlx/`.

```bash
bin/shaft model on
bin/shaft embed on
bin/shaft api on
bin/shaft web on
bin/shaft telegram on
```

`api on` and `web on` expose the services with `tailscale serve`. `web on` also
records this Mac's tailnet name. `web on` refuses without `SHAFT_API_LOGINS`
in `.env`. That setting is copied with `.env`, but if your tailnet login
changed, fix it there first. `api on` records it only when it is empty.

If any instance uses a `codex/*` model, check its login before the scheduler:

```bash
bin/shaft codex status
```

If it shows you signed out, run `bin/shaft codex login`. We have not tested
whether a copied Codex login keeps working on another machine. If Codex cannot
start, a live run falls back to `SHAFT_CODEX_FALLBACK_MODEL` without stopping.
That costs API dollars.

Talk to every instance and see it answer before you start the scheduler.
`talk` works while paused. For instances that use a self-hosted model
(`SHAFT_SELF_HOSTED`), this also proves the server is reachable from the new Mac:

```bash
bin/shaft talk <name>
bin/shaft check <name>
```

Run `check` after `talk`: it does not rebuild a deleted `.venv`, so it needs the
environment `talk` rebuilds. After every instance has answered and checked,
resume and start the scheduler:

```bash
bin/shaft resume
bin/shaft autonomy on
bin/shaft autonomy status
```

## 6. Reconnect your devices

The web UI's address uses the Mac's tailnet name
(`https://<mac>.<tailnet>.ts.net:8444`). `bin/shaft web on` prints it.

- **If the name changed:** browsers and the Home Screen app on your phone or iPad
  treat it as a new site. Open the new address, add it to the Home Screen
  again, pair it (`bin/shaft web pair --name phone-app`, as in
  [the web UI](web-and-api.md#the-web-ui-shaft-web-on)), and turn notifications
  on again. Remove the old Home Screen app. Point API clients at the new
  API endpoint, which `bin/shaft api status` prints
  (`https://<mac>.<tailnet>.ts.net:8443/v1`). The API key (`SHAFT_API_KEY`) is unchanged.
  Pair native apps again too: run `bin/shaft web pair --name <app>` and paste
  the link into the app. They keep the server address from the pairing link.
  Update inbox doorbell callers too. Anything calling
  `https://<mac>.<tailnet>.ts.net:8443/v1/inbox/<instance>` needs the new name.
  `SHAFT_INBOX_KEY` is unchanged.
- **To keep the old name:** in the Tailscale admin console, rename or remove the
  old Mac first, then give the new Mac its name before you run `web on`. Then
  the address stays the same. Paired browsers, native apps, API clients,
  doorbell callers and the push key all keep working.

Telegram bots, Google grants, and instance-owned accounts move with `.env` and
`.supervisor/`, so they need no further steps, with one exception. If you set
up a Web application OAuth client for Google (`GMAIL_<CLIENT>_WEB_CLIENT_ID`
in `.env`) and the name changed, register the new redirect URI in Google Cloud:
`https://<mac>.<tailnet>.ts.net:8444/google/callback`. Existing grants keep
refreshing either way. Terminal logins use a local address.

## If the project path changed

The `.git` files of the worktrees (`template/`, `instances/<name>/`) and git's
records of them contain the old absolute path. Repair them from the project
root before `bin/shaft setup`:

```bash
git worktree repair template instances/*
```

`repair` prints a `repair: gitdir incorrect: …` line for each link it fixes.
The mind stores need no repair, because the supervisor gives each one its
work tree on every call.

Then delete each instance's Python environment. Its `.venv` points at the
instance's own `.uv` folder by absolute path. Delete `.tmp` too, because
`.tmp/home/python3` can be a link to the old Python. The supervisor rebuilds
the environment from `.python-version` and `requirements.txt` at the next run:

```bash
rm -rf instances/*/.venv instances/*/.uv instances/*/.tmp
```

Run `bin/shaft talk <name>` first, then `bin/shaft check <name>` for each
instance. `check` does not rebuild a deleted `.venv`. It falls back to the
system Python, so tests that import the instance's packages fail.

Set each shared folder again (`bin/shaft share <name> <folder>`) if your own
folders moved too.

Each instance's record of its approved code (`.supervisor/baselines/<name>.json`)
names the instance folder by absolute path. After a move, the supervisor
ignores the record and falls back to git's view. At the first run after the
move, a `talk` or a wake, it re-gates code that git shows as uncommitted:
changed tracked files and untracked files. Git-ignored code files are not
gated. They enter the new record as they are. If tests fail, the supervisor
restores the approved version and keeps the rejected bytes in
`.supervisor/rejected/`. `check` does not perform this step; it only runs the
tests. Older records, such as trials, evaluations, and service versions, keep
the old paths as history.

## Afterwards

After a few days of normal cycles on the new Mac, you can delete the old copy.
To keep a backup, archive the same two locations: the project folder and
`~/Library/Application Support/SHAFT/`. Stop the services before you copy, as in
step 2. Copying a running setup can capture a mind store or a ledger in the
middle of a write.
