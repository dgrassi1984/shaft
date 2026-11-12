# Security

SHAFT runs LLM agents that write and execute their own code. Treat that code, and everything the agents read, as
untrusted. This page states what the supervisor protects, what it does not, and how to report a problem. The full list
of enforced rules is in [docs/safety.md](docs/safety.md).

## Threat model in one paragraph

The supervisor assumes an instance may be wrong, manipulated by what it reads (prompt injection), or actively trying
to escape its rules. It keeps the instance away from your credentials, your files outside its own folder, your local
network and the supervisor's code. It does not stop an instance from sending what it has read to the public internet,
from acting within the access you granted it, or from spending money up to the checks described below. Granting access
is granting it to the instance and to anyone who can put text in front of it.

## What the supervisor protects

| Boundary | How | Limits |
|---|---|---|
| Filesystem | Each run executes under macOS `sandbox-exec` (`supervisor/sandbox.py`). Writes are limited to the instance's own folder (minus `secrets/`, `config.env`, `drive.md` and `shaft-docs/`), permitted devices and the image tools' temporary files; the broker's `run` tool may write less. A shared folder you grant is read-only. Your home folder, `/private/tmp`, `/Users/Shared`, `/Volumes` and the per-user temp and cache folder are closed; if macOS does not say where that per-user folder is, the run refuses to start. | `sandbox-exec` is a containment layer, not a virtual machine, and Apple marks it deprecated. |
| Supervisor code and state | Instances cannot write `supervisor/`, `.supervisor/`, their `config.env` or `drive.md`. Each live run imports its `shaft` package from a read-only copy of its last approved code. | Plugins and the broker's `run` tool execute the writable working tree; what they changed is gated when the run settles. |
| Self-changes | Changes to the instance's code paths (`shaft/`, `tests/`, `plugins/`, `.python-version`, `requirements.txt`) and the config lines it set through the broker must pass the test gate. A failed check reverts them. | If the gate cannot run at all (its sandbox cannot be built), nothing is reverted: the change stays unsettled and the run is refused until a later gate settles it. Memory, notes and other non-code files are not gated. |
| Journal and core | The journal must stay append-only: a rewrite is replaced by the previous text. Once a core personality exists, another core change within 72 hours is put back. | If `memory/` is a link or a write fails, the supervisor records a note instead of restoring. A first core, or a change from an empty core, is kept. |
| Credentials | The supervisor reads the root `.env`. The broker attaches keys to the requests it makes; the sandbox passes on only an allow-list of environment variables and denies the instance's secret store. | These controls protect configured credentials. They cannot stop you from pasting a secret into a message or a readable file. |
| Local network | The egress proxy (`supervisor/egress.py`) and the broker's `fetch_page` and `http_request` refuse non-public addresses (localhost, LAN, link-local, tailnet), check the address for every connection and redirect, and connect to the address they checked. The sandbox opens only the supervisor's own local ports. | One opt-in exception: `SHAFT_TAILNET_GRANTS` lets one instance's keyed `http_request` reach named tailnet `host:port` pairs. The granted service sees whatever that key may do there. |
| Leftover processes | When a run ends, the supervisor ends its process group and any orphan still working in the instance's folder before it settles the run. | A process that starts its own session and leaves that folder is not found; only the sandbox limits it. |

## What it does not protect

- **The public internet is open.** An instance can send anything it has read to any public address, and can act on
  public services with keys you gave it.
- **Prompt injection.** Mail, calendar events, Drive files, Google Chat messages and web pages can carry instructions. An instance acting on
  them uses the authority you granted.
- **Spending.** Per-instance daily checks exist for the cloud model (`SHAFT_CLOUD_DAILY_USD`), coding hand-offs
  (`SHAFT_CODER_DAILY_USD`), consultations and the instance's own trials. Each is compared with recorded spending
  before work starts, so one admitted task can exceed it. Judging has an alert threshold, not a cap.
- **What an instance remembers.** Revoking access removes the grant and downloaded copies, not what the instance
  wrote down or copied elsewhere.

## Access you can grant

All Google credentials stay with the supervisor; the broker checks the grant on every call.

- **Operator Gmail:** reads are limited to threads carrying a label you granted. The OAuth scope itself is not
  label-limited; the supervisor checks the label on every thread it returns, reads or downloads from. A draft grant
  on a label also lets an instance leave drafts in that account (a reply in a thread it can read, or a new message),
  never send them: you open Gmail's Drafts and press Send. Gmail has no drafts-only scope, so the `gmail.compose`
  token the supervisor holds could send; no broker tool calls a send endpoint, and a test fails if one ever does. An
  instance can update only a draft it created and you have not edited since; it has no delete. It can put on its
  draft a label you granted it for drafts on that account, which needs `gmail.modify` on the supervisor's token: that
  scope could trash or relabel any message, and the only such call in the code changes labels on a draft the
  instance wrote (a test fails on any other). The supervisor's one door to Google also refuses every Gmail write on your
  accounts except a draft upload and that label change, before a request exists, so a send from your address
  cannot leave it whatever a tool asks. An instance sends mail only from its own account. A labelled draft you send is mail under that label. A draft can carry
  what a hostile email asked for: read its recipients and attachments before you send it.
- **Operator calendars:** read access per calendar. Write grants let an instance create and move events. A change that
  involves other attendees, and every reply to an invitation, becomes a proposal that waits for your approval. For
  instances listed in `SHAFT_ACTION_INSTANCES`, an action you mark as over-reach also demotes that class of action to
  approval-only or refused until you restore it.
- **Operator Drive:** read-only per account. The supervisor holds a whole-Drive read token. `own` reach covers files
  the account owns, its Shared Drives, and same-domain files only when Drive reports that the account can create
  Shared Drives. `--all` adds external shares. Content an instance reads goes to whichever model serves that call;
  with a cloud model, it leaves your Mac.
- **Operator Google Chat:** read-only per account, covering every conversation with no hide list. Every call checks
  the grant; messages and watch deliveries name their senders and mark their instructions as not the operator's.
  Colleagues' messages reach model providers and can carry injected instructions. Revoke deletes downloaded copies
  under `gchat/`, but memories remain. A Workspace admin can see and block the OAuth client. See the
  [four accepted risks](docs/integrations.md#the-chat-risks-you-accept).
- **Instance-owned Google accounts:** an account you create for the instance. Its Gmail access can read, send, label
  and trash mail; its Drive access covers the account's files and files shared with it, including sharing, trashing
  and permanent deletion. This is far broader than the operator grants. The broker refuses a key for an account
  registered as yours.
- **Native push:** the APNs signing key stays at `.supervisor/web/apns/AuthKey_<KEY_ID>.p8`, outside every sandbox;
  keep it at mode 600 (the supervisor forces that mode on `apns.json`, not on the key). A push carries no message
  body: only the title, instance name, badge, sound and thread; the app fetches the text over the tailnet.

## Network access to the supervisor

The web UI, the chat API and the native apps' API bind to localhost and are reached through Tailscale Serve, on your
tailnet only, with one exception for the Mac app on the same Mac (below). Do not expose them through a public proxy
or Tailscale Funnel.

- The chat API (`/v1`) needs a bearer key and an allowed Tailscale login. The inbox endpoint is the exception: it
  accepts the narrower `SHAFT_INBOX_KEY` alone, without the login check, so anyone on your tailnet who holds that key
  can deliver a message and wake an instance.
- Web UI pages need an allowed login, the expected host, and a paired browser cookie; their form posts check origin and
  a CSRF token.
- The native apps' API (`/app/v1`) needs the same login and host and a paired device's bearer token. It reads no
  cookie, and the web pages accept no bearer, so a browser can never send an app credential on its own.
  `shaft web unpair` revokes a device.

### The loopback path for the Mac app

Off by default. `shaft web on --local`, which the Mac app's installer runs, sets `SHAFT_WEB_LOCAL=on`; the Mac app on
the same Mac then reaches `/app/v1` at `http://127.0.0.1:8785` without Tailscale. Only `/app/v1` takes this path: the
web UI pages keep every tailnet check, and answer no one until `shaft web on` records a tailnet name.

- **Who can use it.** A request whose `Host` is `127.0.0.1` or `localhost` at the web port. The tailnet login and host
  checks are skipped, so a paired device's bearer is the only credential, and every route needs it. Such a device is
  made by `shaft web pair --local`, which prints its secret once and needs the login `operator@local` that `shaft api
  on --local` adds. A pairing link is refused on loopback (`403 local_pair`).
- **Who it acts as.** The identity comes from the paired device's record in `.supervisor/web/devices.json`, never from
  a header: any local process could send `Tailscale-User-Login` to 127.0.0.1. A device unpaired since is refused.
- **Proxied requests take the tailnet path.** `tailscale serve` forwards whatever `Host` it was given, so a request
  carrying any header its proxy adds (`X-Forwarded-For`, `Forwarded`, `X-Forwarded-Host`, `Tailscale-User-Login`,
  `Tailscale-User-Name`) must pass the login, host and bearer checks as before.
- **What it adds.** No reach from other machines: the service still listens on 127.0.0.1 only, and instance runs
  cannot reach its port. A process running as you that holds a paired bearer can do what the app can, as it could
  with a browser's cookie.
- **The setup routes.** The wizard's `/app/v1/setup/*` routes run the CLI verbs a terminal would, as the paired device,
  so every verb's own checks apply. `POST /app/v1/setup/secret` writes only an allowlist of provider and tool keys
  (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `SCRAPEBADGER_API_KEY`, `SHAFT_CUSTOM_API_KEY`): the server's own keys
  and logins, Google clients and Telegram tokens cannot be written there. A key or token travels on the verb's stdin,
  never in its arguments, a log line or an answer. The Google client's JSON goes on the stdin of `shaft google client`,
  which the app runs as a local process.
- **One restart barrier.** A restart of the api or web service (`shaft api on`, `shaft api restart`, `shaft web on`, the
  app's API restart, `shaft deploy`) first looks at the open conversations and restarts nothing while one is open. From
  that look until the restart it holds `.supervisor/restarting.lock`, and a conversation that tries to open meanwhile is
  refused with "the server is restarting, try again in a moment". The app's restart routes and the web panel's share one
  lock per service, so two restarts of the same service never overlap.
- **The Mac app runs outside the App Sandbox.** Its setup runs `git`, `uv` and the server's CLI and writes `~/SHAFT`,
  which the App Sandbox forbids. It keeps the hardened runtime, and its notification extension stays sandboxed. It
  allows plain `http` to local addresses only (`NSAllowsLocalNetworking`), which it uses for 127.0.0.1. The iPhone and
  iPad apps reach the server over the tailnet only.

## Supported versions

Only the latest release tag on `main` is supported. Fixes are not backported.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability** on this repository. Include the
affected tag or commit, reproduction steps, expected and observed behaviour, and the boundary involved (from the table
above). Do not include real credentials or private instance data, and do not open a public issue with exploit details.

This is a one-person project. Reports are read and fixed as time allows; there is no response-time commitment.
