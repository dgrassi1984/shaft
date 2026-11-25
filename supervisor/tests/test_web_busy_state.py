"""Issue #60: chat header state icon with tooltip replacing the standing amber p.warn busy paragraph."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from supervisor import web_views
from supervisor.tests.test_web_a11y import declarations
from supervisor.tests.test_web_touch import CSS, block

from supervisor.tests.node_helpers import NODE_TIMEOUT_S

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"
PHONE = "(max-width: 720px), (pointer: coarse) and (max-height: 520px)"


class BusyStateHeaderTest(unittest.TestCase):
    def test_issue_60_busy_header_renders_state_icon_accessible_name_and_sentence_without_warn(self):
        """Reverting #60 brings back the standing p.warn busy paragraph and removes the state icon from .chathead."""
        from supervisor.web_pages import icon as svg
        # An SVG icon (the emoji read as disabled and were blobs in dark mode), the state in words beside
        # it, under the name; its notice says in plain words what a message does now (api.py work()).
        expected = [
            ("in a cycle", "busy", "Your message pauses its cycle; the cycle resumes after the reply."),
            ("working", "busy", None),
            ("sleeping", "sleep", "Your message pauses its sleep; the sleep resumes after the reply."),
            ("paused by you", "pause", "No cycles run until you resume example. It still answers your messages."),
            ("in a conversation (waiting for you)", "clock", "A conversation with you is open, waiting for your "
                                                             "next message. A message is answered now."),
            ("reflecting", "busy", None),
            ("talking", "busy", None),
            ("answering (API)", "busy", None),
            ("writing up a conversation", "busy", None),
            ("being updated", "busy", None),
        ]
        fallback = "Your message is answered once it pauses or finishes what it is doing; the reply&#x27;s activity says which."
        for state, name, words in expected:
            with self.subTest(state=state):
                body = str(web_views.chat_header("example", {"name": "example", "model": "m", "state": state},
                                                 {"id": "c1", "state": "idle", "title": "Topic", "created": "2026-10-27T10:00:00"}, []))
                self.assertNotIn('<p class="warn">', body)
                # M2b puts the conversation title on line one and links Main on line two.
                self.assertIn('title="Switch conversation"><span>Topic</span>' + str(svg("back", 12)) + '</button></h1>', body)
                self.assertIn('<a class="instance-main" href="/i/example/chat">example</a>', body)
                mark = '<span class="statepulse" aria-hidden="true"></span>' if state in ("in a cycle", "working") else svg(name, 14)
                self.assertIn(
                    f'<details class="menu busystate" data-key="busy-state">'
                    f'<summary data-focus="busy" aria-label="example is {state}">{mark}<span>{state}</span>'
                    f'</summary><div class="menubody">', body)
                self.assertIn(words or fallback, body)
                self.assertNotRegex(body, "[⏳💤⏸💬🕘📄☰]")
                facts_start = body.index('<span class="dim facts">')
                self.assertLess(body.index('data-key="busy-state"'), facts_start)
                self.assertLess(body.index('data-key="busy-state"'), body.index("</header>"))

        for state, chat in (("idle", None), ("?", None), ("in a cycle", {"id": "c1", "state": "replying", "created": "2026-10-27T10:00:00"})):
            with self.subTest(state=state, chat=chat):
                body = str(web_views.chat_header("example", {"name": "example", "model": "m", "state": state}, chat, []))
                self.assertNotIn('data-key="busy-state"', body)
                self.assertNotIn("busystate", body)
                self.assertNotIn('<p class="warn">', body)

        self.assertTrue(declarations("details.menu.busystate > summary"))
        phone = block(PHONE)
        self.assertIn("flex: 0 1 auto;", "\n".join(declarations(".chathead h1", phone)))
        self.assertIn("margin-left: auto;", "\n".join(declarations(".chathead .headtools")))

    @unittest.skipUnless(NODE, "node is not installed")
    def test_issue_60_busy_state_opens_on_hover_tap_keyboard_closes_on_escape_outside_and_survives_poll(self):
        """Reverting #60's web.js handlers breaks desktop hover, outside-tap dismissal, keyboard toggle, or open-state preservation across header polls."""
        script = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {};
let touch = false, html = "v1", busy, summary, outside;
function makeDetails() {
  summary = { dataset: { focus: "busy" },
    matches: q => q === 'button, a[href], summary' || q === 'details[data-key="busy-state"] > summary',
    getAttribute: () => null, focus() { document.activeElement = summary; },
    closest: q => q === 'details[data-key="busy-state"] > summary' ? summary
      : q === 'details[data-key="busy-state"]' ? busy : null };
  busy = { dataset: { key: "busy-state" }, open: false,
    contains: n => n === busy || n === summary,
    closest: q => q === 'details[data-key="busy-state"]' ? busy : null };
  outside = { closest: () => null };
}
makeDetails();
const header = {
  dataset: { poll: "/fragment/chat/example/header" }, isConnected: true,
  classList: { add() {}, remove() {}, contains: () => false },
  get innerHTML() { return html; },
  set innerHTML(v) {
    if (document.activeElement === summary) document.activeElement = document.body;
    html = v; makeDetails();
  },
  contains: n => n === busy || n === summary,
  querySelectorAll: q => {
    if (q === "button, a[href], summary") return [summary];
    if (q === "details[data-key][open]") return busy.open ? [busy] : [];
    if (q === "details[data-key]:not([open])") return busy.open ? [] : [busy];
    if (q === "details[data-key]") return [busy];
    return [];
  },
  querySelector: q => q === 'details[data-key] summary' ? summary : null,
  closest: () => null, dispatchEvent() {}, getClientRects: () => [1],
};
const document = { readyState: "loading", visibilityState: "visible", body: { scrollHeight: 0, classList: { add() {}, toggle() {} }, appendChild() {} },
  activeElement: outside,
  addEventListener(k, fn) { (handlers[k] ||= []).push(fn); },
  createElement: () => ({ classList: { contains: () => false }, setAttribute() {}, remove() {} }),
  querySelector: () => null,
  querySelectorAll: q => {
    if (q === "[data-poll]") return [header];
    if (q === "details.menu[open]") return busy.open ? [busy] : [];
    return [];
  },
  getElementById: () => null };
const window = { addEventListener() {}, innerHeight: 800, scrollY: 0, getSelection: () => null,
  matchMedia: q => ({ matches: q === "(hover: none) and (pointer: coarse)" ? touch : false, addEventListener() {} }) };
let nextBody = "v2";
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, window, URL, CustomEvent: class {}, AbortController,
  location: { href: "https://shaft.test/i/example/chat" }, history: { replaceState() {} }, navigator: { onLine: true },
  setInterval() {}, setTimeout: () => 1, clearTimeout() {},
  fetch: async () => ({ status: 200, ok: true, headers: { get: () => "" }, text: async () => nextBody }) });
for (const fn of handlers.DOMContentLoaded || []) fn();
const fire = async (type, ev) => {
  const e = { preventDefault() { this.prevented = true; }, ...ev };
  for (const fn of handlers[type] || []) await fn(e);
  return e;
};
(async () => {
  // 1. Desktop hover opens and mouseout outside closes; mouseout into summary keeps it open.
  await fire("mouseover", { target: summary });
  assert.equal(busy.open, true);
  await fire("mouseout", { target: busy, relatedTarget: summary });
  assert.equal(busy.open, true);
  await fire("mouseout", { target: summary, relatedTarget: outside });
  assert.equal(busy.open, false);

  // 2. Hover then click pins it open; mouseout does not close it; Escape closes it.
  await fire("mouseover", { target: summary });
  assert.equal(busy.open, true);
  await fire("click", { target: summary });
  assert.equal(busy.open, true);
  await fire("mouseout", { target: summary, relatedTarget: outside });
  assert.equal(busy.open, true);
  await fire("keydown", { target: summary, key: "Escape" });
  assert.equal(busy.open, false);

  // 3. Touch device: mouseover does not open on touchOnly, tap opens, outside tap closes.
  touch = true;
  await fire("mouseover", { target: summary });
  assert.equal(busy.open, false);
  await fire("click", { target: summary });
  assert.equal(busy.open, true);
  await fire("click", { target: outside });
  assert.equal(busy.open, false);
  touch = false;

  // 4. Keyboard Enter and Space toggle it open/closed.
  await fire("keydown", { target: summary, key: "Enter" });
  assert.equal(busy.open, true);
  await fire("keydown", { target: summary, key: " " });
  assert.equal(busy.open, false);
  await fire("keydown", { target: summary, key: " " });
  assert.equal(busy.open, true);

  // 5. Open state and focus survive the header's live poll refresh.
  document.activeElement = summary;
  await header._tick();
  assert.equal(html, "v2");
  assert.equal(busy.open, true);
  assert.equal(document.activeElement, summary);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
