import AppKit
import SHAFTKit
import SwiftUI
import UniformTypeIdentifiers

/// Access → "Set up Google…": a sheet that walks the operator through their own OAuth client in Google Cloud (five
/// numbered cards, each with a direct link and what to click), takes the Desktop app client's JSON (dropped or chosen),
/// signs in with Google, then picks the mail labels and calendars and grants Drive and Chat. It opens at the first
/// stage not done (`setup.state.google`); Close keeps where it got. The work is GoogleSetup's.
struct GoogleSetupSheet: View {
    let setup: MacSetup
    let session: SHAFTSession
    @Bindable var steps: SetupSteps
    @Bindable var google: GoogleSetup
    let close: () -> Void
    /// Cards opened by hand (a done one to read again, a later one to read ahead).
    @State private var expanded: Set<GoogleSetupStage> = []
    @State private var dropping = false

    init(setup: MacSetup, session: SHAFTSession, steps: SetupSteps, close: @escaping () -> Void) {
        self.setup = setup
        self.session = session
        self.steps = steps
        self.google = steps.google
        self.close = close
    }

    private var progress: GoogleSetupProgress { setup.state.google }
    private var chosen: Set<AccessRow> { steps.googleChosen }
    /// Chat chosen alone, before any sign-in: said here and on Access, and the sign-in waits for another choice.
    private var problem: String? { GoogleScopes.problem(chosen: chosen, signedIn: progress.isDone(.signIn)) }
    private var name: String { setup.state.createdInstance ?? "your instance" }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: Design.Space.s16) {
                    Text("Set up Google").font(.largeTitle.bold()).foregroundStyle(Color.textPrimary)
                    Text("SHAFT reads Google through an OAuth client of your own, in a Google Cloud project you own: "
                         + "no one else's app ever holds your account. About ten minutes, once. Mark each card done as "
                         + "you finish it; closing keeps your place.")
                        .foregroundStyle(Color.textPrimary)
                        .fixedSize(horizontal: false, vertical: true)
                    if chosen.isEmpty {
                        Text("Tick what \(name) may read under Google on Access first: the cards list the APIs and "
                             + "scopes for those.")
                            .foregroundStyle(Color.warning)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let problem {
                        Text(problem).foregroundStyle(Color.warning).fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(Array(GoogleSetupStage.allCases.enumerated()), id: \.element) { index, stage in
                        card(stage, number: index + 1)
                    }
                }
                .padding(Design.Space.s24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack {
                if progress.resume == nil {
                    Text("Google is set up.").foregroundStyle(Color.textSecondary)
                }
                Spacer()
                Button(progress.resume == nil ? "Done" : "Close", action: close)
                    .keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, Design.Space.s24)
            .padding(.vertical, Design.Space.s16)
            .overlay(alignment: .top) { Rectangle().fill(Color.hairline).frame(height: 1) }
        }
        .frame(minWidth: 640, idealWidth: 700, minHeight: 560, idealHeight: 760)
        .background(Color.canvas)
        .onAppear {
            google.prepare(access: steps.access, setup: setup)
            if progress.isDone(.signIn) { google.loadPickers(chosen: chosen, store: session.store) }
        }
    }

    // MARK: - A card

    private func card(_ stage: GoogleSetupStage, number: Int) -> some View {
        let done = progress.isDone(stage)
        let current = progress.resume == stage
        let open = current || expanded.contains(stage)
        return VStack(alignment: .leading, spacing: Design.Space.s8) {
            Button {
                if expanded.contains(stage) { expanded.remove(stage) } else { expanded.insert(stage) }
            } label: {
                HStack(spacing: Design.Space.s12) {
                    ZStack {
                        Circle().fill(done ? Color.brand : current ? Color.brand.opacity(0.15) : Color.hairline)
                            .frame(width: 26, height: 26)
                        if done {
                            Image(systemName: "checkmark").font(.caption.bold()).foregroundStyle(Color.onAccent)
                        } else {
                            Text("\(number)").font(.callout.bold())
                                .foregroundStyle(current ? Color.brand : Color.textSecondary)
                        }
                    }
                    Text(Self.title(stage))
                        .font(.headline)
                        .foregroundStyle(done || current ? Color.textPrimary : Color.textSecondary)
                    Spacer()
                    if !current {
                        Image(systemName: open ? "chevron.up" : "chevron.down").foregroundStyle(Color.textTertiary)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(current)
            .accessibilityLabel("Step \(number), \(Self.title(stage)), " + (done ? "done" : current ? "current" : "to do"))
            if open {
                content(stage)
                    .padding(.leading, 38)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(Design.Space.s12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .elevatedBlock()
        .overlay(RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous).strokeBorder(current ? Color.brand : .clear, lineWidth: 1))
    }

    static func title(_ stage: GoogleSetupStage) -> String {
        switch stage {
        case .project: "Create a Google Cloud project"
        case .apis: "Turn on the APIs"
        case .audience: "Set the audience, and publish"
        case .scopes: "Add the scopes"
        case .client: "Create a Desktop app client"
        case .json: "Give SHAFT the client's JSON"
        case .signIn: "Sign in with Google"
        case .grants: "Choose what it may read"
        }
    }

    @ViewBuilder
    private func content(_ stage: GoogleSetupStage) -> some View {
        switch stage {
        case .project: projectCard
        case .apis: apisCard
        case .audience: audienceCard
        case .scopes: scopesCard
        case .client: clientCard
        case .json: jsonCard
        case .signIn: signInCard
        case .grants: grantsCard
        }
    }

    private func sentence(_ text: String) -> some View {
        Text(text).foregroundStyle(Color.textPrimary).fixedSize(horizontal: false, vertical: true)
    }

    private func link(_ title: String, _ url: String) -> some View {
        Link(destination: URL(string: url)!) {
            Label(title, systemImage: "arrow.up.right.square")
        }
        .foregroundStyle(Color.brand)
    }

    /// The operator's own "done" (or "not done yet" on a card done before).
    @ViewBuilder
    private func markButton(_ stage: GoogleSetupStage) -> some View {
        if progress.isDone(stage) {
            Button("Not done yet") { google.mark(stage, done: false, setup: setup) }
        } else {
            Button("Done") {
                google.mark(stage, done: true, setup: setup)
                expanded.remove(stage)
            }
            .buttonStyle(ProminentAction())
        }
    }

    // MARK: 1–5: Google Cloud

    private var projectCard: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            sentence("Sign in to Google Cloud with your Google account, create a project (any name, e.g. SHAFT) and "
                     + "make sure it is the project selected at the top of the console for the next cards.")
            link("Create a project", "https://console.cloud.google.com/projectcreate")
            markButton(.project)
        }
    }

    private var apisCard: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            sentence("Open each API below in your project and click Enable.")
            ForEach(GoogleScopes.apis(for: chosen), id: \.id) { api in
                link(api.title, api.library.absoluteString)
            }
            if chosen.contains(.chat) {
                sentence("Google Chat also needs its Chat app described: on the Chat API's Configuration tab, enter an "
                         + "app name, an avatar URL and a description, and save. Chat works only with a Google "
                         + "Workspace account.")
                link("Chat API configuration", "https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat")
            }
            markButton(.apis)
        }
    }

    private var audienceCard: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            sentence("Open Google Auth Platform. If it says it is not configured yet, click Get started: app name "
                     + "SHAFT, your address as support and contact email, and Audience External. Choose Internal "
                     + "instead only if your account belongs to a Google Workspace organisation and only its people "
                     + "will sign in.")
            link("Google Auth Platform", "https://console.cloud.google.com/auth/overview")
            sentence("Then, for External: on Audience, click Publish app so the publishing status reads In "
                     + "production. While it says Testing, Google lets only listed test users sign in, and their "
                     + "sign-in stops working after 7 days, so \(name) would lose Google a week after each sign-in.")
            link("Audience", "https://console.cloud.google.com/auth/audience")
            sentence("Google does not require verification for an app only you use (fewer than 100 users). Until it is "
                     + "verified, Google's sign-in page shows an \"unverified app\" warning: continue past it.")
            Text("Internal apps have no Testing status, so the 7-day limit does not apply to them.")
                .font(.callout).foregroundStyle(Color.textSecondary)
                .fixedSize(horizontal: false, vertical: true)
            link("Google's rule on refresh tokens", "https://developers.google.com/identity/protocols/oauth2#expiration")
            markButton(.audience)
        }
    }

    private var scopesCard: some View {
        let card = GoogleScopes.card(for: chosen)
        let text = card.map(\.scope.url).joined(separator: "\n")
        return VStack(alignment: .leading, spacing: Design.Space.s8) {
            sentence("In Google Auth Platform → Data Access, click Add or remove scopes, paste all of these under "
                     + "Manually add scopes, click Add to table, then Update and Save. SHAFT's sign-in asks for every one "
                     + "of them; those marked are what your choices need now, the others serve later grants.")
            VStack(alignment: .leading, spacing: 2) {
                ForEach(card, id: \.scope.url) { item in
                    HStack(spacing: Design.Space.s8) {
                        Text(item.scope.url)
                            .font(Design.Text.code)
                            .foregroundStyle(Color.textPrimary)
                            .textSelection(.enabled)
                        if item.chosen {
                            Text("needed for your choices").font(.caption).foregroundStyle(Color.brand)
                        }
                    }
                }
            }
            HStack(spacing: Design.Space.s12) {
                Button("Copy all the scopes") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(text, forType: .string)
                }
                link("Data Access", "https://console.cloud.google.com/auth/scopes")
            }
            markButton(.scopes)
        }
    }

    private var clientCard: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            sentence("In Google Auth Platform → Clients, click Create client, choose Application type Desktop app, "
                     + "name it SHAFT and click Create. Then click Download JSON: the next card takes that file.")
            link("Clients", "https://console.cloud.google.com/auth/clients")
            markButton(.client)
        }
    }

    // MARK: 6: the JSON

    private var jsonCard: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            sentence("Drop the file Download JSON saved (client_secret_….json) here, or choose it. SHAFT keeps its "
                     + "client ID and secret in the server's .env on this Mac; the app keeps nothing of it.")
            VStack(spacing: Design.Space.s8) {
                Image(systemName: "doc.badge.arrow.up").font(.title).foregroundStyle(Color.brand)
                Text(dropping ? "Drop it" : "Drop the client's JSON here").foregroundStyle(Color.textSecondary)
                Button("Choose the JSON…", action: chooseJSON)
            }
            .frame(maxWidth: .infinity)
            .padding(Design.Space.s16)
            .background(RoundedRectangle(cornerRadius: Design.Radius.card, style: .continuous)
                .strokeBorder(dropping ? Color.brand : Color.hairline, style: StrokeStyle(lineWidth: 1.5, dash: [6])))
            .onDrop(of: [.fileURL], isTargeted: $dropping) { providers in
                guard let provider = providers.first else { return false }
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    guard let url else { return }
                    Task { @MainActor in save(url) }
                }
                return true
            }
            // Never while a sign-in waits: its consent is for the client a new one would replace (a client sent
            // anyway ends that sign-in first, GoogleSetup.saveClient).
            .disabled(google.savingClient.running || google.signingIn.running)
            if google.signingIn.running {
                Text("A sign-in is waiting: cancel it to give SHAFT another client.")
                    .font(.callout).foregroundStyle(Color.textSecondary)
            }
            if google.savingClient.running {
                HStack(spacing: Design.Space.s8) {
                    ProgressView().controlSize(.small)
                    Text("Saving the client…").foregroundStyle(Color.textSecondary)
                }
            }
            if let failure = google.savingClient.failure {
                SetupFailure(message: failure, retry: chooseJSON)
            }
            if progress.isDone(.json), !google.savingClient.running {
                Label("The client is saved.", systemImage: "checkmark.circle.fill").foregroundStyle(Color.textPrimary)
            }
        }
    }

    private func chooseJSON() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.json]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.prompt = "Use"
        panel.message = "The Desktop app client's JSON from Google Cloud"
        if panel.runModal() == .OK, let url = panel.url { save(url) }
    }

    private func save(_ file: URL) {
        google.saveClient(file: file, installer: setup.installer(for: session), setup: setup)
    }

    // MARK: 7: sign in

    private var address: Binding<String> {
        Binding(get: { setup.state.google.address }, set: { value in setup.edit { $0.google.address = value } })
    }

    @ViewBuilder
    private var signInCard: some View {
        VStack(alignment: .leading, spacing: Design.Space.s8) {
            sentence("Sign in with the Google account \(name) will read. Your browser opens Google's page: tick every "
                     + "box it shows, then come back here.")
            if !progress.isDone(.json) {
                Text("Give SHAFT the client's JSON first (the card above).")
                    .font(.callout).foregroundStyle(Color.warning)
            }
            HStack(spacing: Design.Space.s12) {
                TextField("you@example.com", text: address)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 320)
                    .disabled(google.signingIn.running)
                if google.signingIn.running {
                    Button("Cancel") { google.cancelSignIn() }
                } else {
                    Button(progress.isDone(.signIn) ? "Sign in again" : "Sign in with Google") { signIn() }
                        .buttonStyle(ProminentAction())
                        .disabled(!progress.isDone(.json) || google.savingClient.running || problem != nil)
                }
            }
            if google.signingIn.running {
                HStack(spacing: Design.Space.s8) {
                    ProgressView().controlSize(.small)
                    Text(google.loginURL == nil ? "Starting the sign-in…"
                         : "Waiting for you in the browser. Google's page stays open 5 minutes.")
                        .foregroundStyle(Color.textSecondary)
                    if google.loginURL != nil {
                        Button("Open the page again") { google.openLoginAgain() }
                    }
                }
            }
            if let failure = google.signingIn.failure {
                SetupFailure(message: failure, retry: signIn)
            }
            if progress.isDone(.signIn), !google.signingIn.running {
                Label(google.signedInAs.map { "Signed in as \($0)." } ?? "Signed in.", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(Color.textPrimary)
                let short = google.missingNeeded(for: chosen)
                if !short.isEmpty {
                    Text("Google did not grant \(short.joined(separator: ", ")): unticked on its page, or missing "
                         + "from Data Access (card 4). The grants that need it will be refused; add it, then Sign in "
                         + "again and tick every box.")
                        .foregroundStyle(Color.warning)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func signIn() {
        google.signIn(address: setup.state.google.address, installer: setup.installer(for: session), setup: setup,
                      chosen: chosen, store: session.store)
    }

    // MARK: 8: the grants

    @ViewBuilder
    private var grantsCard: some View {
        VStack(alignment: .leading, spacing: Design.Space.s12) {
            if !progress.isDone(.signIn) {
                Text("Sign in first (the card above).").font(.callout).foregroundStyle(Color.warning)
            } else {
                if chosen.contains(.mail) { labelsPicker }
                if chosen.contains(.calendar) { calendarsPicker }
                if chosen.contains(.drive) { drivePicker }
                if chosen.contains(.chat) {
                    Toggle("Google Chat: every conversation of the account, read-only", isOn: $google.chatOn)
                        .toggleStyle(.checkbox)
                        .disabled(google.applying.running)
                }
                applyRow
            }
        }
        // The card opens once the sign-in is done (it becomes the stage to resume), or the sign-in ends while it is
        // open: either way the lists are read then (each once, until Try again).
        .onAppear {
            if progress.isDone(.signIn) { google.loadPickers(chosen: chosen, store: session.store) }
        }
        .onChange(of: progress.isDone(.signIn)) { _, signedIn in
            if signedIn { google.loadPickers(chosen: chosen, store: session.store) }
        }
    }

    @ViewBuilder
    private var labelsPicker: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text("Mail labels").font(.headline).foregroundStyle(Color.textPrimary)
            Text("\(name) reads the mail under the labels you tick, nothing else, and never sends.")
                .font(.callout).foregroundStyle(Color.textSecondary)
            pickerState(google.loadingLabels)
            if let labels = google.labels {
                if labels.isEmpty {
                    Text("This account has no labels: make one in Gmail, then read again.")
                        .foregroundStyle(Color.textSecondary)
                }
                ForEach(labels, id: \.self) { label in
                    Toggle(label, isOn: member(label, of: \.pickedLabels)).toggleStyle(.checkbox)
                }
            }
        }
        .disabled(google.applying.running)
    }

    @ViewBuilder
    private var calendarsPicker: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text("Calendars").font(.headline).foregroundStyle(Color.textPrimary)
            pickerState(google.loadingCalendars)
            ForEach(google.calendars ?? []) { calendar in
                HStack(spacing: Design.Space.s12) {
                    Toggle(calendar.title + (calendar.primary ? " (primary)" : ""),
                           isOn: member(calendar.id, of: \.pickedCalendars))
                        .toggleStyle(.checkbox)
                    Toggle("can change events", isOn: member(calendar.id, of: \.writableCalendars))
                        .toggleStyle(.checkbox)
                        .disabled(!google.pickedCalendars.contains(calendar.id)
                                  || !["owner", "writer"].contains(calendar.access))
                        .foregroundStyle(Color.textSecondary)
                }
            }
        }
        .disabled(google.applying.running)
    }

    private var drivePicker: some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text("Drive").font(.headline).foregroundStyle(Color.textPrimary)
            Picker("Drive", selection: $google.driveAll) {
                Text("Only its own files").tag(false)
                Text("Also the files shared with you").tag(true)
            }
            .pickerStyle(.radioGroup)
            .labelsHidden()
            Text("Read-only, every file in reach: Drive cannot be limited to one folder.")
                .font(.callout).foregroundStyle(Color.textSecondary)
        }
        .disabled(google.applying.running)
    }

    @ViewBuilder
    private func pickerState(_ work: GoogleSetup.Work) -> some View {
        if work.running {
            ProgressView().controlSize(.small)
        }
        if let failure = work.failure {
            SetupFailure(message: failure) { google.reloadPickers(chosen: chosen, store: session.store) }
        }
    }

    private func member(_ value: String, of keyPath: ReferenceWritableKeyPath<GoogleSetup, Set<String>>) -> Binding<Bool> {
        Binding(get: { google[keyPath: keyPath].contains(value) },
                set: { on in
                    if on { google[keyPath: keyPath].insert(value) } else { google[keyPath: keyPath].remove(value) }
                    if !on, keyPath == \GoogleSetup.pickedCalendars { google.writableCalendars.remove(value) }
                })
    }

    @ViewBuilder
    private var applyRow: some View {
        if progress.isDone(.grants), !google.applying.running {
            Label("Granted. \(name) reads these from its next run.", systemImage: "checkmark.circle.fill")
                .foregroundStyle(Color.textPrimary)
            Button("Change the grants") { google.grantMore(setup: setup) }
        } else {
            HStack(spacing: Design.Space.s12) {
                Button("Apply", action: apply)
                    .buttonStyle(ProminentAction())
                    .disabled(google.applying.running || google.loadingLabels.running || google.loadingCalendars.running)
                if google.applying.running {
                    ProgressView().controlSize(.small)
                    Text(google.sending.map(Self.describe) ?? "Granting…").foregroundStyle(Color.textSecondary)
                }
            }
            if let failure = google.applying.failure {
                if let failed = google.failed {
                    Text("Stopped at \(Self.describe(failed)):").font(.callout).foregroundStyle(Color.textSecondary)
                }
                SetupFailure(message: failure, retry: apply)
            }
        }
    }

    private func apply() {
        google.apply(chosen: chosen, store: session.store, setup: setup, steps: steps)
    }

    static func describe(_ grant: AccessGrant) -> String {
        switch grant.grant {
        case "mail": "the label \(grant.what ?? "")"
        case "calendar": "the calendar \(grant.what ?? "")" + (grant.writable == true ? " (can change events)" : "")
        case "drive": grant.all == true ? "Drive, with files shared with you" : "Drive, its own files"
        case "chat": "Google Chat"
        default: grant.grant
        }
    }
}
