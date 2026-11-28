import SHAFTKit
import SwiftUI
import WebKit

struct ArtifactsPanel: View {
    @Environment(ChatScreenModel.self) private var screen
    @State private var artifacts: ArtifactsStore
    @State private var path: [String] = []

    init(store: InstancesStore, instance: String) {
        _artifacts = State(initialValue: ArtifactsStore(client: store.client, instance: instance))
    }

    var body: some View {
        NavigationStack(path: $path) {
            List(artifacts.list?.artifacts ?? []) { entry in
                NavigationLink(entry.title, value: entry.slug)
                    .listRowBackground(Color.clear)
                    .listRowSeparatorTint(Color.hairline)
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(Color.elevated)
            .overlay {
                if let error = artifacts.error, path.isEmpty, artifacts.list == nil {
                    Text(error.message).foregroundStyle(Color.danger).padding()
                } else if artifacts.list?.artifacts.isEmpty == true {
                    ContentUnavailableView {
                        Label("No artifacts yet.", systemImage: "chart.bar.xaxis")
                    } description: {
                        Text("An instance makes one by writing notes/artifacts/<name>/index.html.")
                    }
                } else if artifacts.list == nil {
                    ProgressView()
                }
            }
            .refreshable { await artifacts.refresh() }
            .navigationTitle("Artifacts")
            .navigationDestination(for: String.self) { slug in
                ArtifactDetail(slug: slug, artifacts: artifacts)
            }
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                #if os(macOS)
                InspectorHeader(title: "Artifacts", symbol: "xmark", label: "Close the artifacts",
                                action: { screen.showDocuments = false }) { EmptyView() }
                #else
                ToolbarItem(placement: .cancellationAction) {
                    Button { screen.showDocuments = false } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Close the artifacts")
                }
                #endif
            }
            #if os(macOS)
            .inspectorPage()
            #endif
        }
        .onChange(of: path) {
            if path.isEmpty { artifacts.close() }
        }
        .task {
            await artifacts.refresh()
            while !Task.isCancelled {
                do {
                    try await Task.sleep(for: .seconds(3))
                } catch {
                    break
                }
                await artifacts.follow()
            }
        }
    }
}

private struct ArtifactDetail: View {
    let slug: String
    let artifacts: ArtifactsStore
    @Environment(\.dismiss) private var dismiss
    @State private var rules: WKContentRuleList?
    @State private var rulesFailed = false
    @State private var link: URL?
    @Environment(\.openURL) private var openURL

    var body: some View {
        let page = artifacts.open.flatMap { $0.slug == slug ? $0 : nil }
        let title = page?.title ?? artifacts.list?.artifacts.first { $0.slug == slug }?.title ?? slug
        VStack(spacing: 0) {
            if let error = artifacts.error {
                Text(error.message).foregroundStyle(Color.danger).padding()
            }
            if let stale = artifacts.stale {
                Label(stale, systemImage: "exclamationmark.triangle")
                    .font(.footnote)
                    .foregroundStyle(Color.warning)
                    .padding(Design.Space.s12)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let page, !page.problems.isEmpty {
                VStack(alignment: .leading, spacing: Design.Space.s4) {
                    ForEach(Array(page.problems.enumerated()), id: \.offset) { _, problem in
                        Label(problem, systemImage: "exclamationmark.triangle")
                    }
                }
                .font(.footnote)
                .foregroundStyle(Color.warning)
                .padding(Design.Space.s12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.warning.opacity(0.12))
            }
            if rulesFailed {
                Text("The artifact could not be opened because its content filter failed.")
                    .foregroundStyle(Color.danger)
                    .padding()
            } else if let page, let rules {
                ArtifactWebView(page: page, rules: rules, openLink: { link = $0 })
            } else if artifacts.error == nil {
                ProgressView()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.elevated)
        .navigationTitle(title)
        #if os(macOS)
        .toolbar {
            InspectorHeader(title: title, symbol: "chevron.left", label: "Back",
                            action: { dismiss() }) { EmptyView() }
        }
        .inspectorPage()
        #else
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .confirmationDialog("Open this link in the browser?", isPresented: Binding(
            get: { link != nil }, set: { if !$0 { link = nil } }), titleVisibility: .visible, presenting: link) { url in
            Button("Open \(url.host() ?? "link")") { openURL(url) }
            Button("Cancel", role: .cancel) {}
        } message: { url in
            Text(url.absoluteString)
        }
        .task { await artifacts.load(slug) }
        .task {
            rules = try? await ArtifactWebView.compileRules()
            rulesFailed = rules == nil
        }
    }
}
