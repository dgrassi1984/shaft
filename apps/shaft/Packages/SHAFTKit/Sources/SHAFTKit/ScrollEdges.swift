/// Whether a horizontal scroll view has more content beyond either edge.
public struct ScrollEdges: Equatable, Sendable {
    public var leading = false
    public var trailing = false

    public static func more(offset: Double, container: Double, content: Double) -> Self {
        Self(leading: content > container && offset > 1,
             trailing: content > container && offset + container < content - 1)
    }
}
