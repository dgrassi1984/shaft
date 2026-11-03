#if DEBUG
import AppKit
import SHAFTKit

/// Debug builds only, with `SHAFT_DEV_DRIVE=<script>` (and `SHAFT_DEV_DIR`, MacDevSession): drives the app from a
/// script of steps, through the app's own event queue — the key and mouse events a person makes go through the same
/// menus, key equivalents, monitors and text views — so the Mac UI can be checked from a terminal. Each step is
/// logged to stdout ("drive: …"). One step per line, `#` for a comment:
///
///     activate                      bring the app to the front
///     float                         keep the window above other apps' windows (shown, not in front)
///     open <instance>               show that chat
///     key <combo>                   return, shift+return, option+return, cmd+return, option+down, cmd+., slash, …
///     type <text>                   type the text, a key at a time
///     select <text>                 select <text> in the first message that shows it (as a selection made there)
///     drag <text>                   drag the mouse over it instead (the window must be in front to take it)
///     contextmenu <text>            log the right-click menu of a message, its selection kept
///     rightclick <text>             right-click on <text> (the window in front): logs the menu, then closes it
///     menu <title>                  pick the menu bar item with that title
///     button <title>                click the button with that title (accessibility title) in the window
///     pastefile <path>              paste that file into the composer, as a file copied in Finder
///     cleardraft                    empty the composer
///     layout                        log the width of each column
///     state                         log the chat's draft, quotes, selection and focus
///     shot <name>                   screenshot of the window to $SHAFT_DEV_DIR/<name>.png
///     close / notify <instance>     close the window / act as a click on a notification about <instance>
///     wait <seconds>
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
        print("drive: \(text)")
        fflush(stdout)
    }

    private static func split(_ step: String) -> (String, String) {
        guard let space = step.firstIndex(of: " ") else { return (step, "") }
        return (String(step[..<space]), String(step[step.index(after: space)...]))
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
        case "rightclick":
            guard let (view, range) = find(rest) else { return log("no message shows \"\(rest)\"") }
            rightClick(view, range)
        case "contextmenu":
            guard let (view, range) = find(rest) else { return log("no message shows \"\(rest)\"") }
            contextMenu(view, range)
        case "menu":
            guard let item = menuItem(rest, in: NSApp.mainMenu) else { return log("no menu item \"\(rest)\"") }
            // As it stands, without opening the menu: what a key equivalent finds too.
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
                    .map { "\(Int($0.frame.width))x\(Int($0.frame.height))\($0.isHidden ? " hidden" : "")" }
                    .joined(separator: ", "))
            }
        case "resize":
            // A new window size (`<width> <height>`), as dragging its corner does.
            let size = rest.split(separator: " ").compactMap { Double($0) }
            guard let window, size.count == 2 else { return }
            window.setContentSize(NSSize(width: size[0], height: size[1]))
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
        "escape": (53, "\u{1B}"), "a": (0, "a"), "n": (45, "n"), "d": (2, "d"), "r": (15, "r"),
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
            if range.location != NSNotFound { found = (view, range) }
        }
        return found
    }

    private static func walk(_ view: NSView, _ visit: (NSView) -> Void) {
        visit(view)
        for sub in view.subviews { walk(sub, visit) }
    }

    /// The point in the window of the character at `index` in `view` (its middle, at the line's middle).
    private static func point(_ view: NSTextView, _ index: Int, end: Bool = false) -> NSPoint {
        guard let layout = view.layoutManager, let container = view.textContainer else { return .zero }
        let glyphs = layout.glyphRange(forCharacterRange: NSRange(location: index, length: 1), actualCharacterRange: nil)
        let rect = layout.boundingRect(forGlyphRange: glyphs, in: container)
        let local = NSPoint(x: end ? rect.maxX - 1 : rect.minX + 1, y: rect.midY)
        return view.convert(local, to: nil)
    }

    private static func mouse(_ type: NSEvent.EventType, at point: NSPoint, clicks: Int = 1) {
        guard let window, let event = NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
                                                         timestamp: ProcessInfo.processInfo.systemUptime,
                                                         windowNumber: window.windowNumber, context: nil,
                                                         eventNumber: 0, clickCount: clicks, pressure: 1) else { return }
        NSApp.postEvent(event, atStart: false)
    }

    private static func select(_ view: NSTextView, _ range: NSRange) {
        view.scrollToVisible(view.firstRect(forCharacterRange: range, actualRange: nil))
        view.window?.makeFirstResponder(view)
        view.setSelectedRange(range)
    }

    /// The menu a right-click there opens (NSTextView's own, then its delegate's items), without opening it.
    private static func contextMenu(_ view: NSTextView, _ range: NSRange) {
        guard let event = NSEvent.mouseEvent(with: .rightMouseDown, location: point(view, range.location),
                                             modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                             windowNumber: view.window?.windowNumber ?? 0, context: nil,
                                             eventNumber: 0, clickCount: 1, pressure: 1),
              let menu = view.menu(for: event) else { return log("no menu") }
        log("context menu: " + menu.items.map { $0.isSeparatorItem ? "|" : $0.title }.joined(separator: " · "))
    }

    private static func drag(_ view: NSTextView, _ range: NSRange) {
        view.scrollToVisible(view.firstRect(forCharacterRange: range, actualRange: nil))
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

    private static func firstComposer(in view: NSView?) -> ComposerNSTextView? {
        guard let view else { return nil }
        if let field = view as? ComposerNSTextView { return field }
        for sub in view.subviews { if let found = firstComposer(in: sub) { return found } }
        return nil
    }

    private static func control(_ title: String, in view: NSView?) -> NSView? {
        guard let view else { return nil }
        if let button = view as? NSButton, button.title.contains(title) { return button }
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
        } catch {
            log("shot failed: \(error)")
        }
    }

    // MARK: Keys, as AppKit routes them: the key window's views, then the menu bar, then the first responder

    private static func send(_ event: NSEvent) -> String {
        guard let window else { return "no window" }
        if window.performKeyEquivalent(with: event) { return "view key equivalent" }
        if NSApp.mainMenu?.performKeyEquivalent(with: event) == true { return "menu key equivalent" }
        window.sendEvent(event)
        return "first responder"
    }
}
#endif
