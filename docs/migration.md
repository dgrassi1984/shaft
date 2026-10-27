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

## What lives where

| What | Where | On the new Mac |
|---|---|---|
| Supervisor code and the template | `main` and `template` branches (also on GitHub) | copied with the folder |
| Instance code | `instance/<name>` branches in `.git/`, **never pushed** | copied with the folder |
| Instance minds (self-model, memory, notes, inbox history) | `.supervisor/minds/<name>.git`, **never pushed** | copied with the folder |
| Instance working files: `memory/`, `notes/`, `logs/`, `self/`, `secrets/` (its own keys), `.browser/` (its browser sessions), `attachments/` | `instances/<name>/`, gitignored | copied with the folder |
| Instance Python environment | `instances/<name>/.venv`, `.uv` | copied; rebuilt at the next wake if you delete it |
| Supervisor credentials | `.env` (mode 600) | copied with the folder |
| Supervisor state: settings, ledgers, judgments, schedule, jobs, trials, Google OAuth tokens (`google/`), paired browsers and the push key (`web/`), Telegram pairing | `.supervisor/` | copied with the folder |
| Capability battery's held-out backup | `~/Library/Application Support/SHAFT/` | **copy separately** |
| Push guard for `instance/*` | `.git/hooks/pre-push` | copied; `bin/shaft setup` reinstalls it |
| launchd services | `~/Library/LaunchAgents/com.shaft.*.plist` | **reinstall** with each service's `on` command |
| Tailnet exposure of the API and web UI | Tailscale's `serve` configuration on the old Mac | **reinstall** with `api on` and `web on` |
| Terminal client, Bonsai, and fine-tuning environments | `.venv-ui`, `.venv-bonsai`, `.venv-ft` | **recreate** |
| Local model packs (about 22 GB) | `.models/` | copy, or download again |
| Shared Chromium | `.browsers/` | **reinstall** with `bin/shaft browser install` |
| Folders shared with an instance | your own folders, referenced by absolute path | must exist at the same path |
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
- Set git `user.name` and `user.email`. The supervisor commits as itself, but
  `new` and your own commits need an identity.
- Install Tailscale and log in to the **same tailnet**. `SHAFT_API_LOGINS` in
  `.env` lists the tailnet logins allowed to use the API and web UI, so those
  logins must keep working.
- For local models, use Apple Silicon (MLX).

## 2. Stop the old Mac

From the project folder on the old Mac:

1. Finish any conversation in progress, and wait until no instance is awake.
   `bin/shaft top` or the web UI's Overview shows each instance's state.
   Stopping the scheduler ends a running wake and records it as stopped. That
   is safe, but you lose the cycle.
2. Commit and push any development branch you want to keep (`.claude/worktrees/`
   and `../shaft-*`). Branches in `.git/` move with the folder. Uncommitted work
   in a worktree outside the folder does not.
3. Stop every service, and take it off the tailnet:

   ```bash
   bin/shaft autonomy off
   bin/shaft telegram off
   bin/shaft web off
   bin/shaft api off
   bin/shaft embed off
   bin/shaft model off
   ```

   `off` stops the service and deletes its plist, so nothing starts again at
   login. Ignore errors from services you never installed.
4. Check that nothing is left:

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
```

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

Keep `.models/` unless you would rather download about 22 GB again.

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
.venv-bonsai/bin/python -m pip install -r .models/bonsai2-27b-mlx/runtime/requirements.txt
bin/shaft browser install
```

If you fine-tune, recreate `.venv-ft` the same way, with the packages you installed in it on the old Mac.

Check the saved supervisor settings for paths and names that belong to the old
Mac. They override the defaults, so the steps below cannot correct them:

```bash
bin/shaft settings
```

Look at `SHAFT_WEB_HOSTS`: if it is set, the web UI answers only those names,
whatever `web on` records. Also look at `SHAFT_FT_PYTHON`, `SHAFT_BONSAI_PYTHON`,
and any other setting whose value is a path. Fix each one with
`bin/shaft settings set KEY=VALUE`, or remove it with `bin/shaft settings unset KEY`.

Check that the instances and their minds arrived intact:

```bash
bin/shaft mind status
bin/shaft status
bin/shaft check <name>
```

`mind status` should show each instance's commit count and date of its last
commit. Compare these with the old Mac. `check` runs the test gate against an
instance's code on this machine.

## 5. Start the services

Run the `on` command for each service you used before, in this order. Each
command regenerates the service's plist with this Mac's Python and paths. Start
the model services first: the API and Telegram can start instance work as soon
as they run. Telegram immediately answers any message it had taken but not
answered when the old Mac stopped. The API writes up idle conversations.

```bash
bin/shaft model on
bin/shaft embed on
bin/shaft api on
bin/shaft web on
bin/shaft telegram on
```

`api on` and `web on` expose the services with `tailscale serve`. `web on` also
records this Mac's tailnet name. Start the scheduler last, after you have
talked to an instance and seen it answer:

```bash
bin/shaft talk <name>
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
- **To keep the old name:** in the Tailscale admin console, rename or remove the
  old Mac first, then give the new Mac its name before you run `web on`. Then
  the address stays the same, and the paired browsers and push key copied in
  `.supervisor/web/` belong to that same address.

Telegram bots, Google grants, and instance-owned accounts move with `.env` and
`.supervisor/`, so they need no further steps.

## If the project path changed

The `.git` files of the worktrees (`template/`, `instances/<name>/`) and git's
records of them contain the old absolute path. Repair them from the project
root before `bin/shaft setup`:

```bash
git worktree repair template instances/*
git worktree prune
```

`repair` prints a `repair: gitdir incorrect: …` line for each link it fixes.
`prune` removes git's records of the development worktrees you did not copy.
Their branches stay in `.git/`. The mind stores need no repair, because the
supervisor gives each one its work tree on every call.

Then delete each instance's Python environment. Its `.venv` points at the
instance's own `.uv` folder by absolute path. The supervisor rebuilds it from
`.python-version` and `requirements.txt` at the next wake:

```bash
rm -rf instances/*/.venv instances/*/.uv
```

Set each shared folder again (`bin/shaft share <name> <folder>`) if your own
folders moved too.

Each instance's record of its approved code (`.supervisor/baselines/<name>.json`)
names the instance folder by absolute path. After a move, the supervisor
ignores the record and falls back to git's view. At its next run, it gates any
approved code that git does not hold, such as ignored or untracked plugin
files. If the tests pass, the code stays. If they fail, the supervisor restores
git's version and keeps the rejected bytes in `.supervisor/rejected/`. Run
`bin/shaft check <name>` before you start autonomy, so a failure shows up while
you are watching. Older records, such as trials, evaluations, and service
versions, keep the old paths as history.

## Afterwards

After a few days of normal cycles on the new Mac, you can delete the old copy.
To keep a backup, archive the same two locations: the project folder and
`~/Library/Application Support/SHAFT/`. Stop the services before you copy, as in
step 2. Copying a running setup can capture a mind store or a ledger in the
middle of a write.
