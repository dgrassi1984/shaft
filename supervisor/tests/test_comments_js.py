"""Exercise the shipped comment matcher in Node, without a browser DOM."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest


COMMENTS_JS = Path(__file__).resolve().parents[1] / "web_static" / "comments.js"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class CommentsJsTest(unittest.TestCase):
    def test_locate(self):
        cases = [
            ("single hit with changed neighbours", "new quote after",
             {"exact": "quote", "prefix": "old ", "suffix": " before", "start": 100},
             {"start": 4, "end": 9}),
            ("zero hits", "new text", {"exact": "gone", "start": 0},
             {"detached": True}),
            ("suffix beats proximity", "quote a quote b",
             {"exact": "quote", "prefix": "", "suffix": " b", "start": 0},
             {"start": 8, "end": 13}),
            ("nearest start", "quote a quote b",
             {"exact": "quote", "prefix": "changed", "suffix": "changed", "start": 7},
             {"start": 8, "end": 13}),
            ("equal distance", "quote a quote b",
             {"exact": "quote", "start": 4}, {"ambiguous": True}),
            ("cross block newline", "\nfirst\nsecond",
             {"exact": "first\nsecond", "start": 1}, {"start": 1, "end": 13}),
            ("both neighbours", "a quote b a quote c",
             {"exact": "quote", "prefix": "a ", "suffix": " c", "start": 2},
             {"start": 12, "end": 17}),
            ("overlapping occurrences", "banana",
             {"exact": "ana", "start": 3}, {"start": 3, "end": 6}),
            ("UTF-16 offsets", "😀 quote",
             {"exact": "quote", "start": 0}, {"start": 3, "end": 8}),
            ("empty quote", "text", {"exact": "", "start": 0}, {"detached": True}),
            ("duplicates without a start", "x x", {"exact": "x"}, {"ambiguous": True}),
        ]
        script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const context = { window: {} };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const cases = JSON.parse(fs.readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(cases.map(([text, anchor]) =>
  context.window.shaftComments.locate(text, anchor))));
"""
        result = subprocess.run(
            [NODE, "-e", script, str(COMMENTS_JS)],
            input=json.dumps([[text, anchor] for _, text, anchor, _ in cases]),
            text=True, capture_output=True, timeout=10, check=True,
        )
        actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(cases))
        for (name, _, _, expected), match in zip(cases, actual):
            with self.subTest(name=name):
                self.assertEqual(match, expected)

    def test_walk_leaves_out_the_pages_buttons(self):
        """Grok review 2026-10-20: web.js puts a "Copy" button in each code block; it is not document text."""
        script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const context = { window: {} };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const text = (data) => ({ nodeType: 3, data, length: data.length });
const el = (tagName, ...childNodes) => {
  const node = { nodeType: 1, tagName, childNodes };
  childNodes.forEach(c => { c.parentNode = node; });
  return node;
};
const pre = el("PRE", el("SPAN", text("x = 1")), el("BUTTON", text("Copy")));
const root = el("DIV", pre, el("P", text("after")));
process.stdout.write(JSON.stringify(context.window.shaftComments.walk(root).text));
"""
        result = subprocess.run([NODE, "-e", script, str(COMMENTS_JS)], text=True, capture_output=True, timeout=10,
                                check=True)
        self.assertEqual(json.loads(result.stdout), "\nx = 1\nafter")


if __name__ == "__main__":
    unittest.main()
