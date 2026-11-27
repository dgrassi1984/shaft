# SHAFT for iPhone, iPad and Mac

The native chat apps: one SwiftUI codebase, an iOS app
for iPhone and iPad, a native macOS app (not Catalyst), and a notification service extension for each. They talk to
the web service's JSON API, `/app/v1` ([app-api.md](../../docs/app-api.md)), over the tailnet.

## Layout

| Path | What |
|---|---|
| `project.yml` | the XcodeGen spec: every target, setting, entitlement and Info.plist key. `SHAFT.xcodeproj` and `Generated/` are made from it and git-ignored; never edit them by hand |
| `Packages/SHAFTKit/` | the local Swift package every target uses: the API models and client, the long-poll, pairing and the Keychain, drafts, the outbox, push registration, the notification body fetch, and the observable stores the UI reads |
| `Shared/` | the SwiftUI views and view models both apps draw: the chat, feed, composer, documents, pairing and settings ([Shared/README.md](Shared/README.md)) |
| `iOS/` | the iPhone and iPad app: its entry point (`SHAFTApp.swift`) and its layouts (`IOSRoot.swift`: the phone's full-screen chat with a chat switcher, the iPad's split view) |
| `macOS/` | the Mac app: its entry point, window, menu commands and Settings, and the AppKit text views of Shared's seams (a message's text selects in place with ↩ Reply; the composer's field) ([macOS/README.md](macOS/README.md)) |
| `macOSTests/` | `SHAFTMacTests`: the Mac's Markdown text (`MarkdownAttributed`) and its text views' menu items, with no window |
| `NotificationService/` | the extension both platforms build: it fetches the notification's body |
| `MeetingWidgets/` | the iOS extension for the meeting Live Activity and recording controls |
| `watchOS/` | the Apple Watch app: talk to an instance from the wrist, through the iPhone app (below) |
| `DevServer/` | `fixture_server.py`: the real `/app/v1` over throwaway data, to drive the apps without the live service (below) |
| `ScreenshotTests/` | `SHAFTScreenshots`: a UI test that drives the iOS demo and writes the App Store screenshots ([AppStore/README.md](AppStore/README.md)) |
| `AppStore/` | the App Store listing: text, screenshots, the support and privacy pages, and the scripts that make them |

| Target | Platform | Bundle id |
|---|---|---|
| `SHAFT-iOS` | iOS 26+, iPhone and iPad | `$(SHAFT_BUNDLE_PREFIX).shaft` |
| `SHAFT-macOS` | macOS 26+ | `$(SHAFT_BUNDLE_PREFIX).shaft` |
| `NotificationService-iOS` | iOS 26+ | `$(SHAFT_BUNDLE_PREFIX).shaft.NotificationService` |
| `NotificationService-macOS` | macOS 26+ | `$(SHAFT_BUNDLE_PREFIX).shaft.NotificationService` |
| `MeetingWidgets-iOS` | iOS 26+, embedded in `SHAFT-iOS` | `$(SHAFT_BUNDLE_PREFIX).shaft.MeetingWidgets` |
| `SHAFT-watchOS` | watchOS 26+, embedded in `SHAFT-iOS` | `$(SHAFT_BUNDLE_PREFIX).shaft.watchkitapp` |
| `SHAFTMacTests` | macOS 26+ | `$(SHAFT_BUNDLE_PREFIX).shaft.MacTests` (unit tests, the `SHAFT-macOS` scheme's test action) |
| `SHAFTScreenshots` | iOS 26+ | `$(SHAFT_BUNDLE_PREFIX).shaft.Screenshots` (UI test, the `SHAFT-Screenshots` scheme's test action) |

All of them use Swift 6 with complete strict concurrency, automatic signing with your local team and bundle prefix. The apps depend on
[MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui), pinned to exactly 2.4.1.

## Generate and build

Requires Xcode 27 or later and XcodeGen (`brew install xcodegen`).

Create `apps/shaft/Signing.local.yml` before generating the project. This file is gitignored.
Use your Apple Developer Team ID and a bundle prefix you control. The include is required;
create this file even for an unsigned build. Example, from `apps/shaft/`:

```bash
# Working directory: apps/shaft/
cat > Signing.local.yml <<'YAML'
settings:
  base:
    DEVELOPMENT_TEAM: YOUR_TEAM_ID
    SHAFT_BUNDLE_PREFIX: com.example
YAML
```

The app identifier and APNs topic are `$(SHAFT_BUNDLE_PREFIX).shaft`; extensions append
`.NotificationService` and tests append `.MacTests`. The shared Keychain group also includes
`$(DEVELOPMENT_TEAM)`. For unsigned builds, use illustrative values in the local file.

```bash
# Working directory: repository root
cd apps/shaft
xcodegen generate                       # makes SHAFT.xcodeproj and Generated/ from project.yml
```

Run `xcodegen generate` again after any change to `project.yml` or after adding or removing a source file.

Regenerate the shared app icon catalog from the PWA mark with `python3 apps/shaft/Shared/make_icons.py` from the repository root.

Unsigned builds of every target (what agents and CI run):

```bash
# Working directory: apps/shaft/
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-iOS -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-macOS -destination 'platform=macOS' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
```

Each app scheme builds its notification extension too.
Every build, in Xcode or `xcodebuild` and in any configuration, stamps the apps and extensions from git.
The nearest reachable `v[0-9]*` tag supplies the plain release version. The build number is the HEAD commit count
plus 100, preserving the offset needed after a history rewrite. No version override is needed.
Settings shows the git description with `+changes` when tracked files differ from HEAD.
Building requires git and a reachable release tag; missing tags fail the build.

## Meeting shortcuts

Say "Record a meeting with SHAFT" or "Stop recording my meeting with SHAFT" to Siri, or run
**Record Meeting** or **Stop Meeting Recording** in Shortcuts. On Mac, **Record Meeting** defaults to
**Online** and also offers **In Person**. On iPhone and iPad, add either recording control to Control Center
or choose one for the Action button on supported iPhones. Starting on iOS opens SHAFT to use the microphone.
The Live Activity shows elapsed time and **Stop** on the Lock Screen and in the expanded Dynamic Island.
See [Meetings](../../docs/meetings.md) for recording and upload behavior.

## The demo

The iPhone and iPad pairing screen offers **Try the demo**: the real app over sample data built into SHAFTKit
(`Packages/SHAFTKit/Sources/SHAFTKit/Demo/`). `DemoServer`, a `URLProtocol`, answers the `/app/v1` routes the app and
its watch relay use for three sample instances, and refuses every other host, so nothing leaves the device. Nothing is
saved to the Keychain; the demo's drafts and outbox live in a temporary folder that Leave the demo (Settings) removes,
and a relaunch starts unpaired. It is how App Review sees the app working, since no reviewer can reach a server on a
tailnet. A Debug build opens it at launch with `SHAFT_START_DEMO=1` (`SIMCTL_CHILD_SHAFT_START_DEMO=1` through
simctl), so the watch app has the iPhone's sample instances.

## Try it against a fixture server

`DevServer/fixture_server.py` runs the web service's own `/app/v1` (app_api, web_chat, documents, comments,
feedback, the push fan-out) on `127.0.0.1` over a temporary folder it seeds: three instances (`INSTANCES` in the fixture), a chat with every item
kind (tundra's thread holds its cycle's answer, "⏾ from its cycle"), an earlier chat (its From feed holds an answer
for it, "re: «title»"), documents with comment threads. It fakes only what lies outside the web service: the tailnet
login, the API a reply streams from (a scripted reply; "fail" in a message fails it, "slow" streams for a minute, "wide" exercises wide tables and code),
APNs (each push is written to `<dir>/pushes/` with apns.py's payload, and handed to a simulator with `--simctl`) and
the instances' live state. It refuses the real `.supervisor/` and the ports of the running services.

```bash
# Working directory: repository root
python3 apps/shaft/DevServer/fixture_server.py serve --dir /tmp/shaft-fixture --port 8899 --simctl <simulator udid>
python3 apps/shaft/DevServer/fixture_server.py pair --dir /tmp/shaft-fixture --port 8899 --name iphone-app
python3 apps/shaft/DevServer/fixture_server.py act --dir /tmp/shaft-fixture --port 8899 question <name> "Archive it?"
```

Use one of the seeded names for `<name>`; `act` defaults to the first when the name is omitted.
The fixture accepts each device's APNs topic unless `SHAFT_APNS_TOPIC` is set, which restricts registration to that topic.
`serve` prints a pairing link; type or paste it into the app. `act` (with the same `--dir`, `--port` and `--simctl`)
makes an instance send a message or a question (with its push), change its state, write a document, or have a chat
past the 80-turn window (`long`). Settings writes stay in the fixture's own instance files. Service restarts change
fake records only; no CLI or launchd runs. `act ... services busy "<name> replying"` makes the API refuse a restart
and gives a scheduler drain that waiting line; `act ... services busy` clears it. `act ... services drain-end`
finishes a pending drain with a new fake pid. Cancel is available in the app. `act ... version 0` (or `version
missing`) answers `GET /app/v1/version` as an older server would, so the app shows "This app needs a newer SHAFT
server" at its next session; `act ... version` restores the real answer. Update server itself runs `shaft deploy`
locally, which a fixture run has not: it shows only for a loopback pairing with `~/SHAFT` installed. Run
`python3 apps/shaft/DevServer/test_fixture_settings.py` to check the settings and service routes without opening an app.
The setup wizard's routes (`/app/v1/setup/*`, docs/app-api.md §18) answer as the real verbs would, over the fixture's
folder: OpenRouter is off until a key is set (only the key's name is kept, in `<dir>/setup.json`), a model whose name
contains "fail" fails its check, `new` makes the instance's folders here (paused until the first reply), and
`first-reply` is the real verb; `test_fixture_setup.py` walks them from Model to Say hello. Access: `share` is the real
check on the fixture's own instance (a folder inside the checkout is refused, as on a real server), `browser install`
records a fake Chromium version, `/setup/telegram` refuses a token without `:` or containing `bad`, `telegram on` writes
a live service record (never launchd), and a pairing code is "sent" to the bot 3 s after the wizard asks for it.
A Debug iOS build may load plain `http://` from this machine (`NSAllowsLocalNetworking`, added to the built
Info.plist by a Debug-only build step in `project.yml`); a Release iOS build keeps App Transport Security whole. The
Mac app gets `NSAllowsLocalNetworking` in every configuration (its setup wizard talks to the server on 127.0.0.1) and
runs outside the App Sandbox (hardened runtime only); its notification extension stays sandboxed.

For a simulator build that keeps the Keychain entitlement, sign it locally (never registers anything with Apple):

```bash
# Working directory: apps/shaft/
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-iOS -destination 'platform=iOS Simulator,name=iPhone 17' \
  -derivedDataPath build/dd CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build
xcrun simctl install booted build/dd/Build/Products/Debug-iphonesimulator/SHAFT.app
xcrun simctl openurl booted shaft://chat/<name>      # opens that chat, as a notification tap does
xcrun simctl openurl booted shaft://chat/<name>/<cid> # one thread of it (`from`: its From feed)
```

### The Mac app against the fixture server

A Debug build of the Mac app can run unsigned from a terminal. Such a build has no Keychain access group (that needs
the team's signing), so `SHAFT_DEV_DIR` keeps its pairing, drafts and outbox in a folder instead, and
`SHAFT_DEV_PAIR` pairs it at launch with the link `serve` printed (Debug builds only: `macOS/MacDevSession.swift`;
a Release build, or a Debug build without `SHAFT_DEV_DIR`, uses the Keychain):

```bash
# Working directory: apps/shaft/
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-macOS -destination 'platform=macOS' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
cp -R build/dd/Build/Products/Debug/SHAFT.app /tmp/ && codesign --force --deep -s - /tmp/SHAFT.app
SHAFT_DEV_DIR=/tmp/shaft-mac SHAFT_DEV_PAIR='http://127.0.0.1:8899/pair?t=…' /tmp/SHAFT.app/Contents/MacOS/SHAFT
```

`SHAFT_DEV_DIR` stores credentials as JSON on disk and accepts only loopback servers
(`127.0.0.1`, `localhost`, or `::1`). Use Keychain pairing for a tailnet server.
Development runs ask for notification permission only with `SHAFT_DEV_NOTIFY=1`;
an ad-hoc build still receives no APNs token.

`SHAFT_DEV_DRIVE=<script>` drives it from a script of steps (`macOS/MacDevDriver.swift` lists them: open a chat, keys
such as `return`, `shift+return`, `cmd+return`, `option+down`, `cmd+.`, `cmd+shift+n` and `slash`, typing, selecting
text in a message and picking from its right-click menu, Select text, menu bar items, pasting a file, the file preview sheet, the
documents, an earlier chat, Retry on a message not sent, closing the window, a notification click, the time a chat
takes to load, snapshots of the window and its sheet), logging each step and what it changed to stdout. Typing goes through the app's own event
queue, and key combinations through the window, then the menu bar, then the first responder, as AppKit routes them.
Snapshots are the window's own drawing: vibrant areas (the sidebar, the inspector's text) come out blank or dark.
SwiftUI's own buttons cannot be pressed from a script (the steps call what they call instead), and on a locked screen
the window takes no mouse moves (hover) and runs no animations (an inspector opened with one stays 0 wide). On a locked or sleeping screen every window counts as covered, so the chat neither
polls nor is marked seen there: `SHAFT_DEV_PRESENCE=front` (or `shown`) says the window is in front (or shown behind
another app's, which also polls and marks seen) instead.

To time the chat screen, grow an instance's Main with answers the size of real ones (`fixture_server.py act ...
rich <name> 160`), run a Release build compiled with `SWIFT_ACTIVE_COMPILATION_CONDITIONS=DEBUG` (a Debug build's
Swift is several times slower) behind other windows (`open -g`), and drive it with `open`, `until`, `scrollfeed <dy> <n>`
and `earlier`. Every build logs each main-thread stall of 100 ms or more (`Packages/SHAFTKit/.../HitchMonitor.swift`)
with the transition under way; read them, with the transitions' own times, on any Mac or device:

```bash
/usr/bin/log show --last 1h --predicate 'subsystem == "SHAFT" AND (category == "Hitches" OR category == "Transitions")'
```

`SHAFT_DEV_FRAMES=<file>` adds a line per second of frames drawn and frames late (`Shared/Support/FrameMeter.swift`).

A chat that opens blank until scrolled: run `DevServer/mac-feed-blank.drive` on that seed; its `feedgeom` lines log
where the feed is and whether what is on screen of it draws anything (`BLANK` when not).

To check the document-opening crash, seed a long chat with `fixture_server.py act ... long <name> 120`
for the instance `mac-doc-open.drive` opens.
Run the Release build with `SWIFT_ACTIVE_COMPILATION_CONDITIONS=DEBUG` and Debug's
`NSAppTransportSecurity.NSAllowsLocalNetworking=true` Info.plist entry, then set
`SHAFT_DEV_DRIVE=DevServer/mac-doc-open.drive` and repeat. The `MacDev*` helpers require `#if DEBUG`.
This is a manual reproduction procedure; no current failure rate is established.

A process started directly stays behind other apps: start the app with `open -n` (`--env` for the variables, `--stdout`
for the log) when its window must be in front. The `activate` step then takes the front, and takes it again before each
`capture`, since another app may take it back; `capture` logs whether the window has the keys and where the pointer is.

### The setup wizard in a dev run

An unpaired Mac app chooses its first screen from `~/SHAFT` (`macOS/Setup/SetupGate.swift`): no `~/SHAFT/.git` opens
the setup wizard (Welcome, Install, then on the pairing Install made: Model, Your instance, Access, How it works alone
and Say hello, which opens the new instance's chat; its Welcome's "Connect to an existing SHAFT" is the pasted-link
pairing), and an install that is there is paired with on its own (`shaft web pair --local`, no link). A loopback pairing the
server no longer knows (`401 unpaired`) is deleted and paired again once on its own; a second loss shows the error.

Debug builds only, these move the install away from the real `~/SHAFT` and the newest release:

| Variable | Effect |
|---|---|
| `SHAFT_DEV_HOME=<absolute path>` | the home the server goes into (`<path>/SHAFT`). With `SHAFT_DEV_DIR` and without this, `<SHAFT_DEV_DIR>/home`: a dev run never reaches the real `~/SHAFT` |
| `SHAFT_DEV_SERVER_REPO`, `SHAFT_DEV_SERVER_REF`, `SHAFT_DEV_TEMPLATE_REF` | clone that origin at that branch or tag, and make that ref the `template` branch, instead of the newest tagged release. All three or none: a partial set stops the app at launch, naming what is missing |
| `SHAFT_DEV_SETUP=<step>` | open the wizard at a step after Install (`model`, `instance`, `access`, `alone`, `hello`) on the run's own pairing: with `SHAFT_DEV_DIR` and `SHAFT_DEV_PAIR` (a fixture server's link) its steps run against the fixture, with nothing installed |

The Install step really runs the commands (the Command Line Tools check, `git clone`, the uv download, `shaft install`,
which installs the services with launchd): run it only on a Mac or user account meant for it.
If the Command Line Tools are missing, someone with an administrator account on that Mac must install them.
After the pair step the wizard goes on to Model while the server has no instance (or this wizard made one already);
a server with instances opens the app. The Model step's "Sign in" for ChatGPT runs `shaft codex login --json` from the install. The app tries
to open the relayed `url` once and shows the page link and `code` with copy controls. It shows recent `say` lines
while waiting. If the page is still missing after 20 seconds, it shows a notice and the executable path from `codex`.
A final `ok` reports success or carries an `error`. Cancel terminates the command, which stops Codex. The Access step grants the
non-Google rows (a folder, the browser, web search, Telegram) through `/access/<n>`, `/setup/secret` and
`/setup/telegram*` (the bot's token, the service on, the pairing code shown until the pairing lands). Its Google
rows (mail labels, calendars, Drive, Chat) are granted by "Set up Google…" (`macOS/Setup/GoogleStep.swift`): five
cards into Google Cloud (a project, the APIs, the audience published In production, the Data Access scopes, a Desktop
app client), then the client's JSON dropped or chosen and sent on `shaft google client google --json`'s stdin, the
sign-in through `shaft google login google <address> --client google --json` (the app opens its `{"url"}`; Cancel
terminates it; a client sent meanwhile ends it), and the pickers (`/google/google/labels|calendars`), which start
from the server's grants: Apply reads `/access/<n>` again and sends only the difference, in order (a cleared control
is a revocation, "can change events" cleared a `revoke` with `writable`). Where it
got is kept in `setup.state` (never the JSON); a ticked Google row still off stops Continue on Access. Against the
fixture server those two commands cannot run (nothing is installed): `fixture_server.py act google` signs the account
in on the fixture's folder instead, so the pickers and grants can be driven.

### Voice against the fixture server

Voice mode asks for the microphone and speech recognition the first time it opens, and needs the
on-device speech model of its language (English or Italian). A missing model is offered as Download on the voice
screen, a system download that runs only on that tap. The Talk button shows for an instance whose `voice_mode` is `on`
(its Settings → Voice). The fixture server's voice replies are scripted: a question containing "tool" runs two tools
first (the working sound), one containing "slow" thinks 4.5 s in silence first (the waiting sound), one containing
"long" gets a long answer (room for a barge-in), anything else a short one.

Debug builds take two test hooks from the environment (`Shared/Voice/VoiceSetup.swift`, read when a session starts):

| Variable | What |
|---|---|
| `SHAFT_DEV_VOICE_FILES=<path>@<seconds>[,<path>@<seconds>…]` | recorded speech instead of the microphone: each file is heard at its offset from the session's start, at real time, with silence between and after. No permission is asked. A relative path is taken from `SHAFT_DEV_DIR` (the Mac), else from the app's Documents folder |
| `SHAFT_DEV_VOICE_SILENT=1` | the silent audio graph: nothing is played aloud and the microphone is never opened. Alone, the session hears silence |

Use both together for a run that neither listens nor plays aloud. A value that cannot be used (a malformed item, a
file that is not there) stops the session with what is wrong, shown on the voice screen; it never falls back to the
microphone. The speech model must already be installed: a test run never downloads it. Make the speech with `say -o`
(written to the file, never played):

```bash
mkdir -p /tmp/shaft-voice
say -o /tmp/shaft-voice/tools.aiff "Could you use a tool to check the garden notes"
say -o /tmp/shaft-voice/long.aiff "Tell me the long story of the lanterns"
say -o /tmp/shaft-voice/barge.aiff "Wait, which lantern is by the door"
```

iPhone Simulator: `SIMCTL_CHILD_` passes a variable to the launched app. The Simulator reads the Mac's files; otherwise
copy them into `$(xcrun simctl get_app_container booted <bundle id> data)/Documents/` and give the bare file name.

```bash
# Working directory: apps/shaft/
xcrun simctl install booted build/dd/Build/Products/Debug-iphonesimulator/SHAFT.app
SIMCTL_CHILD_SHAFT_DEV_VOICE_FILES=/tmp/shaft-voice/long.aiff@2,/tmp/shaft-voice/barge.aiff@14 \
SIMCTL_CHILD_SHAFT_DEV_VOICE_SILENT=1 \
  xcrun simctl launch --terminate-running-process booted <bundle id>
```

The Mac app (paired in `SHAFT_DEV_DIR` as above), with the files in that folder and a drive script: `menu Talk` opens
the voice panel; `voicewait <state> <seconds>` waits for the session to reach `listening`, `userTalking`, `thinking`,
`speaking` or `ended`; `voicestate` logs its state, your transcript, the sentence spoken (marked when it is a status
line), the status line, the voice substitution, the last turn's delay and Mute.

```bash
# Working directory: apps/shaft/
cp /tmp/shaft-voice/tools.aiff /tmp/shaft-mac/
cat > /tmp/shaft-mac/voice.drive <<'DRIVE'
open tundra
menu Talk
voicewait thinking 20
voicewait speaking 30
voicestate
voicewait listening 60
voicestate
quit
DRIVE
SHAFT_DEV_DIR=/tmp/shaft-mac SHAFT_DEV_VOICE_FILES=tools.aiff@1 SHAFT_DEV_VOICE_SILENT=1 \
  SHAFT_DEV_DRIVE=/tmp/shaft-mac/voice.drive /tmp/SHAFT.app/Contents/MacOS/SHAFT
```

The audio graph follows the system's changes by itself (`VoiceAudioGraph`): an engine configuration change (a device
added or removed, another input picked) starts the engine again, and so does an iOS route change that adds or removes a
device. An iOS interruption (a call, Siri) pauses it until the system says the app may resume. A session goes on with
the iPhone locked (`UIBackgroundModes` `audio`). The hooks cannot show any of this: it needs a device.

### The Apple Watch app

watchOS runs no VPN, so the watch can't reach the server on the tailnet: it holds no credential and makes no network
call. It asks the iPhone app over WatchConnectivity (`iOS/WatchRelay.swift`; the messages are SHAFTKit's
`Watch/WatchProtocol.swift`, which the watch target compiles as a file). The iPhone lists the instances, sends what was
dictated as a voice message (`voice=1`, the watch's own `mid`), follows the reply to its end (`WatchTurn`) and pushes it
back with its language, the instance's voice for it and the sentences to speak. iOS wakes the iPhone app for a watch
message and gives it a short while; a reply that outlasts it still comes as the usual notification, and opening the
instance on the watch again fetches it (`latest`).

The iPhone Air simulator is paired with an Apple Watch simulator (`xcrun simctl list pairs`). The iOS build embeds the
watch app; install it on both and launch the iPhone app first. The watch's dictation sheet can't be scripted, so a Debug
build takes the first turn from `SHAFT_WATCH_SAY` (through simctl, `SIMCTL_CHILD_SHAFT_WATCH_SAY`): it opens the first
instance and sends that text as if dictated; `SHAFT_WATCH_OPEN=1` opens the first instance without sending anything.

```bash
# Working directory: apps/shaft/, after the iOS simulator build above; the iPhone app paired with the fixture server
APP=build/ios-sim/Build/Products/Debug-iphonesimulator/SHAFT.app
xcrun simctl install <iphone-udid> "$APP" && xcrun simctl install <watch-udid> "$APP/Watch/SHAFT.app"
xcrun simctl launch <iphone-udid> "$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$APP/Info.plist")"
SIMCTL_CHILD_SHAFT_WATCH_SAY="How did October close?" \
  xcrun simctl launch <watch-udid> "$(/usr/libexec/PlistBuddy -c 'Print CFBundleIdentifier' "$APP/Watch/SHAFT.app/Info.plist")"
xcrun simctl io <watch-udid> screenshot /tmp/watch.png
```

## Tests

```bash
# Working directory: repository root
cd apps/shaft/Packages/SHAFTKit
swift test                                   # or: xcodebuild test -scheme SHAFTKit -destination 'platform=macOS'
```

The fixture tests decode every `*.json` in the server's `supervisor/tests/app_api_fixtures/`, or in
`$SHAFT_APP_API_FIXTURES` when it is set. Each one is matched to its model by its keys and must decode with
every non-null field kept, so a new fixture needs no test code, and a field the server adds that the Kit does not
read fails the test.

The Mac app's own tests (`macOSTests/`, no window needed):

```bash
# Working directory: apps/shaft/
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-macOS -destination 'platform=macOS' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO test
```

## Sign and install on your own devices

Xcode Run installs the apps directly, development-signed. For TestFlight or App Store distribution, see
[Release to the App Store](#release-to-the-app-store).

1. In Xcode → Settings → Accounts, sign in with your own Apple Developer account.
   Set its team and your bundle prefix in `Signing.local.yml`, then regenerate the project.
   The team id is the `OU` of the Apple Development certificate, not the id in the certificate's name.
2. Open `SHAFT.xcodeproj`. Automatic signing registers the two App IDs with Push Notifications and Keychain Sharing on
   the first signed build.
3. **iPhone or iPad:** connect it once by cable, trust the Mac, turn on Developer Mode (Settings → Privacy & Security),
   then pick the `SHAFT-iOS` scheme and the device and Run. From the terminal:

   ```bash
   # Working directory: apps/shaft/
   xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-iOS -destination 'platform=iOS,name=<device name>' \
     -derivedDataPath build/dd -allowProvisioningUpdates build
   xcrun devicectl device install app --device <device name or id> \
     build/dd/Build/Products/Debug-iphoneos/SHAFT.app
   ```

4. **Mac:** pick the `SHAFT-macOS` scheme and Run, or build it as above with `-destination 'platform=macOS'` and open
   `build/dd/Build/Products/Debug/SHAFT.app`.

### The pairing credential

The apps and their extensions share one Keychain item (the server URL and the device's bearer), in the access group
`$(DEVELOPMENT_TEAM).$(SHAFT_BUNDLE_PREFIX).shaft`. `project.yml` builds the group from `DEVELOPMENT_TEAM` once
(`SHAFT_KEYCHAIN_GROUP`) and writes it both into each target's `keychain-access-groups` entitlement and into its
Info.plist as `SHAFTKeychainAccessGroup`, which SHAFTKit reads. A target without the key refuses to start rather than
keep the credential where the extension cannot read it.

On the first iOS app launch after installation, the app deletes any pairing left in the Keychain before opening its
session. An app-only UserDefaults flag keeps later launches paired; notification extensions never run this check.
The first launch after updating an older version without the flag also requires pairing again. On macOS, deleting
the app normally keeps its preferences as well as its Keychain item, so this install check is not applied there.

### Pair

On the Mac: `shaft web pair --name iphone-app` prints a link that works once, for 10 minutes. Paste it into the app
(Universal Clipboard carries it from the Mac). The app takes the server's address from the link and keeps the bearer
in the Keychain. `shaft web status` lists the device; `shaft web unpair <device>` revokes it, and the app then shows
its pairing screen again. The Mac app pairs with the SHAFT installed on the same Mac without a link (above, "The setup
wizard in a dev run"); its pasted-link pairing is the wizard's "Connect to an existing SHAFT".

### Push

- An Xcode build is development-signed, so its push token belongs to the APNs sandbox. The app reads `aps-environment`
  from its embedded provisioning profile and registers its token with `env` `sandbox` (`production` for a build with
  no profile or a production one), `topic` its bundle id, at every launch.
- Create your own APNs authentication key for your Apple Developer team. The server needs `SHAFT_APNS_KEY_ID`, `SHAFT_APNS_TEAM_ID=<your-team-id>`, `SHAFT_APNS_TOPIC=<your-bundle-prefix>.shaft`
  and the key at `.supervisor/web/apns/AuthKey_<KEYID>.p8` ([configuration.md](../../docs/configuration.md)). Without
  them registration answers `503 push_off`, naming what is missing. Set each value from the repository root
  with `bin/shaft settings set KEY=VALUE`, then restart the web service after active work finishes.
  Use the same team as `Signing.local.yml` and the app's expanded bundle identifier for the topic.
  These are settings in `supervisor/apns.py`; the server does not read the XcodeGen signing file.
- A push carries the title only. The notification service extension fetches the body from `/app/v1/push/latest`
  with the shared credential, within its time limit; on any failure the title shows alone.
- The simulator receives remote pushes too (Xcode 14+ on Apple silicon), so the iOS extension can be tried there.

## Release a signed build

`Scripts/release-mac.sh` makes the Mac app's download, a Developer ID signed, notarized, stapled
`build/release/SHAFT-<version>.dmg` (with an Applications shortcut), and its signed update feed,
`build/release/appcast.xml`. It uploads nothing.

One-time setup, with your paid Apple Developer team:

1. A **Developer ID Application** certificate in your login keychain (Xcode → Settings → Accounts → Manage
   Certificates → + → Developer ID Application). `security find-identity -v -p codesigning` must list it.
2. Use a **Team App Store Connect API key** with the Developer role or higher for notarization. In App Store
   Connect, go to Users and Access → Integrations → Team Keys. You can use the same key with `release-ios.sh`.

   For an interactive release, you can use `--notary-profile shaft-notary` instead. A profile saved by
   `notarytool store-credentials` in the default keychain is unreadable while the Mac's screen is locked.
   `notarytool` then reports the profile as missing. Use the API key for unattended releases.
   To create the profile, run this command and enter an app-specific password from account.apple.com
   (Sign-In and Security) when prompted.

   ```bash
   xcrun notarytool store-credentials shaft-notary --apple-id '<your Apple ID>' --team-id '<TEAMID>'
   ```

3. **Provisioning profiles.** A Developer ID app that uses push (`com.apple.developer.aps-environment`) or a
   team keychain group needs a provisioning profile that grants it, for the app *and* for the notification extension
   (`<prefix>.shaft` and `<prefix>.shaft.NotificationService`). In the developer portal, both App IDs need Push
   Notifications (the app) and Keychain Sharing capabilities; the script lets Xcode create and embed the Developer ID
   profiles at export (`-allowProvisioningUpdates`, using the account signed in to Xcode). The entitlement's
   value is the build setting `SHAFT_APS_ENVIRONMENT` in `project.yml`: `development` for every everyday build (Release
   included, signed with your Apple Development identity), and the release script passes
   `SHAFT_APS_ENVIRONMENT=production` for its archive. It checks the exported bundles and fails if a profile is missing,
   the app's does not grant production push, or the signed app's `aps-environment` is not `production`.

4. **Update signing key.** Run Sparkle's `generate_keys` once. It comes with the Sparkle package: after any build
   it is in the derived data's `SourcePackages/artifacts/sparkle/Sparkle/bin/` (`release-mac.sh` uses
   `build/release/dd/SourcePackages/artifacts/sparkle/Sparkle/bin/`). It stores the EdDSA private key in the login
   keychain, service `https://sparkle-project.org`, account `ed25519`. Set its public half as `SHAFT_UPDATE_KEY` in
   `project.yml`; it goes into every shipped app for good. Back up the private key with `generate_keys -x <file>`
   into a password manager, then delete the file. Never put the private key in the repository or send it anywhere.
   Losing it means updates can only be shipped by rotating the key with a Developer ID-signed DMG from the same
   team. Sparkle allows changing the key or the code signature, never both at once. On a new Mac, import the backup
   with `generate_keys -f <file>`, then delete the file.

Then, from a clean checkout on a release tag:

```bash
# Working directory: apps/shaft/
Scripts/release-mac.sh --api-key-path '<path to AuthKey_<KEYID>.p8>' \
  --api-key-id '<KEYID>' --api-issuer '<ISSUER UUID>'
```

Pass one notarization credential. Use `--api-key-path PATH --api-key-id ID --api-issuer ID` together,
or pass `--notary-profile NAME`.

Other options include `--team TEAMID` (default `DEVELOPMENT_TEAM` in `Signing.local.yml`), `--out DIR` (default `build/release`),
`--feed-url URL` (default `https://dgrassi1984.github.io/shaft-app/appcast.xml`; baked into the shipped app, so never
change it for released apps), `--download-base URL` (default for a tagged build:
`https://dgrassi1984.github.io/shaft-app/`; required for an untagged trial), `--allow-untagged` (a trial build from a commit that is not a `v*` tag), `--dry-run` (checks the
local preconditions and prints the plan without signing or contacting Apple), `--self-test` (checks the script's
entitlement readers, URL rules and appcast writer on synthetic data; signs nothing). Both URL options accept `https`, or `http` to `127.0.0.1`
or `localhost` only, for a local test. It stops at the first failed step, and when Apple rejects the notarization it
prints the submission id and `notarytool log`.

The script refuses to sign when the keychain key's public half differs from the app's `SUPublicEDKey`. It checks
the exported app's update settings and the Developer ID signatures of Sparkle's helpers. After stapling, it
EdDSA-signs the DMG and writes `build/release/appcast.xml`, a single-item feed; both are signed and verified.
The Mac app's update window shows the release tag's notes inline and links to the GitHub release.
On success it prints the DMG's path and SHA-256, and the feed's path.

Per release:

1. Copy `build/release/SHAFT-<version>.dmg` and the signed `build/release/appcast.xml` into `shaft-app/` in the
   public `dgrassi1984/dgrassi1984.github.io` Pages checkout. Keep the DMG's exact name.
2. Review and push the site repository. The DMG and feed are served at `https://dgrassi1984.github.io/shaft-app/`.
   Never edit `appcast.xml` by hand: it is signed, and installed apps refuse a modified feed.
3. The `shaft` repository stays private. Optionally attach the DMG to the tag's GitHub release as an archive copy.

**Test an update locally.** Make two trial builds from two commits, A older and B newer, each with
`--allow-untagged --feed-url http://127.0.0.1:<port>/appcast.xml --download-base http://127.0.0.1:<port>/ --out <dir>`,
choosing a port nothing else listens on (`lsof -nP -iTCP:<port> -sTCP:LISTEN` prints nothing): both URLs are
baked into the builds and the signed feed. Use a separate output directory for each. Serve B's DMG and
`appcast.xml` from one folder with `python3 -m http.server <port> --bind 127.0.0.1`. Copy A's app somewhere other than `/Applications`, open it, and
choose **Check for Updates…**. With the app closed, `defaults delete <bundle id> SULastCheckTime` makes a scheduled
check due at the next launch. Everyday builds, not from `release-mac.sh`, carry no feed and never check.

## Release to the App Store

The iPhone/iPad app, its Share, notification, and MeetingWidgets extensions, and its embedded Apple Watch app ship together.
The Mac app stays outside the Store: its setup wizard runs tools and writes the server installation without App
Sandbox. Its Developer ID distribution remains [Release a signed build](#release-a-signed-build).

One-time setup, with a paid Apple Developer team:

1. Set the team and bundle prefix in `Signing.local.yml`. Register explicit App IDs for `<prefix>.shaft`,
   `<prefix>.shaft.ShareExtension`, `<prefix>.shaft.NotificationService`, `<prefix>.shaft.MeetingWidgets`,
   and `<prefix>.shaft.watchkitapp`.
   Enable Push Notifications for the iOS app and Keychain Sharing for the app and its iOS extensions.
2. Create the iOS app record in App Store Connect with the app's bundle ID.
3. Sign in to Xcode → Settings → Accounts. Automatic signing creates the Apple Distribution certificate and
   provisioning profiles with `-allowProvisioningUpdates`; the account needs permission to manage them.

From a clean checkout on a release tag:

```bash
# Working directory: apps/shaft/
Scripts/release-ios.sh
# Export, verify, then upload using the account signed in to Xcode:
Scripts/release-ios.sh --out build/app-store-upload --upload
```

The script archives Release with production APNs, then exports `build/release-ios/export/SHAFT.ipa` by default.
It checks all five bundles for matching version and build numbers and for the absence of `NSAllowsLocalNetworking`.
It requires privacy manifests for the iOS app, ShareExtension, NotificationService, and the Watch app.
It also checks the iOS app's signed production push entitlement and exempt-encryption declaration.
It prints the IPA path. With `--upload`, it first verifies a local export,
then exports the same archive with destination `upload`. App Store Connect still processes and validates the upload.

Options: `--team TEAMID`, `--out DIR` (choose a fresh export directory), `--allow-untagged` (allows an untagged or dirty
trial build), `--manual-signing` (manual export signing), `--dry-run` (preconditions and printed commands without
signing or Apple requests), and `--self-test`
(synthetic plist checks). To authenticate with an App Store Connect API key instead of the Xcode account, pass
`--api-key-path PATH --api-key-id ID --api-issuer ID` together. No password is passed to the script.

Archiving uses automatic signing. Export also uses automatic signing by default. Use `--manual-signing` when your API
key lacks access to cloud-managed distribution certificates. Register an App ID for every archived app and extension,
including each new extension.
Create a manually managed App Store provisioning profile for each App ID with your Apple Distribution certificate
in the Apple Developer portal or through the App Store Connect API. Install the profiles in
`~/Library/Developer/Xcode/UserData/Provisioning Profiles/`. The script selects the matching profile with the latest
expiration date for each bundle. Xcode-managed profiles are ineligible for manual export.
With `--dry-run`, the script prints the selected profiles if the archive already exists.

Listing material is in [AppStore/README.md](AppStore/README.md).
