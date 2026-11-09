# SHAFT — Plan: native chat apps for iPhone, iPad and Mac

*Drafted and signed off 2026-11-03. Decisions the operator made are marked as theirs. "My call" marks a choice
I made that they did not state.*

## 0. What you asked

"launch a multi-agent build of a native iOS app and a Mac app to be used instead of the PWA on iPhone and iPad and
Mac … focus strictly on the chats … It needs to support push notifications of course".

Your answers (2026-11-03):

- **Signing:** use team `<TEAM_ID>` and an APNs auth key `<KEY_ID>`.
- **Install:** Xcode direct install (development signing, APNs sandbox). No TestFlight or App Store for now.
- **Scope:** full parity with the PWA chat, the documents sheet with comments included.
- **Mac UI:** a SwiftUI multiplatform app, with one codebase and a real macOS app (not Catalyst).

## 1. What it is, and what it is not

**Goals**
- One SwiftUI app with three targets:
  - **iOS/iPadOS:** the chat full screen on iPhone, and a split view on iPad.
  - **macOS:** a sidebar of instances, the chat, the documents as an inspector, and menu commands.
- Everything the PWA chat does:
  - **The chat list:** state, last line and unread mark per instance.
  - **The feed:**
    - rendered Markdown, with tables, task lists and code blocks with Copy;
    - images and files both ways;
    - the reply's activity and clock, live;
    - the instance's messages and feedback cards;
    - the "Show the N earlier messages" link, and earlier chats.
  - **The composer:**
    - attachments;
    - **Send now**, which interjects while a reply runs;
    - **Queue after reply**, with Cancel;
    - **Next cycle**;
    - Stop and End;
    - multi-quote **↩ Reply** cards, each with its own answer field;
    - a draft per instance that survives a quit;
    - a client message id, so a retried send is never posted twice.
  - **Documents:** list, view, edit, delete, and comments with threads.
- **Native push:**
  - "example-b replied", "example-b's reply failed" and "example-b asks …", with the badge;
  - a tap opens that chat;
  - no push to a device that has that chat on screen.
- Keyboard rules as in the PWA:
  - **iPhone, or iPad with the on-screen keyboard:** Return is a new line, and the Send button sends.
  - **iPad hardware keyboard and Mac:** Enter sends, Shift/Option-Enter is a new line, and Cmd-Enter queues.

**Non-goals**
- Manage, Overview, Config, Journal, Drive, Settings, Services, trials and experiments stay in the web UI.
- TestFlight, the App Store, Android, Windows.
- Changing the PWA. It keeps working, Web Push included, for any browser.

**Anti-goals (a failure even if it works)**
- **Message text through Apple.** The APNs payload carries the title only (the instance's name and what happened).
  The body is fetched over the tailnet by a notification extension, as the service worker does today.
- **A second chat model that drifts.** The JSON API and the HTML feed must draw from one function that says what a
  chat shows. A fix to one must not miss the other.
- **Weaker auth.** The API stays tailnet only, on the same port and with the same `SHAFT_API_LOGINS` check. A native
  device is a paired device in `devices.json`, so `shaft web status` and `shaft web unpair` cover it.
- **An instance holding or seeing an APNs credential.** The key lives in `.supervisor/web/apns/`, which the sandbox
  never mounts.

## 2. How it works

### 2.1 Server: `/app/v1`, a JSON API on the web service (port 8444 via `tailscale serve`)

- **Auth:**
  - Every request needs the tailnet login check the web UI already does.
  - It also needs `Authorization: Bearer <device>.<secret>`, the same pair the cookie holds.
  - There is no CSRF token: a header that is not sent on its own carries no ambient credential.
  - `last_seen` is refreshed as it is for the cookie. *(My call.)*
- **The contract:** [docs/app-api.md](../app-api.md) (M0) is the complete one: every route, field, status code and
  example. Where it changed this section, the change is marked *(M0)*.
- **Pairing:**
  - `shaft web pair --name iphone-app` prints the link as it does now.
  - The app takes the whole link pasted. On the Mac it is copied from the terminal, and Universal Clipboard carries it
    to the iPhone or iPad. *(M0: QR pairing dropped.)*
  - The app posts the link (or the token) to `POST /app/v1/pair {link|token, platform}` and gets
    `{device, secret, name, login}`, kept in the Keychain. `POST /app/v1/unpair` unpairs it from the app.
  - The token stays one-use and valid for 10 minutes.
- **One model:**
  - A new `web_chat.feed(name, cid=None, whole=False) -> dict` returns what a chat shows: turns, `_sent_during` cards,
    background messages, queued turns, pending feedback, and the reply's activity. Its pure part,
    `web_chat.build(name, chat, sent, archived, whole)`, is what `web_views.chat_feed` renders. *(M0: `whole`, as
    `chat_feed` names it.)*
  - The HTML views render from it, and the JSON API serialises it. Each JSON item's `key` is the HTML's `data-k`.
  - It carries two revisions:
    - `version`, a hash of everything but the running reply's activity;
    - `live`, a hash of that activity and the streamed answer.
    - Both are computed over the whole chat, so the 80-turn window changes neither. *(M0.)*
  - The HTML ETags hash rendered bytes and leave the activity to `/fragment/chat/<n>/live` (`web.py:338`,
    `web_views.py:1404`), so they cannot serve as either revision. *(Builder check, Codex.)*
  - The long-poll answers as soon as either revision moves. When only `live` moved it sends the reply alone
    (`partial: true`), not the whole feed again each second. *(M0.)*
  - Times go out with the server's UTC offset (`2026-11-03T14:02:11+01:00`): the supervisor stores local times.
    *(M0.)*
- **Routes** (all JSON; `<n>` = instance, `<cid>` = chat id):

  | Route | What |
  |---|---|
  | `GET /app/v1/instances` | the rail: name, state, line, last, replying, unread for this device, next cycle; and the badge |
  | `GET /app/v1/chat/<n>?since=<version>&live=<live>&wait=25[&all=1]` | the feed. It holds the request up to 25 s for a change (long-poll, *my call*: fewer wakes than the PWA's 1.5 s poll), then answers `304`. Holding it marks this device as watching (`web_chat.watching`), unless `watch=0` (a sheet covers the chat). *(M0: `live` and `watch`.)* |
  | `GET /app/v1/chats/<n>`, `GET /app/v1/chat/<n>/<cid>` | earlier chats |
  | `POST /app/v1/chat/<n>/send` | multipart: `text`, `mid` (required), `mode=now\|queue\|background`, `quotes` (JSON: `[{quote, answer, doc}]`, made into the text on the server exactly as `web.js` does), `retry_of`, files. It is the same path as `/chat/<n>/send`. *(M0: `doc`, `retry_of`.)* |
  | `POST /app/v1/chat/<n>/{unqueue,stop,end,clear,seen,away}` | as the web actions; `seen {version}` clears unread for this device, and is refused (`409 stale_version`) when that version is no longer the feed's; `away` stops watching *(M0: the refusal)* |
  | `GET /app/v1/file/chat/<cid>/<k>/<j>`, `GET /app/v1/file/message/<file>/<j>` | attachments, through `_serve_attachment`. No server-side thumbnails: the app downsizes. *(M0.)* |
  | `GET/POST /app/v1/docs/<n>[/…]` | documents: list, read, raw download, save (with its sha, so a stale save is refused), delete, and comments (create, edit, delete, send, state) |
  | `POST /app/v1/feedback/<n> {op, …}` | the thumbs: rate, clear, send, unpend, as `POST /feedback/<n>`. *(M0: was `/feedback/<id>`; a rating has no id until it is made.)* |
  | `POST /app/v1/push/register {token, env, topic, platform}`, `DELETE /app/v1/push/register`, `GET /app/v1/push/status` | this device's APNs token *(M0: `platform`, `status`)* |
  | `GET /app/v1/push/latest` | what the next notification shows (the notification extension's fetch), with the instance to open and the badge |

- **Unread:**
  - Today the PWA keeps unread marks in the browser. A native badge needs the server to know.
  - The server keeps the last seen version per device and instance in `.supervisor/web/seen.json`. The badge is the
    count of instances with something unseen.
  - "Unseen" is the instance's own newest answer or message (`web_chat.summary`'s `last`) newer than what the device
    marked: the operator's own messages never make a chat unread, and an instance a device never marked counts from
    the device's pairing. *(M0.)*
  - The PWA keeps its own marks. *(My call.)*

### 2.2 Server: APNs (`supervisor/apns.py`)

- **The token:**
  - A provider JWT (ES256), signed with `/usr/bin/openssl` as VAPID already is (it shares `webpush`'s helpers).
  - It is renewed every 50 minutes.
- **The send:**
  - APNs requires HTTP/2, which the stdlib lacks. The send goes through `/usr/bin/curl --http2`, the second
    exception to "stdlib only", for the same reason as openssl.
  - The host follows the device's `env`: `api.sandbox.push.apple.com` for Xcode builds, `api.push.apple.com` later.
- **The payload:**
  - `{"aps": {"alert": {"title": "example-b replied"}, "badge": N, "sound": "default", "thread-id": "example-b",
    "mutable-content": 1}, "instance": "example-b"}`.
  - No message text.
- **Fan-out:**
  - `webpush.notify` becomes the single fan-out over web subscriptions and APNs tokens.
  - It keeps the per-device queue (`take()`), the skip of watchers, the paired-only rule, and the retries.
- **Dead tokens:**
  - APNs `410` / `BadDeviceToken` / `Unregistered` drops the token.
  - `400 BadTopic` or a bad key logs the actual config (key id, team, topic) and does not drop anything.
- **Config** (documented in `docs/configuration.md`):
  - `SHAFT_APNS_KEY_ID`;
  - `SHAFT_APNS_TEAM_ID=<TEAM_ID>`;
  - `SHAFT_APNS_TOPIC`, the bundle id;
  - The team ID is the Apple Development certificate's OU, not the ID in its common name.
  - the key at `.supervisor/web/apns/AuthKey_<id>.p8` (mode 600).
  - A missing key means native push is off and `shaft web status` says so. There is no silent fallback.

### 2.3 The app (`apps/shaft/`)

- **Project:**
  - `project.yml` for XcodeGen. The `.xcodeproj` is generated and git-ignored, so agents never hand-edit a pbxproj.
    *(My call. XcodeGen comes from Homebrew.)*
  - Swift 6 with strict concurrency.
  - Minimum iOS 26 and macOS 26. *(My call: one release behind the current 27.)*
  - Bundle id `$(SHAFT_BUNDLE_PREFIX).shaft`, display name **SHAFT**. *(My call.)*
- **Targets:**
  - `SHAFTKit`, a local Swift package: models, the API client, long-poll, drafts, the Keychain, and the send queue
    with `mid`.
  - `SHAFT-iOS`.
  - `SHAFT-macOS`.
  - `NotificationService-iOS` and `NotificationService-macOS`, which fetch `/app/v1/push/latest` with the device bearer and fill the body, with
    the title only on failure.
- **A send that fails** (no network, server down, the app quit mid-send):
  - The message stays in the chat as "not sent", with its files kept in the app's own storage, and Retry.
  - Retry keeps its `mid`, so a send the server did take is not posted twice.
  - Nothing is replayed on its own.
  - If the chat ended meanwhile, Retry goes into a new chat, as the web's send does (`web_chat.py:848`).
  - Editing the text makes a new `mid`.
  - *(My call. The PWA cannot keep files across a reload; a native app can.)*
- **Markdown:** rendered natively with MarkdownUI (SPM) for GFM tables, task lists and code. Selection for ↩ Reply
  uses the platform text selection on a message's text view. *(My call.)*
- **Server address:**
  - The app asks for the web URL once, or takes it from the pairing link.
  - The phone, iPad and Mac reach it over Tailscale, as the PWA does.

## 3. Milestones

| M | What | Done when |
|---|---|---|
| **M0 — contract** | `docs/app-api.md`: every route, field, status code and example, written from §2.1. The JSON fixtures are produced by the server's own serialiser in a test, so the Swift side decodes real output. | Fixtures exist for an idle chat, a live reply, a queued turn, quotes, attachments, sent cards, feedback, docs and comments. |
| **M1 — server API** | `web_chat.feed` with the HTML views moved onto it (the existing web tests unchanged and green); `supervisor/app_api.py` with all routes; Bearer auth; pairing; long-poll; seen/unread. | The full suite is green. Tests refuse no bearer, a wrong login, an unpaired device and a stale device. The JSON and HTML render the same feed for the same chat (a test). |
| **M2 — APNs** | `apns.py`, the `notify` fan-out, config and status, docs. | Unit tests pass: JWT shape and signature verified with openssl, the curl call, a dead token dropped, a bad topic kept and logged. A real push reaches the Mac app and the iOS simulator (sandbox). |
| **M3 — SHAFTKit + scaffold** | The XcodeGen project with every target and the Kit: API client, long-poll loop, drafts, Keychain, pairing, the `mid` send queue, push registration. | `xcodebuild` builds every target. Kit tests decode every M0 fixture and drive the client against a stub server. |
| **M4 — iPhone and iPad UI** | The chat list, feed, composer (all modes, quotes, attachments, Return rules), documents sheet and comments, earlier chats, pairing screen, notification tap → chat. | Driven in the simulator (iPhone 17, iPad) against a branch server on real chat data. |
| **M5 — Mac UI** | Sidebar, feed, composer (Enter/Shift-Enter/Cmd-Enter), documents inspector, menu commands (New message, Stop, End, Next/previous chat Alt-↑/↓), the Dock badge, notifications. | Run on this Mac against the branch server. A push arrives while the window is closed. |
| **M6 — review and verification** | A cross-model review, the docs (`web-and-api.md`, `configuration.md`, `SECURITY.md`, README), and a checklist for your devices. | Every finding fixed or answered. You confirm on the iPhone and iPad. |

**Order:** M0 first, then M1 ∥ M2 ∥ M3. M4 ∥ M5 follow once M3 lands, then M6.

## 4. The multi-agent build (one Workflow, about 8 agents)

Each lane gets its own worktree off `claude/native-apps`, and I merge the lanes. No lane reads `.env`, `instances/`,
`secrets/` or `.supervisor/`. A lane using real chat data runs on my side only.

| Lane | Work | Model | Why |
|---|---|---|---|
| A | M0 + M1 server API | Claude (Opus) | auth and the shared feed model: hard, touches the web's core |
| B | M2 APNs | Codex `gpt-6.1-sol` | self-contained module + tests (implementation/medium) |
| C | M3 Kit + scaffold | Claude | sets every Swift contract the UI lanes build on |
| D | M4 iOS/iPadOS UI | Claude | hard UI work; needs `xcodebuild` and the simulator |
| E | M5 macOS UI | Claude | the same, on macOS |
| R1 | review of the A+B diff (auth, push, secrets) | Codex `gpt-6.1-sol` | a different family from the writer |
| R2 | review of the C+D+E diff | Grok `grok-4.7` | a different family from the writers |
| V | verification | me, in the main session | needs this Mac, the simulator, the live tailnet and the APNs key |

## 5. Verification

- **Python:** the full suite after M1 and after M2. Every bug fixed along the way gets a regression test named after
  it.
- **Swift:** `xcodebuild test` for SHAFTKit, and a build of every target for both platforms.
- **The real system:**
  - A branch web server on its own port (a copy of `.supervisor/`, tailnet :8446). Your :8444 service is untouched.
  - The app is driven in the iOS simulator and on the Mac:
    - pair;
    - chat with a real instance;
    - quote, queue, interject;
    - Next cycle;
    - attach an image;
    - edit a document and comment;
    - lock, or background the app, and get the push;
    - tap it and land in the chat.
- **Only your devices can check:**
  - Xcode install on the iPhone and iPad;
  - a push to a locked phone;
  - the badge;
  - the iPad hardware keyboard.

  I will list exactly what to try.

## 6. What you do

1. Create the APNs key: developer.apple.com → Keys → + → Apple Push Notifications service (Sandbox & Production).
   Save `AuthKey_<KEYID>.p8` to `.supervisor/web/apns/` and tell me the key id. I never send it to a relay.
2. Allow `brew install xcodegen`.
3. When it is done: plug in the iPhone and iPad once, so Xcode can install the app.

## 7. Status

Signed off 2026-11-03. Building on `claude/native-apps`.

### 2026-11-03 evening — built, reviewed, verified short of signing

On `claude/native-apps` (not pushed, not merged to `main`):

- **Built:** M0–M5. The `/app/v1` server and APNs, SHAFTKit, the iPhone/iPad UI (`claude/na-ios`) and the Mac UI
  (`claude/na-mac`) are merged.
- **Reviewed:** Grok reviewed the server (6 findings) and Codex the Swift (9 findings). All 15 are fixed, each with a
  regression test where it fits.
- **Driving the merged app found 2 more bugs, now fixed:**
  - an endless spinner when the server cannot be reached;
  - a pairing that survives an uninstall.
- **Tests:**
  - full Python suite: 3131 OK (57 skipped);
  - SHAFTKit: 71;
  - Mac tests: 13;
  - SHAFT-iOS (simulator) and SHAFT-macOS both build.
- **Verified in the iPhone simulator** against the fixture server with the real `supervisor.apns`:
  - pairing, and the app's APNs sandbox token registered;
  - instance messages land in the chat;
  - the unreachable-server screen shows its error with Retry and Settings, then recovers by itself;
  - a reinstall shows pairing again.

### 2026-11-04: push verification

Simulator verification covered title-only APNs payloads, extension fetching, unread badges and chat links.
Physical-device installation and notification taps still need verification. The first badge after pairing
was intermittently absent; the cause was unknown.
The app icon uses the PWA mark (`apps/shaft/Shared/make_icons.py`).
