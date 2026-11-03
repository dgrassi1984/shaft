# SHAFT for iPhone, iPad and Mac

The native chat apps ([PLAN-native-apps.md](../../docs/plans/PLAN-native-apps.md)): one SwiftUI codebase, an iOS app
for iPhone and iPad, a native macOS app (not Catalyst), and a notification service extension for each. They talk to
the web service's JSON API, `/app/v1` ([app-api.md](../../docs/app-api.md)), over the tailnet.

## Layout

| Path | What |
|---|---|
| `project.yml` | the XcodeGen spec: every target, setting, entitlement and Info.plist key. `SHAFT.xcodeproj` and `Generated/` are made from it and git-ignored; never edit them by hand |
| `Packages/SHAFTKit/` | the local Swift package every target uses: the API models and client, the long-poll, pairing and the Keychain, drafts, the outbox, push registration, the notification body fetch, and the observable stores the UI reads |
| `Shared/` | SwiftUI views both apps share (the UI lanes) |
| `iOS/` | the iPhone and iPad app: its entry point (`SHAFTApp.swift`) and iOS-only UI |
| `macOS/` | the Mac app: its entry point and Mac-only UI |
| `Scaffold/` | the stand-in UI (pairing, the chat list, a plain-text feed) until the UI lanes replace it |
| `NotificationService/` | the extension both platforms build: it fetches the notification's body |

| Target | Platform | Bundle id |
|---|---|---|
| `SHAFT-iOS` | iOS 26+, iPhone and iPad | `io.github.dgrassi1984.shaft` |
| `SHAFT-macOS` | macOS 26+ | `io.github.dgrassi1984.shaft` |
| `NotificationService-iOS` | iOS 26+ | `io.github.dgrassi1984.shaft.NotificationService` |
| `NotificationService-macOS` | macOS 26+ | `io.github.dgrassi1984.shaft.NotificationService` |

All four use Swift 6 with complete strict concurrency, team `<TEAM_ID>` and automatic signing. The apps depend on
[MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui), pinned to exactly 2.4.1.

## Generate and build

Requires Xcode 27 or later and XcodeGen (`brew install xcodegen`).

```bash
cd apps/shaft
xcodegen generate                       # makes SHAFT.xcodeproj and Generated/ from project.yml
```

Run `xcodegen generate` again after any change to `project.yml` or after adding or removing a source file.

Unsigned builds of every target (what agents and CI run):

```bash
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-iOS -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
xcodebuild -project SHAFT.xcodeproj -scheme SHAFT-macOS -destination 'platform=macOS' \
  -derivedDataPath build/dd CODE_SIGNING_ALLOWED=NO build
```

Each app scheme builds its notification extension too.

## Test the Kit

```bash
cd apps/shaft/Packages/SHAFTKit
swift test                                   # or: xcodebuild test -scheme SHAFTKit -destination 'platform=macOS'
```

The fixture tests decode every `*.json` in the server's `supervisor/tests/app_api_fixtures/`, or in
`$SHAFT_APP_API_FIXTURES` when it is set. Each one is matched to its model by its keys and must decode with
every non-null field kept, so a new fixture needs no test code, and a field the server adds that the Kit does not
read fails the test.

## Sign and install on a device

Xcode installs the apps directly, development-signed (no TestFlight or App Store).

1. In Xcode → Settings → Accounts, sign in with the Apple ID of team `<TEAM_ID>`.
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
