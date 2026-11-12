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
[Where an instance reaches you](#where-an-instance-reaches-you-shaft-channel-)). The service long-polls Telegram; no port,
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
  instance's chat. Messages sent during a reply belong to that thread; cycle messages belong to the From feed
  (`supervisor.web_chat.sent_during` and `from_feed`). Quoting a question answers it.
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

`send_message(to="operator", ..., urgent=False)` always stores the message immediately in the
operator inbox and web UI. Only device notifications wait. Use `urgent=True` for time-critical things only.
Urgent messages and messages during an open conversation notify immediately. Other notifications wait during local
quiet hours (`SHAFT_QUIET_HOURS`, default `22-07`, may wrap midnight; empty disables) — except replies within two hours
of an operator message — and once the instance's notifications that went out today reach the daily budget
(`SHAFT_PUSH_BUDGET`, default 6; 0 means unlimited and hides the count), replies included. Held messages do not use the budget. `SHAFT_PUSH_HOLD=0` turns holding off for new messages.
`send_message`'s result says when a notification is held, until when and why, and gives the instance its
`should_have_waited=yes` share of judged push cycles over the last 7 days.

Held messages produce one combined digest with each sender and first line, at `SHAFT_DIGEST_AT` (default `07:15`)
or the end of quiet hours if later. The next running web or Telegram forwarding loop sends it, including any
overdue messages after downtime. The supervisor persists hold decisions in the message log and digest claims
under its directory. A durable claim prevents duplicate digest dispatch across restarts; a crash between claiming
and sending can lose the notification. Messages remain in the inbox. Telegram digests use one available bot;
open the web inbox to read or answer individual messages and access attachments.

The instance sees its daily count in its operational envelope and after `send_message`. Counts include held
notification requests. Stats report counts, judged push timing and recorded operator reactions separately.
The message log records `urgent` and `push_due`; judges see urgency in the tool arguments and assess the content.

## Google accounts (`shaft google …`)

Mail, Calendar, Drive, and Google Chat share your Google accounts. An instance never holds a
Google credential: these scopes cannot be limited to one label or one calendar, so the supervisor keeps each account's
refresh token in `.supervisor/google/<alias>.json` (0600) and enforces every grant itself. One login per account asks
for `gmail.readonly`, `gmail.compose`, `gmail.modify`, `calendar.readonly`, `calendar.events`, `drive.readonly`, `chat.spaces.readonly`,
`chat.messages.readonly`, and `chat.memberships.readonly` together and records the scopes Google actually granted
(the consent page lets you untick one): `shaft google` lists them. A tool whose scope was not granted refuses, naming
the login to run again; it never returns an empty result. Mail drafts and calendar writes need per-instance grants
on top (`grant-draft`, `grant-write`). `gmail.compose` is the only Gmail scope that writes drafts, and it could also
send: no SHAFT code calls a send endpoint. An account logged in before drafts lacks it; its reads keep working, and
drafting asks for Log in again after you add `https://www.googleapis.com/auth/gmail.compose` to Data Access.
Labels on a draft need `https://www.googleapis.com/auth/gmail.modify` the same way: Gmail changes a message's labels
only through `messages.modify`, which `gmail.compose` cannot call. That scope could also trash or relabel any
message; SHAFT's code only changes labels on a draft an instance wrote, and a test fails on any other write. The
session on an operator's account (`google.Account`) refuses every Gmail write but a draft upload and a label change
on one message, so no send, insert, trash, delete, batch or settings call is ever made with your token. An
account without it keeps reading and drafting; only a draft that asks for labels is refused, before it is written.

Before re-login, add `https://www.googleapis.com/auth/calendar.events` in Google Cloud → Google Auth Platform →
Data Access for each OAuth project. Then run `shaft google login <alias> <address>` again for each operator account
that needs calendar writes (with `--client <name>` for its OAuth client, if needed).

Each account records the OAuth client it logged in with (a refresh token works only with the client that issued it).
A client is named in `.env` as `GMAIL_<CLIENT>_CLIENT_ID` / `GMAIL_<CLIENT>_CLIENT_SECRET`; `--client` picks it, and
it defaults to the alias. One client per Google organisation, because an **Internal** app signs in only its own
organisation's users:
Create a Desktop app OAuth client in a Google Cloud project with the APIs and scopes
required by the grant. Use an Internal consent audience for an eligible Workspace
organisation, or configure an External audience for personal accounts.

**A login that lasts.** Google's OAuth documentation says a project with an External user type and the publishing
status **Testing** gets refresh tokens that expire after 7 days (unless it asks only for basic profile, email and
OpenID scopes, which SHAFT never does), and that test users' authorizations lapse after a week
([refresh token expiration](https://developers.google.com/identity/protocols/oauth2#expiration),
[manage app audience](https://support.google.com/cloud/answer/15549945)). An External app in Testing therefore
stops working a week after each login. To keep it working, open Google Auth Platform → **Audience** and click
**Publish app**, so the status reads **In production**, then log in (again: a token issued while it was in Testing
keeps its 7-day expiry). Google does not require verification for an app for personal use (fewer than 100 users,
[exceptions to verification](https://support.google.com/cloud/answer/13464323)); until verified, the consent page
shows an "unverified app" warning, which you continue past, and the app may sign in at most 100 new users in its
lifetime. An **Internal** app (Workspace only) has no Testing status, so the 7-day rule does not apply to it. A
refresh token can still stop working for Google's other reasons (revoked access, six months unused, a password
change with Gmail scopes, an admin's policy): the tool that needs it then refuses, naming the login to run again.

The Mac app's setup wizard walks through the same steps (Access → Set up Google…) and keeps the client from the JSON
Google Cloud downloads for a Desktop app client: `shaft google client <name>` reads that JSON on stdin and writes
`GMAIL_<NAME>_CLIENT_ID` / `_CLIENT_SECRET` to `.env` (a Web application client's JSON is refused: it needs a
Desktop app client). With `--json`, `client` prints one line, and `login` prints `{"url": …}` (the consent page, for
the caller to open) before it waits, then `{"ok": true, "alias", "address", "client", "granted", "missing"}` or
`{"ok": false, "error"}`.

```bash
shaft google client personal < ~/Downloads/client_secret_….json
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
quota waits up to 60 s, then fails, saying so. Accounts logged in before Calendar kept their files in
`.supervisor/mail/`: `shaft google migrate` moves them (until then they are not read, and the error says so).

Source: [Google account login](../supervisor/google.py).

## Mail (`shaft mail …`)

The read tools check the granted label on each thread. The draft tools are `reversible` under the
[action class model](safety.md#operator-action-classes). There is no operator mail-send broker tool.

**Drafts.** With a draft grant, `mail_draft` leaves a draft in the operator's Gmail and never sends it. The instance
picks the account in `account` among those with a draft grant; the tool's description lists them with their
addresses. `reply_to` (a thread handle) fills To (the last message's Reply-To or From; the operator's own last
message keeps its To and Cc), `Re:` subject, `In-Reply-To` and `References`. From the thread's own account the draft
goes into that thread, which needs a granted label with the draft flag; from another drafted account it is a draft in
that account with the reply headers, outside the original thread. Without `reply_to`, `account`, `to` and `subject`
are required. Attachments are paths in the instance's folder (its `mail/` and `gdrive/` downloads included; links and
reserved paths refused), 25 MB in all. The result gives the draft's handle and its Gmail link. `mail_draft_update`
replaces a draft the instance created, only while Gmail still holds the version it wrote (an edit by the operator, or
a send, makes the draft theirs). `labels` puts Gmail labels on the draft: only labels granted to the instance for
drafts on the draft's account (the tool's description lists them), by name, so a label renamed since is refused. An
update follows the whole-draft rule for them too: the labels asked again stay, the ones the instance put before and
does not ask again come off. A sent message keeps its labels, so a labelled draft, once you send it, is mail under
that label. There is no delete. Five draft calls per run, inside the 30 mail calls. The broker
log keeps counts, never recipients, subject or body. A trial replay checks a draft and answers as a cycle would, and
writes nothing. The supervisor's register of drafts is `.supervisor/mail-drafts/<name>.json`.

An instance you pick may read the Gmail threads that carry a label you pick, on an
account logged in with `shaft google login`. The search filters on the label's ID, and every thread it returns,
reads or downloads from is checked again before anything of it is shown. A thread is visible while any of its
messages carries the label; then all its messages are, later replies included. Drafts, trash and spam are left out.

One instance may read several accounts: grant a label on each; `mail_search` covers every grant unless told one.
`mail_search(query=…, max=…)` returns up to 50 threads per call, following Gmail pages to fill the limit.
When more remain, its final `more: yes` line gives the next `mail_search` call with a `page` handle.
Keep the query and account selection on continuations. The handle tracks each account/label separately,
including threads left over after merging accounts; every continuation checks the current grant and label again.
`shaft mail example grant work AssistantAccess` checks that the label exists and saves the grant in
`.supervisor/tools/example.json`. `shaft mail <name> grant-draft work AssistantAccess` also permits drafts,
never sending. It requires the label to exist and the login to include `gmail.compose`, and adds read access
if missing. `shaft mail <name> revoke-draft work AssistantAccess` keeps read access. A plain `grant` keeps
existing draft access. `shaft mail example revoke work AssistantAccess` or `shaft mail example off` holds from example's
next mail call. A label renamed or deleted in Gmail is refused until you grant it again. Up to 30 mail calls per run; a continuation page of `mail_read` (message or offset past the start, of a thread already opened this run) counts as a call but not against those 30.
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

**Watch for new mail.** An instance may ask the supervisor to tell it when new mail arrives under its
granted labels, instead of polling with a scheduled plugin: `WATCH_MAIL` in its own `config.env` — `off` (default),
`inbox` or `wake` — which it can set with `set_config`. The scheduler's watch ([watch.py](../supervisor/watch.py))
reads Gmail's history from a stored history ID, filtered by the granted label's ID, every `SHAFT_WATCH_EVERY_S`
seconds (default 60). The supervisor polls incrementally; a granted label may need several
history pages per pass, plus label lookups and thread visibility checks. Every thread the
history names is checked with the same label check `mail_search` makes; one that fails is never mentioned, and a
draft, trashed or spam message is not news. What it finds goes into the instance's inbox as one message per pass,
`From: supervisor (mail watch)`: each thread's handle, sender and subject (at most 20 lines, the rest counted), never
a body. With `wake` the instance is also woken through the jobs' wake path (`watch-mail`), at most once per
`SHAFT_WATCH_WAKE_MIN` minutes (default 15); a change inside that window waits in the inbox and wakes it when the
window ends, unless it has been read by then. The instance never holds the subscription or a token; nothing is public
(no Gmail push, no Pub/Sub). A new grant, a watch switched on again and an expired history ID (Gmail keeps about a
week) start from now, with one log line and no backlog. `SHAFT_WATCH=off` stops the watch for every instance. State:
`.supervisor/watch/<name>/`.

Source: [mail grants and reads](../supervisor/mail.py).

## An instance's own Gmail (`shaft secret <name> google …`)

An instance may have a Google account of its own, such as `assistant@example.com`.
It reads the whole mailbox and sends from it through its own plugin, calling the
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

The same account's files: a second Google key on the instance's own account,
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

For instances in `SHAFT_ACTION_INSTANCES` (default: none), the [action classes](safety.md#operator-action-classes)
preserve the initial calendar behavior: changes that tell nobody run directly; supplied attendees, invitations,
incomplete attendee lists and RSVPs require proposals. An over-reach can make direct changes require approval or
make a staged class refused. `shaft actions <name>` shows class state, history and proposals; `restore <class>`
restores the initial rule. All classes share the existing `/calendar/proposals` queue, expiry and approval checks.
Other instances keep their current calendar behavior.

An instance you pick may read the calendars you pick, one per grant, with full
event details: title, time and time zone, location, description, organiser, attendees and their responses, your
response, conference link, recurrence, attachment links (never downloaded). Private events on a granted calendar are
shown; declined ones are shown and marked. `shaft calendar calendars work` lists the account's calendars;
`shaft calendar example grant work primary` (or a title, or an ID) resolves it once and saves its ID with its title in
`.supervisor/tools/example.json`. A title shared by two calendars is refused, both listed: grant by ID. `revoke` and
`off` hold from the instance's next calendar call. `grant-write` adds write access to one calendar and implies read;
`revoke-write` keeps read access. See [calendar write safety](safety.md) and
calendar write plan for direct writes and operator-approved proposals.

Every Calendar API call names a granted calendar in its path, and nothing an instance calls spans calendars (no
free/busy, no calendar list). An event's id is `<alias>/<hash of the calendar>/<event>`, resolved against the
instance's grants as they are then, so an event of another calendar cannot be addressed at all. The tools:
`calendar_events` (a window, default now to 7 days, at most 92; optional free-text query; up to 100 events, the cut
said) and `calendar_event` (one event in full; descriptions cut at 10,000 characters, the cut said). Up to 30
calendar calls per run. An event's description and location under an organiser other than you come under a line
saying who wrote it and that it is not your instruction. Trial replays read the calendar live, shared by the pair,
with events created after the replayed cycle hidden (an older event edited since shows its edit; the judge is told).
Battery exams get none.

Write tools require a write grant and the `calendar.events` scope:

| Tool | Request and effect |
|---|---|
| `calendar_create` | `calendar`, `title`, `start`, `end`; optional `timezone`, `location`, `description`, `attendees`. Creates one event on the granted calendar. |
| `calendar_move` | `id`, `start`, `end`; the same optional fields, including `title`. Changes one occurrence, never a recurring series. |
| `calendar_respond` | `id`, `response=yes|no|maybe`. Proposes an RSVP for operator approval. |

Start must precede end, and both must be within 92 days of now. All-day dates use an
exclusive end date. Date-times need an offset or IANA time zone; otherwise SHAFT uses
the calendar's zone. Direct writes use `sendUpdates=none` and require no attendees
other than the operator. Supplied attendees, invitations, incomplete attendee lists,
and RSVPs need operator approval through `/calendar/proposals`. The outcome reaches
the instance's inbox. The default daily limit is 20 submitted write attempts per
instance (`SHAFT_CALENDAR_WRITES_PER_DAY`; 0 disables writes). Reads and writes share
the 30-call per-run limit. Trial replays cannot write.
Sources: `supervisor/broker.py`, `CALENDAR_WRITE_TOOLS` and `Broker._calendar_write`;
`supervisor/calendar.py`, `write_times` and `direct_only`.

The risks are mail's, plus other people's data: attendees' names and addresses, and invitations they wrote, reach
the instance, its model providers and the judges.

**Watch for changed events.** `WATCH_CALENDAR` in the instance's `config.env` (`off`, `inbox`,
`wake`; set with `set_config`) works as the mail watch does: Calendar's `events.list` with a stored sync token. Each granted calendar can require several pages per pass.
The first pass pages through the whole calendar to get the token and reports nothing. Created, changed and cancelled events reach the inbox as one message per pass, `From: supervisor (calendar
watch)`: each event's handle, time and title, never its description, location or attendees. `wake` uses the same
wake path (`watch-calendar`) and the same per-instance limit; an expired token (410) starts from now. The instance's
own `calendar_create` and `calendar_move` changes are reported too. `calendar_event` refuses a deleted event like one
that never existed.

Source: [calendar tools](../supervisor/calendar.py).

## The operator's Drive (`shaft gdrive`)

An instance may search, read, and download files from your Google accounts through `drive_search`, `drive_read`,
and `drive_download`. Access is read-only. The supervisor holds the credential and checks the account grant and
file reach before returning content. This is separate from [an instance's own Drive](#an-instances-own-drive-sheets-docs-and-slides-shaft-secret-name-google----scope-drive).
See [Drive tools](../supervisor/broker.py), [Drive access checks](../supervisor/gdrive.py), and the plan.

Enable the **Google Drive API** in each account's OAuth project. In Google Auth Platform → **Data Access**, add
`https://www.googleapis.com/auth/drive.readonly`. Run `shaft google login` again and tick Drive on the consent page.
The login requests this scope alongside mail and calendar scopes ([Google scopes](../supervisor/google.py)).
For Workspace accounts, check both admin settings:

- In the Admin console's API controls, Drive must not be Restricted for this app, or the app must be trusted.
- In the Drive and Docs settings, "Allow users to access Google Drive with the Drive SDK API" must be on.

```bash
shaft google login work operator@work.example
shaft google                         # check that drive.readonly was granted
shaft gdrive example grant work      # own reach
shaft gdrive example grant work --all
shaft gdrive example                 # show this instance's grants
shaft gdrive                         # show accounts and all instances' grants
shaft gdrive example revoke work
shaft gdrive example off
```

Use `--client <name>` on login if the OAuth client differs from the alias. Grant checks Drive access before saving.
Granting the same account again replaces its reach. Narrowing from `all` to `own` also deletes that account's downloaded copies, because a copy doesn't record how it was reached. Without `--all`, the reach is `own`.
Commands: [CLI](../supervisor/__main__.py).

| Reach | Files shown |
|---|---|
| `own` | Files you own, files in Shared Drives you belong to, and files owned by an address in your account's domain only when Drive's `about` reports `canCreateDrives=true` for the account |
| `all` | Every file the account can reach, including files shared by strangers |

A Workspace that blocks Shared Drive creation shows no same-domain files under `own` unless they qualify as owned files or files in a member Shared Drive.
Both reaches exclude trash. `own` is not a label or a selected folder. It can include files written by others.
A shortcut and its target must both pass the visibility check. Shared Drive membership is cached for the run.

Search covers all granted accounts unless given an `account` or a `folder` handle. It returns 20 files by default,
at most 50, with handles in the form `<alias>/<fileId>`. Follow its continuation call for more results.
Use `modified_after` to filter by Drive modification time, for example
`modified_after="2026-11-01T00:00:00Z"`. The comparison is strictly newer than that value.
Use `type` (`doc`, `sheet`, `slides`, `pdf`, or `folder`) when looking for a kind of file.
Without search text, results are newest first by modification time; with text, they keep Google's relevance order within each account.
It reports accounts it could not search and Google's incomplete-search flag.
Reads return at most 6,000 characters per page, including attribution and the continuation call.
They mark the owner and last editor and say that instructions in the file are not yours.

There are at most 30 Drive calls per run. A read continuation with a nonzero offset does not use that allowance
if the handle was already read successfully in this run.
Each file fetch is limited to 50,000,000 bytes. Downloads also have a combined limit of 100 MiB per run;
text reads do not use that byte allowance.
These caps slow a bulk copy. They do not prevent one over many runs.

`drive_read` exports Docs as Markdown, Sheets as CSV of the first tab, and Slides as plain text.
It also reads text files, JSON, and XML. For PDFs, images, and other binary files, use `drive_download`, then the
instance's local tools. Downloads export Docs as DOCX, Sheets as XLSX, Slides as PPTX, and Drawings as PNG.
Folders and unsupported Google types, including Forms, Maps, Earth, and Sites, cannot be read or downloaded.
Files with `canDownload=false` are refused. Google's export limit can also block a supported file.

Copies go under `gdrive/<alias>/` in the instance's folder. Downloads refuse until that path is gitignored;
run `shaft update example` to bring in the template rule.
Revoke takes effect on the next Drive call and deletes `gdrive/<alias>/`. `off` removes all Drive grants and
all of `gdrive/`. If deletion fails, the command reports that the grant was revoked but copies remain.
It does not erase memories or copies the instance made elsewhere.

### The risks you accept

1. **Your files leave your machine.** What an instance reads goes to its model provider. Judges and other models
   may see it too. What it remembers stays in its memory after you revoke, and `shaft gdrive <name> off` deletes
   only the downloaded copies.
2. **Prompt injection can lead to disclosure.** A document can carry instructions. An instance with permission to
   email anyone, share its own files with anyone, or reach the public internet can send what it reads through those channels.
   Nothing blocks those sends after a Drive read. `own` is the only limit on which files it can read here,
   and it is weaker than mail's label limit.
3. **Reads may leave a mark in a Workspace audit log.** On Workspace, Drive API reads should appear in the admin
   audit log as "Item content accessed", with the app's OAuth client ID, per Google's documentation. A read is
   not expected to change "last viewed" (`viewedByMeTime`) or "modified" (`modifiedTime`).

## The operator's Google Chat (`shaft gchat`)

An instance may search and read every conversation on a granted Google Chat account, including DMs, group chats,
spaces, and the operator's own messages. It can download uploaded files. Access is read-only, with no per-conversation
hide list. The supervisor holds the credential and checks the account grant on every call.
See [Chat tools](../supervisor/broker.py), [Chat reads](../supervisor/gchat.py), and the
plan.

Enable the **Google Chat API** in the account's Google Cloud project. In Google Auth Platform → **Data Access**, add:

- `https://www.googleapis.com/auth/chat.spaces.readonly`
- `https://www.googleapis.com/auth/chat.messages.readonly`
- `https://www.googleapis.com/auth/chat.memberships.readonly`

A Workspace admin can block `chat.messages.readonly` for outside apps through API controls, including an Internal
OAuth client. Run `shaft google login <alias> <address>` again and grant all three scopes. Use `--client <name>` if
the OAuth client differs from the alias. Existing mail, calendar, and Drive logins keep working without Chat scopes;
a Chat call without them refuses and names the login command to run.

```bash
shaft google login work operator@work.example
shaft google                         # check that all three Chat scopes were granted
shaft gchat example grant work
shaft gchat example                   # show this instance's grants
shaft gchat                           # show accounts and all instances' grants
shaft gchat example revoke work
shaft gchat example off
```

Grant checks Chat access before saving. Revoke takes effect on the next Chat call and deletes `gchat/<alias>/`.
`off` removes all Chat grants and all of `gchat/`. A deletion failure is reported. Memories and copies made elsewhere
remain. Downloads refuse until `gchat/` is gitignored; run `shaft update example` to bring in the template rule.

| Tool | What it does |
|---|---|
| `gchat_search` | With no arguments, lists conversations by last activity. Searches messages with `text`, `conversation`, `from` (address or `users/<id>`), `after` / `before` (dates), and `kind` (`dm`, `group`, `space`). `account` selects one granted account. Returns 20 results by default, at most 50 per account, with a continuation call. A listing is at most 6,000 characters: rows that do not fit are counted, with a hint to narrow the search or lower `max`. Every search but one within a `conversation` with neither `text` nor `from` goes through Google's search, which does not reach muted conversations; the result says so. |
| `gchat_read` | Opens a conversation handle (`<alias>/<spaceId>`) or a message handle (`<alias>/<spaceId>/<messageId>`). Returns at most 6,000 characters per page in time order, naming each sender and attachments. Follow the footer's exact call for older messages, or for the rest of a page too long for one result (`More of this page`, with `offset`), until `Start of conversation`. |
| `gchat_download` | Downloads an uploaded file selected by attachment number or name from a message handle, under `gchat/<alias>/<spaceId>/`. A linked Drive file needs `drive_read` / `drive_download` and a Drive grant. Each uploaded file is limited to 25,000,000 bytes; downloads total at most 100 MiB per run. |

Every page marks the content as written by its senders, whose instructions are not the operator's. The operator's
own messages are labelled `you`. There are at most 30 counted Chat calls per run. A read continuation for a
conversation already opened successfully in that run does not use another tool-specific call allowance.
Trial replays hide messages created after the replay time, but show the current text of edited messages.
Battery exams get no Chat tools.

**Watch for new messages.** `WATCH_GCHAT` in the instance's `config.env` accepts `off` (default), `inbox`, or `wake`,
set with `set_config`. The supervisor polls every `SHAFT_WATCH_EVERY_S` seconds (default 60). It lists conversations,
then reads new messages where activity has advanced. One inbox message per pass groups messages by conversation,
with handles, times, and senders. Each text is cut at 500 characters including `…`; at most 40 message lines appear
in the inbox message. The rest is counted with a `gchat_read` call. The content carries the same sender marking as
reads. With `wake`, the watch uses the shared wake path, at most once per `SHAFT_WATCH_WAKE_MIN` minutes (default 15).
The operator's own messages reach the inbox, but a pass containing only those messages does not wake the instance.
The first pass, a new grant, or switching the watch on again starts from now without reporting old messages.
A newly seen conversation reports messages since the previous pass. Edits and deletions are not news.
Source: [watch](../supervisor/watch.py).

### The Chat risks you accept

1. **Colleagues' messages reach model providers** (the instance's model, the judges), and what the instance writes from them
   stays in its mind store. Its own conversation episodes and tool logs also hold what it read, as with mail. The
   rule that no log holds Chat content covers the supervisor's logs (`broker.log`, the watch's log lines), not the
   instance's own records. This may be workplace data; whether your organisation's policy allows it is yours to judge.
2. **Anyone who can message you can write instructions to the instance.** The marking helps; it is not a guarantee.
   The instance has the internet, so anything it reads it could send anywhere.
3. **Downloads in `gchat/` are deleted on revoke; memories formed from them are not.**
4. **A Workspace admin may see the OAuth client** in the Admin console's app access list, and can block it at any time
   (the tools would then refuse with Google's reason).

## Scheduled jobs (`shaft jobs …`)

An instance can run one of its own plugins on a timer, for example a watcher that
checks the tagged mail every 15 minutes. It declares the job in the plugin's own manifest:
`"schedule": {"every_minutes": 15, "args": {…}, "can_wake": true}` in `plugins/<name>/plugin.json`. A cycle
writes it, the gate tests it, and the commit records it. No launchd job is installed: SHAFT's scheduler service runs
due jobs as children.

Each run happens in the `run` sandbox, with its own broker and egress proxy:
- supervisor tools, mail and calendar grants, the instance's keys, its Python environment and the internet;
- no model, and no `delegate_coding`, `trial` or `send_message`;
- writes only to `.jobs/<plugin>/` (gitignored state), `.tmp/` and its browser profile `.browser/`. The broker's
  `run` inside a job may still write notes/ and code as a cycle's does, until the operator sets
  `SHAFT_JOB_RUN_CONFINED=1` (default `0`): then it writes only the job's own set too;
- at most 120 s and 30 tool calls, at least 5 minutes apart (`SHAFT_JOB_MIN_MINUTES`), at most `SHAFT_JOBS_MAX`
  jobs per instance (default 12); beyond it, the rest by name are not run and are named in the problem report.

What a run returns lands in the instance's inbox, from "job <plugin>". An empty return delivers nothing. A job with
`can_wake: true` whose run returns `{"message": …, "wake": true}` starts a cycle as soon as the instance is idle.
Several requests while it is awake make one wake, there is no rate limit, and a paused instance is never woken.
For new mail, calendar changes, or Google Chat messages a job is not needed. The supervisor's watch
(`WATCH_MAIL`, `WATCH_CALENDAR`, `WATCH_GCHAT`, in the corresponding sections) checks every minute and wakes through
the same path.

A job's work reaches the instance whole:
- **Hand-off.** `{"message": …, "files": ["brief.md", …]}`: up to 5 files of at most 256 KB from `.jobs/<plugin>/`
  (a path relative to it, or starting with `.jobs/<plugin>/`; no link is followed) are copied beside the message,
  into `inbox/<stamp>-from-job-<plugin>.files/`. The message lists each with its size and names each refused one
  with why. The folder is removed once its message has been archived and the same job delivers again.
- **Cut output.** A message over 4,000 characters is cut, and its whole text (up to 1 MB) is kept in that folder as
  `output.txt`; the cut says so: `[… cut at 4000 of N characters; the whole text: inbox/….files/output.txt]`.
- **Publish.** `"publishes": {"notes/briefing.md": "briefing.md"}` in the schedule (at most 5; each target under
  `notes/`, each source in `.jobs/<plugin>/`, checked with the rest of the schedule). After a successful run, each
  source that changed since it was last published (at most 256 KB) replaces its target in one rename; nothing is
  published after a failed run. The instance's next notes commit records it. A problem (a source too big, a link on
  the way) reaches its inbox once.
- **Long jobs.** `"long": true`: `SHAFT_JOB_LONG_TIMEOUT_S` (900) and `SHAFT_JOB_LONG_CALLS` (200) per run, one long
  job at a time per instance, and `SHAFT_JOB_CHECKPOINT=.jobs/<plugin>/checkpoint.json` in its environment. A run
  stopped by the timeout that wrote that checkpoint since it started is resumed at the scheduler's next tick, with
  `SHAFT_JOB_RESUME=1`, up to `SHAFT_JOB_LONG_RESUMES` (4) times; then it gives up, the instance gets one message,
  and the job starts afresh at its next interval. Each run's log row carries `resumed: n`.

A run still going when the next comes due means the next is skipped, and the skip is logged. After 3 failures in a
row the instance gets one message and the job backs off (its interval doubles, up to a day). Every run is logged in
`.supervisor/jobs/<name>.jsonl`. `shaft jobs`, the panel's and `shaft top`'s *jobs* row, and the instance's own
`record(kind='jobs')` show them: each job's arguments, what it publishes, and its status (running, or for a long job
done / resuming n of N / gave up). Jobs are on by default; `shaft jobs <name> off` stops an instance's jobs. They
never run during a battery exam, nor in trial replays.

Source: [jobs execution and scheduling](../supervisor/jobs.py).

## Images

Instances can see images. There are two ways:
- **Native.** If the instance's model accepts images, a photo you attach on Telegram reaches its model with your
  message, and its `view_image` tool shows it an image file in its folder (`attachments/`, `mail/`, `shared/`).
- **Described.** Every instance has the supervisor tool `describe_image(path, question)`. A vision model
  (`SHAFT_DESCRIBE_MODEL`, default `claude-haiku-5-5`; any model the supervisor can call: Claude, OpenRouter, a priced
  `custom/` model or a `codex/` plan model) answers in text, marked as described rather than seen. This
  is how an instance whose model cannot see images (an instance on Bonsai) learns what is in one, and the cost counts
  toward its daily cloud cap. At most 10 calls per run.

Which models see images is learned, not listed:
- Claude models do. Codex and Grok plan models accept images through `planchannel.sees_images`.
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

Instances can drive a headless Chromium: pages that need JavaScript, logins, forms, clicks.
The supervisor provides only these:
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

## Speaking voices

The supervisor holds the installed voices reported by the operator's iOS and macOS devices. Instances cannot
reach those devices. `supervisor/voices.py` stores one assignment per instance and language, English (`en`) or
Italian (`it`). Two instances cannot hold the same identifier for the same language.

- `list_voices(language?)` returns the last reported list per platform. `language` filters by prefix, so `it`
  includes `it-IT` and `it-CH`. Each row has `current` for this instance's assignment and `taken_by` for another
  instance holding that voice for the language, or null when no other instance holds it.
- `propose_voice(language, identifier, why)` asks the operator to confirm a voice in Settings. The identifier must
  appear in a reported list and must not be held by another instance for that language. A new proposal replaces
  the instance's open proposal for the same language. Acceptance assigns the voice; rejection keeps the previous
  assignment. Both decisions reach the instance's inbox, including the operator's rejection reason.

Both tools use the normal broker call budget and tool switches. `shaft tools <name> off list_voices` and
`shaft tools <name> off propose_voice` hide and refuse them from the next run or conversation; use `on` to restore
one. Trial proposals are simulated and recorded without changing the store.
