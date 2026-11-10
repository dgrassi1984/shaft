#if DEBUG
import AppKit
import SHAFTKit
import SwiftUI

/// Debug builds only, with `SHAFT_DEV_DRIVE=<script>` (and `SHAFT_DEV_DIR`, MacDevSession): drives the app from a
/// script of steps, through the app's own event queue — the key and mouse events a person makes go through the same
/// menus, key equivalents, monitors and text views — so the Mac UI can be checked from a terminal. Each step is
/// logged to stdout ("drive: …"). One step per line, `#` for a comment:
///
///     activate                      bring the app to the front, and again before each capture
///     float                         keep the window above other apps' windows (shown, not in front)
///     open <instance>               show that chat
///     key <combo>                   return, shift+return, option+return, cmd+return, option+down, cmd+., slash, …
///     type <text>                   type the text, a key at a time
///     select <text>                 select <text> in the first message that shows it (as a selection made there)
///     drag <text>                   drag the mouse over it instead (the window must be in front to take it)
///     dragacross <a> | <b>          drag from <a> to <b>, which may be in different blocks of one message
///     contextmenu <text>            log the right-click menu of a message, its selection kept
///     rightclick <text>             right-click on <text> (the window in front): logs the menu, then closes it
///     menu <title>                  pick the menu bar item with that title
///     button <title>                click the button with that title (accessibility title) in the window
///     pastefile <path>              paste that file into the composer, as a file copied in Finder
///     archived <cid>                open that earlier chat of the chat shown
///     windows / shotwindow <title>  log the visible windows / draw the one with that title
///     docs                          show or hide the documents, without animation
///     doc <path>                    open that document in the documents
///     contextpick <text> | <item>   pick an item of the right-click menu on <text>
///     retryunsent                   what Retry on the first not-sent message does
///     quicklook <path>              open a file as a file chip does (file preview sheet)
///     rows                          log the chat list's unread marks and the badge
///     selecttext <doc> | <text>     open Select text over <text> (<doc>: the document it is from, or "")
///     sheetpick <text> | <item>     in the sheet's text, select <text> and pick an item of its right-click menu
///     hover <text>                  move the pointer over <text> in a message
///     until                         wait for the chat shown to have its feed; log how long it took
///     selection                     log what is selected in the message that has the keyboard
///     cleardraft                    empty the composer
///     layout                        log the width of each column, and what each split item is limited to
///     divider <width>               drag the inspector's divider so the inspector is that wide
///     state                         log the chat's draft, quotes, selection and focus
///     shot <name>                   screenshot of the window to $SHAFT_DEV_DIR/<name>.png
///     close / notify <instance>     close the window / act as a click on a notification about <instance>
///     nofocus                       take the keyboard from whatever has it
///     capture <name>                the window as the screen shows it (with a terminal that answers, see the step)
///     capturewindow <title> | <name>  the same for another window (Settings)
///     appearance light|dark         draw the app in that appearance (the system's stays as it is)
///     blocks                        log each code block and table: its frame and how wide its text is
///     scrollblock <n> <x>           scroll the n-th block sideways to x
///     wheelblock <n> <dx> <dy>      a wheel gesture over the n-th block's text; logs where the block and the feed went
///     voicestate                    log the voice session on screen (VoiceDebug.current): state, transcript, the
///                                   sentence spoken, status line, substitution, last delay, muted
///     voicewait <state> <seconds>   wait up to <seconds> for the voice session to reach <state> (listening,
///                                   userTalking, thinking, speaking, ended, idle); logs it reached or timed out
///     wait <seconds>
///     quit
@MainActor
enum MacDevDriver {
    /// The app's own key monitor ("/"), run before a combo is routed, as AppKit runs it for a queued event.
    static var delegateMonitor: ((NSEvent) -> NSEvent??)?

    static func start(state: MacState, model: AppModel, delegate: AppDelegate) {
        guard let path = ProcessInfo.processInfo.environment["SHAFT_DEV_DRIVE"], let dir = MacDevSession.directory,
              let script = try? String(contentsOfFile: path, encoding: .utf8) else { return }
        let steps = script.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty && !$0.hasPrefix("#") }
        Task {
            try? await Task.sleep(for: .seconds(4))
            for step in steps {
                let (verb, rest) = split(step)
                log("\(verb) \(rest)")
                await run(verb, rest, state: state, model: model, delegate: delegate, dir: dir)
                try? await Task.sleep(for: .milliseconds(700))
            }
            log("done")
        }
    }

    static func log(_ text: String) {
        print("drive: \(Date().formatted(.dateTime.hour().minute().second().secondFraction(.fractional(2)))) \(text)")
        fflush(stdout)
    }

    private static func split(_ step: String) -> (String, String) {
        guard let space = step.firstIndex(of: " ") else { return (step, "") }
        return (String(step[..<space]), String(step[step.index(after: space)...]))
    }

    /// The voice session on screen, as `voicestate` logs it.
    private static func voiceState() -> String {
        guard let voice = VoiceDebug.current else { return "voice: no session on screen" }
        let spoken = voice.speaking.map { "\(String(reflecting: $0.text))\($0.isStatus ? " (status)" : "")" } ?? "-"
        let delay = voice.latency.map { String(format: "%.2f s", $0) } ?? "-"
        return "voice: instance=\(voice.instance) language=\(voice.language.rawValue) state=\(voice.state.rawValue) "
            + "transcript=\(String(reflecting: voice.transcript)) speaking=\(spoken) "
            + "status=\(String(reflecting: voice.statusLine)) substitution=\(voice.voiceSubstitution.map { String(reflecting: $0) } ?? "-") "
            + "latency=\(delay) muted=\(voice.muted)"
    }

    private static var window: NSWindow? {
        NSApp.windows.first { $0.identifier?.rawValue.hasPrefix(MacRoot.windowID) == true && $0.isVisible }
    }

    // swiftlint:disable:next cyclomatic_complexity function_body_length
    private static func run(_ verb: String, _ rest: String, state: MacState, model: AppModel, delegate: AppDelegate,
                            dir: URL) async {
        switch verb {
        case "activate":
            NSApp.activate(ignoringOtherApps: true)
            window?.makeKeyAndOrderFront(nil)
            keepFront = true
            await comeToFront()
        case "float":
            // Above other apps' windows (not in front: the operator keeps working), so the chat shows and polls.
            window?.level = .floating
            window?.orderFrontRegardless()
        case "open":
            state.open(rest)
        case "wait":
            try? await Task.sleep(for: .seconds(Double(rest) ?? 1))
        case "key":
            key(rest)
        case "type":
            for character in rest { key(String(character), literal: true) }
        case "select":
            guard let (view, range) = find(rest) else { return log("no message shows \"\(rest)\"") }
            select(view, range)
        case "drag":
            guard let (view, range) = find(rest) else { return log("no message shows \"\(rest)\"") }
            drag(view, range)
        case "dragacross":
            let parts = rest.components(separatedBy: " | ")
            guard parts.count == 2, let (from, a) = find(parts[0]), let (to, b) = find(parts[1]) else {
                return log("no message shows \"\(rest)\"")
            }
            let (start, end) = (point(from, a.location), point(to, NSMaxRange(b) - 1, end: true))
            mouse(.leftMouseDown, at: start)
            for step in 1...6 {
                let t = CGFloat(step) / 6
                mouse(.leftMouseDragged, at: NSPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t))
            }
            mouse(.leftMouseUp, at: end)
        case "rightclick":
            guard let (view, range) = find(rest) else { return log("no message shows \"\(rest)\"") }
            rightClick(view, range)
        case "contextmenu":
            guard let (view, range) = find(rest) else { return log("no message shows \"\(rest)\"") }
            contextMenu(view, range)
        case "menu":
            guard let item = menuItem(rest, in: NSApp.mainMenu) else { return log("no menu item \"\(rest)\"") }
            // As it stands, without opening the menu: what a key equivalent finds too.
            log("menu item \"\(rest)\": key \(String(reflecting: item.keyEquivalent)) mask \(item.keyEquivalentModifierMask.rawValue) on \(item.isEnabled)")
            guard item.isEnabled else { return log("menu item \"\(rest)\" is off") }
            item.menu?.performActionForItem(at: item.menu?.index(of: item) ?? 0)
        case "button":
            guard let window, let button = control(rest, in: window.contentView) else { return log("no button \"\(rest)\"") }
            button.accessibilityPerformPress()
        case "state":
            let chat = state.screen?.chat
            log("chat=\(state.selection ?? "-") archived=\(state.screen?.archived ?? false) draft=\(String(reflecting: chat?.draft.text ?? "")) quotes=\(chat?.draft.quotes.map(\.quote) ?? []) answers=\(chat?.draft.quotes.map(\.answer) ?? []) files=\(chat?.draft.attachments.map(\.name) ?? []) first=\(window?.firstResponder.map { String(describing: type(of: $0)) } ?? "-") said=\(chat?.said ?? "-") error=\(chat?.actionError?.message ?? "-") visibleChat=\(model.visibleChat ?? "-") docs=\(state.screen?.showDocuments ?? false) dock=\(NSApp.dockTile.badgeLabel ?? "-") window=[key \(window?.isKeyWindow ?? false) occluded \(!(window?.occlusionState.contains(.visible) ?? false)) active \(NSApp.isActive)] replying=\(chat?.feed?.reply != nil) feed=\(chat?.feed != nil) polling=\(chat?.isPolling ?? false) connection=\(String(describing: chat?.connection)) queued=\(chat?.content?.queued.count ?? -1) unsent=\(chat?.unsent.count ?? -1)")
        case "shot":
            shot(rest, dir: dir)
        case "voicestate":
            log(voiceState())
        case "voicewait":
            // `<state> <seconds>`: the voice session on screen reaching that state (the panel opens with `menu Talk`).
            let parts = rest.split(separator: " ")
            guard parts.count == 2, let wanted = VoiceState(rawValue: String(parts[0])),
                  let seconds = Double(parts[1]) else {
                return log("voicewait <state> <seconds>; states: \(VoiceState.allCases.map(\.rawValue).joined(separator: ", "))")
            }
            let start = Date()
            while VoiceDebug.current?.state != wanted, Date().timeIntervalSince(start) < seconds {
                try? await Task.sleep(for: .milliseconds(50))
            }
            let took = String(format: "%.2f", Date().timeIntervalSince(start))
            log(VoiceDebug.current?.state == wanted ? "voice reached \(wanted.rawValue) after \(took) s"
                : "voice did not reach \(wanted.rawValue) in \(parts[1]) s; \(voiceState())")
        case "updatemenus":
            // What opening each menu does: its owner brings its items up to date.
            for item in NSApp.mainMenu?.items ?? [] {
                if let menu = item.submenu { menu.delegate?.menuNeedsUpdate?(menu); menu.update() }
            }
        case "layout":
            // The split view's columns, as laid out.
            guard let root = window?.contentView else { return }
            walk(root) { view in
                guard let split = view as? NSSplitView else { return }
                log("split \(split.isVertical ? "columns" : "rows"): " + split.arrangedSubviews
                    .map { "\(Int($0.frame.width))x\(Int($0.frame.height))\($0.isHidden ? " hidden" : "") \($0.effectiveAppearance.name.rawValue)" }
                    .joined(separator: ", "))
                guard let controller = split.delegate as? NSSplitViewController else { return }
                for item in controller.splitViewItems {
                    log("  item \(item.behavior.rawValue) \(type(of: item.viewController)): min \(Int(item.minimumThickness)) max \(item.maximumThickness) hold \(item.holdingPriority.rawValue) collapsed \(item.isCollapsed) sep \(item.titlebarSeparatorStyle.rawValue) view \(Int(item.viewController.view.frame.width)) fit \(Int(item.viewController.view.fittingSize.width))")
                }
            }
            log("window \(Int(window?.frame.width ?? 0))x\(Int(window?.frame.height ?? 0)) content \(Int(window?.contentView?.frame.width ?? 0)) min \(window?.minSize ?? .zero) contentMin \(window?.contentMinSize ?? .zero)")
        case "divider":
            // The divider of the split view that has an inspector, dragged so the inspector is <width> wide.
            guard let width = Double(rest), let root = window?.contentView else { return }
            walk(root) { view in
                guard let split = view as? NSSplitView, let items = (split.delegate as? NSSplitViewController)?.splitViewItems,
                      items.contains(where: { $0.behavior == .inspector }) else { return }
                split.setPosition(split.bounds.width - width - split.dividerThickness, ofDividerAt: 0)
            }
        case "resize":
            // A new window size (`<width> <height>`), as dragging its corner does.
            let size = rest.split(separator: " ").compactMap { Double($0) }
            guard let window, size.count == 2 else { return }
            window.setContentSize(NSSize(width: size[0], height: size[1]))
        case "archived":
            // An earlier chat of the chat shown, as Earlier chats opens it.
            guard let name = state.selection else { return }
            state.path.append(ArchivedRoute(instance: name, cid: rest))
        case "windows":
            log("windows: " + NSApp.windows.filter(\.isVisible).map { "\($0.title.isEmpty ? "-" : $0.title) \(type(of: $0)) (\(Int($0.frame.width))x\(Int($0.frame.height)))" }.joined(separator: ", "))
            for other in NSApp.windows where other != window {
                log("  \(type(of: other)) visible \(other.isVisible) level \(other.level.rawValue) frame \(other.frame) parent \(other.parent.map { "\(type(of: $0))" } ?? "-") alpha \(other.alphaValue): \(other.contentView.map { describe($0) } ?? "-")")
            }
            log("  children: \(window?.childWindows?.map { "\(type(of: $0))" } ?? [])")
        case "shotwindow":
            // The visible window with that title (Settings), drawn as `shot` draws the main one.
            guard let other = NSApp.windows.first(where: { $0.isVisible && $0.title == rest }),
                  let view = other.contentView?.superview ?? other.contentView,
                  let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return log("no window \(rest)") }
            view.cacheDisplay(in: view.bounds, to: rep)
            let url = dir.appending(path: "window-\(rest).png")
            try? rep.representation(using: .png, properties: [:])?.write(to: url)
            log("shot \(url.path)")
        case "docs":
            // The documents toggled with no animation (a locked screen runs none).
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { state.screen?.showDocuments.toggle() }
        case "doc":
            // A document opened in the documents, as a chip or a document card in the feed opens it.
            state.screen?.openDocument(rest)
        case "contextpick":
            // `<text> | <item>`: pick that item of the right-click menu on <text> in a message.
            let parts = rest.components(separatedBy: " | ")
            guard parts.count == 2, let (view, range) = find(parts[0]) else { return log("no message shows \"\(rest)\"") }
            guard let menu = contextMenuOf(view, range),
                  let item = menu.items.first(where: { $0.title == parts[1] }), let action = item.action else {
                return log("no item \"\(parts[1])\"")
            }
            NSApp.sendAction(action, to: item.target, from: item)
        case "retryunsent":
            // What a not-sent message's Retry does: the same message, with its mid, sent again.
            guard let chat = state.screen?.chat, let entry = chat.unsent.first else { return log("nothing not sent") }
            _ = await chat.retry(entry.mid)
        case "quicklook":
            // A file opened as a file chip opens it: the file preview sheet over the window.
            state.screen?.preview.open(local: URL(fileURLWithPath: rest))
            try? await Task.sleep(for: .seconds(2))
            log("file preview sheet requested: \(state.screen?.preview.showing == true), markdown page: \(state.screen?.preview.page != nil)")
        case "rows":
            // The chat list as drawn: name, unread, replying.
            log("rows: " + (state.store?.instances.map { "\($0.name)\($0.unread ? " (unread)" : "")\($0.replying ? " (replying)" : "")" } ?? []).joined(separator: ", ") + " badge \(state.store?.badge ?? -1)")
        case "selecttext":
            // `<doc> | <text>`: Select text over that text (as a message's or a document's Select text opens it).
            let parts = rest.components(separatedBy: " | ")
            guard parts.count == 2 else { return log("selecttext <doc> | <text>") }
            state.screen?.select(parts[1], doc: parts[0])
        case "sheetpick":
            // `<text> | <item>`: in the sheet's text, select <text> and pick that item of its right-click menu.
            let parts = rest.components(separatedBy: " | ")
            guard parts.count == 2, let sheet = window?.attachedSheet,
                  let view = textView(in: sheet.contentView) else { return log("no sheet with text") }
            let range = (view.string as NSString).range(of: parts[0])
            guard range.location != NSNotFound else { return log("the sheet does not show \"\(parts[0])\"") }
            view.window?.makeFirstResponder(view)
            view.setSelectedRange(range)
            guard let menu = contextMenuOf(view, range) else { return log("no menu") }
            log("sheet menu: " + menu.items.map { $0.isSeparatorItem ? "|" : $0.title }.joined(separator: " · "))
            guard let item = menu.items.first(where: { $0.title == parts[1] }), let action = item.action else {
                return log("no item \"\(parts[1])\"")
            }
            NSApp.sendAction(action, to: item.target, from: item)
        case "hover":
            // The pointer moved over <text> in a message (the window must take mouse moves: in front).
            guard let (view, range) = find(rest) else { return log("no message shows \"\(rest)\"") }
            let at = point(view, range.location)
            for dx in stride(from: -40.0, through: 0, by: 10) { mouse(.mouseMoved, at: NSPoint(x: at.x + dx, y: at.y)) }
        case "until":
            // Wait (up to 20 s) until the chat shown has its feed, then log how long that took.
            let start = Date()
            while state.screen?.chat.feed == nil, Date().timeIntervalSince(start) < 20 {
                try? await Task.sleep(for: .milliseconds(20))
            }
            log("feed after \(Int(Date().timeIntervalSince(start) * 1000)) ms: \(state.screen?.chat.content?.items.count ?? -1) items, \(state.screen?.chat.content?.earlier ?? -1) earlier")
        case "selection":
            // What is selected in the message that has the keyboard.
            let view = window?.firstResponder as? MessageNSTextView
            log("selection: \(view.map { String(reflecting: $0.selectedText ?? "") } ?? "no message has the keyboard")")
        case "cleardraft":
            state.screen?.chat.draft = Draft()
        case "pastefile":
            // A file copied in Finder, pasted into the composer (a private pasteboard: the operator's stays as it is).
            let board = NSPasteboard(name: .init("io.github.dgrassi1984.shaft.dev"))
            board.clearContents()
            board.writeObjects([URL(fileURLWithPath: rest) as NSURL])
            guard let window, let field = firstComposer(in: window.contentView) else { return log("no composer") }
            log("pasted files: \(field.pasteFiles(from: board))")
        case "close":
            window?.performClose(nil)
        case "nofocus":
            window?.makeFirstResponder(nil)
        case "capture":
            // The window as the screen shows it, vibrancy and glass included: a `screencapture -l` from the terminal
            // that runs this, which watches for this line and answers with `<name>.ack` (the app has no such right).
            let ack = dir.appending(path: "\(rest).ack")
            try? FileManager.default.removeItem(at: ack)
            // Where the pointer is (a hover over the toolbar changes what it draws), and whether the window has the keys.
            if keepFront { await comeToFront() }
            let pointer = NSEvent.mouseLocation
            log("window [key \(window?.isKeyWindow ?? false) occluded \(!(window?.occlusionState.contains(.visible) ?? false)) active \(NSApp.isActive)] pointer \(Int(pointer.x)),\(Int(pointer.y)) in window \(window?.frame.contains(pointer) ?? false)")
            log("CAPTURE \(rest) \(window?.windowNumber ?? 0)")
            let start = Date()
            while !FileManager.default.fileExists(atPath: ack.path), Date().timeIntervalSince(start) < 5 {
                try? await Task.sleep(for: .milliseconds(50))
            }
        case "capturewindow":
            // `<title> | <name>`: as `capture`, for the visible window with that title (Settings).
            let parts = rest.components(separatedBy: " | ")
            guard parts.count == 2, let other = NSApp.windows.first(where: { $0.isVisible && $0.title == parts[0] }) else {
                return log("no window \(rest)")
            }
            let ack = dir.appending(path: "\(parts[1]).ack")
            try? FileManager.default.removeItem(at: ack)
            log("CAPTURE \(parts[1]) \(other.windowNumber)")
            let start = Date()
            while !FileManager.default.fileExists(atPath: ack.path), Date().timeIntervalSince(start) < 5 {
                try? await Task.sleep(for: .milliseconds(50))
            }
        case "appearance":
            NSApp.appearance = NSAppearance(named: rest == "dark" ? .darkAqua : .aqua)
        case "blocks":
            guard let root = window?.contentView else { return }
            var index = 0
            walk(root) { view in
                guard let block = view as? MessageBlockView else { return }
                log("block \(index): \(block.driveDescription)")
                index += 1
            }
        case "scrollblock":
            let parts = rest.split(separator: " ").compactMap { Double($0) }
            guard parts.count == 2, let root = window?.contentView else { return log("scrollblock <n> <x>") }
            var blocks: [MessageBlockView] = []
            walk(root) { view in if let block = view as? MessageBlockView { blocks.append(block) } }
            guard Int(parts[0]) < blocks.count else { return log("no block \(rest)") }
            blocks[Int(parts[0])].scrollSideways(to: parts[1])
        case "wheelblock":
            let parts = rest.split(separator: " ").compactMap { Int32($0) }
            guard parts.count == 3, let root = window?.contentView else { return log("wheelblock <n> <dx> <dy>") }
            var blocks: [MessageBlockView] = []
            walk(root) { view in if let block = view as? MessageBlockView { blocks.append(block) } }
            guard Int(parts[0]) < blocks.count else { return log("no block \(rest)") }
            let block = blocks[Int(parts[0])]
            func feedY() -> CGFloat { block.enclosingScrollView?.enclosingScrollView?.contentView.bounds.minY
                ?? sequence(first: block.superview) { $0?.superview }.compactMap { $0 as? NSScrollView }.first?.contentView.bounds.minY ?? -1 }
            let before = (block.driveDescription, feedY())
            for phase in [CGScrollPhase?.none, .none] {
                guard let cg = CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: parts[2],
                                       wheel2: parts[1], wheel3: 0) else { continue }
                if let phase { cg.setIntegerValueField(.scrollWheelEventScrollPhase, value: Int64(phase.rawValue)) }
                guard let event = NSEvent(cgEvent: cg) else { continue }
                block.driveText?.scrollWheel(with: event)
                log("wheel: passes to the feed: \(block.drivePassing)")
            }
            try? await Task.sleep(for: .milliseconds(300))
            log("wheel: block \(before.0) -> \(block.driveDescription); feed \(Int(before.1)) -> \(Int(feedY()))")
        case "quit":
            log("done")
            NSApp.terminate(nil)
        case "notify":
            // What `userNotificationCenter(_:didReceive:)` does with a click on a notification about `rest`.
            model.notificationTapped(instance: rest)
            state.bringForward()
        default:
            log("unknown step \(verb)")
        }
    }

    // MARK: Keys

    private static let codes: [String: (UInt16, String)] = [
        "return": (36, "\r"), "down": (125, "\u{F701}"), "up": (126, "\u{F700}"), ".": (47, "."), "slash": (44, "/"),
        "escape": (53, "\u{1B}"), "a": (0, "a"), "n": (45, "n"), "N": (45, "N"), "d": (2, "d"), "r": (15, "r"),
        "e": (14, "e"), "E": (14, "E"), "y": (16, "y"), "Y": (16, "Y"),
    ]

    private static func key(_ combo: String, literal: Bool = false) {
        guard let window else { return log("no window") }
        var flags: NSEvent.ModifierFlags = []
        var name = combo
        if !literal {
            let parts = combo.split(separator: "+").map(String.init)
            name = parts.last ?? combo
            for part in parts.dropLast() {
                switch part {
                case "cmd": flags.insert(.command)
                case "shift": flags.insert(.shift)
                case "option": flags.insert(.option)
                case "ctrl": flags.insert(.control)
                default: break
                }
            }
        }
        let (code, characters) = codes[name] ?? (0, name)
        if code == 125 || code == 126 { flags.formUnion([.numericPad, .function]) }
        let ignoring = characters
        for type in [NSEvent.EventType.keyDown, .keyUp] {
            guard let event = NSEvent.keyEvent(with: type, location: .zero, modifierFlags: flags,
                                               timestamp: ProcessInfo.processInfo.systemUptime,
                                               windowNumber: window.windowNumber, context: nil,
                                               characters: characters, charactersIgnoringModifiers: ignoring,
                                               isARepeat: false, keyCode: code) else { continue }
            if literal {
                NSApp.postEvent(event, atStart: false)     // typing: through the queue and its monitors
            } else if type == .keyDown {
                // A combo: the app may be behind (a locked screen), where AppKit skips key equivalents, so they
                // are tried here in its order. The local monitors ("/") run first, as for a queued event.
                if let monitored = delegateMonitor?(event), monitored == nil {
                    log("key \(combo): taken by a monitor")
                } else {
                    log("key \(combo): \(send(event))")
                }
            }
        }
    }

    // MARK: Mouse

    /// The first message text view (in the window's order) whose text holds `text`, and where.
    private static func find(_ text: String) -> (MessageNSTextView, NSRange)? {
        guard let root = window?.contentView else { return nil }
        var found: (MessageNSTextView, NSRange)?
        walk(root) { view in
            guard found == nil, let view = view as? MessageNSTextView else { return }
            let range = (view.string as NSString).range(of: text)
            guard range.location != NSNotFound, let layout = view.layoutManager else { return }
            // A message's text views share their text: the one that lays this part of it out.
            let glyph = layout.glyphIndexForCharacter(at: range.location)
            let owner = layout.textContainer(forGlyphAt: glyph, effectiveRange: nil)?.textView as? MessageNSTextView
            found = (owner ?? view, range)
        }
        return found
    }

    /// `activate` was asked for: each capture takes the front again first (another app may have taken it since).
    private static var keepFront = false

    /// Activates the app and waits for its window to have the keys, then for it to redraw as the window in front.
    private static func comeToFront() async {
        for attempt in 0..<60 where !(NSApp.isActive && window?.isKeyWindow == true) {
            if attempt % 10 == 0 {
                NSApp.activate(ignoringOtherApps: true)
                window?.makeKeyAndOrderFront(nil)
            }
            try? await Task.sleep(for: .milliseconds(50))
        }
        try? await Task.sleep(for: .milliseconds(250))
    }

    private static func describe(_ view: NSView) -> String {
        "\(type(of: view))" + (view.subviews.isEmpty ? "" : "[" + view.subviews.map(describe).joined(separator: ", ") + "]")
    }

    private static func walk(_ view: NSView, _ visit: (NSView) -> Void) {
        visit(view)
        for sub in view.subviews { walk(sub, visit) }
    }

    /// The point in the window of the character at `index` in `view` (its middle, at the line's middle).
    private static func point(_ view: NSTextView, _ index: Int, end: Bool = false) -> NSPoint {
        let rect = view.firstRect(forCharacterRange: NSRange(location: index, length: 1), actualRange: nil)
        guard let window = view.window, rect != .zero else { return .zero }
        let inWindow = window.convertFromScreen(rect)
        return NSPoint(x: end ? inWindow.maxX - 1 : inWindow.minX + 1, y: inWindow.midY)
    }

    private static func mouse(_ type: NSEvent.EventType, at point: NSPoint, clicks: Int = 1) {
        guard let window, let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
                                                         timestamp: ProcessInfo.processInfo.systemUptime,
                                                         windowNumber: window.windowNumber, context: nil,
                                                         eventNumber: 0, clickCount: clicks, pressure: 1) else { return }
        NSApp.postEvent(event, atStart: false)
    }

    private static func select(_ view: NSTextView, _ range: NSRange) {
        view.scrollRangeToVisible(range)
        view.window?.makeFirstResponder(view)
        view.setSelectedRange(range)
    }

    /// The menu a right-click there opens (NSTextView's own, then its delegate's items), without opening it.
    private static func contextMenuOf(_ view: NSTextView, _ range: NSRange) -> NSMenu? {
        guard let event = NSEvent.mouseEvent(with: .rightMouseDown, location: point(view, range.location),
                                             modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                             windowNumber: view.window?.windowNumber ?? 0, context: nil,
                                             eventNumber: 0, clickCount: 1, pressure: 1) else { return nil }
        return view.menu(for: event)
    }

    private static func contextMenu(_ view: NSTextView, _ range: NSRange) {
        guard let menu = contextMenuOf(view, range) else { return log("no menu") }
        log("context menu: " + menu.items.map { $0.isSeparatorItem ? "|" : $0.title }.joined(separator: " · "))
    }

    private static func drag(_ view: NSTextView, _ range: NSRange) {
        view.scrollRangeToVisible(range)
        let start = point(view, range.location)
        let end = point(view, NSMaxRange(range) - 1, end: true)
        mouse(.leftMouseDown, at: start)
        for step in 1...4 {
            let t = CGFloat(step) / 4
            mouse(.leftMouseDragged, at: NSPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t))
        }
        mouse(.leftMouseUp, at: end)
    }

    private static var menuObserver: NSObjectProtocol?

    private static func rightClick(_ view: NSTextView, _ range: NSRange) {
        menuObserver = NotificationCenter.default.addObserver(forName: NSMenu.didBeginTrackingNotification, object: nil,
                                                              queue: .main) { note in
            nonisolated(unsafe) let tracked = note.object as? NSMenu
            MainActor.assumeIsolated {
                guard let menu = tracked else { return }
                log("context menu: " + menu.items.map { $0.isSeparatorItem ? "|" : $0.title }.joined(separator: " · "))
                if let observer = menuObserver { NotificationCenter.default.removeObserver(observer) }
                menuObserver = nil
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { menu.cancelTracking() }
            }
        }
        let at = point(view, range.location + range.length / 2)
        mouse(.rightMouseDown, at: at)
        mouse(.rightMouseUp, at: at)
    }

    // MARK: Menus, buttons, screenshots

    private static func menuItem(_ title: String, in menu: NSMenu?) -> NSMenuItem? {
        for item in menu?.items ?? [] {
            if item.title == title { return item }
            if let found = menuItem(title, in: item.submenu) { return found }
        }
        return nil
    }

    private static func textView(in view: NSView?) -> NSTextView? {
        guard let view else { return nil }
        if let text = view as? NSTextView { return text }
        for sub in view.subviews { if let found = textView(in: sub) { return found } }
        return nil
    }

    private static func firstComposer(in view: NSView?) -> ComposerNSTextView? {
        guard let view else { return nil }
        if let field = view as? ComposerNSTextView { return field }
        for sub in view.subviews { if let found = firstComposer(in: sub) { return found } }
        return nil
    }

    private static func control(_ title: String, in view: NSView?) -> NSView? {
        guard let view else { return nil }
        if let button = view as? NSButton,
           button.title.contains(title) || button.accessibilityLabel()?.contains(title) == true { return button }
        for sub in view.subviews { if let found = control(title, in: sub) { return found } }
        return nil
    }

    /// The window's own drawing (works on a locked screen, where the window server captures nothing).
    private static func shot(_ name: String, dir: URL) {
        guard let window, let frame = window.contentView?.superview ?? window.contentView else {
            return log("no window to shoot")
        }
        let url = dir.appending(path: "\(name).png")
        guard let rep = frame.bitmapImageRepForCachingDisplay(in: frame.bounds) else { return log("no bitmap") }
        frame.cacheDisplay(in: frame.bounds, to: rep)
        do {
            try rep.representation(using: .png, properties: [:])?.write(to: url)
            log("shot \(url.path)")
            // A sheet is a window of its own: shot beside the window.
            if let sheet = window.attachedSheet, let view = sheet.contentView?.superview ?? sheet.contentView,
               let sheetRep = view.bitmapImageRepForCachingDisplay(in: view.bounds) {
                view.cacheDisplay(in: view.bounds, to: sheetRep)
                let sheetURL = dir.appending(path: "\(name)-sheet.png")
                try sheetRep.representation(using: .png, properties: [:])?.write(to: sheetURL)
                log("shot \(sheetURL.path)")
            }
        } catch {
            log("shot failed: \(error)")
        }
    }

    // MARK: Keys, as AppKit routes them: the key window's views, then the menu bar, then the first responder

    private static func send(_ event: NSEvent) -> String {
        guard let main = window else { return "no window" }
        let window = main.attachedSheet ?? main             // a sheet takes the keys while it is up
        if window.performKeyEquivalent(with: event) { return "view key equivalent" }
        if NSApp.mainMenu?.performKeyEquivalent(with: event) == true { return "menu key equivalent" }
        window.sendEvent(event)
        return "first responder"
    }
}
#endif
