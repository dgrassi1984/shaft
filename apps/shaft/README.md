# SHAFT for iPhone, iPad and Mac

The native chat apps ([PLAN-native-apps.md](../../docs/plans/PLAN-native-apps.md)): one SwiftUI codebase, an iOS app
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
| `DevServer/` | `fixture_server.py`: the real `/app/v1` over throwaway data, to drive the apps without the live service (below) |

| Target | Platform | Bundle id |
|---|---|---|
| `SHAFT-iOS` | iOS 26+, iPhone and iPad | `io.github.dgrassi1984.shaft` |
| `SHAFT-macOS` | macOS 26+ | `io.github.dgrassi1984.shaft` |
| `NotificationService-iOS` | iOS 26+ | `io.github.dgrassi1984.shaft.NotificationService` |
| `NotificationService-macOS` | macOS 26+ | `io.github.dgrassi1984.shaft.NotificationService` |
| `SHAFTMacTests` | macOS 26+ | `io.github.dgrassi1984.shaft.MacTests` (unit tests, the `SHAFT-macOS` scheme's test action) |

All of them use Swift 6 with complete strict concurrency, team `<TEAM_ID>` and automatic signing. The apps depend on
[MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui), pinned to exactly 2.4.1.

## Generate and build

Requires Xcode 27 or later and XcodeGen (`brew install xcodegen`).

```bash
cd apps/shaft
xcodegen generate                       # makes SHAFT.xcodeproj and Generated/ from project.yml
```

Run `xcodegen generate` again after any change to `project.yml` or after adding or removing a source file.

Regenerate the shared app icon catalog from the PWA mark with `python3 apps/shaft/Shared/make_icons.py` from the repository root.

Unsigned builds of every target (what agents and CI run):

```bash
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-iOS -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-macOS -destination 'platform=macOS' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
```

Each app scheme builds its notification extension too.

## Try it against a fixture server

`DevServer/fixture_server.py` runs the web service's own `/app/v1` (app_api, web_chat, documents, comments,
feedback, the push fan-out) on `127.0.0.1` over a temporary folder it seeds: three instances, a chat with every item
kind, an earlier chat, documents with comment threads. It fakes only what lies outside the web service: the tailnet
login, the API a reply streams from (a scripted reply; "fail" in a message fails it, "slow" streams for a minute, "wide" exercises wide tables and code),
APNs (each push is written to `<dir>/pushes/` with apns.py's payload, and handed to a simulator with `--simctl`) and
the instances' live state. It refuses the real `.supervisor/` and the ports of the running services.

```bash
python3 apps/shaft/DevServer/fixture_server.py serve --dir /tmp/shaft-fixture --port 8899 --simctl <simulator udid>
python3 apps/shaft/DevServer/fixture_server.py pair --dir /tmp/shaft-fixture --port 8899 --name iphone-app
python3 apps/shaft/DevServer/fixture_server.py act --dir /tmp/shaft-fixture --port 8899 question thelos "Archive it?"
```

`serve` prints a pairing link; type or paste it into the app. `act` (with the same `--dir`, `--port` and `--simctl`)
makes an instance send a message or a question (with its push), change its state, write a document, or have a chat
past the 80-turn window (`long`). A Debug build may load plain `http://` from this machine (`NSAllowsLocalNetworking`,
added to the built Info.plist by a Debug-only build step in `project.yml`); a Release build keeps App Transport
Security whole.

For a simulator build that keeps the Keychain entitlement, sign it locally (never registers anything with Apple):

```bash
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-iOS -destination 'platform=iOS Simulator,name=iPhone 17' \
  -derivedDataPath build/dd CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual build
xcrun simctl install booted build/dd/Build/Products/Debug-iphonesimulator/SHAFT.app
xcrun simctl openurl booted shaft://chat/thelos      # opens that chat, as a notification tap does
```

### The Mac app against the fixture server

A Debug build of the Mac app can run unsigned from a terminal. Such a build has no Keychain access group (that needs
the team's signing), so `SHAFT_DEV_DIR` keeps its pairing, drafts and outbox in a folder instead, and
`SHAFT_DEV_PAIR` pairs it at launch with the link `serve` printed (Debug builds only: `macOS/MacDevSession.swift`;
a Release build, or a Debug build started from Xcode or Finder, uses the Keychain):

```bash
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-macOS -destination 'platform=macOS' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
cp -R build/dd/Build/Products/Debug/SHAFT.app /tmp/ && codesign --force --deep -s - /tmp/SHAFT.app
SHAFT_DEV_DIR=/tmp/shaft-mac SHAFT_DEV_PAIR='http://127.0.0.1:8899/pair?t=…' /tmp/SHAFT.app/Contents/MacOS/SHAFT
```

`SHAFT_DEV_DRIVE=<script>` drives it from a script of steps (`macOS/MacDevDriver.swift` lists them: open a chat, keys
such as `return`, `shift+return`, `cmd+return`, `option+down`, `cmd+.`, `cmd+shift+n` and `slash`, typing, selecting
text in a message and picking from its right-click menu, Select text, menu bar items, pasting a file, Quick Look, the
documents, an earlier chat, Retry on a message not sent, closing the window, a notification click, the time a chat
takes to load, snapshots of the window and its sheet), logging each step and what it changed to stdout. Typing goes through the app's own event
queue, and key combinations through the window, then the menu bar, then the first responder, as AppKit routes them.
Snapshots are the window's own drawing: vibrant areas (the sidebar, the inspector's text) come out blank or dark.
SwiftUI's own buttons cannot be pressed from a script (the steps call what they call instead), and on a locked screen
the window takes no mouse moves (hover) and runs no animations (an inspector opened with one stays 0 wide). On a locked or sleeping screen every window counts as covered, so the chat neither
polls nor is marked seen there: `SHAFT_DEV_PRESENCE=front` (or `shown`) says the window is in front (or shown behind
another app's) instead.

To check the document-opening crash, seed a long chat with `fixture_server.py act ... long thelos 120`.
Run the Release build with Debug conditions and `SHAFT_DEV_DRIVE=DevServer/mac-doc-open.drive` several times.
The crash is intermittent (unfixed: 2 of 3 runs).

A process started directly stays behind other apps: start the app with `open -n` (`--env` for the variables, `--stdout`
for the log) when its window must be in front. The `activate` step then takes the front, and takes it again before each
`capture`, since another app may take it back; `capture` logs whether the window has the keys and where the pointer is.

## Tests

```bash
cd apps/shaft/Packages/SHAFTKit
swift test                                   # or: xcodebuild test -scheme SHAFTKit -destination 'platform=macOS'
```

The fixture tests decode every `*.json` in the server's `supervisor/tests/app_api_fixtures/`, or in
`$SHAFT_APP_API_FIXTURES` when it is set. Each one is matched to its model by its keys and must decode with
every non-null field kept, so a new fixture needs no test code, and a field the server adds that the Kit does not
read fails the test.

The Mac app's own tests (`macOSTests/`, no window needed):

```bash
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-macOS -destination 'platform=macOS' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO test
```

## Sign and install on a device

Xcode installs the apps directly, development-signed (no TestFlight or App Store).

1. In Xcode → Settings → Accounts, sign in with the Apple ID of team `<TEAM_ID>`.
   The team id is the `OU` of the Apple Development certificate, not the id in the certificate's name.
2. Open `SHAFT.xcodeproj`. Automatic signing registers the two App IDs with Push Notifications and Keychain Sharing on
   the first signed build.
3. **iPhone or iPad:** connect it once by cable, trust the Mac, turn on Developer Mode (Settings → Privacy & Security),
   then pick the `SHAFT-iOS` scheme and the device and Run. From the terminal:

   ```bash
   xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-iOS -destination 'platform=iOS,name=<device name>' \
     -derivedDataPath build/dd -allowProvisioningUpdates build
   xcrun devicectl device install app --device <device name or id> \
     build/dd/Build/Products/Debug-iphoneos/SHAFT.app
   ```

4. **Mac:** pick the `SHAFT-macOS` scheme and Run, or build it as above with `-destination 'platform=macOS'` and open
   `build/dd/Build/Products/Debug/SHAFT.app`.

### The pairing credential

The apps and their extensions share one Keychain item (the server URL and the device's bearer), in the access group
`<TEAM_ID>.io.github.dgrassi1984.shaft`. `project.yml` builds the group from `DEVELOPMENT_TEAM` once
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
its pairing screen again.

### Push

- An Xcode build is development-signed, so its push token belongs to the APNs sandbox. The app reads `aps-environment`
  from its embedded provisioning profile and registers its token with `env` `sandbox` (`production` for a build with
  no profile or a production one), `topic` its bundle id, at every launch.
- The server needs `SHAFT_APNS_KEY_ID`, `SHAFT_APNS_TEAM_ID=<TEAM_ID>`, `SHAFT_APNS_TOPIC=io.github.dgrassi1984.shaft`
  and the key at `.supervisor/web/apns/AuthKey_<KEYID>.p8` ([configuration.md](../../docs/configuration.md)). Without
  them registration answers `503 push_off`, naming what is missing.
- A push carries the title only. The notification service extension fetches the body from `/app/v1/push/latest`
  with the shared credential, within its time limit; on any failure the title shows alone.
- The simulator receives remote pushes too (Xcode 14+ on Apple silicon), so the iOS extension can be tried there.
