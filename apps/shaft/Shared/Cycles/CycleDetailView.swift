import SHAFTKit
import SwiftUI

struct CycleDetailView: View {
    let id: String
    @Environment(CyclesStore.self) private var cycles

    var body: some View {
        List {
            if let detail = cycles.open, detail.id == id {
                if let at = detail.at { Text(Stamp.full(at)).font(Design.Text.metadata) }
                if detail.kind == "cycle" {
                    planned(detail.planned)
                    did(detail.did)
                    result(detail.result)
                    left(detail.left)
                } else {
                    Section(detail.kind.capitalized) {
                        if let content = detail.content { MarkdownText(source: content) }
                        else { Text("nothing recorded") }
                    }
                    did(detail.did)
                }
            } else if let error = cycles.error {
                Text(error.message).foregroundStyle(Color.danger)
                Button("Retry") { Task { await cycles.load(id) } }
            } else { ProgressView() }
        }
        .listStyle(.plain)
        .scrollContentBackground(.hidden)
        .background(Color.elevated)
        .navigationTitle("Cycle")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .task(id: id) { await cycles.load(id) }
    }

    @ViewBuilder
    private func planned(_ planned: CycleDetail.Planned?) -> some View {
        Section("Planned") {
            if let planned {
                CycleLine(label: "Goal", value: planned.goal)
                CycleLine(label: "Success criterion", value: planned.successCriterion)
                CycleLine(label: "Origin", value: planned.origin)
                CycleLine(label: "Origin reference", value: planned.originRef)
                CycleLine(label: "Confidence", value: planned.confidence.map { String($0) })
                DisclosureGroup("Predictions") { CycleLines(values: planned.predictions) }
                if !planned.attention.isEmpty {
                    DisclosureGroup("Attention") { CycleLines(values: planned.attention) }
                }
            } else { Text("nothing recorded") }
        }
    }

    @ViewBuilder
    private func did(_ did: CycleDetail.Did) -> some View {
        Section("Did") {
            CycleLine(label: "What happened", value: did.whatHappened)
            CycleLine(label: "Duration", value: did.durationS.map { "\($0) s" })
            CycleLine(label: "Cost", value: did.usd.map { String(format: "$%.4f", $0) })
            Text("Tool calls: \(did.toolCalls)")
            CycleLine(label: "Ended by", value: did.endedBy)
            CycleLine(label: "Wrapped up", value: did.wrappedUp)
            CycleLine(label: "Delivered via", value: did.deliveredVia)
        }
    }

    @ViewBuilder
    private func result(_ result: CycleDetail.Result?) -> some View {
        Section("Result") {
            Text("claimed \(result?.claim ?? "unknown")")
            if let result {
                if !result.judgeError.isEmpty {
                    Text("verdicts could not be read: \(result.judgeError)").foregroundStyle(Color.danger)
                }
                if let judge = result.judge { judgment(judge, label: "Judge") }
                else if result.judgeError.isEmpty { Text("Not judged") }
                if let second = result.second { judgment(second, label: "Second judge") }
                DisclosureGroup("Surprises") { CycleLines(values: result.surprises) }
                DisclosureGroup("Doubts") { CycleLines(values: result.doubts) }
            } else { Text("Not judged") }
        }
    }

    @ViewBuilder
    private func judgment(_ judge: CycleDetail.Judgment, label: String) -> some View {
        Text(label).font(.headline)
        if !judge.error.isEmpty { Text(judge.error).foregroundStyle(Color.danger) }
        if let verdict = judge.verdict { CycleVerdict(verdict: verdict) }
        else if judge.error.isEmpty { Text("Not judged") }
        CycleLine(label: "Claim supported", value: judge.claimSupported.map { $0 ? "yes" : "no" })
        CycleLine(label: "First error", value: judge.firstError.map { String($0) })
        CycleLine(label: "Reasoning", value: judge.reasoning)
        CycleLine(label: "Judge model", value: judge.model)
    }

    @ViewBuilder
    private func left(_ left: CycleDetail.Left?) -> some View {
        Section("Left") {
            DisclosureGroup("Unfinished") { CycleLines(values: left?.unfinished ?? []) }
            Text("Said it would do next").font(.headline)
            if let action = left?.nextAction?.action, !action.isEmpty { Text(action) }
            else { Text("no next action recorded") }
            if let revise = left?.nextAction?.reviseIf { CycleLine(label: "Revise if", value: revise) }
            CycleLine(label: "The next cycle's goal", value: left?.nextGoal)
        }
    }
}
