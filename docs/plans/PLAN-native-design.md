# SHAFT — Design: the native apps' visual system

Direction approved by the operator on 2026-11-04 ("Yes, this direction"), from the iPhone mockup in dark and light.
This document turns it into exact values. Sources: Gemini's critique of the 15 current screens, Grok's research on
iOS 26 / macOS 26 and current chat apps, and my own read of the same screens. Marked **(my call)** where the operator
has not decided and I did.

## 1. Intent

The apps work; they look unfinished. The operator wants them to look state of the art on iPhone, iPad and Mac. He reads
long agent answers and acts on them: approves, answers, corrects. Success is an app that looks designed, reads
beautifully for long stretches, and shows at a glance what needs him.

**Goals**

- One visual system, in code, used by every screen on all three platforms.
- The SHAFT identity (the mark's carbon, periwinkle and bone) in the app, not stock iOS blue.
- A clear reading order in the feed: what the agent said, what needs an answer, what merely happened.
- Layouts that hold at every size: no truncated status, no crushed chat, no text under the toolbar.

**Non-goals**

- No new features, and no feature removed: the only thing leaving a place is the feed's "End the conversation" link,
  which stays in the ⋯ menu.
- No change to the server, the API, push or the chat model.
- No change to the keyboard rules, status wording or what a message does.
- Settings offers light, dark or the system's appearance, and a text size for chats and documents, as per-device choices. No custom colours.

**Anti-goals** (a failure even if it works)

- A skinned web page. It must feel native to iOS 26 and macOS 26.
- Glass in the content layer. Apple's rule: glass is for controls and navigation; content sits on a solid surface.
- Dropped frames while a reply streams, or a slower long chat than today.
- Decoration that makes long answers harder to read.
- A palette that fails contrast.

## 2. The system

All of it lives in one place, `Shared/Design/` **(my call)**: colours as asset-catalog sets with light and dark values,
everything else as a small Swift `Design`. No screen writes a raw colour, opacity, radius, font size or padding again.
Today's 13 opacities of `Color.secondary` go.

### 2.1 Colour

| Token | Light | Dark | Use |
|---|---|---|---|
| `canvas` | `#F7F6F2` | `#141413` | The reading surface: chat, documents |
| `elevated` | `#EFECE6` | `#1E1E1C` | Sidebar, code, tables, cards, inspector |
| `hairline` | `#E2DFD7` | `#2E2E2B` | Dividers inside tables and cards |
| `operator` | `#E4E9F7` | `#262B3A` | Your own message chip |
| `textPrimary` | `#141413` | `#ECECE8` | Prose, headings |
| `textSecondary` | `#5C5B57` | `#9E9D97` | Timestamps, activity, metadata |
| `textTertiary` | `#6B6963` | `#8A8983` | Placeholders, step counts |
| `accent` | `#2B5CE6` | `#7AA2FF` | Actions, links, unread, questions |
| `onAccent` | `#FFFFFF` | `#141413` | Text and icons on `accent` |
| `warning` | `#A85400` | `#F59E0B` | Offline, incomplete, not sent |
| `danger` | `#C4261A` | `#F97066` | Failures, destructive actions |

- Dark `accent` is the mark's bar and dark `textPrimary` its dot; dark `canvas` is its tile. Light `accent` is a deeper
  blue because `#7AA2FF` fails contrast on paper.
- Every text token above measured at least 4.5:1 on both `canvas` and `elevated` in the mode it belongs to, except the
  `operator` pair, measured separately (`textPrimary` on `operator`: 15.2:1 light, 11.9:1 dark). A test pins this.
- `AccentColor` in the catalog is `accent`, so system controls pick it up.

### 2.2 Type

Dynamic Type styles only, so text scales with the operator's setting. Numbers at the iOS default size; macOS uses its
own defaults for the same styles.

| Role | Style | Details |
|---|---|---|
| Agent prose | `.body` (17) | Line spacing 4 pt, 10 pt between paragraphs |
| Headings in prose | 1.25 / 1.12 / 1.02 em | Bold, semibold, semibold; 1 em above, 0.35 em below |
| Code | SF Mono 0.88 em | Fenced and inline |
| Table cells | `.subheadline` | Tabular figures; numeric columns right-aligned |
| Metadata | `.caption2`, tabular | Times, elapsed timers |
| Pills, labels | `.caption`, medium | "4 steps", "example-b asks" |

Line length is capped at 620 pt for prose, tables and code on iPad and Mac, with the column centred (§5).

### 2.3 Space and shape

- 4 pt grid. Gutter 16 pt on iPhone, 24 pt on iPad and Mac.
- 20 pt between turns, 8 pt between things in one turn.
- Radii, all continuous: operator chip 18; composer 24 (capsule while one line); cards, code, tables 10; pills, inline
  code 6.
- No drop shadows in the feed. Depth comes from `elevated` and `hairline`. Floating controls use the system's glass.

### 2.4 Glass and motion

- **Glass:** header buttons, the composer, the "Latest" pill, sheets. System glass only (`glassEffect`, `.glass` button
  styles, `GlassEffectContainer`), no custom bar backgrounds.
- **Scroll edges:** the feed uses `scrollEdgeEffectStyle(.soft, …)` and the composer `safeAreaBar`, so text fades as it
  goes under the header and composer instead of colliding with them.
- **Motion:** springs of 0.22 s without bounce for disclosures and quote cards. No animation on tokens while streaming
  (today's rule stays). The only pulse is the replying and in-a-cycle symbol.
- **Reduce Motion and Reduce Transparency** are honoured: pulses and springs go; glass falls back to solid `elevated`.

### 2.5 Touch targets

The icon is drawn small; the hit area stays 44 pt without taking up layout. `.touchTarget()` becomes a content-shape
expansion, so a footer of 13 pt icons sits in a 28 pt row. This removes the 44 pt of height under every message.

## 3. The feed

Agent prose has no box. Everything else is one of three quiet forms: the chip, the filled block, or the accent bar.

| Item | Today | New |
|---|---|---|
| Agent answer | Flowing text | Flowing type, per §2.2 |
| Your message | Pale blue bubble | `operator` chip, trailing, max 78% of the column, radius 18. Quote text and chips inside keep contrast |
| Table | Four-sided grid | `elevated` block, radius 10, horizontal hairlines only, header in `textSecondary` semibold, tabular figures. Scrolls sideways rather than squeezing, on all platforms |
| Code block | Grey slab, Copy over the code | `elevated` block with a header row: language at left, Copy at right. Syntax colours (§3.1) |
| Inline code | Plain mono | `elevated` pill, radius 6 |
| "What it did" | Blue disclosure across the width | "⚙ 4 steps" pill beside the time; opens the same list below it |
| Footer | 44 pt icons | Time, then Copy, Reply, thumbs in a 28 pt row in `textTertiary` |
| Document card | Grey slab | `elevated` row with its icon in `accent`; one line for name and "changed" |
| "answered c1" chip | Grey pill | Pill, per §2.3 |
| Question from an agent | Outlined box | 3 pt `accent` bar, "example-b asks" in `accent`, inline Reply. The loudest thing in the feed |
| Message from an agent | Outlined box | Same bar in `hairline`, "example-b sent you a message" in `textSecondary`. Attachments stay |
| Next cycle | Dashed outline | `operator` chip at reduced emphasis (outline in `hairline`, `canvas` fill) with a "next cycle · waiting" line and moon symbol |
| Queued | As today | Same form as next cycle, with Cancel |
| Not sent | As today | `operator` chip with a `warning` line and Retry, Edit, Discard as text buttons |
| Live reply | Grey slab, large Stop | Flowing type as it streams. A single status line above it (symbol, "replying · 0:13", steps). Stop moves into the composer: while a reply runs, the button is Stop when the field is empty and Send once there is text, so interjecting keeps working **(my call)** |
| Failure | Orange slab, blue pill | 3 pt `danger` bar, the sentence, Retry as a compact `accent` button, Technical details as a quiet disclosure |
| Day separator | Line and text | Text only, centred, `.caption`, `textSecondary` |
| "End the conversation" | Link in the feed | Removed from the feed; it stays in ⋯ |
| "Latest" | Plain capsule | Glass pill, above the composer so it never covers text |

Unread on the rail, the "New below" pill and Show earlier use the same tokens.

### 3.1 Syntax colours

**(my call)** A small tokeniser of our own in SHAFTKit for keywords, strings, comments and numbers, covering Python,
Swift, JavaScript/TypeScript, shell, JSON, YAML, SQL and diffs, with no new dependency. An unknown language stays plain.
Colours are four extra tokens set in `Shared/Design/`, at the same contrast bar. Both the iOS and the Mac renderer use
the tokeniser, and it runs on finished blocks only.

## 4. Chrome

- **iPhone header:** a glass chats button at left, a glass group of Documents, New chat and ⋯ at right, and between them
  two centred lines: the name in semibold and one status line. The status drops its optional parts in order (model
  first, then time) instead of truncating twice. Tapping it opens the popover as today.
- **Composer:** one glass capsule holding +, the text and Send. Once there is text or other draft content, a chevron
  beside Send opens "More ways to send", as on the web (Queue after reply, Next cycle). These choices also stay in +
  and, on iOS, a long press on Send. On the Mac, Send is a plain button. The Divider above goes.
  Text insets 12 / 9 pt, so nothing clips.
- **Sidebar and chat switcher:** `elevated`. Rows: name, status symbol and line, last line, time. The unread dot overlaps
  the left margin, so read rows align. Replying rows pulse their symbol.
- **Sheets:** system glass sheets. The thumbs-down reason is a compact sheet rather than a Settings-style form. Settings
  stays a form.
- **Pairing, empty and error screens:** the mark at the top, one sentence, one action, per the wording in place.
- **Comment threads:** actions collapse to one primary (Send) and a ⋯ menu for the rest; the current two rows of blue
  links go.

## 5. Layout

- **iPad:** the chat column is centred at up to 620 pt of prose width. Documents are an inspector beside the chat, and the chat
  is never under 380 pt wide **(my call)**: the sidebar (260 pt), inspector (320 pt) and chat need 960 pt together, so
  below 1000 pt of window width opening documents collapses the sidebar, and below 700 pt, where chat and inspector no
  longer fit, documents open as a sheet, as they do on a phone. The duplicate ⋯ goes.
- **Mac:** the same column rule. The sidebar and inspector use the system's materials and `elevated`. Mac message text
  stays the AppKit renderer, restyled to match: rounded `elevated` blocks for code, tables capped to the column and
  scrolling sideways, no double list markers, and Copy on code blocks.
- **Document reading:** same type as the chat, same column. Commented passages get a soft `accent` highlight, and tapping
  one opens its thread **(my call: this is the one visible gain outside the chat, and it was listed as a parity gap)**.

## 6. Rendering

**(my call)** Keep MarkdownUI on iOS, restyled through its theme, and keep the AppKit renderer on Mac. Grok's sources say
MarkdownUI is in maintenance and its successor Textual is not built for streaming, and one forum benchmark put Textual
well behind it. A replacement is a separate piece of work, to be decided if real long replies stutter. Measure the
frame rate of a streaming reply before and after this work; no regression is allowed.

## 7. Icon and identity

The app icon stays. Accent, dark surfaces and the empty-screen mark bring it through the app.

## 8. Verification

- **The contrast test** in the Kit or app tests reads the tokens and fails under 4.5:1 for text.
- **A token check** fails the build's test run if a screen uses a raw hex colour, `Color.secondary.opacity` or a stray
  `cornerRadius` outside `Shared/Design/`.
- **Every state, driven:** a screenshot set from the fixture server of every feed item and sheet on iPhone, iPad and Mac,
  light and dark, plus largest accessibility text and Reduce Transparency. Reviewed by eye against this document by me,
  and by a second model.
- **Streaming:** frame timing on a long reply, before and after.
- **Real devices:** on the operator's iPhone and the signed Mac app.
- **Function:** all existing Kit and Mac tests stay green, and the feature list in `docs/app-api.md` still works by hand.

## 9. After first use (operator's decisions of 2026-11-04)

The operator used the built apps and decided these. They go beyond the sections above and win where they differ.

- **Selected row:** no system accent fill. A quiet fill (`hairline` over `elevated`, radius 10); `textPrimary` and
  `textSecondary` stay at full contrast, in both modes, on the Mac, iPad and the phone's switcher. Unread stays the accent
  dot. A test pins the contrast.
- **Thumbs:** in the footer of every instance message and answer with text, always, on every platform; never only on hover
  or in a menu. A turn with no answer text, a failure and nothing else, has none. On the Mac the footer is always time,
  steps, thumbs, then Copy and Reply on hover; hidden, they leave no gap. A rated thumb is filled in `accent`.
- **Title:** a glass capsule of its own, the height of the header's buttons, on all platforms, with at least 16 pt inside
  each end and 8 pt from what stands beside it at every width. Narrowing, its status drops the model, then the time, then
  its icon. On the iPhone it stays leading, beside the chats button.
- **Scroll edges (beyond §2.4):** nothing may be legible behind the header or the composer. One treatment on every platform
  and under Reduce Transparency: the system's scroll edge is hidden, and a plain eased gradient fades the feed into `canvas`
  over 32 pt at the default text size, growing with the text, under the header and above the composer; the glass sits on solid `canvas`. At rest the feed keeps its first and
  last lines clear of the fades.
- **Latest (§3):** never stands on text. While it shows, the foot of the feed is solid `canvas` up to its top, with the
  fade above that, so text dissolves before it reaches the pill. With no pill there is no band.
- **Composer:** no shortcut legend under the field, which moved the layout with focus: the keys are a tooltip on Send, on
  the Mac and on an iPad with a pointer.
- **Bars (§3):** the accent bar of a question, a message or a failure stops at its content.
- **Reduce Transparency (§2.4):** the system's own fallback for its bar buttons is pure white or black, so the chat's
  header draws its own solid `elevated` there.

## 10. Left to the builder

File layout under `Shared/Design/`, how the token check is built, the exact syntax colours (within the contrast bar), and
small spacing decisions that follow the grid.
