"""Issues #14 and #28: unread state follows rendered replies, not the selected instance."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor import web_views

from supervisor.tests.node_helpers import NODE_TIMEOUT_S

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"


def feed(archived=False, ts="2026-10-20T11:59:00"):
    return str(web_views.chat_feed("wren", {
        "id": "old" if archived else "current", "state": "ended" if archived else "idle",
        "turns": [{"role": "assistant", "content": "A recorded answer", "ts": ts}],
    }, [], archived=archived))


HARNESS = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const input = JSON.parse(process.argv[2]), handlers = {}, winHandlers = {};
const old = "2026-10-20T10:00:00", latest = "2026-10-20T11:59:00";
let stored = JSON.stringify({wren: old}), badge = 0, writes = 0;
const classes = (...initial) => {
  const s = new Set(initial);
  return { contains: k => s.has(k), add: k => s.add(k), remove: k => s.delete(k),
    toggle(k, on) { if (on) s.add(k); else s.delete(k); } };
};
const link = {dataset: {inst: "wren", last: latest}, classList: classes("inst", "on")};
const button = {classList: classes()};
let replies = [];
const panel = {dataset: {poll: "/fragment/chat/wren", every: "1500"}, isConnected: true,
  classList: classes("feed"), children: [], getClientRects: () => [1],
  querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  get innerHTML() { return this.html; }, set innerHTML(html) {
    this.html = html;
    replies = [...html.matchAll(/<div class="turn it"([^>]*)>/g)].flatMap(m => {
      const attrs = Object.fromEntries([...m[1].matchAll(/data-([\w-]+)="([^"]*)"/g)].map(a => [a[1], a[2]]));
      // on screen: inside the feed's scroll box (W8: rendered below the fold is not seen)
      return attrs["seen-reply"] ? [{dataset: {seenInst: attrs["seen-inst"], seenReply: attrs["seen-reply"]},
        getClientRects: () => [1], getBoundingClientRect: () => ({top: 10, bottom: 60}),
        closest: q => q === "[data-scroll]" ? {getBoundingClientRect: () => ({top: 0, bottom: 600})} : null}] : [];
    });
  }, dispatchEvent(e) { emit(e.type, {target: this}); },
};
panel.innerHTML = input.html;
const rail = {classList: classes("rail"), querySelector: () => null, querySelectorAll: q => q === "a[data-inst]" ? [link] :
  q === "a.unread" && link.classList.contains("unread") ? [link] : []};
const emit = (type, e = {}) => { for (const f of handlers[type] || []) f(e); };
const document = {readyState: "loading", visibilityState: "visible", title: "Config · SHAFT",
  body: {scrollHeight: 0, classList: classes(), appendChild() {}},
  addEventListener(k, f) { (handlers[k] ||= []).push(f); },
  querySelector: q => q === "nav .rail" ? rail : null,
  querySelectorAll: q => q === ".feed [data-seen-reply], .feed [data-sent-at]" ? replies :
    q === "[data-toggle-nav]" ? [button] : q === "[data-poll]" ? [panel] : [],
  getElementById: () => ({setAttribute() {}, remove() {}}),
};
const context = {document, navigator: {setAppBadge: async n => {badge = n;}, clearAppBadge: async () => {badge = 0;}},
  window: {addEventListener(k, f) { (winHandlers[k] ||= []).push(f); }, innerHeight: 0, scrollY: 0,
    getSelection: () => ({isCollapsed: true})},
  localStorage: {getItem: k => k === "shaft.seen" ? stored : null,
    setItem(k, v) { if (k === "shaft.seen") {stored = v; writes++;} }},
  setTimeout: () => 1, clearTimeout() {}, setInterval() {}, AbortController,
  CustomEvent: class {constructor(type) {this.type = type;}},
  fetch: async () => {throw Error("feed unavailable");},
};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const check = unread => {
  assert.equal(link.classList.contains("unread"), unread, "rail dot");
  assert.equal(button.classList.contains("news"), unread, "nav badge");
  assert.equal(badge, unread ? 1 : 0, "app badge");
  assert.equal(document.title, (unread ? "(1) " : "") + "Config · SHAFT", "title");
};
(async () => {
  emit("DOMContentLoaded");
  if (input.mode === "current") assert.equal(JSON.parse(stored).wren, latest, "initial loaded reply is seen");
  emit("shaft:swapped", {target: rail});
  if (input.mode === "current") {
    assert.equal(JSON.parse(stored).wren, latest, "initial loaded reply is seen");
    check(false);
  } else {
    check(true);
    assert.equal(JSON.parse(stored).wren, old, "unseen reply must not be consumed");
  }
  if (input.mode === "failure") {
    await panel._tick();
    assert.ok(panel.classList.contains("stale"), "failed fetch leaves the old feed");
    emit("shaft:swapped", {target: rail});
    check(true);
    assert.equal(JSON.parse(stored).wren, old);
    context.fetch = async () => ({status: 200, ok: true, headers: {get: () => ""}, text: async () => input.newHtml});
    await panel._tick();
    assert.equal(panel.innerHTML, input.newHtml, "the fetched reply was swapped");
    check(false);
    assert.equal(JSON.parse(stored).wren, latest, "successful feed swap clears unread");
  }
  if (input.mode === "tabs") {
    stored = JSON.stringify({wren: latest});
    for (const f of winHandlers.storage || []) f({key: "shaft.seen"});
    check(false);
    emit("shaft:swapped", {target: rail});
    assert.equal(writes, 0, "a stale tab must not roll seen state back");
  }
})().catch(e => {console.error(e); process.exitCode = 1;});
'''


@unittest.skipUnless(NODE, "node is not installed")
class UnreadTest(unittest.TestCase):
    def run_page(self, mode, html=""):
        result = subprocess.run([NODE, "-e", HARNESS, str(JS), json.dumps({
            "mode": mode, "html": html, "newHtml": feed(),
        })], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_28_config_keeps_reply_unread(self):
        """Reverting #28 consumes the unseen reply on Config through the active rail link."""
        self.run_page("config")

    def test_issue_14_archive_keeps_current_reply_unread(self):
        """Reverting #14 consumes the current reply while displaying an archived answer."""
        self.run_page("archive", feed(archived=True))

    def test_issue_14_current_reply_is_seen_on_load(self):
        """Reverting #14 leaves the loaded reply unseen until a rail refresh consumes it."""
        self.run_page("current", feed())

    def test_issue_28_failed_feed_stays_unread_until_successful_swap(self):
        """Reverting #28 clears the mark before a failed feed can render its new answer."""
        self.run_page("failure", feed(ts="2026-10-20T10:00:00"))

    def test_issue_14_seen_state_updates_across_tabs(self):
        """Reverting #14 loses cross-tab updates and lets an older feed overwrite seen state."""
        self.run_page("tabs", feed(ts="2026-10-20T10:00:00"))
