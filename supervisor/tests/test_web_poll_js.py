"""The shipped poll() of web.js, run in Node against a stub page: what an action changed shows at once.

Live, 2026-10-20: "Dismiss these alerts" said done and the alerts stayed. An action asks the polled parts to refresh
(shaft:refresh, 300 ms); when a poll was already on its way, its finally rescheduled the next one at the normal 2 s,
cancelling the asked-for refresh, and it showed what it had fetched from before the action."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB_JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"
NODE = shutil.which("node")

HARNESS = r"""
const POLL_LIMIT_MS = 15000;
const net = { down: new Set() };
const busyIn = () => false, fresh = () => {}, stale = () => {}, netNote = () => {}, checkVersion = () => {};
const pairAgain = () => false;
const textOf = (s) => s, toBottom = () => {}, morph = () => { throw new Error("not keyed"); };
const listeners = {};
const document = {
  visibilityState: "visible", body: { scrollHeight: 0 },
  addEventListener: (t, f) => { (listeners[t] = listeners[t] || []).push(f); },
  dispatchEvent: (e) => (listeners[e.type] || []).forEach(f => f(e)),
};
const window = { addEventListener: () => {}, innerHeight: 0, scrollY: 0 };
const navigator = { onLine: true };
const shown = ["START"];                 // every content the panel was given, in order
const el = {
  dataset: { poll: "/fragment/overview", every: "2000" }, isConnected: true,
  get innerHTML() { return shown[shown.length - 1]; }, set innerHTML(v) { shown.push(v); },
  getClientRects: () => [1], querySelectorAll: () => [], closest: () => null, dispatchEvent: () => {},
};
// The server: the state is OLD until the action, NEW after it. A GET answers what was true when it arrived,
// 150 ms later (the overview takes ~0.3 s to build).
let state = "OLD";
globalThis.fetch = (url) => {
  const body = state;
  return new Promise(r => setTimeout(() => r({ status: 200, ok: true, headers: { get: () => "" },
                                               text: async () => body }), 150));
};
//POLL//
poll(el);
setTimeout(() => {                       // 2.05 s: the first poll (sent at 2 s) is on its way
  state = "NEW";                         // the action changed it on the server...
  const at = Date.now();
  document.dispatchEvent({ type: "shaft:refresh" });   // ...and asked the page to show it
  const done = setInterval(() => {
    if (el.innerHTML === "NEW" || Date.now() - at > 3000) {
      clearInterval(done);
      console.log(JSON.stringify({ ms: Date.now() - at, final: el.innerHTML, shown }));
      process.exit(0);
    }
  }, 5);
}, 2050);
"""


HEADER_HARNESS = r"""
const POLL_LIMIT_MS = 15000, net = { down: new Set() };
const pairAgain = () => false;
const busyIn = () => false, netNote = () => {}, checkVersion = () => {};
const textOf = s => s, toBottom = () => {};
globalThis.setTimeout = () => 1;
globalThis.clearTimeout = () => {};
globalThis.CustomEvent = class {};
const window = { addEventListener: () => {}, innerHeight: 0, scrollY: 0 };
const navigator = { onLine: true };
const document = { visibilityState: "visible", body: { scrollHeight: 0 }, addEventListener: () => {},
  createElement: () => ({ classList: { contains: x => x === "stale-note" }, setAttribute: () => {}, remove() { el.previousElementSibling = null; } }),
};
let menu, summary, html = "old", status = 200, answer = "new";
const composer = { value: "unfinished draft" };
function makeSummary() {
  return { dataset: { focus: "menu" }, matches: () => true, getAttribute: () => null,
    focus: () => { document.activeElement = summary; } };
}
function resetHeader() { menu = { dataset: { key: "chat-menu" }, open: false }; summary = makeSummary(); }
resetHeader();
menu.open = true;
document.activeElement = summary;
const classes = new Set();
const el = {
  dataset: { poll: "/fragment/chat/wren/header" }, isConnected: true,
  classList: { add: x => classes.add(x), remove: x => classes.delete(x) },
  parentNode: { insertBefore: n => { el.previousElementSibling = n; } },
  get innerHTML() { return html; },
  set innerHTML(v) {
    if (document.activeElement === summary) document.activeElement = document.body;
    html = v; resetHeader();
  },
  contains: n => n === summary,
  querySelectorAll: selector => {
    if (selector === 'button, a[href], summary') return [summary];
    if (selector === 'details[data-key][open]') return menu.open ? [menu] : [];
    if (selector === 'details[data-key]:not([open])') return menu.open ? [] : [menu];
    if (selector === 'details[data-key]') return [menu];
    return [];
  },
  querySelector: () => summary, closest: () => null, dispatchEvent: () => {}, getClientRects: () => [1],
};
globalThis.fetch = async () => ({ status, ok: status === 200, headers: { get: () => "" }, text: async () => answer });
//POLL//
(async () => {
  poll(el);
  await el._tick();
  const focused = document.activeElement === summary, open = menu.open;
  document.activeElement = composer;
  answer = "next";
  await el._tick();
  const typing = document.activeElement === composer && composer.value === "unfinished draft";
  menu.open = false;
  status = 500;
  await el._tick();
  const failed = classes.has("stale") && el.previousElementSibling.textContent.includes("HTTP 500") && html === "next";
  status = 200; answer = "recovered";
  await el._tick();
  console.log(JSON.stringify({ focused, open, typing, failed, recovered: !classes.has("stale"), shut: !menu.open }));
})();
"""


@unittest.skipUnless(NODE, "node is not installed")
class PollRefreshTest(unittest.TestCase):
    def run_poll(self) -> dict:
        src = WEB_JS.read_text()
        m = re.search(r"^  function poll\(el\) \{\n.*?^  \}\n", src, re.S | re.M)
        self.assertIsNotNone(m, "poll() not found in web.js")
        r = subprocess.run([NODE, "-e", HARNESS.replace("//POLL//", m.group(0))], capture_output=True, text=True,
                           timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_issue_10_18_header_preserves_menu_focus_draft_and_marks_failure(self):
        """Reverting #10/#18 focus restoration drops keyboard focus when the header swaps."""
        src = WEB_JS.read_text()
        functions = []
        for name in ("poll", "stale", "fresh"):
            m = re.search(r"^  function " + name + r"\(.*?^  \}\n", src, re.S | re.M)
            self.assertIsNotNone(m)
            functions.append(m.group(0))
        r = subprocess.run([NODE, "-e", HEADER_HARNESS.replace("//POLL//", "\n".join(functions))],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"focused": True, "open": True, "typing": True,
                                               "failed": True, "recovered": True, "shut": True})

    def test_issue_18_header_restores_device_controls_after_swap(self):
        """Reverting #18's refresh hook resets notification state and open-panel accessibility after a swap."""
        src = WEB_JS.read_text()
        m = re.search(r"^  function chatHeaderState\(el\) \{\n.*?^  \}\n", src, re.S | re.M)
        self.assertIsNotNone(m)
        self.assertIn("chatHeaderState(ev.target);", src)
        script = r"""
let notifications = 0, sheets = 0, unread = 0;
const notifyButton = () => notifications++, syncSheets = () => sheets++, markRail = () => unread++;
const document = { body: { classList: { contains: () => true } }, querySelector: () => ({}) };
const el = { querySelector: s => s === '.chathead' ? {} : null };
//FUNCTION//
chatHeaderState(el);
chatHeaderState({ querySelector: () => null });
console.log(JSON.stringify({ notifications, sheets, unread }));
"""
        r = subprocess.run([NODE, "-e", script.replace("//FUNCTION//", m.group(0))],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"notifications": 1, "sheets": 1, "unread": 1})

    def test_a_refresh_asked_while_a_poll_is_on_its_way_shows_the_new_state_at_once(self):
        out = self.run_poll()
        self.assertEqual(out["final"], "NEW", out)
        self.assertLess(out["ms"], 1000, out)                 # was ~2 s: the next ordinary poll
        self.assertEqual(out["shown"], ["START", "NEW"], out)  # the in-flight answer (OLD, from before) never shown



SHOW_HARNESS = r"""
const POLL_LIMIT_MS = 15000, net = { down: new Set() };
const pairAgain = () => false, busyIn = () => false, fresh = () => {}, stale = () => {}, netNote = () => {};
const checkVersion = () => {}, textOf = s => s, toBottom = () => {};
globalThis.setTimeout = () => 1;                 // the 1.5 s timer never fires: only pageshow can fetch here
globalThis.clearTimeout = () => {};
const shows = [];
const window = { addEventListener: (t, f) => { if (t === "pageshow") shows.push(f); }, innerHeight: 0, scrollY: 0 };
const navigator = { onLine: true };
const document = { visibilityState: "visible", body: { scrollHeight: 0 }, addEventListener: () => {} };
const el = { dataset: { poll: "/fragment/chat/wren", every: "1500" }, isConnected: true, children: [],
  getClientRects: () => [1], querySelectorAll: () => [], closest: () => null, dispatchEvent: () => {} };
let fetched = 0;
globalThis.fetch = async () => { fetched += 1; return { status: 304, ok: false, headers: { get: () => "" } }; };
//POLL//
(async () => {
  poll(el);
  shows.forEach(f => f({ persisted: false }));   // the load's own pageshow
  await new Promise(r => setImmediate(r));
  const onLoad = fetched;
  shows.forEach(f => f({ persisted: true }));    // back from the back-forward cache
  await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({ onLoad, restored: fetched - onLoad }));
})();
"""


@unittest.skipUnless(NODE, "node is not installed")
class PageShowTest(unittest.TestCase):
    def test_the_loads_own_pageshow_fetches_nothing_a_restore_refreshes(self):
        """Regression (2026-10-26): every panel was fetched again the moment the page loaded — the first pageshow is
        the load itself — so a chat was downloaded twice on every open. A page restored from the cache still refreshes."""
        m = re.search(r"^  function poll\(el\) \{\n.*?^  \}\n", WEB_JS.read_text(), re.S | re.M)
        r = subprocess.run([NODE, "-e", SHOW_HARNESS.replace("//POLL//", m.group(0))], capture_output=True, text=True,
                           timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"onLoad": 0, "restored": 1})


if __name__ == "__main__":
    unittest.main()
