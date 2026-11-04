import Testing
@testable import SHAFTKit

struct ScrollEdgesTests {
    @Test func fadesOnlyAtEdgesWithMoreContent() {
        let start = ScrollEdges.more(offset: 0, container: 100, content: 300)
        #expect(!start.leading && start.trailing)
        let middle = ScrollEdges.more(offset: 100, container: 100, content: 300)
        #expect(middle.leading && middle.trailing)
        let end = ScrollEdges.more(offset: 200, container: 100, content: 300)
        #expect(end.leading && !end.trailing)
        let narrow = ScrollEdges.more(offset: 0, container: 100, content: 80)
        #expect(!narrow.leading && !narrow.trailing)
    }
}
