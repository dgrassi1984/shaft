# Native design build Implementation Plan

> **For agentic workers:** executed multi-agent, as the operator asked ("launch a multi-agent build", "Ensure you use
> sub-agents/models", "Approve, build it"). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put the approved visual system on every screen of the iPhone, iPad and Mac apps, losing no feature.

**Architecture:** One `Shared/Design/` module (colour sets plus a `Design`) is built and merged first; every lane then
restyles its own folder against it, each in its own worktree, and the lanes merge into `claude/na-design`. A source
check keeps screens from going back to raw colours.

**Tech Stack:** Swift 6, SwiftUI on iOS 26 and macOS 26, AppKit for Mac message text, MarkdownUI 2.4.1, XcodeGen.

**Spec:** `docs/plans/PLAN-native-design.md`. Every lane reads it; values are not repeated here.

## Global Constraints

- Tokens, type, spacing, radii and glass rules are exactly the spec's §2. No value is invented by a lane.
- No feature is lost, and no keyboard rule or status wording changes (spec §1). No server change.
- Glass on controls only. No custom bar backgrounds. No shadows in the feed.
- No new package dependency.
- Reduce Motion and Reduce Transparency honoured; Dynamic Type everywhere.
- Secrets rule: no lane opens `.env`, `instances/`, `secrets/`, a real `.supervisor/` or a `.p8`.

## Review Focus

1. A very long table or code line must scroll, never widen the column or clip. Pinned by M1's feed screenshot of a
   12-column table and a 400-character code line.
2. A reply streaming with the sheet of documents open, or with the composer focused: no jump, no lost scroll position.
   Pinned in M2's drive.
3. Largest accessibility text: the header status, composer and footer must wrap or drop parts, never overlap. Pinned in
   the M5 screenshot set.
4. Stop and Send: a reply running, then typing, then clearing the field must switch the button both ways, and a hardware
   Return still sends. Pinned by M2's `ComposerActionTests`.
5. A narrow iPad window resized while documents are open must move between inspector and sheet without losing the open
   document. Pinned in M2's drive.

---

## Lanes

| Milestone | Lane | Worktree, branch | Owns | Who |
|---|---|---|---|---|
| M0 | Foundation | `../shaft-na-design`, `claude/na-design` | `Shared/Design/`, `Support.swift` `touchTarget`, the two tests, FrameMeter | Codex |
| M1 | Feed | `../shaft-nd-feed` | `Shared/Feed/`, `Shared/Markdown/`, `Packages/SHAFTKit` tokeniser | Claude (needs the simulator); tokeniser Codex |
| M2 | Chrome | `../shaft-nd-chrome` | `Shared/Chat/`, `Shared/Composer/`, `Shared/Root/`, `iOS/` | Claude (needs the simulator) |
| M3 | Documents | `../shaft-nd-docs` | `Shared/Documents/`, `Shared/Attachments/` | Claude (needs the simulator) |
| M4 | Mac | `../shaft-nd-mac` | `macOS/`, Mac halves in `Shared/Platform/` | Claude (needs this Mac) |
| M5 | Verify | main session | the screenshot set, frame timing, devices | me, with Codex and Gemini reviewing |

A lane changes only what it owns. A change it needs elsewhere goes in its report, and I make it at merge.

### Task M0: Foundation

**Files:**
- Create: `apps/shaft/Shared/Assets.xcassets/<token>.colorset/` for the 11 colours of spec §2.1 and `AccentColor`
- Create: `apps/shaft/Shared/Design/Design.swift`
- Create: `apps/shaft/Packages/SHAFTKit/Tests/SHAFTKitTests/DesignSystemTests.swift`
- Create: `apps/shaft/Shared/Support/FrameMeter.swift` (Debug only)
- Modify: `apps/shaft/Shared/Support/Support.swift` (`touchTarget`), `apps/shaft/project.yml`

**Interfaces:**
- Produces: `Color.canvas`, `.elevated`, `.hairline`, `.operatorChip`, `.textPrimary`, `.textSecondary`, `.textTertiary`,
  `.brand` (the `accent` token; `Color.accentColor` stays the system's and resolves to it), `.onAccent`, `.warning`,
  `.danger`.
- Produces: `Design.Space` (`gutter`, `turn` 20, `inTurn` 8, `s4…s24`), `Design.Radius` (`chip` 18, `composer` 24, `card` 10,
  `pill` 6), `Design.Text` (font helpers per §2.2), `Design.Motion.spring` (0.22 s, no bounce), `Design.measure` (620).
- Produces: `View.elevatedBlock()` (`elevated` fill, radius 10, continuous), `View.pill()`, `AccentBar` (3 pt bar view),
  `View.quietHitArea()` (44 pt hit area with no layout size) replacing `touchTarget()`.

- [ ] **Step 1: Write `DesignSystemTests`**, with `textTokensMeetContrast` (reads each colorset's JSON by `#filePath`, and
  requires ≥ 4.5:1 for the text tokens on `canvas` and `elevated` in each mode) and `screensUseOnlyTheDesignSystem` (scans
  `Shared`, `iOS`, `macOS` outside `Shared/Design/`: fails on `Color(red:`, `Color(#`, hex literals, `.opacity(` on
  `Color.secondary`, and `cornerRadius:` with a literal, with an allow-list file listing today's offenders that each
  lane empties).
- [ ] **Step 2: Run them:** `cd apps/shaft/Packages/SHAFTKit && swift test --filter DesignSystemTests`, expected FAIL.
- [ ] **Step 3: Add the colour sets, `Design`, helpers and `quietHitArea`.**
- [ ] **Step 4: Add `FrameMeter`**: Debug only, on with `SHAFT_DEV_FRAMES=<path>`; a `CADisplayLink` that appends per-second
  counts of frames over 1.5× the refresh interval to the file.
- [ ] **Step 5: Run the tests and `xcodegen generate`, then build both schemes:** expected PASS, BUILD SUCCEEDED.
- [ ] **Step 6: Commit** on `claude/na-design`. I then record the streaming baseline: the "slow" fixture reply on iPhone 17,
  60 s, with `FrameMeter`, on this commit, before any lane starts.

### Task M1: Feed

**Files:** Modify `apps/shaft/Shared/Feed/*.swift`, `apps/shaft/Shared/Markdown/MarkdownView.swift`.
Create `apps/shaft/Packages/SHAFTKit/Sources/SHAFTKit/CodeTokens.swift` and `.../Tests/SHAFTKitTests/CodeTokensTests.swift`.

**Interfaces:**
- Produces: `CodeTokens.tokens(in: String, language: String?) -> [CodeToken]`, with `CodeToken { range: Range<String.Index>,
  kind: .keyword | .string | .comment | .number | .plain }`; it never throws and an unknown language gives one `.plain` token.

- [ ] **Step 1: Tokeniser tests** with `aStringWithAHashIsNotAComment` (Python), `anUnknownLanguageIsPlain`,
  `tokensCoverTheWholeTextInOrder`, `anUnterminatedStringEndsAtTheEnd`; then implement for the spec §3.1 languages.
- [ ] **Step 2: Restyle per the spec's §3 table**, one item kind at a time, emptying this lane's lines of the allow-list.
  The live reply loses its Stop button (M2 adds it to the composer) and "End the conversation" leaves the feed.
- [ ] **Step 3: Tables and code** scroll sideways inside `elevatedBlock`; code has the language and Copy header; finished
  blocks use `CodeTokens` through MarkdownUI's `CodeSyntaxHighlighter`.
- [ ] **Step 4: Drive it** on iPhone 17 and iPad in the fixture server, light and dark, with a 12-column table and a
  400-character code line added to the fixture: expected nothing clipped, every item kind matches §3.
- [ ] **Step 5: Tests and builds, then commit.**

### Task M2: Chrome

**Files:** Modify `apps/shaft/Shared/Chat/*.swift`, `apps/shaft/Shared/Composer/ComposerView.swift`, `apps/shaft/Shared/Root/SessionViews.swift`,
`apps/shaft/iOS/IOSRoot.swift`. Create `apps/shaft/Packages/SHAFTKit/Sources/SHAFTKit/ComposerAction.swift` and its test.

**Interfaces:**
- Produces: `ComposerAction.primary(hasContent: Bool, replying: Bool) -> .send | .stop`; `.send` whenever there is content.

- [ ] **Step 1: `ComposerActionTests`:** `emptyWhileReplyingIsStop`, `typingWhileReplyingIsSend`, `idleIsSend`. Implement.
- [ ] **Step 2: Composer**: one glass capsule with +, the text and the primary button, no Divider; Queue after reply and Next
  cycle on a long press of Send and in +; insets 12 / 9; `safeAreaBar`, and soft scroll edges on the feed.
- [ ] **Step 3: Header**: glass groups, two-line title, status parts dropping model first and then time.
- [ ] **Step 4: Sidebar, switcher, sheets, pairing, empty and error screens** per spec §4. The duplicate ⋯ goes.
- [ ] **Step 5: iPad layout**: the column capped at `Design.measure`; sidebar collapses under 1000 pt when documents open;
  documents become a sheet under 700 pt; resizing keeps the open document.
- [ ] **Step 6: Drive** Review Focus 2, 4 and 5, then tests, builds and commit.

### Task M3: Documents

**Files:** Modify `apps/shaft/Shared/Documents/*.swift`, `apps/shaft/Shared/Attachments/AttachmentViews.swift`.

**Interfaces:**
- Consumes: M0. Produces: `CommentHighlight` (ranges of commented passages in a rendered document, from the existing
  anchors).

- [ ] **Step 1: Restyle** the panel, document page, editor, stale-save and threads per spec §4 and §5; one primary action per
  thread with ⋯ for the rest.
- [ ] **Step 2: Highlight** commented passages in `accent` at low strength; a tap opens the thread. Keep the threads list below
  the document, for passages an anchor cannot find.
- [ ] **Step 3: Drive** every document action in the fixture, then tests, builds and commit.

### Task M4: Mac

**Files:** Modify `apps/shaft/macOS/MarkdownAttributed.swift`, `MessageTextView.swift`, `MacRoot.swift`, `MacTextViews.swift`.

**Interfaces:**
- Consumes: M0, `CodeTokens`.

- [ ] **Step 1: Regression tests** in `macOSTests/MarkdownAttributedTests.swift`: `codeBlockCarriesTheTokenColours`,
  `aWideTableStaysInsideTheColumn`, plus the existing bullet and table cell tests.
- [ ] **Step 2: Restyle the renderer**: rounded `elevated` code and table blocks, tables and code scrolling sideways, Copy on
  code, type per §2.2 and the 620 pt column.
- [ ] **Step 3: Sidebar, inspector, composer and toolbar** per §4 and §5, with the Mac's type sizes.
- [ ] **Step 4: Drive** with the Debug script driver against the fixture, then tests, builds and commit.

### Task M5: Verify and ship to devices

- [ ] **Step 1: Merge** the lanes into `claude/na-design`; the allow-list must be empty. Run Kit, Mac tests and both builds.
- [ ] **Step 2: Cross-model review:** Codex on the Swift diff; Gemini on the new screenshots against the spec. Fix findings.
- [ ] **Step 3: Screenshot set:** every feed item and sheet on iPhone, iPad and Mac, light, dark, largest text and Reduce
  Transparency. I review each by eye against the spec.
- [ ] **Step 4: Streaming:** the same 60 s run as M0's baseline; missed frames must be no worse.
- [x] **Step 4b: The Mac document crash.** AppKit's Update Constraints guard, reached from
  `SplitViewChildController … didUpdateMinSize`: a message answered a zero-width size probe with a width from its content, so the
  chat column's minimum moved while the documents opened. Fixed by a fixed size for that probe, plus a fixed column minimum.
  Reproduced with a long chat: unfixed crashed 2 of 3 runs; fixed builds 0 of 14 valid runs. `DevServer/mac-doc-open.drive` repeats it,
  on an unlocked screen only. The freeze first reported here did not exist: see the commit removing the block splitter.
- [ ] **Step 5: Install** on the operator's iPhone, and in `/Applications` on the Mac, and report what he should look at.
