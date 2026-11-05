import SHAFTKit
import SwiftUI

struct CyclesPanel: View {
    @Environment(ChatScreenModel.self) private var screen
    @Environment(\.scenePhase) private var scenePhase
    @State private var cycles: CyclesStore

    init(store: InstancesStore, instance: String) {
        _cycles = State(initialValue: CyclesStore(client: store.client, instance: instance))
    }

    var body: some View {
        NavigationStack {
            List {
                if let error = cycles.error { Text(error.message).foregroundStyle(Color.danger) }
                if let plan = cycles.plan { next(plan) }
                Section("Done") {
                    if !cycles.judgeError.isEmpty {
                        Text("verdicts could not be read: \(cycles.judgeError)")
                            .foregroundStyle(Color.danger)
                    }
                    if cycles.days.isEmpty {
                        if cycles.loading || (cycles.plan == nil && cycles.error == nil) {
                            ProgressView()
                        } else if cycles.error == nil {
                            Text(cycles.filter.isEmpty ? "no cycles yet" : "no cycles match this filter")
                        }
                    }
                }
                ForEach(cycles.days, id: \.date) { day in
                    Section {
                        if day.skippedLines > 0 { Text("\(day.skippedLines) skipped lines") }
                        ForEach(day.rows) { row in
                            NavigationLink(value: row.id) { cycleRow(row) }
                        }
                    } header: {
                        VStack(alignment: .leading, spacing: Design.Space.s4) {
                            Text(day.date)
                            Text(counts(day.counts))
                        }
                    }
                }
                if cycles.next != nil {
                    Button("Earlier days") { Task { await cycles.loadEarlier() } }
                        .disabled(cycles.loading)
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(Color.elevated)
            .navigationTitle("Cycles")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .navigationDestination(for: String.self) { id in
                CycleDetailView(id: id)
                    .environment(cycles)
                    .environment(screen)
            }
            .toolbar {
                #if os(macOS)
                InspectorHeader(title: "Cycles", symbol: "xmark", label: "Close the cycles",
                                action: { screen.showDocuments = false }) { filterMenu }
                #else
                ToolbarItem(placement: .cancellationAction) {
                    Button { screen.showDocuments = false } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Close the cycles")
                }
                ToolbarItem(placement: .primaryAction) { filterMenu }
                #endif
            }
            .refreshable { await cycles.refresh() }
            #if os(macOS)
            .inspectorPage()
            #endif
        }
        .environment(cycles)
        .task { await cycles.refresh() }
        .onChange(of: scenePhase) { if scenePhase == .active { Task { await cycles.refresh() } } }
    }

    private var filterMenu: some View {
        Menu {
            Picker("Filter", selection: Binding(get: { cycles.filter }, set: { value in
                Task { await cycles.setFilter(value) }
            })) {
                Text("All").tag("")
                Text("For you").tag("yours")
                Text("Fell short").tag("short")
                Text("Delivered").tag("delivered")
            }
        } label: { Label("Filter", systemImage: "line.3.horizontal.decrease") }
    }

    @ViewBuilder
    private func next(_ plan: CyclePlan) -> some View {
        Section("Next") {
            CycleLine(label: "Now", value: plan.now.state)
            if let since = plan.now.since { CycleLine(label: "Since", value: since) }
            if let goal = plan.now.goal { Text(goal) }
            CycleLine(label: "Next check", value: plan.check.next)
            if plan.check.paused && plan.check.next != "paused" { Text("paused") }
            if !plan.check.skipped.isEmpty { CycleLine(label: "Last skipped check", value: plan.check.skipped) }
        }
        Section("Said it would do next") {
            if let step = plan.nextStep {
                Text(step.action)
                CycleLine(label: "From cycle", value: step.at.map { Stamp.short($0) })
                if let revise = step.reviseIf { CycleLine(label: "Revise if", value: revise) }
            } else { Text("no next action recorded") }
        }
        Section("Unfinished") { CycleLines(values: plan.unfinished, empty: "nothing recorded") }
        Section {
            if !plan.commitmentsError.isEmpty {
                Text("could not read commitments: \(plan.commitmentsError)").foregroundStyle(Color.danger)
            } else if plan.commitments.isEmpty {
                Text("no open commitments")
            } else {
                DisclosureGroup("Commitments · \(plan.commitments.count)"
                                + (plan.commitments.contains { $0.status == "overdue" } ? " · overdue" : "")) {
                    ForEach(plan.commitments, id: \.id) { item in
                        VStack(alignment: .leading, spacing: Design.Space.s4) {
                            Text(item.promise)
                            CycleLine(label: "Due", value: item.due.map { due in
                                SHAFTJSON.parseStamp(due).map { Stamp.short($0) } ?? due
                            })
                            if item.status == "overdue" { Text("overdue").foregroundStyle(Color.danger) }
                        }
                    }
                }
            }
        }
        Section {
            if plan.queue.state == "none" { Text("no task queue") }
            else if plan.queue.state == "unreadable" {
                Text(plan.queue.error).foregroundStyle(Color.danger)
            } else if plan.queue.rows.isEmpty {
                Text("no open tasks")
            } else {
                DisclosureGroup("Task queue · \(plan.queue.open) open · \(plan.queue.overdue) overdue") {
                    ForEach(plan.queue.rows, id: \.id) { row in
                        VStack(alignment: .leading, spacing: Design.Space.s4) {
                            Text(row.task)
                            Text(queueDue(row))
                            if row.overdue { Text("overdue").foregroundStyle(Color.danger) }
                        }
                    }
                    if plan.queue.open > plan.queue.rows.count {
                        Text("\(plan.queue.open - plan.queue.rows.count) more")
                    }
                }
            }
        }
        Section {
            DisclosureGroup("Goals") {
                if !plan.goalsError.isEmpty { Text(plan.goalsError) }
                else if plan.goals.isEmpty { Text("nothing recorded") }
                else { MarkdownText(source: plan.goals) }
            }
            DisclosureGroup("Improvement plan") {
                if !plan.improvementError.isEmpty { Text(plan.improvementError) }
                else if let improvement = plan.improvement {
                    CycleLine(label: "From", value: improvement.at.map { Stamp.full($0) })
                    MarkdownText(source: improvement.text)
                } else { Text("no improvement plan recorded") }
            }
        }
    }

    private func queueDue(_ row: CyclePlan.Queue.Row) -> String {
        if let text = row.dueAtText { return text }
        guard let date = row.dueAt else { return "due now" }
        if row.dueAtIsDate {
            let formatter = DateFormatter()
            formatter.timeZone = TimeZone(secondsFromGMT: 0)
            formatter.dateStyle = .medium
            formatter.timeStyle = .none
            return formatter.string(from: date)
        }
        return Stamp.short(date)
    }

    private func counts(_ c: CycleDay.Counts) -> String {
        "\(c.cycles) \(c.cycles == 1 ? "cycle" : "cycles") · \(c.yes) yes · \(c.partial) partial · "
            + "\(c.no) no (claimed) · "
            + "\(c.judged) judged · \(c.forYou) for you"
    }

    private func cycleRow(_ row: CycleRow) -> some View {
        VStack(alignment: .leading, spacing: Design.Space.s4) {
            Text(row.at.map { Stamp.time($0) } ?? "unknown").font(Design.Text.metadata)
            Text(row.goal ?? row.kind.capitalized).lineLimit(2)
            if row.kind == "cycle" { Text(row.claim.map { "claimed \($0)" } ?? "no finish") }
            if cycles.judgeError.isEmpty {
                if let verdict = row.verdict { CycleVerdict(verdict: verdict) }
                else if row.kind == "cycle" { Text("Not judged").font(Design.Text.metadata) }
            }
            if row.delivered { Label("Delivered", systemImage: "paperplane") }
        }
    }
}

struct CycleVerdict: View {
    let verdict: String
    var body: some View {
        Text(verdict)
            .font(Design.Text.pill)
            .foregroundStyle(Color.brand)
            .padding(Design.Space.s4)
            .pill()
    }
}

struct CycleLine: View {
    let label: String
    let value: String?
    var body: some View { Text("\(label): \(value.flatMap { $0.isEmpty ? nil : $0 } ?? "unknown")") }
}

struct CycleLines: View {
    let values: [String]
    var empty = "nothing recorded"
    var body: some View {
        if values.isEmpty { Text(empty) }
        else { ForEach(Array(values.enumerated()), id: \.offset) { _, text in Text(text) } }
    }
}
