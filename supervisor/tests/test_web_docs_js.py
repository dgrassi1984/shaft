"""Drive the shipped delegated controls in Node; no browser or production instance is needed."""
from pathlib import Path
import shutil
import subprocess
import unittest


NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"


@unittest.skipUnless(NODE, "node is not installed")
class DocumentControlsJsTest(unittest.TestCase):
    def test_reply_copy_and_back(self):
        script = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {}, elements = {}, queries = {}, timers = new Map();
let nextTimer = 0, copied = "", asked = "", allow = false, ticks = 0, replaced = "";
const box = { value: "beforeAFTER", selectionStart: 6, selectionEnd: 6,
  focus() { this.focused = true; }, setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
  dispatchEvent(ev) { assert.equal(ev.type, "input"); this.input = true; } };
queries["form.composer textarea"] = box;
const document = { readyState: "loading", addEventListener(k, fn) { (handlers[k] ||= []).push(fn); },
  querySelector: q => queries[q] || null, querySelectorAll: () => [], getElementById: id => elements[id] || null };
const context = { document, navigator: { clipboard: { async writeText(t) { copied = t; } } }, URL, Event,
  location: { href: "https://shaft.test/i/wren/chat?doc=sub%2Fp.md&thread=c1&id=archive" },
  history: { replaceState(a, b, u) { replaced = String(u); } },
  window: { addEventListener() {}, getSelection: () => ({ removeAllRanges() {} }),
    confirm(t) { asked = t; return allow; } },
  setInterval() {}, setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
  clearTimeout(id) { timers.delete(id); } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const click = async (selector, button) => {
  const target = { closest: q => q === selector ? button : null };
  for (const fn of handlers.click) await fn({ target, preventDefault() {} });
};
(async () => {
  const message = { dataset: { raw: "**hello**\nsecond line" } };
  await click("[data-reply-message]", { closest: () => message });
  assert.equal(box.value, "before\n\n> **hello**\n> second line\n\nAFTER");
  assert.equal(box.selectionStart, 35);
  assert.equal(box.selectionEnd, 35);
  assert.ok(box.focused && box.input);
  const copy = { closest: q => q === "pre" ? null : message };
  await click("[data-copy]", copy);
  assert.equal(copied, "**hello**\nsecond line");
  assert.match(copy.innerHTML, /m5 12 4 4L19 6/);
  await click("[data-copy]", copy);
  assert.equal(timers.size, 1);                     // repeated copies must still restore the clipboard
  [...timers.values()][0]();
  assert.match(copy.innerHTML, /<rect/);
  assert.doesNotMatch(copy.innerHTML, /Copied/);
  const edit = { value: "unsaved" }, raw = { value: "saved" }, md = {}, form = {};
  const view = { querySelector: q => ({ "form.docedit textarea": edit, "textarea.raw": raw,
      ".docmd": md, "form.docedit": form })[q], classList: { toggle(k, on) { assert.equal(on, false); } } };
  const panel = { dataset: { poll: "/fragment/docs/wren?doc=sub%2Fp.md&thread=c1&id=archive", etag: "old" },
    querySelector: () => view, _tick() { ticks++; } };
  elements.docs = panel;
  queries["#docs .docview.editing"] = view;
  await click("[data-back-docs]", {});
  assert.equal(asked, "Drop your changes?");
  assert.equal(ticks, 0);
  assert.equal(replaced, "");
  allow = true;
  await click("[data-back-docs]", {});
  assert.equal(ticks, 1);
  assert.equal(panel.dataset.poll, "/fragment/docs/wren?id=archive&list=1");   // not bounced back to it
  assert.equal(replaced, "https://shaft.test/i/wren/chat?id=archive");
  assert.equal(panel.dataset.etag, undefined);
  assert.equal(form.hidden, true);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
