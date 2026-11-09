# SHAFT — Plan: the iPhone and iPad app at market standard

*Drafted 2026-10-27 from a four-lane review ("use subagents to do a proper UX review of iphone and ipad pwa and bring it
to market standard"). Branch `claude/pwa-market`, merged when you say.*

## 0. How it was reviewed

| Lane | Model | How | Result |
|---|---|---|---|
| iPhone hands-on | Claude | iPhone 17 simulator, Safari tab and installed Home Screen app, light/dark, portrait/landscape, Large → AX-XL text, 55 screenshots | 6 S1, 19 S2/S3 |
| iPad hands-on | Claude | iPad Air 11″ simulator | **blocked**: simulator access for that device not granted; 8 candidates from code only |
| Code | Codex gpt-6.1-sol | read-only review of web*.py and web_static/ | 2 S1, 12 S2, 4 S3 |
| Market bar | Grok grok-4.7 | HIG, WebKit notes, ChatGPT/Claude/Slack/Linear/Telegram docs | 47-item rubric with PWA feasibility |

The rig: a copy of `.supervisor/` and the instances, a stub API that streams a canned reply (no instance, no model
spend), the dev web server behind a localhost-only TLS proxy trusted by the simulators. Your service on :8444 untouched.

"Checked" below = I confirmed it against the code or a screenshot; the rest are a reviewer's claim, confirmed when fixed.

## 1. What a PWA on iOS cannot do (not attempted)

Dark/tinted/clear Home Screen icons; haptics; the system swipe-back (every screen needs its own back control); widgets,
Live Activities, App Intents; silent push; Background Sync. Drawing under the status bar is reported broken on iOS
27.0.1 (WebKit 325807), unverified on 27.2.

## 2. Findings, merged and ranked

### S1 — broken

| # | Finding | Source | |
|---|---|---|---|
| B1 | One tap on **Send** with the keyboard up only closes the keyboard (Safari, 3/3) | iPhone I2 | |
| B2 | Focusing the composer can scroll the whole page off screen (Safari) | iPhone I1 | |
| B3 | Phone landscape: feed ~90 pt, composer hidden under the keyboard | iPhone I3 | |
| B4 | AX text sizes: header overflows, ⋯ (the only way to the pages) off screen, feed ~20 pt | iPhone I4, Codex C5 | |
| B5 | Installed app: non-chat pages have no back control and open on a nav wall (~45 % of the screen, 12 tabs on 3 rows) | iPhone I5, I25 | checked |
| B6 | Content wider than the phone: Config inputs and Save cut off; alert text overflows; **Markdown tables in replies collapsed into key/value cards** | iPhone I6, I16 | tables checked |
| B7 | **Reload / update** deletes the live cache: the next offline launch has no offline page | Codex C1 | checked |
| B8 | Sending a comment closes the editor over text typed during the request | Codex C2 | |
| B9 | Text lost on send ("Fourth demo" saved as "F", words missing mid-message) — simulator typing or a real race: investigate before calling it either | iPhone U1 | record checked |

### S2 — clearly below market

| # | Finding | Source |
|---|---|---|
| M1 | **No streaming:** the answer the server already holds is hidden behind "the answer appears when it is ready" | Codex C11, iPhone I9 (checked) |
| M2 | Icons: colour emoji, Unicode and SVG mixed; 🕘 📄 read as disabled; white blobs in dark mode | iPhone I10, Codex C16, iPad C2 |
| M3 | Header is seven bordered 44 pt boxes; Clear one tap from the title | iPhone I11, Codex C5 |
| M4 | Composer: "Attach files" link + Send now / Queue after reply / Next cycle text buttons, wrapping to two rows | iPhone I7, iPad C5 |
| M5 | Two bordered ↩/⧉ boxes on every message, bigger than short ones | iPhone I12 |
| M6 | Your own message is not followed after Send; a "↓ New below" pill appears instead | iPhone I8 |
| M7 | Destructive actions beside navigation: Kill in ⋯ and on every card, Unpair top right of every page | iPhone I13, iPad C6 |
| M8 | Retry only refills the composer; error copy reads like a manual | iPhone I14 |
| M9 | Overview alerts are raw log lines with Python dicts, all bold red | iPhone I15 |
| M10 | A sheet over the chat still marks it read and silences its push | Codex C3 |
| M11 | A notification tap can bring up an archived chat instead of the current one | Codex C4 |
| M12 | Pinch zoom is treated as the keyboard (layout resized, scroll reset) | Codex C7 |
| M13 | ⋯ and Earlier chats sheets: no keyboard-aware bounds, no focus containment; ⋯ likely off the right edge on iPad | Codex C8, iPad C1 |
| M14 | Safe areas missing on comment box, toasts, notices; toast covers the Send row and can sit under the keyboard | Codex C9, iPhone I19 |
| M15 | 44 pt targets still missing: inputs, selects, comment-thread disclosures, earlier-chat links | Codex C10 |
| M16 | Choosing a document shows no loading state | Codex C12 |
| M17 | Push notification waits on an unbounded fetch before showing | Codex C14 |
| M18 | iPad floating keyboard can make Return send | Codex C6 |
| M19 | Keyboard shortcuts hidden on an iPad with a hardware keyboard | iPad C3 |
| M20 | Chat line length unbounded on iPad landscape (up to 52rem) | iPad C4 |
| M21 | Long conversations render and re-render in full | Codex C13 |

### S3 — polish

Quote-reply inserts raw `> text` (a chip instead); bare empty chat; jargon in the busy popover; Delete next to Copy in
the document reader; the stretched Overview badge; pairing page (hard-coded `--name phone-app`, "Back to app" loops, no
Paste); prose vs filename field attributes; comment text not following Dynamic Type; launch screen and iPad touch icons;
touch-callout/overscroll on app chrome.

### Already at market standard

16 px fields (no zoom); Return = new line on touch; composer above the keyboard in the installed app; manifest and
install flow; safe areas for status bar, Dynamic Island and home indicator; app-switcher card; pairing by paste; long
URLs wrap; code blocks scroll with a language label and Copy; Stop, timer and activity during a reply; Technical details
behind a tap; dark mode coherent; documents list and reader typography; server 5–68 ms to first byte.

## 3. Decisions (signed off 2026-10-27: all four defaults; on 2, "ensure this works on desktops too")

Message actions on a desktop: hover shows them, right-click opens the same menu, and they stay reachable by keyboard
(focus a message, its actions appear; the menu is a real menu with arrow keys and Escape).


1. **The composer.** **One send arrow inside the field's row and a paperclip; the other modes behind it.** Tap = Send now
   (during a reply: interject, as today). Long-press the arrow (or a small ⌄ beside it) = Queue after reply / Next cycle.
   Cmd-Return keeps queueing on a hardware keyboard.
2. **Message actions.** **Hidden until wanted:** long-press a message on touch (Copy, Reply, Select), hover on pointer.
   Under the last assistant reply a quiet Copy row stays visible.
3. **Non-chat pages on a phone.** **The chat's frame everywhere:** a bar with ‹ Back (to the chat) and the page title; the
   rail moves into the ☰ sheet; the instance tabs become one horizontally scrolling strip.
4. **Destructive actions.** **Kill, Delete, Unpair move to the end of their menu, in red, under a divider**; Unpair leaves
   the page frame and goes to Settings and the ☰ sheet footer. Overview cards keep Pause, lose Kill (it stays on Summary
   and in ⋯).

## 4. Milestones

| M | What | Acceptance |
|---|---|---|
| **M0 — broken** | B1–B9 | Each reproduced on the simulator first, then fixed; a regression test per bug; re-driven on the iPhone simulator in the mode it failed |
| **M1 — the look** | M2 (one SVG icon set, theme-tinted, scaled with text), M3 (borderless header: title + state subtitle, ≤3 icons, the rest in ⋯), M14, M15, S3 visual items | Screenshots light/dark, Large and AX-XL, portrait and landscape: nothing clipped, every control 44 pt |
| **M2 — the chat** | M1 streaming, M4 composer (decision 1), M5 (decision 2), M6, M8, quote chip, empty state | A stub reply streams in; send, queue, next-cycle, retry each one tap; your message always followed |
| **M3 — the frame** | B5 with decision 3, M7 (decision 4), M9, M13, M16, pairing page | Every page reachable and leavable in the installed app; ⋯ on screen on iPhone and iPad |
| **M4 — correctness** | M10, M11, M12, M17, M18, M21 | Tests for each; push paths with the existing webpush tests |
| **M5 — iPad** | the iPad lane re-run once access is granted, M19, M20, then its findings | Portrait, landscape, Split View widths, hardware keyboard |

Order M0 → M1 → M2 → M3 → M4 → M5; the full test suite after each, and a fresh simulator pass at the end.
**Only your iPhone/iPad can check:** push, the launch screen, real keyboard timing (B9), and anything the simulator's
typing cannot reproduce.

## 5. Status (2026-10-27)

Built on `claude/pwa-market`, not merged. Fixed, each with a regression test (`test_web_market.py`,
`test_web_market_js.py`, and the older tests repinned where they pinned the old design):

| Done | Items |
|---|---|
| M0 — broken | B1 (I2), B2 (I1), B3 (I3), B4 (I4), B5 (I5, I25), B6 (I6, I16), B7 (C1), B8 (C2); B9 (U1) is the simulator's typing, not the app: its text tool returns before the characters arrive (seen: "typed 21" with one on screen), and the reviewer sent mid-typing. Re-check on a real iPhone. |
| M1 — the look | M2 (one SVG set), M3 (header), M14 (toasts at the top, notch insets), M15 (fields 44 pt), badge, sheet ✕, an iOS-like heading scale in answers and the journal (documents keep #59's) |
| M2 — the chat | M1 streaming, M4 composer (decision 1), M5 message menu (decision 2, desktop too), M6, M8 one-tap Retry, quote chip, empty state |
| M3 — the frame | B5 page bar on every page (decision 3), M7 Kill last and apart (decision 4), M9 alert lines, M13 ⋯ bounded to the visible viewport, M16 documents loading |
| M4 — correctness | M10, M11, M12, M17 (Codex, reviewed and committed here) |

**Checked live** on the iPhone 17 simulator over the rig (stub API streaming a reply): Safari and the installed app;
light and dark; Large and AX-XL text; portrait and landscape, keyboard up and down; send in one tap with the keyboard
up, the stream, the ⋯ menu and its order, the ☰ sheet on a non-chat page, journal and config at 402 pt, overview alerts.

**Reviews of the build itself** (2026-10-27): Codex read the branch diff (7 regressions, all confirmed and fixed with
executed tests: the message long-press threw, a cancelled send long-press ate the next Send, a held scrollbar did not
stop following, Retry sent a waiting quote and stayed disabled, the busy notice was clipped beside the rail, and the
"waiting for you" notice was false). A fresh iPad pass (iPad Air 11-inch simulator, Safari and the installed app,
portrait/landscape, hardware and on-screen keyboard, dark, XXL) found 14; fixed P1 (a long quote widened the page —
S1), P2 (dead rail toggle), P4 (Unpair to Settings), P6, P7, P8, P9 pill, P10, P11 (side sheet), P13, P14, re-checked on
the iPad after.

**Remaining items, done 2026-10-27** (the operator: "Fix the remaining items too, then merge"): P3 page tables stack by
the page's width; P5 alert tokens in plain type (words unchanged); P9 Retry in place; P12 ⋯ actions as rows; M13 the ⋯
menu takes and returns focus; M18 Return sends on a touch device only after a hardware key proved itself; M21 the last
80 turns, the rest on request; C17 launch screens and iPad touch icons (Codex). Each with a test; P3, P9, P12 and the
launch image checked live; a tap stopping the feed's following was found live and fixed.

**Still open:**
- M18 and Split View: the simulator tool has no floating keyboard, no Split View and no pointer, so the hardware-key
  rule and hover are checked in tests only.
- One first tap on Send in Safari did not send, with Safari's own address pill floating over the page; two retries of
  the same flow sent at the first tap. Watch for it on the real iPhone.
- Landscape with the keyboard up in a **Safari tab** leaves ~15 pt between Safari's two bars and the keyboard: nothing
  fits there; in the installed app the header and field sit above the keyboard.

**Only a real iPhone/iPad can check:** typing speed against Send (B9), push, the launch screen, the floating keyboard.
