# App Store listing

Everything App Store Connect asks for the iPhone/iPad app (with its embedded Apple Watch app), in one place. The Mac
app is not on the Store: it runs without the App Sandbox, so it ships as a Developer ID download
([Release a signed build](../README.md#release-a-signed-build)). How to build and upload the binary is
[Release to the App Store](../README.md#release-to-the-app-store).

| Path | What |
|---|---|
| `metadata/` | the listing text, in the layout `fastlane deliver` reads (one `.txt` per field, per language: `en-US`, `it`) |
| `metadata/review_information/notes.txt` | the notes for App Review: how to use the demo |
| `screenshots/` | the screenshots per language and device class (below) |
| `site/` | the support and privacy pages, English and Italian, with `SUPPORT_EMAIL` as a placeholder |
| `publish-site.sh` | copies `site/` into a checkout of the public site repository with the real address filled in |

The listing text can be pasted into App Store Connect by hand or uploaded with
`fastlane deliver --metadata_path metadata --skip_screenshots --skip_binary_upload` (fastlane is not a dependency of
this repository; use it only if you already have it). Upload the screenshots by hand, one folder per device slot.

## Before the first submission

1. **Publish the pages.** Create the public site repository (for example `<owner>.github.io`), turn on GitHub Pages
   for its default branch, then `AppStore/publish-site.sh --email <support address> --to <its checkout>` and push it.
   The pages go live under `/shaft-app/`, the URLs `metadata/*/support_url.txt` and `privacy_url.txt` name. Apple
   requires the support page to give real contact details, hence the email address. Check both URLs open before
   submitting.
2. **The repository must be public before the listing goes live.** The description says SHAFT is open source and
   that the Mac app installs it; the Mac app's installer clones the public repository. Until then, keep the version
   unreleased (Manual release) or distribute through TestFlight only.
3. **Upload a build** with `Scripts/release-ios.sh --upload` from a release tag, and wait for App Store Connect to
   finish processing it.

## App Store Connect, field by field

### App Information

| Field | Value |
|---|---|
| Name | `metadata/<lang>/name.txt` (SHAFT). If the name is taken, `SHAFT Agents` |
| Subtitle | `metadata/<lang>/subtitle.txt` |
| Primary / secondary category | Productivity / Developer Tools |
| Content rights | Does not contain, show or access third-party content: everything shown comes from the user's own server |
| Age rating | below |
| Privacy Policy URL | `metadata/<lang>/privacy_url.txt` |
| Encryption | answered by the build: `ITSAppUsesNonExemptEncryption` is false (HTTPS through URLSession and the Keychain only), so no question is asked and no documentation is needed |

### Pricing and Availability

Free, no in-app purchases, all countries and regions. **EU Digital Services Act:** declare *not a trader* (a free
personal research project with no ads, purchases or business behind it); no contact details are shown on the EU
listing. Apple leaves that judgement to the developer; revisit it if the app is ever monetised.

### App Privacy

Answer **Data Not Collected**. Apple defines "collect" as sending data off the device in a way the developer or its
partners can access. The app sends everything only to the server on the user's own Mac, which the developer cannot
reach; it contains no analytics, advertising or third-party data SDKs; voice is transcribed on the device. The privacy
manifests in each bundle say the same (`iOS/`, `ShareExtension/`, `NotificationService/`, `watchOS/`
`PrivacyInfo.xcprivacy`). If the developer ever runs anything the app talks to (a push relay, a hosted demo, crash
reporting), this answer must change first.

### Age rating

The rating is **18+**, set as an override: answer the questions truthfully, then raise the rating. Instances write with
models the user chooses, unfiltered by the app, so the app cannot promise what they will say.

| Section | Answer |
|---|---|
| In-app controls (parental controls, age assurance) | None |
| Capabilities: unrestricted web access, user-generated content, messaging and chat, social media, advertising | No (the chat is between the user and their own agents; links open in Safari) |
| Mature themes: profanity or crude humor; horror or fear; alcohol, tobacco or drugs | Infrequent |
| Medical or wellness topics | Infrequent |
| Sexuality or nudity: mature or suggestive themes | Infrequent |
| Sexuality or nudity: sexual content or nudity; graphic sexual content | None |
| Violence: cartoon or fantasy; realistic | Infrequent |
| Violence: prolonged graphic or sadistic; guns or other weapons | None |
| Chance-based activities (gambling, simulated gambling, contests, loot boxes) | None / No |
| Override | 18+ |
| Made for Kids | No |

### Version information (per language)

| Field | File | Limit |
|---|---|---|
| Promotional text | `promotional_text.txt` | 170 characters |
| Description | `description.txt` | 4000 characters |
| Keywords | `keywords.txt` | 100 bytes |
| Support URL | `support_url.txt` | |
| Marketing URL | `marketing_url.txt` | |
| What's New | `release_notes.txt` | 4000 characters (not shown for the first version) |
| Copyright | `metadata/copyright.txt` | |

The app's interface is English only; the Italian listing says so.

### App Review Information

- **Sign-in required:** no. The app has no accounts; the reviewer uses the built-in demo.
- **Notes:** `metadata/review_information/notes.txt` (under the 4000-byte limit). It explains why no server can be
  reached and walks through *Try the demo*.
- **Contact:** your name, phone and email, entered in App Store Connect only (never in this repository).
- **Demo mode:** Guideline 2.1(a) accepts "a built-in demo mode in lieu of a demo account with prior approval by
  Apple". The notes explain it; if review asks, answer in Resolution Center that the app's server runs on the user's
  own private network and cannot be exposed.
- **Release:** Manual, so the version goes live only when the repository is public (above).

## Screenshots

`AppStore/take-screenshots.sh` (from `apps/shaft/`) takes them all from the demo, on simulators of its own named
"SHAFT Shots …", and checks each image's size and that it has no alpha channel. Pass device classes to retake only
some. The iPhone and iPad screens are driven by the `SHAFT-Screenshots` UI test
(`ScreenshotTests/StoreScreenshots.swift`), which also checks the demo's Settings and Leave the demo on each device.

| Folder | App Store Connect slot | Size | Simulator |
|---|---|---|---|
| `iphone-6.9` | iPhone 6.9" display | 1320 × 2868 | iPhone 18 Pro Max |
| `iphone-6.3` | iPhone 6.3" display (Dynamic Island, medium) | 1206 × 2622 | iPhone 18 Pro |
| `ipad-13` | iPad 13" display, landscape | 2752 × 2064 | iPad Pro 13-inch (M5) |
| `watch` | Apple Watch Ultra 3/4 | 422 × 514 | Apple Watch Ultra 4 (49mm), paired with the 6.9" iPhone |

Apple's page is ambiguous about which iPhone class is mandatory now, so both are provided. Each iPhone and iPad folder
holds, in order: `01-chat` (a reply with a table and code), `02-conversations` (instances and their threads),
`03-question` (a question with choices, under a digest the instance sent from a cycle), `04-document`, `05-plan` (what
the instance will do next) and `06-cycle` (one work cycle, planned against done). The iPad runs in landscape, where
sidebar, chat and documents fit side by side. The watch shows an instance's latest reply. The status bar shows the
time the shots were taken, so the messages' times agree with it. The same images serve both languages: the app is
English only.

## Review risks

- **Guidelines 1.2 and 4.7 (AI content).** Apple asks apps that show AI-generated content for a way to filter, report
  and block. Here the user runs and chooses the models, and there is no other user to block. If review raises it, the
  answer is the reasoning above plus the thumbs-down feedback already on every reply; a "Report" action that emails
  the support address is the fallback.
- **Guideline 5.1.2(i) (third-party AI).** The developer shares nothing with any AI provider: the user's own server
  calls the providers the user configured. The description and privacy policy say so.
- **Push.** Self-hosted servers cannot send push to the Store build (it needs the publisher's APNs key). The listing
  says notifications are not yet available for self-hosted servers.
