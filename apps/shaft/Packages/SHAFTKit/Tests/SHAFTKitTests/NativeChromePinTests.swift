import Foundation
import Testing

/// The operator's three decisions of 2026-11-04 live in view code a unit test cannot draw, so these pin the source: each
/// one catches removal of the pinned source, not broken behaviour. The frames check how it behaves.
struct NativeChromePinTests {
    private func shared(_ path: String) throws -> String {
        let app = URL(filePath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: app.appending(path: path), encoding: .utf8)
    }

    @Test func cycleConfidenceRemovesOnlyTheWholeNumbersDecimalSuffix() throws {
        let source = try shared("Shared/Cycles/CycleDetailView.swift")
        let format = ##"String($0).replacingOccurrences(of: #"\.0$"#, with: "", options: .regularExpression)"##
        #expect(source.contains(format))
    }

    @Test func anImprovementReadErrorIsShownBeforeTheNoPlanState() throws {
        let source = try shared("Shared/Cycles/CyclesPanel.swift")
        #expect(source.contains("if !plan.improvementError.isEmpty { Text(plan.improvementError) }"))
        #expect(source.contains("else if let improvement = plan.improvement"))
    }

    @Test func openingADocumentReturnsThePanelToDocuments() throws {
        #expect(try shared("Shared/Chat/ChatScreenModel.swift").contains("sidePanel = .documents"))
    }

    @Test func theSidePanelUsesASystemSegmentedControl() throws {
        #expect(try shared("Shared/Cycles/SidePanel.swift").contains(".pickerStyle(.segmented)"))
    }

    /// Bug: an AppKit text view holding the keyboard left the Quick Look panel saying "No items selected".
    @Test func theMacPreviewsFilesWithoutTheResponderChain() throws {
        let preview = try shared("Shared/Attachments/FilePreviewView.swift")
        #expect(preview.contains("QLPreviewView"))
        let app = URL(filePath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        for directory in ["Shared", "macOS", "iOS"] {
            let files = try #require(FileManager.default.enumerator(
                at: app.appending(path: directory), includingPropertiesForKeys: nil
            ))
            for case let file as URL in files where file.pathExtension == "swift" {
                if file.lastPathComponent == "FilePreviewView.swift" { continue }
                let source = try String(contentsOf: file, encoding: .utf8)
                #expect(!source.contains(".quickLookPreview("), "Unexpected preview modifier in \(file.path)")
            }
        }
    }

    /// A whole row taller than four screens never crossed the old visibility threshold.
    @Test func theDotClearsAtTheEndOfTheNewestRowHoweverLongItIs() throws {
        let source = try shared("Shared/Feed/FeedView.swift")
        #expect(!source.contains("threshold: 0.25"))
        #expect(source.contains("if let newest, row.id == newest {"))
        #expect(source.contains(".id(newest)"))
        #expect(source.contains(".onScrollVisibilityChange { visible in"))
        // Both lost the dot in the simulator: a report from the row that was newest spoke for the new one, and a cancelled
        // seen held its version until its task noticed, so the send for the new row was refused.
        #expect(source.contains("@State private var endOnScreen: String?"))
        #expect(source.contains(".onChange(of: newest != nil && endOnScreen == newest, initial: true)"))
        let chat = try shared("Shared/Chat/ChatScreen.swift")
        let stop = try #require(chat.range(of: "private func stopMarkingSeen()"))
        #expect(chat[stop.upperBound...].prefix(120).contains("seenVersion = nil"))
        // A retry must not outlive the chat being in front.
        #expect(chat.contains("if watched && inFront { markSeen() } else { stopMarkingSeen() }"))
    }

    /// Bug: thumbs showed only on the last answer, so a message sent proactively had none; and a turn that failed with no
    /// text asked for a rating of nothing.
    @Test func theThumbsAreInEveryFooterWithSomethingToRate() throws {
        let source = try shared("Shared/Feed/FeedItemViews.swift")
        let footer = try #require(source.range(of: "struct AnswerFooter"))
        let body = source[footer.lowerBound...]
        let thumb = try #require(body.range(of: #"ThumbButton(thumb: "up""#))
        let before = body[..<thumb.lowerBound]
        #expect(!before.contains("if keepActions || rating != nil {"))
        #expect(before.hasSuffix("if rateable {\n                    // Always here, on every message and answer: never only on hover or in a menu (2026-11-04).\n                    "))
        #expect(source.contains("rateable: !answer.content.isEmpty"))
    }

    /// Bug: on the Mac the last turn had Copy and Reply before the thumbs and every other turn after them.
    @Test func theMacsFooterIsInOneOrder() throws {
        let source = try shared("Shared/Feed/FeedItemViews.swift")
        let footer = try #require(source.range(of: "struct AnswerFooter"))
        let body = source[footer.lowerBound...]
        let thumb = try #require(body.range(of: #"ThumbButton(thumb: "up""#))
        let hover = try #require(body.range(of: "HoverActions(source: source, shown: hovering"))
        #expect(thumb.lowerBound < hover.lowerBound)
        let row = body[..<thumb.lowerBound]
        #expect(row.contains("#if os(iOS)\n                if keepActions && !source.isEmpty {"))
        #expect(!body.contains("if !keepActions && !source.isEmpty { HoverActions"))
    }

    /// Bug: the title sat bare on the scrolling text, then in a capsule with its text in the curve.
    @Test func theTitleHasItsOwnCapsule() throws {
        let source = try shared("Shared/Chat/ChatScreen.swift")
        #expect(source.contains(".frame(minHeight: Design.Header.height)"))
        #expect(source.contains(".glassSurface(in: Capsule())"))
        #expect(source.contains(".padding(.horizontal, Design.Header.titleInset)"))
        #expect(try shared("Shared/Design/Design.swift").contains("static let titleInset: CGFloat = 16"))
    }

    /// Bug: the system's edge left a blurred half line under the bar, its opaque band sliced the last line beside Latest,
    /// and Reduce Transparency hit a hard cut. One plain fade, the same everywhere.
    @Test func theFeedEndsInOnePlainFade() throws {
        let screen = try shared("Shared/Chat/ChatScreen.swift")
        #expect(screen.contains(".scrollEdgeEffectHidden(true, for: .all)"))
        #expect(!screen.contains("scrollEdgeEffectStyle"))
        #expect(!screen.contains("scrollEdgeEffectHidden(reduceTransparency"))
        #expect(!screen.contains(".safeAreaBar"))
        #expect(screen.contains(".feedEdges(pillVisible: screen.latestPill != nil) {"))
        #expect(screen.contains(".feedFloor()"))
        let design = try shared("Shared/Design/Design.swift")
        #expect(design.contains("static let fade: CGFloat = 32"))
        // At rest the last line stands clear of the fade.
        let feed = try shared("Shared/Feed/FeedView.swift")
        #expect(feed.contains(".padding(.top, metrics.fade)"))
        #expect(feed.contains(".padding(.bottom, metrics.fade - Design.Space.turn)"))
    }

    /// Bug: the 3 pt bar ran past the time and icons to the end of their 28 pt target.
    @Test func anAccentBarStopsAtItsContent() throws {
        let source = try shared("Shared/Feed/FeedItemViews.swift")
        #expect(source.contains(".padding(.bottom, -Design.Footer.slack)"))
        #expect(source.contains(".padding(.vertical, -Design.Footer.slack)"))
    }

    /// Bug: the shortcuts line appeared with focus and moved the composer and the feed.
    @Test func theComposersShortcutsAreATooltip() throws {
        let source = try shared("Shared/Composer/ComposerView.swift")
        #expect(source.contains(#".help("Return sends · Shift-Return new line · ⌘-Return queues after the reply")"#))
        #expect(!source.contains("HardwareKeyboard"))
    }

    /// Bug: Queue after reply and Next cycle were hidden behind a long press on Send and the + menu.
    @Test func theWaysToSendAreVisibleBesideSendNotOnlyBehindALongPress() throws {
        let source = try shared("Shared/Composer/ComposerView.swift")
        #expect(source.contains("if !chat.draft.isEmpty {\n                        moreWaysToSend.padding(.bottom, controlLift)\n                    }\n                    primaryButton"))
        let menuStart = try #require(source.range(of: "private var moreWaysToSend: some View {"))
        let primaryStart = try #require(source.range(of: "private var primaryButton: some View {"))
        let menu = source[menuStart.lowerBound..<primaryStart.lowerBound]
        #expect(menu.contains("Menu {\n            sendModes\n        } label: {"))
        #expect(menu.contains(#"Image(systemName: "chevron.up")"#))
        #expect(menu.contains(#".accessibilityLabel("More ways to send")"#))
        #expect(menu.contains(#".help("More ways to send")"#))
        #expect(menu.contains(".disabled(sending)"))
        let sendStart = try #require(source.range(of: "private var sendButton: some View {"))
        let send = source[sendStart.lowerBound...]
        let macStart = try #require(send.range(of: "#if os(macOS)"))
        let macEnd = try #require(send.range(of: "#else"))
        let mac = send[macStart.upperBound..<macEnd.lowerBound]
        #expect(mac.contains("Button {\n                send(.now)\n            } label: {"))
        #expect(mac.contains(#"buttonFace("arrow.up", on: !chat.draft.isEmpty)"#))
        #expect(mac.contains(".buttonStyle(.plain)"))
        #expect(!mac.contains("Menu {"))
    }

    /// Bug: a change of text size with the keyboard up left an empty field's line at the old size: the capsule stayed tall,
    /// or the placeholder and the caret stood sliced at its top. Reproduced by the frames in the 2026-11-04 polish.
    @Test func anEmptyFieldFollowsTheTextSize() throws {
        let source = try shared("Shared/Platform/TextViews.swift")
        #expect(source.contains("registerForTraitChanges([UITraitPreferredContentSizeCategory.self])"))
        #expect(source.contains("guard let self, self.text.isEmpty else { return }"))
        #expect(source.contains("super.contentOffset = isScrollEnabled ? newValue : .zero"))
        #expect(source.contains("placeholder.topAnchor.constraint(equalTo: frameLayoutGuide.topAnchor"))
    }

    /// Bug: iOS's code and tables cut off at the block's edge with nothing to say more lay beyond.
    @Test func sidewaysBlocksFadeAtAnEdgeWithMore() throws {
        let source = try shared("Shared/Markdown/MarkdownView.swift")
        #expect(source.components(separatedBy: ".sidewaysFade()").count == 4)
    }

    /// Bugs: a sheet taller than its text; the chat showing through Earlier chats; the error twice on the iPad.
    @Test func sheetsFitAndCoverAndTheErrorShowsOnce() throws {
        #expect(try shared("Shared/Documents/DocumentDetailView.swift").contains(".presentationDetents(textHeight.map"))
        #expect(try shared("Shared/Chat/ChatSheets.swift").contains(".presentationBackground(Color.canvas)"))
        #expect(try shared("Shared/Root/SessionViews.swift").contains("if store.loaded, let error = store.error {"))
    }

    /// Bug: without the sidebar's toggle, documents open in portrait left the iPad no way back to its chats.
    @Test func thePadKeepsTheSidebarsToggle() throws {
        #expect(try !shared("iOS/IOSRoot.swift").contains(".toolbar(removing:"))
    }

    /// Bug: the selected row was the system's accent, with its grey text unreadable on it.
    @Test func aSelectedRowIsNotTheSystemsAccent() throws {
        let source = try shared("Shared/Root/SessionViews.swift")
        #expect(source.contains(".fill(row.name == selection ? Color.rowSelected : Color.clear)"))
        #expect(source.contains(".listRowBackground(rowFill(row))"))
        #expect(source.contains("table.selectionHighlightStyle = .none"))
    }

    /// Bug: opening + or Send's menu morphed the glass it sat in, and the whole composer went with it.
    @Test func theComposersGlassIsBehindItsMenus() throws {
        let source = try shared("Shared/Composer/ComposerView.swift")
        #expect(source.contains("Color.clear.glassSurface(in: RoundedRectangle"))
    }

    /// Bug: the Latest pill was an overlay on the scroll view and covered table cells and footers; then, in the bar, it
    /// raised an opaque band at rest. It keeps its own width; the foot clears behind it only while it shows.
    @Test func latestSitsInTheFade() throws {
        #expect(try !shared("Shared/Feed/FeedView.swift").contains(".overlay(alignment: .bottom)"))
        let screen = try shared("Shared/Chat/ChatScreen.swift")
        let edges = try #require(screen.range(of: ".feedEdges(pillVisible: screen.latestPill != nil) {"))
        let pill = try #require(screen.range(of: "LatestPillButton(pill: pill)"))
        let bar = try #require(screen.range(of: ".safeAreaInset(edge: .bottom"))
        #expect(edges.lowerBound < pill.lowerBound && pill.lowerBound < bar.lowerBound)
        let button = try shared("Shared/Feed/FeedView.swift")
        let start = try #require(button.range(of: "struct LatestPillButton"))
        let body = button[start.lowerBound...]
        let end = try #require(body.range(of: "\n}\n"))
        #expect(!body[..<end.lowerBound].contains(".frame(maxWidth: .infinity)"))
    }

    @Test func theSettingsAreReachableFromTheChatMenuAndTheTitle() throws {
        let source = try shared("Shared/Chat/ChatScreen.swift")
        let menuStart = try #require(source.range(of: "        Menu {"))
        let earlier = try #require(source.range(of: #"Button { screen.showEarlierChats = true }"#))
        let menu = source[menuStart.lowerBound..<earlier.lowerBound]
        #expect(menu.contains("if !screen.archived {"))
        #expect(menu.contains(#"Label("Settings for \(chat.instance)", systemImage: "gearshape")"#))
        #expect(menu.contains("screen.showInstanceSettings = true"))
        let titleStart = try #require(source.range(of: "struct ChatTitle: View"))
        let title = source[titleStart.lowerBound...]
        #expect(title.contains("if !screen.archived {"))
        #expect(title.contains(#"Button("Settings for \(instance)")"#))
        #expect(title.contains("showNote = false\n                        screen.showInstanceSettings = true"))
        #expect(source.contains(".sheet(isPresented: $screen.showInstanceSettings)"))
        #expect(source.contains("|| screen.showInstanceSettings ||"))
        #expect(try shared("Shared/Chat/ChatScreenModel.swift").contains("var showInstanceSettings = false"))
    }

    @Test func theServerSectionIsSharedByBothSettingsScreens() throws {
        #expect(try shared("Shared/Root/SessionViews.swift").contains("ServerSection(store: store.services)"))
        #expect(try shared("Shared/Settings/InstanceSettingsView.swift").contains("ServerSection(store: services)"))
        let server = try shared("Shared/Settings/ServerSection.swift")
        #expect(server.contains("let store: ServicesStore"))
        #expect(server.contains("These restart a service for all instances."))
        #expect(!server.contains(".stop()"))
    }

    @Test func aRestartAsksFirst() throws {
        let source = try shared("Shared/Settings/ServerSection.swift")
        #expect(source.contains(#"Button("Restart") { confirming = service.name }"#))
        let dialogStart = try #require(source.range(of: ".confirmationDialog(confirmation"))
        let dialogEnd = try #require(source.range(of: "private var confirmation"))
        let dialog = source[dialogStart.lowerBound..<dialogEnd.lowerBound]
        #expect(dialog.contains("Task { await store.restart(name) }"))
        #expect(source.components(separatedBy: "store.restart(").count == 2)
        #expect(source.contains("Restart the API? Running replies would be cut, so it refuses while one runs."))
        #expect(source.contains("Restart the scheduler once its runs end? It starts nothing new until then, and cuts nothing."))
    }

    @Test func textDraftSavesOnFocusLossAndDisappearanceWithoutClearingRefusals() throws {
        let source = try shared("Shared/Settings/InstanceSettingsView.swift")
        #expect(source.contains("@FocusState private var focused: Bool"))
        #expect(source.contains(".onChange(of: focused) { if !focused { submit() } }"))
        #expect(source.contains(".onDisappear(perform: submit)"))
        #expect(source.contains("guard let text = draft, text != serverText else { return }"))
        #expect(source.contains("if await store.save(field, value: value), draft == text { draft = nil }"))
        #expect(!source.contains(".onChange(of: serverText) { draft = nil }"))
        #expect(source.contains(".keyboardType(field == .wakeInterval ? .numberPad : field == .cloudCap ? .decimalPad : .default)"))
    }

    @Test func dailyCapPlaceholderDoesNotRepeatItsLabel() throws {
        let source = try shared("Shared/Settings/InstanceSettingsView.swift")
        #expect(source.contains(#"prompt: Text(field == .cloudCap ? "Default""#))
    }

    /// Bug: a Mac settings field repeated "Your name" as a label beside an unmarked blank field.
    @Test func aSettingsFieldHidesItsOwnLabelOnTheMac() throws {
        let source = try shared("Shared/Settings/InstanceSettingsView.swift")
        #expect(source.contains(".labelsHidden()"))
        #expect(source.contains("prompt:"))
    }

    @Test func servicesTitleAndPidAvoidAmbiguousServerAndLocalizedNumbers() throws {
        let source = try shared("Shared/Settings/ServerSection.swift")
        #expect(source.contains(#"header: { Text("Services") }"#))
        #expect(source.contains(#"Text("Restarted, new process \(String(pid))")"#))
    }

    @Test func unconfirmedTextDoesNotClaimAMinuteForSchedulerDrains() throws {
        let source = try shared("Shared/Settings/ServerSection.swift")
        #expect(source.contains("Asked, but no new process has appeared yet."))
        #expect(!source.contains("within a minute"))
    }

    @Test func chosenReadingSizePreservesInheritedAccessibilitySize() throws {
        let source = try shared("Shared/Design/Appearance.swift")
        #expect(source.contains(#"@Environment(\.dynamicTypeSize) private var inheritedSize"#))
        #expect(source.contains("if !inheritedSize.isAccessibilitySize, let size ="))
    }

}
