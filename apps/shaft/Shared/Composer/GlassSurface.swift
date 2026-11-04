import SwiftUI

extension View {
    /// The system's glass behind a floating control (spec §2.4): solid `elevated` under Reduce Transparency.
    func glassSurface<S: Shape>(in shape: S) -> some View {
        modifier(GlassSurface(shape: shape))
    }
}

private struct GlassSurface<S: Shape>: ViewModifier {
    let shape: S
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        if reduceTransparency {
            // It may sit on `elevated` itself (a block in the feed): the hairline keeps its edge.
            content
                .background(Color.elevated, in: shape)
                .overlay { shape.stroke(Color.hairline, lineWidth: 1) }
        } else {
            content.glassEffect(.regular, in: shape)
        }
    }
}
