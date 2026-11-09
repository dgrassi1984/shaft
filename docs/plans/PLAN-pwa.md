# SHAFT — Plan: the web UI as a proper PWA, great on iPhone

*Drafted and signed off 2026-10-19 (the operator: "Go ahead … your defaults for the three decisions") from a multi-agent review (4 reviewers across model families, each checked against the code by a
second agent: 65 findings, none refuted, merged here into 30). "My call" marks a choice I made
that you did not state.*

## 0. What you asked

"launch a multi agent review of the webapp, in particular the chat, to become a proper pwa and
work greatly on iPhone".

What the review found, in one line each (the details, with file:line, are in the review's journal):

- **It is not an app yet.**
  - There is no manifest and no service worker.
  - An app added to the Home Screen keeps its own cookies, separate from Safari's, and the pairing link opens in
    Safari. So the installed app cannot pair itself, and stays on "not paired".
- **Typing on an iPhone is broken.**
  - Return always sends, so a message cannot have a line break: iOS has no Shift key.
  - The composer is 15 px, and iOS zooms the page every time you tap it. Other fields are 13 px.
  - The composer is not kept above the on-screen keyboard.
- **Notifications cannot work.** iOS stops a page's JavaScript when the app is in the background. Nothing tells you
  a reply landed, yet the button reads "Notifications on".
- **Polling is costly on cellular.** The chat polls about 100 times a minute. Each change re-sends the whole feed's
  HTML, with no "not modified" answer. That also wipes the scroll and the live timer during a reply.
- **Touch.**
  - Most controls are 16–35 px tall, below the 44 pt guideline.
  - Copy buttons only show on hover, which a phone does not have.
  - The ⋯ menu runs off the right edge.
  - Long words and links widen the chat.
  - A phone in landscape gets the desktop layout.
- **Accessibility.**
  - Icon-only controls have no names, nothing announces a reply, and toasts are not read out.
  - Dynamic Type is ignored, and reduced-motion is ignored.

What is good and stays: escape-first rendering, the CSRF and same-origin checks, content-hashed static files, the
draft per instance, the attachment download rules.

## 1. Decisions for you

1. **The iPhone Return key.** On a touch device, Return inserts a line break and the Send button sends, as in
   Messages and WhatsApp. A desktop keeps Enter = send, Shift-Enter = line break. *(My default.)*
2. **Notifications when the app is closed.** There are two ways:
   - **(a) Web Push to the Home Screen app** (iOS 16.4+). This is the only way a suspended iPhone app gets a
     notification. It needs a service worker, a subscription per device, and a push sent when a reply ends. The
     push is signed with VAPID ES256, which the Python stdlib cannot do. That means calling `/usr/bin/openssl` (on
     the Mac already), a first exception to "stdlib only". The push goes through Apple's server, carrying no
     content: the app fetches the text itself over the tailnet.
   - **(b) Telegram:** the bots you already have send "example-b replied" with a link. Nothing new to build, but a tap
     opens Safari, not the app.

   *My default: (a), with (b) left as it is.*
3. **Pairing the installed app.** The refusal page gets a "Paste the pairing link" box. You run `shaft web pair`,
   copy the link and paste it into the app. The token stays one-use and valid for 10 minutes, and the form is
   same-origin only. The paired cookie is renewed on use, so it does not lapse after 400 days. *(My default.)*

## 2. Milestones

| M | What | Effort |
|---|---|---|
| **P0 — typing and touch** | Fields at 16 px (no zoom); Return = new line on touch, with Send; touch targets ≥ 44 pt (buttons, chips' ×, ⋯, the rail); Copy visible on touch (`@media (hover: none)`); the ⋯ menu kept on screen; long tokens wrap; `text-size-adjust`; landscape phone gets the phone layout; the "↩ Reply" button placed for a selection inside the scrolling feed. | S each, ~1 day |
| **P1 — an installable app** | Manifest (`/manifest.webmanifest`: id, start_url `/`, standalone, name, theme colours, 192/512 opaque and maskable icons); `apple-mobile-web-app-*` and `theme-color` for light and dark; `viewport-fit=cover` with safe-area insets for the header, rail and composer; pairing from inside the app (decision 3); a service worker that caches only versioned static files plus an offline page, never HTML, fragments or files, with an update flow; the unread count as an app badge (`setAppBadge`). | M |
| **P2 — the phone layout** | The chat full screen on a phone: the rail becomes a chat switcher in the header; documents become a sheet over the chat, not a section below it; the composer stays above the keyboard (`visualViewport`), with no page jump; the feed sized by the real viewport, not a 19 rem guess. | M |
| **P3 — network and battery** | ETag / `304 Not Modified` on every fragment (a version per chat and per document, so an unchanged poll costs ~200 bytes); fetch timeouts; one "offline — retrying" note with backoff and the `online` event, not three red notes; the docs panel not polled while hidden; the feed updated by turn, so scroll, focus and the reply's clock survive a refresh; a send whose answer was lost not sent twice (a client message id); `chats()` cached by mtime; lazy thumbnails and immutable static caching; attachment sizes checked before upload. | M |
| **P4 — notifications** | Decision 2: Web Push, with a subscribe route (CSRF-checked, Apple and Mozilla push hosts only), a push when a reply ends, and a service-worker handler that shows "example-b replied" and the badge. The button tells the truth on every platform. | L |
| **P5 — accessibility** | Names for icon-only controls; a live region that announces a new reply; toasts as `status` / `alert`; a real label for the composer; focus back to the composer after Send and to Edit after Save; Dynamic Type via rem; `prefers-reduced-motion`; contrast fixes (dark error toast, light-mode composer border). | S each |

**Order:** P0 → P1 → P2 → P3, then P4 and P5. P0 alone makes it usable on the iPhone today. P1 and P2 make it an
app. P3 is the data and battery bill.

**How each milestone is checked:**
- Tests as before: guards broken on purpose.
- Live in Chrome at 390×844 with iPhone emulation, and on your iPhone, where only it can tell: Home Screen install,
  the keyboard, push. I will list exactly what to try.
- Everything on a branch, merged when you say.

## 3. Status

**Built 2026-10-19 on `claude/pwa`; merged into main (88c1843) and deployed on the operator's word. Checked on their iPhone 2026-10-20: "Confirmed, all good" — the checklist below, push notifications included.** Commits: P0 typing and touch;
P1 an app on the Home Screen; P2 the phone layout; P3 network and battery; P4 Web Push; P5 accessibility.

Driven in Chrome on the branch's server (a copy of `.supervisor/`, tailnet :8445; your service on :8444 untouched):
- **Layout and app shell:**
  - the phone layout at 400×860: the three sheets (chats, documents, ⋯), with ✕ to close;
  - Return as a new line on a touch-only device, Enter sends on a computer;
  - the service worker in control, with only the shell in its cache.
- **Network:**
  - an unchanged refresh a 304 with no body (15.7 KB → 0);
  - the feed updated per item (the first turn's element kept across a new message);
  - the server stopped under an open page: one note, then recovery and "SHAFT was updated — reload";
  - a message sent twice with its id posted once.
- **Push and accessibility:**
  - Apple accepts our push token: BadDeviceToken for a made-up device, BadJwtToken for a junk token;
  - a new answer announced for VoiceOver;
  - Attach files reachable by keyboard.

Tests: test_web_touch.py, test_web_pwa.py and test_webpush.py, plus additions to test_web_chat.py. Guards broken on
purpose where the fault was a race or a stale state.

**Only your iPhone can check these.** Try them in this order:
1. In Safari, open the URL. Return makes a new line; tapping the box does not zoom.
2. Share → Add to Home Screen, then open SHAFT from the icon. The "not paired" page takes the pasted link from `shaft
   web pair --name phone-app`.
3. In the app:
   - nothing hides under the notch or the home indicator;
   - the composer stays right above the keyboard;
   - ☰, 📄 and ⋯ open sheets.
4. ⋯ → Notify me of replies → Allow. Lock the phone, have an instance reply (e.g. from Telegram or the laptop). A
   notification "… replied" should arrive and open that chat.
5. Airplane mode for a moment: one "Offline" note, and the chat back without a reload.

### iPad composer follow-up, 2026-10-21

The composer offers **Send now** and **Queue after reply** on every device. Send now interjects during an active
reply; Queue after reply waits for it to finish, or sends immediately when idle. Cmd+Enter and Ctrl+Enter queue
with an iPad hardware keyboard too. Unmodified Return still inserts a newline on touch-only devices.

Every chat layout uses the visual viewport height, including the iPad sidebar layout. Opening or closing the
on-screen keyboard resizes the draft to at most one third of the visible height and leaves the feed scrollable.
Verify on a physical iPad in portrait, landscape and Split View, with both on-screen and hardware keyboards.

### System review follow-up, 2026-10-24

The web UI findings W1–W18 of the 2026-10-24 system review, fixed on `claude/pwa-fixes`: pushes only to paired
devices and per device (a desktop tab no longer silences the phone; a page going hidden says so); a dead or
re-keyed subscription renewed by the page and by the service worker; a notification brings an open chat forward
instead of reloading it; the reply's activity polled on its own, so the feed is a 304 during a reply; the documents
sheet and comment box sized to what the keyboard leaves; ↩ Reply from the documents sheet works (the button was made
inert with the rest of the page) and closes the sheet; drafts and leave warnings for Drive, Config and Settings.

Driven in Chrome on the branch's own server (an empty `.supervisor/`, a fixture instance) at a 452 px phone-width
window and on desktop. **Only the iPhone can check these:** the sheets above the on-screen keyboard (Save and Cancel
visible while typing in a document or a comment), a push while another device has the chat open, a push right after
locking the phone, and a tap on a notification while the chat is already open (it must not reload).

Follow-up after Grok's review of that commit (same day): a wake retrying an old endpoint no longer drops the
subscription the page renewed; a renewal cut short is finished on the next visit; the activity log keeps its place;
leaving the page (not only hiding it) says so, stamped so an earlier refresh cannot undo it; a saved Drive, Config or
Settings form no longer reads as unsaved; the sheets drop a stale keyboard inset; the documents' hash cache holds only
what is there. `pushsubscriptionchange` is not sent by WebKit (bugs.webkit.org/show_bug.cgi?id=294421): on the iPhone
the page's check on each visit is what renews a subscription.
