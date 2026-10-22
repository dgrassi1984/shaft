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
    def run_composer(self, checks):
        script = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const storage = new Map(), requests = [], toasts = [];
let fail = false, message = "sent to wren";
function page() {
  const handlers = {}, elements = {};
  let selection = { removeAllRanges() {} };
  function node() {
    return { dataset: {}, style: {}, value: "", checked: true, textContent: "", isConnected: true,
      handlers: {}, addEventListener(k, fn) { this.handlers[k] = fn; },
      setAttribute() {}, focus() {}, remove() { this.isConnected = false; delete elements[this.id]; },
      querySelector(q) { return this.children[q]; },
      set innerHTML(html) {
        this.children = {};
        for (const q of ["textarea", ".comment-quote", ".comment-result", "button[type=submit]",
                         "[data-comment-cancel]", "[data-comment-discard]"]) {
          if (q === "[data-comment-discard]" && !html.includes("data-comment-discard")) continue;
          this.children[q] = node();
        }
        this.children["button[type=submit]"].textContent = html.match(/type="submit">([^<]*)/)[1];
        this.children[".comment-send"] = node();
        this.children[".comment-send"].children = { legend: node() };
        const radios = ["now", "cycle", "keep"].map(v => Object.assign(node(), { value: v, checked: v === "now" }));
        this.children[".comment-send"].radios = radios;
        this.children[".comment-send"].querySelectorAll = () => radios;
      } };
  }
  const panel = { dataset: { poll: "/fragment/docs/wren?doc=plan.md" } };
  const view = { dataset: { doc: "plan.md", sha: "sha" },
    querySelectorAll: () => [{ dataset: { pid: "p-0000000000000009", text: "old note" } }] };
  const document = { readyState: "loading", activeElement: null,
    body: { appendChild(n) { elements[n.id] = n; } }, createElement: node,
    getElementById: id => id === "docs" ? panel : elements[id],
    addEventListener(k, fn) { (handlers[k] ||= []).push(fn); } };
  const context = { document, URL, URLSearchParams, Uint8Array, AbortController, setTimeout, clearTimeout,
    crypto: require("node:crypto").webcrypto, location: { href: "https://shaft.test/i/wren/chat" },
    localStorage: { getItem: k => storage.get(k) || null, setItem: (k,v) => storage.set(k,v),
      removeItem: k => storage.delete(k) },
    MutationObserver: class { observe() {} }, requestAnimationFrame() {},
    window: { addEventListener() {}, getSelection: () => selection,
      shaftWeb: { csrf: () => "token", textOf: s => s.replace(/<[^>]*>/g, ""),
        toast: s => toasts.push(s), refreshDocs() {}, pairAgain: () => false } },
    async fetch(url, options) {
      requests.push({ url, body: options.body });
      if (fail) throw Error("lost response");
      return { ok: true, status: 200, headers: { get: () => null }, text: async () => `<p>${message}</p>` };
    } };
  vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
  return { view, panel,
    select(start) {
      const leaf = { nodeType: 3, data: "quote and quote", length: 15 };
      const md = { nodeType: 1, tagName: "DIV", childNodes: [leaf], contains: () => true,
        closest: () => view };
      leaf.parentNode = md;
      leaf.parentElement = { closest: () => md };
      selection = { isCollapsed: false, rangeCount: 1, removeAllRanges() {},
        getRangeAt: () => ({ startContainer: leaf, endContainer: leaf, startOffset: start, endOffset: start + 5 }) };
      elements["comment-sel"].handlers.click();
      return elements["comment-box"];
    },
    open(kind = "doc", id = "c1") {
      const selector = kind === "reply" ? "[data-reply-thread]" : kind === "edit" ? "[data-edit-post]" : "[data-comment-doc]";
      const target = { dataset: { replyThread: id, editPost: "p-0000000000000009" },
        matches: q => q === selector, closest: q => q === ".docview" ? view : target };
      for (const fn of handlers.click) fn({ target, preventDefault() {} });
      return elements["comment-box"];
    }, escape() {
      for (const fn of handlers.keydown) fn({ key: "Escape", preventDefault() {}, stopImmediatePropagation() {} });
    }, box: () => elements["comment-box"] };
}
const text = f => f.querySelector("textarea"), radios = f => f.querySelector(".comment-send").radios;
const when = f => radios(f).find(r => r.checked).value;
const choose = (f, v) => { radios(f).forEach(r => { r.checked = r.value === v; }); radios(f).find(r => r.value === v).handlers.change?.(); };
const label = f => f.querySelector("button[type=submit]").textContent;
const type = (f, value) => { text(f).value = value; text(f).handlers.input?.({ target: text(f) }); };
const toggle = (f, checked) => choose(f, checked ? "now" : "keep");
const click = (f, action) => f.querySelector(`[data-comment-${action}]`).handlers.click();
const submit = f => f.handlers.submit({ preventDefault() {} });
(async () => {
""" + checks + r"""
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([NODE, "-e", script, str(COMMENTS_JS)], text=True,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_15_archive_selection_cannot_comment(self):
        """Reverting #15 lets selecting archive document text open a live comment composer."""
        self.run_composer(r"""
const p = page();
p.view.dataset.readOnly = "1";
assert.equal(p.select(0), undefined);
delete p.view.dataset.readOnly;
assert.ok(p.select(0));
""")

    def test_issue_7_action_labels_and_feedback(self):
        """Reverting #7 hides the send/private outcome behind the same Save label."""
        self.run_composer(r"""
const p = page();
for (const kind of ["doc", "reply"]) {
  let f = p.open(kind);
  assert.equal(label(f), "Send comment");
  toggle(f, false); assert.equal(label(f), "Save without sending");   // not private: its instance reads it
  toggle(f, true); assert.equal(label(f), "Send comment");
  type(f, "Question"); await submit(f);
  assert.equal(toasts.at(-1), "sent to wren");
  assert.equal(requests.at(-1).body.get("send"), "1");
  f = p.open(kind); toggle(f, false); type(f, "Private note");
  message = "saved, not sent: wren sees it in notes/comments/"; await submit(f);
  assert.equal(requests.at(-1).body.has("send"), false);
  assert.equal(toasts.at(-1), message); message = "sent to wren";
}
const f = p.open("edit"); assert.equal(label(f), "Save note");
type(f, "edited"); await submit(f); assert.equal(requests.at(-1).body.has("send"), false);
message = "saved, but not sent: busy";
const failed = p.open(); type(failed, "Question"); await submit(failed);
assert.equal(toasts.at(-1), message); assert.equal(p.box(), undefined);
""")

    def test_issue_13_drafts_survive_dismissal_reload_and_retry(self):
        """Reverting #13 loses dismissed drafts, their send choice, and retry post identity."""
        self.run_composer(r"""
let p = page(), f = p.open();
type(f, "whole draft"); toggle(f, false); p.escape();
f = p.open(); assert.equal(text(f).value, "whole draft"); assert.equal(when(f), "keep");
click(f, "cancel"); f = p.open("reply", "c1"); type(f, "reply one"); toggle(f, false);
f = p.open("reply", "c2"); assert.equal(text(f).value, ""); type(f, "reply two");
p = page(); f = p.open("reply", "c1"); assert.equal(text(f).value, "reply one"); assert.equal(when(f), "keep");
f = p.open(); assert.equal(text(f).value, "whole draft"); assert.equal(when(f), "keep");
p.view.dataset.doc = "other.md"; f = p.open(); assert.equal(text(f).value, "");
p.view.dataset.doc = "plan.md"; p.panel.dataset.poll = "/fragment/docs/robin";
f = p.open(); assert.equal(text(f).value, "");
p.panel.dataset.poll = "/fragment/docs/wren";
f = p.open(); click(f, "discard"); f = p.open(); assert.equal(text(f).value, "");
assert.equal(requests.length, 0);
type(f, "retry me"); fail = true; await submit(f);
const pid = requests.at(-1).body.get("pid");
p = page(); f = p.open(); assert.equal(text(f).value, "retry me");
fail = false; message = "saved, but not sent: busy"; await submit(f);
assert.equal(requests.at(-1).body.get("pid"), pid);
f = p.open(); assert.equal(text(f).value, "");
f = p.open("edit"); type(f, "edited draft"); p.escape();
f = p.open("edit"); assert.equal(text(f).value, "edited draft");
await submit(f); f = p.open("edit"); assert.equal(text(f).value, "old note");
f = p.select(0); type(f, "first quote"); toggle(f, false);
f = p.select(10); assert.equal(text(f).value, ""); type(f, "second quote");
p = page(); f = p.select(0); assert.equal(text(f).value, "first quote");
assert.equal(when(f), "keep");
f = p.select(10); assert.equal(text(f).value, "second quote");
""")

    def test_next_cycle_choice_sends_cycle_and_survives_reload(self):
        """PLAN-background-messages: "in its next cycle" posts send=cycle (not 1), says so on its button, and a draft
        keeps the choice; a draft kept before the choice existed (send: false) still opens as kept."""
        self.run_composer(r"""
let p = page(), f = p.open();
assert.equal(f.querySelector(".comment-send").querySelector("legend").textContent, "Send to wren");
choose(f, "cycle"); assert.equal(label(f), "Send for its next cycle");
type(f, "look at this later"); p.escape();
p = page(); f = p.open(); assert.equal(when(f), "cycle"); assert.equal(text(f).value, "look at this later");
message = "for wren's next cycle (≈14:20)"; await submit(f);
assert.equal(requests.at(-1).body.get("send"), "cycle");
assert.equal(toasts.at(-1), message);
f = p.open("reply", "c3"); choose(f, "now"); type(f, "now please"); await submit(f);
assert.equal(requests.at(-1).body.get("send"), "1");
storage.set(`shaft.draft.comment.${JSON.stringify(["wren", "plan.md", "reply", "c4"])}`,
            JSON.stringify({ text: "old draft", send: false, pid: "p-00000000000000aa" }));
f = p.open("reply", "c4"); assert.equal(text(f).value, "old draft"); assert.equal(when(f), "keep");
""")

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
        """The copy button's SVG is not document text, even if an accessible title is added later."""
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
const pre = el("PRE", el("SPAN", text("x = 1")), el("BUTTON", el("SVG", el("TITLE", text("Copy")))));
const root = el("DIV", pre, el("P", text("after")));
process.stdout.write(JSON.stringify(context.window.shaftComments.walk(root).text));
"""
        result = subprocess.run([NODE, "-e", script, str(COMMENTS_JS)], text=True, capture_output=True, timeout=10,
                                check=True)
        self.assertEqual(json.loads(result.stdout), "\nx = 1\nafter")


if __name__ == "__main__":
    unittest.main()
