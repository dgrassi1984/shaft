# Integrations

These grants expose content to instances and their model providers. Read the
[security policy](../SECURITY.md) and [enforced rules](safety.md) before granting access.
The name `example` below is a placeholder for an instance you created.

## Telegram (`shaft telegram …`)

One bot per instance, one paired Telegram account for all of them.

1. Create a bot per instance with @BotFather, then `shaft telegram token example` (the token is read without
   echo, checked with Telegram, and kept in `.env` as `TELEGRAM_TOKEN_EXAMPLE`; never visible to an instance).
2. `shaft telegram pair` prints a one-time code (10 minutes); send `/pair <code>` to any of the bots from your
   account. From then on every bot answers that account only; anyone else is ignored and logged. Pairing
   another account needs `shaft telegram unpair` in your terminal.
3. `shaft telegram on` (a launchd service, shown in `shaft top` then services).

Writing to a bot is a live conversation with its instance, through the API (`shaft api on` is needed): a
running cycle is paused only while the reply is written, and the conversation is written into the journal after
`SHAFT_IDLE_MINUTES`. A message you send while it is still replying joins that reply at its next step (it
does not wait for the reply to end); one that arrives once the answer is already being written is answered right
after it. Replying to one of its messages quotes it, and answers it if it was a question. Files and
photos you send a bot are saved in that instance's `attachments/from-operator/…` and the turn tells it where;
files an instance attaches to a message for you arrive after its text (pictures as photos). `/new`
starts a new conversation (not while a reply is running), `/wake` starts a cycle, `/status` says what it is doing. A message an
instance sends you (`send_message` to the operator) arrives from its bot when it is routed to Telegram (see
[Where an instance reaches you](#where-an-instance-reaches-you-shaft-channel)). The service long-polls Telegram; no port,
webhook or Funnel. A message it has taken is kept in `.supervisor/telegram.json` until its answer is delivered — one sent
while a reply runs, until that reply has heard it or its own reply is sent — so one that arrives as the service stops is
answered when it starts again; a message whose reply had already come back is answered from the API's record of
it, not run again (`X-Shaft-Request-Id`, [web-and-api.md](web-and-api.md)), and so are the messages that reply heard.
An answer Telegram will not take now (a network error, 429, 5xx) is sent again after 2 s, 10 s, 1 min and 5 min, then
kept for the next start; one it refuses for good (400 chat not found, 403 the bot was blocked) is logged as FAILED
and not tried again. A message you send while an answer is being sent again goes into the follow-up reply, kept with
it; each request is kept with the exact messages it sent, so one answered again after a restart sends them all. A
message whose handling fails is tried again at the next start, three starts at most; then it is set aside
(`quarantined` in `.supervisor/telegram.json`) and logged as FAILED, an alert on the panel. Full text both ways: bot chats are not end-to-end encrypted, so Telegram's servers see them
.

Source: [Telegram transport and pairing](../supervisor/telegram.py).

## Where an instance reaches you (`shaft channel …`)

Every message an instance sends you (`send_message` to the operator) lands in your inbox, which the web UI shows,
and raises a banner on the Mac. It also notifies you on **one** channel, where you pick the conversation up:

- **The web UI**: a push to every device with notifications on (⋯ then **Notify me on this device**). It opens the
  instance's chat, where the message shows under the conversation with a Reply button; quoting a question there
  answers it.
- **Telegram**: from the instance's own bot. Replying to it answers a question.

Which one, decided when the message is sent:

1. A conversation with the instance still open on the web or Telegram: that one.
2. Otherwise your setting for the instance: `shaft channel <name> web` or `telegram`, or the **Access** page's
   *Where it reaches you*.
3. `auto` (the default): where you last wrote to the instance, web or Telegram. With no conversation yet: Telegram if
   its bot reaches you, else the web.

A channel that cannot notify you now gives way to the other: Telegram with no bot, not paired or its service off;
the web with its service off or no device with notifications on. `shaft channel <name>` shows the setting and where a
message would go now; each message's page in the web UI says where it notified you and why. The instance's
`send_message` tool tells it the same.

Source: [channel routing](../supervisor/channel.py), [the web UI's pushes](../supervisor/web_alerts.py).

## Google accounts (`shaft google …`)

Mail and Calendar share your Google accounts ([PLAN-calendar.md](plans/PLAN-calendar.md)). An instance never holds a
Google credential: these scopes cannot be limited to one label or one calendar, so the supervisor keeps each account's
refresh token in `.supervisor/google/<alias>.json` (0600) and enforces every grant itself. One login per account asks
for `gmail.readonly`, `calendar.readonly`, and `calendar.events` together and records the scopes Google actually granted
(the consent page lets you untick one): `shaft google` lists them. A tool whose scope was not granted refuses, naming
the login to run again; it never returns an empty result. The operator's mailbox access stays read-only; calendar
writes require per-instance write grants ([calendar write plan](../docs/plans/PLAN-calendar-write.md)).

Before re-login, add `https://www.googleapis.com/auth/calendar.events` in Google Cloud → Google Auth Platform →
Data Access for each OAuth project. Then run `shaft google login <alias> <address>` again for each operator account
that needs calendar writes (with `--client <name>` for its OAuth client, if needed).

Each account records the OAuth client it logged in with (a refresh token works only with the client that issued it).
A client is named in `.env` as `GMAIL_<CLIENT>_CLIENT_ID` / `GMAIL_<CLIENT>_CLIENT_SECRET`; `--client` picks it, and
it defaults to the alias. One client per Google organisation, because an **Internal** app signs in only its own
organisation's users:
Create a Desktop app OAuth client in a Google Cloud project with the APIs and scopes
required by the grant. Use an Internal consent audience for an eligible Workspace
organisation, or configure an External audience for personal accounts. Follow the
current Google consent-screen requirements; token lifetime and verification rules
are provider policy, not guarantees made by SHAFT.

```bash
shaft google login work operator@work.example
shaft google login home operator@example.com --client personal
```

For **Log in again** on the web Access page, create a separate OAuth client of type **Web application** in the
same project as the account's desktop client. Register the authorized redirect URI `<web.url()>/google/callback`,
substituting the web UI's actual HTTPS tailnet URL. Access shows the exact URI to register when setup is missing.
Set `GMAIL_<CLIENT>_WEB_CLIENT_ID` and `GMAIL_<CLIENT>_WEB_CLIENT_SECRET` in `.env`, with the same `<CLIENT>` as
the account's desktop client. Replace hyphens with underscores and use uppercase in environment names.
Add any missing scopes in [Google Auth Platform → Data Access](https://console.cloud.google.com/auth/scopes)
before logging in again, especially `https://www.googleapis.com/auth/calendar.events` for write grants.
There is no fallback to another client. Web consent records the client as `<client>:web`; refresh resolves that
suffix to the web credential names, because a refresh token must use the client that issued it.
The CLI re-login command still names the base desktop client and can replace a web login with a desktop login.

Google's quotas are per user per minute, shared by every instance on the account. A call over
quota waits up to 60 s, then fails, saying so. Accounts logged in before Calendar (PLAN-mail) kept their files in
`.supervisor/mail/`: `shaft google migrate` moves them (until then they are not read, and the error says so).

Source: [Google account login](../supervisor/google.py).

## Mail (`shaft mail …`)

These operator-mail tools remain read-only under the [action class model](safety.md#operator-action-classes-142).
They still check the granted label on each thread. There is no operator mail-send broker tool.

An instance you pick may read the Gmail threads that carry a label you pick ([PLAN-mail.md](plans/PLAN-mail.md)), on an
account logged in with `shaft google login`. The search filters on the label's ID, and every thread it returns,
reads or downloads from is checked again before anything of it is shown. A thread is visible while any of its
messages carries the label; then all its messages are, later replies included. Drafts, trash and spam are left out.

One instance may read several accounts: grant a label on each; `mail_search` covers every grant unless told one.
`shaft mail example grant work AssistantAccess` checks that the label exists and saves the grant in
`.supervisor/tools/example.json`. `shaft mail example revoke work AssistantAccess` or `shaft mail example off` holds from example's
next mail call. A label renamed or deleted in Gmail is refused until you grant it again. Up to 30 mail calls per run; a continuation page of `mail_read` (message or offset past the start) counts as a call but not against those 30.
`mail_read(id=…)` returns pages of at most 6,000 characters, including headers and footer, so the agent's tool-result
cap preserves the whole page. Follow the footer's exact `mail_read(id=…, message=…, offset=…)` call until
`End of thread`. `message` starts at 1, oldest first; `offset` is a character offset in that message's text,
default 0. Long messages continue across pages without losing text. Each continuation repeats the message headers
and sender marking, and each page checks the granted label again. The footer counts remaining messages, including
any partly read message, and remaining body characters.
Downloads go to `mail/<alias>/<thread>/` in the instance's
folder (gitignored by the template; a download is refused until `shaft update` brings that rule). Limits: 25 MB per
file, 100 MB per run. Each body is shown under a line saying who wrote it and that it is not your instruction.
Trial replays read the same mail, as of the replayed cycle, shared by the pair. Battery exams get none.

The risks you accept by granting a label: anything an instance reads it can send anywhere (it has the internet).
Mail content reaches model providers (a cloud-brained instance, the judges). Senders can write instructions into
mail (prompt injection). What the instance writes from it into its mind stays in its private mind store's history (never in the project's
git), and downloads stay in `mail/`, after a revoke.

Source: [mail grants and reads](../supervisor/mail.py).

## An instance's own Gmail (`shaft secret <name> google …`)

An instance may have a Google account of its own, such as `assistant@example.com`
([PLAN-own-mail.md](plans/PLAN-own-mail.md)). It reads the whole mailbox and sends from it through its own plugin, calling the
Gmail API with `http_request(auth=…)`. The supervisor holds the login as a **Google key** in the instance's key store
(`secrets/`, never readable by the instance). Each run, the broker makes an access token from the key's refresh token,
adds it like any key and only to the key's hosts, and masks it in answers. Sign it in once, in a browser signed in as
that account:

    shaft secret example google gmail assistant@example.com --client personal

- The scope is `gmail.modify`: read, send, label, archive, trash. It cannot delete
  permanently, change settings, forwarding or filters, or reach anything outside Gmail. The key is bound to
  `gmail.googleapis.com`. The `personal` client's Data Access list must include `gmail.modify`.
- **Never one of your accounts.** An address logged in with `shaft google` is refused, at sign-in and on every use.
  Your mail reaches instances only by label (`shaft mail`).
- The instance can delete the key (`secret_delete`), but `secret_set` cannot replace it. A refused refresh names the
  command to sign it in again.
- Trial replays never send (the POST is recorded, not carried out), and a pair shares its reads. Battery exams make
  no Google token, and never write with any of an instance's keys.

The risks you accept: it sends as itself to anyone, and anyone can write to it (prompt injection with a send button;
every send is in `broker.log`). Google may limit automated sending. Do not forward your own mail there: it would be read whole, with no label. Mail content reaches
model providers and the instance's mind store, as with labels.

Sources: [scope and host bindings](../supervisor/google.py),
[own-account login](../supervisor/google.py).

## An instance's own Drive, Sheets, Docs and Slides (`shaft secret <name> google … --scope drive`)

The same account's files ([PLAN-own-drive.md](plans/PLAN-own-drive.md)): a second Google key on the instance's own account,
used the same way (its own plugins, `http_request(auth="drive")`). Sign it in once, as that account:

    shaft secret example google drive assistant@example.com --client personal --scope drive

- The `drive` bundle is four scopes, all granted or nothing saved: `drive` (every file in its Drive and every file
  shared with it), `spreadsheets`, `documents`, `presentations`. The key is bound to `www.googleapis.com` (Drive v3
  and its uploads), `sheets.googleapis.com`, `docs.googleapis.com` and `slides.googleapis.com`. The `personal`
  client's project must have the four APIs enabled and the four scopes in Data Access; configure the consent audience for the intended use.
- Binary files go through `http_request`'s `body_file` (upload) and `save_to` (download).
- The same guards as its Gmail: never one of your accounts, refreshed by the supervisor, trial replays never write
  (a `save_to` in a replay lands in the replay's copy), battery exams make no token.

The risks you accept: it can share any of its files with anyone, or with anyone who has the link (logged in
`broker.log`, no allowlist). Whatever you share with it, it reads whole, and as an editor it can change or trash it:
share as Viewer when it need not edit. Files shared with it by others are written by them (prompt injection). Unlike
`gmail.modify`, `drive` can delete its own files permanently. Storage and sending quotas depend on the Google account.

Source: [Drive scope bundle](../supervisor/google.py).

## Calendar (`shaft calendar …`)

For instances in `SHAFT_ACTION_INSTANCES` (default `thelos`), the [action classes](safety.md#operator-action-classes-142)
preserve the initial calendar behavior: changes that tell nobody run directly; supplied attendees, invitations,
incomplete attendee lists and RSVPs require proposals. An over-reach can make direct changes require approval or
make a staged class refused. `shaft actions <name>` shows class state, history and proposals; `restore <class>`
restores the initial rule. All classes share the existing `/calendar/proposals` queue, expiry and approval checks.
Other instances keep their current calendar behavior.

An instance you pick may read the calendars you pick, one per grant ([PLAN-calendar.md](plans/PLAN-calendar.md)), with full
event details: title, time and time zone, location, description, organiser, attendees and their responses, your
response, conference link, recurrence, attachment links (never downloaded). Private events on a granted calendar are
shown; declined ones are shown and marked. `shaft calendar calendars work` lists the account's calendars;
`shaft calendar example grant work primary` (or a title, or an ID) resolves it once and saves its ID with its title in
`.supervisor/tools/example.json`. A title shared by two calendars is refused, both listed: grant by ID. `revoke` and
`off` hold from the instance's next calendar call. `grant-write` adds write access to one calendar and implies read;
`revoke-write` keeps read access. See [calendar write safety](safety.md) and
[calendar write plan](../docs/plans/PLAN-calendar-write.md) for direct writes and operator-approved proposals.

Every Calendar API call names a granted calendar in its path, and nothing an instance calls spans calendars (no
free/busy, no calendar list). An event's id is `<alias>/<hash of the calendar>/<event>`, resolved against the
instance's grants as they are then, so an event of another calendar cannot be addressed at all. The tools:
`calendar_events` (a window, default now to 7 days, at most 92; optional free-text query; up to 100 events, the cut
said) and `calendar_event` (one event in full; descriptions cut at 10,000 characters, the cut said). Up to 30
calendar calls per run. An event's description and location under an organiser other than you come under a line
saying who wrote it and that it is not your instruction. Trial replays read the calendar live, shared by the pair,
with events created after the replayed cycle hidden (an older event edited since shows its edit; the judge is told).
Battery exams get none.

The risks are mail's, plus other people's data: attendees' names and addresses, and invitations they wrote, reach
the instance, its model providers and the judges.

Source: [calendar tools](../supervisor/calendar.py).

## Scheduled jobs (`shaft jobs …`)

An instance can run one of its own plugins on a timer ([PLAN-jobs.md](plans/PLAN-jobs.md)), for example a watcher that
checks the tagged mail every 15 minutes. It declares the job in the plugin's own manifest:
`"schedule": {"every_minutes": 15, "args": {…}, "can_wake": true}` in `plugins/<name>/plugin.json`. A cycle
writes it, the gate tests it, and the commit records it. No launchd job is installed: SHAFT's scheduler service runs
due jobs as children.

Each run happens in the `run` sandbox, with its own broker and egress proxy:
- supervisor tools, mail and calendar grants, the instance's keys, its Python environment and the internet;
- no model, and no `delegate_coding`, `trial` or `send_message`;
- writes only to `.jobs/<plugin>/` (gitignored state), `.tmp/` and its browser profile `.browser/`;
- at most 120 s and 30 tool calls, at least 5 minutes apart (`SHAFT_JOB_MIN_MINUTES`), at most 5 jobs per
  instance.

What a run returns lands in the instance's inbox, from "job <plugin>". An empty return delivers nothing. A job with
`can_wake: true` whose run returns `{"message": …, "wake": true}` starts a cycle as soon as the instance is idle.
Several requests while it is awake make one wake, there is no rate limit, and a paused instance is never woken.

A run still going when the next comes due means the next is skipped, and the skip is logged. After 3 failures in a
row the instance gets one message and the job backs off (its interval doubles, up to a day). Every run is logged in
`.supervisor/jobs/<name>.jsonl`. `shaft jobs`, the panel's and `shaft top`'s *jobs* row, and the instance's own
`record(kind='jobs')` show them. Jobs are on by default; `shaft jobs <name> off` stops an instance's jobs. They
never run during a battery exam, nor in trial replays.

Source: [jobs execution and scheduling](../supervisor/jobs.py).

## Images

Instances can see images ([PLAN-vision.md](plans/PLAN-vision.md)). There are two ways:
- **Native.** If the instance's model accepts images, a photo you attach on Telegram reaches its model with your
  message, and its `view_image` tool shows it an image file in its folder (`attachments/`, `mail/`, `shared/`).
- **Described.** Every instance has the supervisor tool `describe_image(path, question)`. A vision model
  (`SHAFT_DESCRIBE_MODEL`, default `claude-haiku-5-5`) answers in text, marked as described rather than seen. This
  is how an instance whose model cannot see images (shaft on Bonsai) learns what is in one, and the cost counts
  toward its daily cloud cap. At most 10 calls per run.

Which models see images is learned, not listed:
- Claude models do.
- An OpenRouter model does if OpenRouter's model list includes `image` among its input modalities. The list is
  cached for a day in `.supervisor/models.json`.
- Self-hosted models do not.

The run's environment tells the instance (`SHAFT_VISION=1`).

The broker's model proxy checks every image before any provider sees it:
- data: URLs only;
- JPEG, PNG, GIF or WebP, recognised from the bytes;
- downscaled with `sips` to 1,568 px when larger;
- at most 4 per call (`SHAFT_MAX_IMAGES`).

A model that cannot see images gets a refusal that names it. Only the newest images are resent in a conversation.

Image bytes are omitted from text records; attachment files remain on disk: episodes, the journal, logs and the tool log keep the path. Trial replays copy the
attachments sent before the replayed cycle. Battery exams get no image tools. A judge, which reads text only, is told
when a cycle used an image.

Sources: [vision validation](../supervisor/vision.py),
[attachment storage](../supervisor/telegram.py).

## A real browser (`shaft browser …`)

Instances can drive a headless Chromium: pages that need JavaScript, logins, forms, clicks
([PLAN-browser.md](plans/PLAN-browser.md)). The supervisor provides only these:
- **One Chromium for every instance.** `shaft browser install [--playwright X.Y.Z]` downloads Playwright's
  Chromium builds into `.browsers/`, swapped in whole and gitignored. `shaft browser` shows the install and each
  instance's profile.
- **A profile per instance**, `.browser/` in its folder:
  - it holds its sessions and the passwords of its own accounts;
  - it is never committed, never copied to the coder or to trials, and never read by the judges.
- **The environment:** `PLAYWRIGHT_BROWSERS_PATH`, `SHAFT_PLAYWRIGHT` (the version to pin) and
  `SHAFT_BROWSER_PROFILE`, alongside the egress proxy.

Everything else is the instance's: Playwright in its `.venv`, its plugin, signing up and logging in. The template's
`skills/writing-plugins.md` tells it how.

A browser runs only in `run` and in scheduled jobs, whose sandboxes reach nothing but the egress proxy (and a job's
own broker). The agent's own sandbox, which its plugins inherit, carries the broker token and reaches the model ports,
so it cannot read `.browsers/` and a launch there fails. A plugin browses through `broker.call("run", …)`.

Playwright sends localhost and the LAN through the proxy too, which refuses them, and every host lands in the egress
log. Chromium runs without its own sandbox: ours is the wall around it. Playwright's fake keychain keeps it away from
your login keychain.

When a run ends, a Chromium left orphaned on the profile is killed. Trial replays get the copy's empty profile and a
proxy that refuses everything. The scheduler logs a line at start if nothing is installed, and the panel's *browser*
row shows the install and the profile's size.

Implementation sources by feature:

| Feature | Source |
|---|---|
| Telegram pairing, attachments, conversations | [supervisor/telegram.py](../supervisor/telegram.py) |
| OAuth clients, scopes, own-account guards | [supervisor/google.py](../supervisor/google.py) |
| Mail grants and limits | [supervisor/mail.py](../supervisor/mail.py) |
| Calendar grants and limits | [supervisor/calendar.py](../supervisor/calendar.py) |
| Host-bound keys and HTTP requests | [supervisor/broker.py](../supervisor/broker.py) |
| Scheduled plugin jobs | [supervisor/jobs.py](../supervisor/jobs.py) |
| Image validation | [supervisor/vision.py](../supervisor/vision.py) |
| Chromium installation and profile cleanup | [supervisor/browser.py](../supervisor/browser.py) |
| Browser and job sandboxes | [supervisor/sandbox.py](../supervisor/sandbox.py) |
