"""Issues #14 and #28: unread state follows rendered replies, not the selected instance. With topic threads the marks
are per thread (and From): the rail lights the thread not on screen, and its instance."""
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
    return str(web_views.chat_feed("wisp", {
        "id": "old" if archived else "current", "state": "ended" if archived else "idle",
        "turns": [{"role": "assistant", "content": "A recorded answer", "ts": ts}],
    }, [], archived=archived))


HARNESS = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const input = JSON.parse(process.argv[2]), handlers = {}, winHandlers = {};
const old = "2026-10-20T10:00:00", latest = "2026-10-20T11:59:00";
let stored = null, badge = 0, writes = 0, posts = [];
const server = {wisp: {current: old, from: old}}, timers = new Map();
let timerId = 0;
const setTimer = (fn, ms) => {const id = ++timerId; timers.set(id, {fn, ms}); return id;};
const drain = async () => {
  for (let n = 0; n < 20; n++) {
    const work = [...timers.entries()].filter(([id, t]) => t.ms === 150);
    for (const [id, t] of work) {timers.delete(id); await t.fn();}
    await Promise.resolve();
  }
};
const classes = (...initial) => {
  const s = new Set(initial);
  return { contains: k => s.has(k), add: k => s.add(k), remove: k => s.delete(k),
    toggle(k, on) { if (on) s.add(k); else s.delete(k); } };
};
// the instance (its data-lasts: each open thread's newest, and From's) and, under it, its threads
const lasts = input.lasts || {current: latest};
const link = {dataset: {inst: "wisp", last: latest, lasts: JSON.stringify(lasts)}, classList: classes("inst", "on")};
const rows = Object.entries(lasts).map(([k, last]) =>
  ({dataset: {threadOf: "wisp", seenKey: k, last}, classList: classes("thread")}));
if (input.stored !== undefined) stored = JSON.stringify(input.stored);
const button = {classList: classes()};
let replies = [];
const panel = {dataset: {poll: "/fragment/chat/wisp", every: "1500"}, isConnected: true,
  classList: classes("feed"), children: [], getClientRects: () => [1],
  querySelector: () => null, querySelectorAll: () => [], closest: () => null,
  get innerHTML() { return this.html; }, set innerHTML(html) {
    this.html = html;
    replies = [...html.matchAll(/<div class="turn (?:it|sent)"([^>]*)>/g)].flatMap(m => {
      const attrs = Object.fromEntries([...m[1].matchAll(/data-([\w-]+)="([^"]*)"/g)].map(a => [a[1], a[2]]));
      // on screen: inside the feed's scroll box (W8: rendered below the fold is not seen)
      return attrs["seen-reply"] || attrs["sent-at"] ? [{dataset: {seenInst: attrs["seen-inst"], seenKey: attrs["seen-key"],
        seenReply: attrs["seen-reply"], sentAt: attrs["sent-at"]},
        getClientRects: () => [1], getBoundingClientRect: () => ({top: 10, bottom: 60}),
        closest: q => q === "[data-scroll]" ? {getBoundingClientRect: () => ({top: 0, bottom: 600})} : null}] : [];
    });
  }, dispatchEvent(e) { emit(e.type, {target: this}); },
};
panel.innerHTML = input.html;
const rail = {classList: classes("rail"), querySelector: () => null, querySelectorAll: q => q === "a[data-inst]" ? [link] :
  q === "a[data-seen-key]" ? rows : q === "a[data-inst].unread" && link.classList.contains("unread") ? [link] : []};
const row = k => rows.find(r => r.dataset.seenKey === k);
const mark = (w, k) => typeof w === "string" ? w : [w[k] || "", w["*"] || ""].sort()[1];
rail._tick = async () => {
  rows.forEach(r => {r.dataset.unread = r.dataset.last > mark(server.wisp, r.dataset.seenKey) ? "1" : "0";});
  link.dataset.unread = rows.some(r => r.dataset.unread === "1") ? "1" : "0";
  emit("shaft:swapped", {target: rail});
};
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
    setItem(k, v) { if (k === "shaft.seen") {stored = v; writes++;} },
    removeItem(k) { if (k === "shaft.seen") stored = null; }},
  setTimeout: setTimer, clearTimeout: id => timers.delete(id), setInterval() {}, AbortController, URLSearchParams,
  CustomEvent: class {constructor(type) {this.type = type;}},
  fetch: async (url, opts) => {
    if (url !== "/seen") throw Error("feed unavailable");
    const m = Object.fromEntries(opts.body);
    assert.equal(opts.headers["Content-Type"], "application/x-www-form-urlencoded");
    assert.ok("X-CSRF-Token" in opts.headers);
    posts.push(m);
    const marks = server[m.inst] ||= {};
    if ((marks[m.key] || "") < m.last) marks[m.key] = m.last;
    return {ok: true, status: 200, headers: {get: () => ""}};
  },
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
  await rail._tick();
  await drain();
  const key = input.key || "current";
  if (input.mode === "current") assert.equal(mark(server.wisp, key), latest, "initial loaded reply is seen");
  emit("shaft:swapped", {target: rail});
  if (input.mode === "current") {
    assert.equal(mark(server.wisp, key), latest, "initial loaded reply is seen");
    check(false);
  } else if (input.mode === "threads") {
    // thread A on screen and seen; B, not on screen, has a newer answer: B and its instance are unread, A is not
    assert.equal(mark(server.wisp, "current"), latest);
    assert.equal(row("current").classList.contains("unread"), false, "the thread on screen is seen");
    assert.equal(row("web-b").classList.contains("unread"), true, "the thread not on screen is unread");
    assert.equal(row("from").classList.contains("unread"), input.fromUnread, "From");
    check(true);
    assert.equal(server.wisp["web-b"], undefined, "B was never on screen");
  } else if (input.mode !== "migration") {
    check(true);
    assert.equal(mark(server.wisp, "current"), old, "unseen reply must not be consumed");
    assert.equal(row("current").classList.contains("unread"), true, "its thread too");
  }
  if (input.mode === "failure") {
    await panel._tick();
    assert.ok(panel.classList.contains("stale"), "failed fetch leaves the old feed");
    emit("shaft:swapped", {target: rail});
    check(true);
    assert.equal(server.wisp.current, old);
    const previousFetch = context.fetch;
    context.fetch = async (url, opts) => url === "/seen" ? previousFetch(url, opts) :
      ({status: 200, ok: true, headers: {get: () => ""}, text: async () => input.newHtml});
    await panel._tick();
    assert.equal(panel.innerHTML, input.newHtml, "the fetched reply was swapped");
    await drain();
    check(false);
    assert.equal(mark(server.wisp, "current"), latest, "successful feed swap clears unread");
  }
  if (input.mode === "tabs") {
    server.wisp.current = latest;
    for (const f of winHandlers.storage || []) f({key: "shaft.seen"});
    check(false);
    const before = posts.length;
    emit("shaft:swapped", {target: rail});
    await drain();
    assert.equal(posts.length, before, "unchanged rendered items must not repost");
    assert.equal(writes, 0, "read state is never written to browser storage");
  }
  if (input.mode === "migration") {
    assert.equal(stored, null, "legacy browser marks removed after acceptance");
    assert.equal(server.wisp.current, latest);
    assert.equal(server.wisp.from, latest);
    assert.equal(posts.length, 2, "legacy wildcard expands once per conversation");
    await rail._tick(); await drain();
    assert.equal(posts.length, 2, "migration does not repeat");
  }
})().catch(e => {console.error(e); process.exitCode = 1;});
'''


@unittest.skipUnless(NODE, "node is not installed")
class UnreadTest(unittest.TestCase):
    def run_page(self, mode, html="", **extra):
        result = subprocess.run([NODE, "-e", HARNESS, str(JS), json.dumps({
            "mode": mode, "html": html, "newHtml": feed(), **extra,
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

    def test_threads_the_one_not_on_screen_is_unread_and_lights_its_instance(self):
        """Topic threads: seeing thread A must not consume thread B's newer answer (one mark per instance did), and the
        instance stays marked while B is unread. A mark from before threads ("*") still counts for From."""
        later = "2026-10-20T12:30:00"
        self.run_page("threads", feed(), lasts={"current": "2026-10-20T11:59:00", "web-b": later,
                                                "from": "2026-10-20T09:00:00"}, fromUnread=False)
        self.run_page("threads", feed(), lasts={"current": "2026-10-20T11:59:00", "web-b": later, "from": later},
                      fromUnread=True)

    def test_browser_marks_migrate_once_then_leave_storage(self):
        for value in ("2026-10-20T11:59:00", {"*": "2026-10-20T11:59:00"},
                      {"current": "2026-10-20T11:59:00", "from": "2026-10-20T11:59:00"}):
            self.run_page("migration", stored={"wisp": value}, lasts={"current": "2026-10-20T11:59:00",
                                                                       "from": "2026-10-20T11:59:00"})

    def test_threads_from_feed_items_are_seen_under_from(self):
        """A message the instance sent outside any thread, on screen in From, clears From's mark only."""
        html = str(web_views.feed_html({"instance": "wisp", "chat": None, "archived": False, "earlier": 0,
                                        "items": [{"key": "sm.md", "type": "sent", "ts": "2026-10-20T11:59:00",
                                                   "file": "m.md", "text": "From a cycle", "kind": "note",
                                                   "read": False, "attachments": [], "rating": None}]},
                                       from_feed=True))
        self.assertIn('data-seen-key="from"', html)
        self.run_page("current", html, lasts={"from": "2026-10-20T11:59:00"}, key="from")
