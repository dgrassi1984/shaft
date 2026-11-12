import AppKit
import SHAFTKit
import SwiftUI

/// The wizard's Access step: what the new instance may reach beyond its own folder, every row optional. "Your Mac" (a
/// folder you choose, the real browser) and "Elsewhere" (web search through a ScrapeBadger key, a Telegram bot); the
/// rows the chosen drive suggests come ticked, tagged "suggested". Continue applies the ticked rows in order and stops
/// at the first failure with its real words and Try again (SetupSteps.applyAccess); "Skip all", in the footer, goes on
/// granting nothing. The Google rows are the next step of this build: a debug build shows them, inactive.
struct AccessStepView: View {
    let setup: MacSetup
    let session: SHAFTSession
    @Bindable var steps: SetupSteps

    static let botFather = URL(string: "https://t.me/BotFather")!
    static let scrapeBadger = URL(string: "https://scrapebadger.com")!

    var body: some View {
        let name = setup.state.createdInstance ?? "your instance"
        VStack(alignment: .leading, spacing: Design.Space.s16) {
            Text("Access").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
            Text("What \(name) may reach beyond its own folder. Everything here is optional: the rows your drive "
                 + "suggests are ticked, and you can grant more, or take it back, at any time.")
                .foregroundStyle(Color.textPrimary)
                .fixedSize(horizontal: false, vertical: true)
            Label("Anything \(name) reads can leave your Mac.", systemImage: "exclamationmark.triangle")
                .foregroundStyle(Color.warning)
                .fixedSize(horizontal: false, vertical: true)
            if steps.loadingAccess.running {
                ProgressView().controlSize(.small)
            }
            if let failure = steps.loadingAccess.failure {
                SetupFailure(message: failure, retry: prepare)
            }

            group("Your Mac") {
                row(.folder, detail: "\(name) reads it, never writes it: it sees it as shared/.") { folder }
                row(.browser, detail: "Chromium, shared by every instance, used from inside its run tool. The first "
                    + "install downloads it: a few minutes.") { browser }
            }
            #if DEBUG
            google
            #endif
            group("Elsewhere") {
                row(.web, detail: "Search the web through ScrapeBadger. The key is SHAFT's, for every instance: "
                    + "\(name) never sees it.") { webSearch }
                row(.telegram, detail: "Talk with \(name) from Telegram, through a bot of its own.") { telegram }
            }
        }
        .onAppear(perform: prepare)
    }

    private func prepare() { steps.prepareAccess(store: session.store, setup: setup) }
    private func apply() { steps.applyAccess(store: session.store, setup: setup) }

    private func group<Content: View>(_ title: String, @ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            Text(title).font(.headline).foregroundStyle(Color.textPrimary)
            content()
        }
        .padding(Design.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .elevatedBlock()
    }

    /// One row: its tick (or "On" once granted), the "suggested" tag, a line about it, its own field, and while it is
    /// applied its progress, or the failure that stopped Continue there.
    private func row<Extra: View>(_ row: AccessRow, detail: String, @ViewBuilder extra: () -> Extra) -> some View {
        let on = steps.isOn(row)
        let ticked = steps.accessChecked.contains(row)
        return VStack(alignment: .leading, spacing: Design.Space.s4) {
            HStack(spacing: Design.Space.s8) {
                if on {
                    Label(row.title, systemImage: "checkmark.circle.fill")
                        .foregroundStyle(Color.textPrimary)
                    Text("On").font(.caption).foregroundStyle(Color.brand)
                } else {
                    Toggle(row.title, isOn: checked(row))
                        .toggleStyle(.checkbox)
                        .foregroundStyle(Color.textPrimary)
                        .disabled(steps.granting.running)
                }
                if steps.accessSuggested.contains(row) {
                    Text("suggested")
                        .font(.caption)
                        .foregroundStyle(Color.brand)
                        .padding(.horizontal, Design.Space.s8)
                        .overlay(Capsule().strokeBorder(Color.brand, lineWidth: 1))
                }
            }
            Text(detail).font(.callout).foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            if ticked, !on {
                extra()
                    .disabled(steps.granting.running)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 480, alignment: .leading)
            }
            if steps.grantingRow == row {
                applying(row)
            }
            if steps.failedRow == row, let failure = steps.granting.failure {
                SetupFailure(message: failure, retry: apply)
            }
        }
        .accessibilityElement(children: .contain)
    }

    private func checked(_ row: AccessRow) -> Binding<Bool> {
        Binding(get: { steps.accessChecked.contains(row) },
                set: { tick in
                    if tick { steps.accessChecked.insert(row) } else { steps.accessChecked.remove(row) }
                })
    }

    @ViewBuilder
    private func applying(_ row: AccessRow) -> some View {
        HStack(spacing: Design.Space.s8) {
            ProgressView().controlSize(.small)
            Text(row == .browser ? "Installing the browser and turning it on: this can take a few minutes…"
                 : "Granting…")
                .foregroundStyle(Color.textSecondary)
            if row == .browser {
                Button("Cancel") { steps.cancelAccess() }
            }
        }
        if row == .browser {
            Text("Cancel stops the wait here; an install the server has started finishes there.")
                .font(.caption).foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: Rows' own fields

    private var folder: some View {
        HStack(spacing: Design.Space.s12) {
            Button(steps.accessFolder.isEmpty ? "Choose a folder…" : "Choose another…", action: chooseFolder)
            if !steps.accessFolder.isEmpty {
                Text(steps.accessFolder)
                    .font(Design.Text.code)
                    .foregroundStyle(Color.textSecondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
            }
        }
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = false
        panel.prompt = "Share"
        panel.message = "The folder \(setup.state.createdInstance ?? "your instance") may read"
        if panel.runModal() == .OK, let url = panel.url {
            steps.choose(folder: url)
        }
    }

    @ViewBuilder
    private var browser: some View {
        if let installed = steps.access?.browser.installed {
            Text("Chromium \(installed) is installed; Continue turns on the run tool of "
                 + "\(setup.state.createdInstance ?? "your instance").")
                .font(.caption).foregroundStyle(Color.textSecondary)
        }
    }

    private var webSearch: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            SecureField("ScrapeBadger key", text: $steps.webKey)
            Link("Get a key at scrapebadger.com", destination: Self.scrapeBadger).foregroundStyle(Color.brand)
        }
    }

    private var telegram: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text("In Telegram, message @BotFather, send /newbot, and paste the token it gives you here.")
                .font(.caption).foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            SecureField("Bot token", text: $steps.telegramToken)
            Link("Open @BotFather", destination: Self.botFather).foregroundStyle(Color.brand)
        }
    }

    #if DEBUG
    /// The Google rows, inactive until the Google setup is built (the next step of this build): a debug build shows
    /// where they go; a release build leaves the group out.
    private var google: some View {
        group("Google — one-time setup, ~10 min, guided") {
            ForEach(AccessRow.google) { row in
                HStack(spacing: Design.Space.s8) {
                    Toggle(row.title, isOn: .constant(steps.accessSuggested.contains(row)))
                        .toggleStyle(.checkbox)
                        .disabled(true)
                    if steps.accessSuggested.contains(row) {
                        Text("suggested").font(.caption).foregroundStyle(Color.textSecondary)
                    }
                }
            }
            Button("Set up Google…") {}
                .disabled(true)
            Text("Available in the next step of this build").font(.caption).foregroundStyle(Color.textSecondary)
        }
    }
    #endif
}
