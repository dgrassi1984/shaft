import SHAFTKit
import SwiftUI

enum SidePanelKind: Hashable {
    case documents, artifacts, cycles
}

struct SidePanel: View {
    let store: InstancesStore
    let instance: String
    let cid: String?
    @Environment(ChatScreenModel.self) private var screen

    var body: some View {
        VStack(spacing: 0) {
            Picker("Panel", selection: Bindable(screen).sidePanel) {
                Text("Documents").tag(SidePanelKind.documents)
                Text("Artifacts").tag(SidePanelKind.artifacts)
                Text("Cycles").tag(SidePanelKind.cycles)
            }
            .pickerStyle(.segmented)
            .padding(Design.Space.s12)
            switch screen.sidePanel {
            case .documents:
                DocumentsPanel(store: store, instance: instance, cid: cid)
                    .id("\(instance)-\(cid ?? "")")
            case .artifacts:
                ArtifactsPanel(store: store, instance: instance)
                    .id(instance)
            case .cycles:
                CyclesPanel(store: store, instance: instance)
                    .id(instance)
            }
        }
        .background(Color.elevated)
    }
}
