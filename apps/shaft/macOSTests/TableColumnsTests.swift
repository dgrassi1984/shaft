import Testing

struct TableColumnsTests {
    @Test func aBareNumericColumnAlignsRight() {
        let source = "| Name | Total |\n| --- | --- |\n| One | 12 |\n| Two | 3.5 |"
        #expect(TableColumns.aligned(source) == "| Name | Total |\n| --- | ---: |\n| One | 12 |\n| Two | 3.5 |")
    }

    @Test func statedLeftAlignmentIsKept() {
        let source = "| Total |\n| :--- |\n| 12 |"
        #expect(TableColumns.aligned(source) == source)
    }

    @Test func aTableInsideACodeFenceIsUnchanged() {
        let source = "```markdown\n| Total |\n| --- |\n| 12 |\n```"
        #expect(TableColumns.aligned(source) == source)
    }
}
