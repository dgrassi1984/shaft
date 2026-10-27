"""The web UI and PWA findings of the system review (REVIEW-system-2026-10-24.md §4), client side: the shipped web.js,
comments.js and service worker, run in Node against stub pages. Each test names its finding and fails on the code
that had it. The server halves are in test_web_review_fixes.py."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor import web_pages

NODE = shutil.which("node")
STATIC = Path(__file__).resolve().parents[1] / "web_static"
JS = (STATIC / "web.js").read_text()
CSS = (STATIC / "web.css").read_text()


def function(name: str, src: str = JS) -> str:
    """One top-level function of web.js's IIFE, as shipped."""
    m = re.search(r"^  function " + name + r"\(.*?^  \}\n", src, re.S | re.M)
    assert m, f"{name}() not found in web.js"
    return m.group(0)


def node(script: str, *args: str) -> None:
    result = subprocess.run([NODE, "-e", script, *args], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


PRELUDE = 'const assert = require("node:assert/strict"), vm = require("node:vm"), fs = require("node:fs");\n'


@unittest.skipUnless(NODE, "node is not installed")
class ReplyFromDocumentTest(unittest.TestCase):
    def test_w2_reply_from_a_document_sheet_closes_it_before_quoting(self):
        """W2: ↩ Reply on text selected in the phone's documents sheet (or full screen) quoted into the composer behind
        it — inert, unseen — and left the sheet open, so nothing visible happened."""
        node(PRELUDE + r"""
let sheet, full, calls, q;
const grid = { classList: { contains: c => c === "docfullscreen" && full } };
const document = { querySelector: s => s === ".chatgrid" ? grid : null };
const quotable = () => q, quoteButton = () => ({ hidden: false });
const fullscreenDoc = on => { calls.push("full:" + on); full = on; if (!sheet.phone) sheet = null; };
const showDocs = on => { calls.push("docs:" + on); sheet = null; };
const quoteText = (text, box, doc) => calls.push(`quote:${text}:${doc}`);
""" + function("quoteSelection") + r"""
function run(where, doc) { calls = []; q = { text: "t", composer: {}, doc };
  sheet = where ? { kind: "docs", phone: where !== "desktop-full" } : null; full = where && where.endsWith("full");
  quoteSelection(); return calls; }
assert.deepEqual(run("phone", "plan.md"), ["docs:false", "quote:t:plan.md"]);
assert.deepEqual(run("phone-full", "plan.md"), ["full:false", "docs:false", "quote:t:plan.md"]);
assert.deepEqual(run("desktop-full", "plan.md"), ["full:false", "quote:t:plan.md"]);   // back beside the chat
assert.deepEqual(run(null, "plan.md"), ["quote:t:plan.md"]);                            // beside the chat: stays
sheet = { kind: "docs", phone: true }; calls = []; q = { text: "t", composer: {}, doc: "" };
quoteSelection(); assert.deepEqual(calls, ["quote:t:"]);                                // the feed: nothing closes
""")


@unittest.skipUnless(NODE, "node is not installed")
class KeyboardTest(unittest.TestCase):
    def test_w3_the_sheets_cover_only_what_the_keyboard_leaves(self):
        """W3: the documents sheet and the comment box were fixed with inset 0 / bottom 0, so the iOS keyboard covered
        their bottom (Save, Cancel). fitViewport now gives the visible part's edges, and the sheets use them."""
        out = node(PRELUDE + r"""
const props = {}, classes = new Set();
const window = { innerHeight: 800, scrollY: 0, visualViewport: { height: 500, offsetTop: 40 }, scrollTo() {} };
const document = { documentElement: { style: { setProperty: (k, v) => { props[k] = v; } } }, querySelector: () => null,
  body: { classList: { toggle: (k, on) => on ? classes.add(k) : classes.delete(k), contains: k => classes.has(k) } } };
const grow = () => {}, keyboardUp = () => true;
""" + function("fitViewport") + r"""
fitViewport(); console.log(JSON.stringify(props));
""")
        self.assertEqual(json.loads(out), {"--vvh": "500px", "--vvtop": "40px", "--vvbottom": "260px"})

    def test_the_insets_clear_when_the_keyboard_is_gone(self):
        """Grok review of 8735c62 (S3): iOS can leave visualViewport a few px off after the keyboard closes; the sheets
        then kept a sliver of inset. Within a few px of the whole window, and not panned, there is none; never below 0."""
        out = node(PRELUDE + r"""
const props = {}, window = { innerHeight: 800, scrollY: 0, visualViewport: { height: 797, offsetTop: 0.4 }, scrollTo() {} };
const document = { documentElement: { style: { setProperty: (k, v) => { props[k] = v; } } }, querySelector: () => null,
  body: { classList: { toggle() {}, contains: () => false } } };
const grow = () => {}, keyboardUp = () => false;
""" + function("fitViewport") + r"""
fitViewport(); const closed = { ...props };
Object.assign(window.visualViewport, { height: 820, offsetTop: -12 }); fitViewport();
console.log(JSON.stringify([closed, props]));
""")
        closed, overshoot = json.loads(out)
        self.assertEqual((closed["--vvtop"], closed["--vvbottom"]), ("0px", "0px"))
        self.assertEqual((overshoot["--vvtop"], overshoot["--vvbottom"]), ("0px", "0px"))
        for rule in re.findall(r"\.chatgrid\.(?:showdocs|docfullscreen) > aside\.docs \{[^}]*position: fixed[^}]*\}", CSS):
            self.assertIn("inset: var(--vvtop, 0px) 0 var(--vvbottom, 0px)", rule)
        self.assertEqual(len(re.findall(r"inset: var\(--vvtop, 0px\) 0 var\(--vvbottom, 0px\)", CSS)), 3)
        self.assertIn("bottom: calc(1rem + var(--vvbottom, 0px))", CSS)          # the comment box, beside the chat
        self.assertIn("#comment-box { left: 0; right: 0; bottom: var(--vvbottom, 0px);", CSS)   # and on a phone
        self.assertIn('window.visualViewport.addEventListener("scroll", fitViewport)', JS)


@unittest.skipUnless(NODE, "node is not installed")
class ClosedEditorTest(unittest.TestCase):
    def test_w4_an_unchanged_editor_in_a_closed_sheet_no_longer_freezes_the_documents(self):
        """W4: closing the sheet while editing left .editing in the hidden view; busyIn counted it, so #docs never
        refreshed again that visit (no new-document badge, nothing opened on its own)."""
        node(PRELUDE + r"""
const selectingIn = () => false, document = { activeElement: null };
let shown = false, text = "same";
const view = { getClientRects: () => shown ? [1] : [],
  querySelector: q => ({ "form.docedit textarea": { value: text }, "textarea.raw": { value: "same" } })[q] };
const el = { dataset: {}, querySelector: q => q === ".editing" ? view : null,
  querySelectorAll: q => q === ".editing" ? [view] : [], contains: () => false };
""" + function("busyIn") + function("editDirty") + r"""
assert.equal(busyIn(el), false, "hidden and unchanged: the panel refreshes");
text = "mine"; assert.equal(busyIn(el), true, "hidden with unsaved text: it waits");
text = "same"; shown = true; assert.equal(busyIn(el), true, "on screen: it waits");
""")
        node(PRELUDE + r"""
const classes = new Set(["showdocs", "docfullscreen"]), closed = [];
const grid = { classList: { remove: k => classes.delete(k), toggle: (k, on) => on ? classes.add(k) : classes.delete(k) } };
let dirty = false;
const view = {};
const document = { querySelector: q => q === ".chatgrid" ? grid : q === "#docs .docview.editing" ? view : null };
const localStorage = { setItem() {}, removeItem() {} }, syncSheets = () => {};
const editDirty = () => dirty, editing = (v, on) => closed.push(on);
""" + function("showDocs") + r"""
showDocs(false); assert.deepEqual(closed, [false], "closed unchanged: the editor closes with it");
dirty = true; showDocs(false); assert.deepEqual(closed, [false], "with changes: kept, with its draft");
""")


@unittest.skipUnless(NODE, "node is not installed")
class OnScreenTest(unittest.TestCase):
    def test_seen_means_its_start_is_in_view_or_it_fills_the_view(self):
        """Grok review of 8735c62 (S3): onScreen() took a 1 px peek for seen, unlike its comment. Seen: the item's top
        is inside the feed's visible box, or the item fills the box (a reply longer than the screen)."""
        out = node(PRELUDE + r"""
const window = { innerHeight: 800 };
const box = { getBoundingClientRect: () => ({ top: 100, bottom: 500 }) };
const item = (top, bottom) => ({ getClientRects: () => [1], closest: () => box,
  getBoundingClientRect: () => ({ top, bottom }) });
""" + function("onScreen") + r"""
console.log(JSON.stringify([[120, 300], [499, 900], [-50, 101], [480, 501], [50, 600], [500, 700], [0, 99]]
  .map(([t, b]) => onScreen(item(t, b)))));
""")
        self.assertEqual(json.loads(out), [True, True, False, True, True, False, False])


@unittest.skipUnless(NODE, "node is not installed")
class AwayTest(unittest.TestCase):
    def test_leaving_the_page_says_so_and_says_when(self):
        """Grok review of 8735c62 (S2): away() returned unless the page was hidden, so pagehide (closing the tab,
        navigating away while visible) sent nothing; and with no time on it, a poll sent before it but arriving after
        marked the device watching again. It goes on pagehide too, stamped, and the feed's polls are stamped."""
        out = node(PRELUDE + r"""
const sent = [];
let state = "visible";
const document = { get visibilityState() { return state; },
  querySelector: q => q === ".feed[data-follow][data-poll]" ? { dataset: { poll: "/fragment/chat/wren" } } : null };
const Date = { now: () => 1234 };
const fetch = (u, o) => { sent.push([u, !!o.keepalive]); return Promise.resolve(); };
""" + function("away") + r"""
away({ type: "visibilitychange" });                 // still visible: not away
away({ type: "pagehide" });                         // leaving, though visible
state = "hidden"; away({ type: "visibilitychange" });
console.log(JSON.stringify(sent));
""")
        self.assertEqual(json.loads(out), [["/fragment/chat/wren?away=1&at=1234", True]] * 2)
        self.assertIn('"X-Shaft-At": String(Date.now())', JS)


@unittest.skipUnless(NODE, "node is not installed")
class ServiceWorkerTest(unittest.TestCase):
    HARNESS = PRELUDE + r"""
const handlers = {}, opened = [], posted = [];
const self = { location: { origin: "https://shaft.test" }, addEventListener: (k, fn) => { handlers[k] = fn; },
  registration: { pushManager: { subscribe: async () => ({ endpoint: "https://web.push.apple.com/new" }) } },
  clients: { matchAll: async () => clients, openWindow: async u => { opened.push(u); return null; } } };
let clients = [], tokenStatus = 200;
const fetch = async (u, o = {}) => { posted.push([u, o.method || "GET", (o.headers || {})["X-CSRF-Token"] || "", o.body || ""]);
  const status = u === "/push/token" ? tokenStatus : 200;
  return { ok: status === 200, status, json: async () => { if (status !== 200) throw new SyntaxError("not JSON");
    return u === "/push/token" ? { csrf: "tok" } : { key: "AQID" }; } }; };
vm.runInNewContext(process.argv[1], { self, fetch, caches: {}, URL, atob: s => Buffer.from(s, "base64").toString("binary"),
  Uint8Array, console });
async function click(url) { let wait; handlers.notificationclick({ notification: { close() {}, data: { url } },
  waitUntil: p => { wait = p; } }); return wait; }
"""

    def test_w6_a_notification_brings_an_open_chat_forward_and_opens_one_otherwise(self):
        """W6: notificationclick navigated whatever window it found: on one the worker does not control navigate()
        rejected, uncaught, and nothing opened; on the chat itself it reloaded it, dropping attached files."""
        node(self.HARNESS + r"""
(async () => {
  const there = { url: "https://shaft.test/i/wren/chat?doc=a.md", focused: 0, navigated: 0,
    focus() { this.focused++; return this; }, navigate() { this.navigated++; return Promise.resolve(this); } };
  clients = [there];
  await click("/i/wren/chat");
  assert.deepEqual([there.focused, there.navigated], [1, 0], "already on the chat: brought forward, not reloaded");
  const other = { url: "https://shaft.test/overview", focus() { return this; },
    navigate: () => Promise.reject(new TypeError("not controlled")) };
  clients = [other];
  await click("/i/wren/chat");
  assert.deepEqual(opened, ["https://shaft.test/i/wren/chat"], "navigate refused: a window opens instead");
})().catch(e => { console.error(e); process.exitCode = 1; });
""", web_pages.service_worker())

    def test_m11_notification_navigates_an_archived_chat_to_the_current_chat(self):
        """M11: notificationclick focused an archive because it matched only the current chat's pathname."""
        node(self.HARNESS + r"""
(async () => {
  const there = {url: "https://shaft.test/i/wren/chat?id=web-old", focused:0, navigated:[],
    focus() {this.focused++; return this;},
    async navigate(url) {this.navigated.push(url); this.url=url; return this;}};
  clients = [there];
  await click("/i/wren/chat");
  assert.deepEqual(there.navigated, ["https://shaft.test/i/wren/chat"]);
  assert.equal(there.focused, 1);
  assert.deepEqual(opened, []);
  await click("/i/wren/chat");
  assert.equal(there.navigated.length, 1, "the current chat keeps its composer");
})().catch(e => {console.error(e); process.exitCode = 1;});
""", web_pages.service_worker())

    def test_w5_a_subscription_the_browser_renewed_is_handed_to_shaft(self):
        """W5: no pushsubscriptionchange handler: when the push service rotated the subscription, SHAFT kept the dead
        endpoint and every push failed."""
        node(self.HARNESS + r"""
(async () => {
  let wait;
  handlers.pushsubscriptionchange({ newSubscription: { endpoint: "https://web.push.apple.com/rotated" },
    waitUntil: p => { wait = p; } });
  await wait;
  assert.deepEqual(posted.map(p => p.slice(0, 3)), [["/push/token", "GET", ""], ["/push/subscribe", "POST", "tok"]]);
  assert.match(posted[1][3], /rotated/);
})().catch(e => { console.error(e); process.exitCode = 1; });
""", web_pages.service_worker())

    def test_a_renewal_without_a_token_posts_nothing_and_says_why(self):
        """Grok review of 8735c62 (S3): the worker read /push/token's body as JSON whatever its status (an unpaired
        device's 403 page) and never looked at the subscribe answer. A refusal now stops it, by name."""
        node(self.HARNESS + r"""
(async () => {
  tokenStatus = 403; let wait;
  handlers.pushsubscriptionchange({ newSubscription: { endpoint: "https://web.push.apple.com/rotated" },
    waitUntil: p => { wait = p; } });
  await assert.rejects(wait, /token: HTTP 403/);
  assert.deepEqual(posted.map(p => p[0]), ["/push/token"]);
})().catch(e => { console.error(e); process.exitCode = 1; });
""", web_pages.service_worker())
        self.assertIn("bugs.webkit.org/show_bug.cgi?id=294421", web_pages.service_worker())

    def test_a_focus_that_fails_opens_a_window(self):
        """Grok review of 8735c62 (S3): focus() can reject (the window is closing, or the browser refuses): nothing
        opened."""
        node(self.HARNESS + r"""
(async () => {
  clients = [{ url: "https://shaft.test/i/wren/chat", focus: () => Promise.reject(new Error("not allowed")) }];
  await click("/i/wren/chat");
  assert.deepEqual(opened, ["https://shaft.test/i/wren/chat"]);
})().catch(e => { console.error(e); process.exitCode = 1; });
""", web_pages.service_worker())


@unittest.skipUnless(NODE, "node is not installed")
class RenewSubscriptionTest(unittest.TestCase):
    HARNESS = PRELUDE + r"""
const mode = process.argv[2], handlers = {}, queries = {}, els = [], posts = [];
const storage = new Map(JSON.parse(process.argv[3] || "[]"));
const button = { dataset: {}, hidden: false, disabled: false, textContent: "" };
queries['meta[name="csrf"]'] = { content: "tok" }; queries["[data-notify]"] = button;
const box = { sub: mode === "after-throw" ? null : { endpoint: "https://web.push.apple.com/mine",
  options: { applicationServerKey: new Uint8Array([1, 2, 3]).buffer },
  async unsubscribe() { box.unsubscribed = true; box.sub = null; } } };
const document = { readyState: "loading", body: { appendChild() {}, classList: { contains: () => false, toggle() {} } },
  addEventListener(k, fn) { (handlers[k] ||= []).push(fn); }, querySelector: q => queries[q] || null, querySelectorAll: () => [],
  getElementById: id => els.find(e => e.id === id) || null,
  createElement() { const el = { id: "", setAttribute() {}, appendChild() {} }; els.push(el); return el; } };
const pushManager = { async getSubscription() { return box.sub; },
  async subscribe(o) { if (mode === "throws") throw new Error("push service unreachable");
    box.key = [...o.applicationServerKey]; box.sub = { endpoint: "https://web.push.apple.com/new" }; return box.sub; } };
const held = { rotated: { subscribed: true, endpoint: "https://web.push.apple.com/old", key: "AQID", stale: false },
  rekeyed: { subscribed: true, endpoint: "https://web.push.apple.com/mine", key: "BAUG", stale: true },
  throws: { subscribed: true, endpoint: "https://web.push.apple.com/mine", key: "BAUG", stale: true },
  "after-throw": { subscribed: false, endpoint: "", key: "BAUG", stale: false },
  dropped: { subscribed: false, endpoint: "", key: "AQID", stale: false } }[mode];
async function fetch(url, o = {}) {
  if (url === "/push/status") return { ok: true, json: async () => held };
  if (url === "/push/subscribe") { posts.push(JSON.parse(o.body).endpoint); return { ok: true, status: 200 }; }
  throw new Error("unexpected " + url);
}
const Notification = { permission: "granted" };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, Notification, fetch, URL,
  atob: s => Buffer.from(s, "base64").toString("binary"), btoa: s => Buffer.from(s, "binary").toString("base64"),
  setTimeout() { return 0; }, clearTimeout() {}, setInterval() { return 0; },
  localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)), removeItem: k => storage.delete(k) },
  navigator: { serviceWorker: { ready: Promise.resolve({ pushManager }), register: () => Promise.resolve() } },
  window: { addEventListener() {}, PushManager: function () {}, Notification, matchMedia: () => ({ matches: false }) } });
(async () => {
  for (const fn of handlers.DOMContentLoaded) fn();
  for (let i = 0; i < 60; i++) await new Promise(r => setImmediate(r));
  console.log(JSON.stringify({ state: button.dataset.state, posts, unsubscribed: !!box.unsubscribed, key: box.key || null,
    storage: [...storage] }));
})().catch(e => { console.error(e); process.exitCode = 1; });
"""

    def run_mode(self, mode, storage=()):
        return json.loads(node(self.HARNESS, str(STATIC / "web.js"), mode, json.dumps(list(storage))))

    def test_w5_a_visit_gives_shaft_the_subscription_it_lost_or_never_had(self):
        """W5: the button read "on" while SHAFT held a dead endpoint (the browser rotated it) or none at all (a push
        service refused it): every push failed. A visit now hands SHAFT the browser's subscription again."""
        self.assertEqual(self.run_mode("rotated"), {"state": "on", "posts": ["https://web.push.apple.com/mine"],
                                                    "unsubscribed": False, "key": None, "storage": []})
        self.assertEqual(self.run_mode("dropped")["posts"], ["https://web.push.apple.com/mine"])

    def test_w5_a_subscription_made_for_another_key_is_made_again(self):
        """After the server's key changed, the old subscription can never be pushed to: it is replaced by one made
        with the key now (fingerprint() was computed and never used)."""
        out = self.run_mode("rekeyed")
        self.assertEqual(out, {"state": "on", "posts": ["https://web.push.apple.com/new"], "unsubscribed": True,
                               "key": [4, 5, 6], "storage": []})

    def test_a_renewal_that_failed_half_way_is_finished_on_the_next_visit(self):
        """Grok review of 8735c62 (S2): renewing for a new key unsubscribes first; when subscribe() then threw, the page
        marked it renewed and the next visit found no browser subscription and showed "off" for good. It says
        incomplete, remembers the renewal, and the next visit subscribes and hands SHAFT the new one."""
        out = self.run_mode("throws")
        self.assertEqual((out["state"], out["posts"], out["unsubscribed"]), ("incomplete", [], True))
        self.assertIn(["shaft.push.renew", "1"], out["storage"])
        again = self.run_mode("after-throw", out["storage"])
        self.assertEqual((again["state"], again["posts"], again["key"], again["storage"]),
                         ("on", ["https://web.push.apple.com/new"], [4, 5, 6], []))


@unittest.skipUnless(NODE, "node is not installed")
class LostAnswerFormTest(unittest.TestCase):
    def test_w15_a_whole_page_refusal_shows_as_its_words_only(self):
        """W15: [data-refusal] put whatever came back into the document view — a 403's whole notice page, nav and all,
        or a 413's error page. A fragment of this server's shows as written; anything else as one line of text."""
        out = node(PRELUDE + r"""
const document = { createElement: () => ({ textContent: "" }) };
class DOMParser { parseFromString(body) {
  if (/doctype/i.test(body)) return { body: { textContent: "SHAFT nav… Not allowed This request…" },
    querySelector: q => q === "section.notice" ? { querySelectorAll: () => [{ textContent: "Not allowed" },
      { textContent: "This request did not come from this page." }, { textContent: "Back to app" }] } : null };
  return { body: { childNodes: ["fragment:" + body] }, querySelector: () => null }; } }
const r = type => ({ status: 403, headers: { get: k => k === "Content-Type" ? type : null } });
""" + function("refusalNodes") + r"""
console.log(JSON.stringify([
  refusalNodes(r("text/html; charset=utf-8"), "<p>refused: changed</p><pre class=diff>x</pre>"),
  refusalNodes(r("text/html; charset=utf-8"), "<!doctype html><html><body><nav>…</nav><main>…</main>").map(p => p.textContent),
  refusalNodes(r("text/plain; charset=utf-8"), "Too large: at most 1 MB can be sent at once.").map(p => p.textContent)]));
""")
        fragment, page, plain = json.loads(out)
        self.assertEqual(fragment, ["fragment:<p>refused: changed</p><pre class=diff>x</pre>"])
        self.assertEqual(page, ["Not allowed — This request did not come from this page."])
        self.assertEqual(plain, ["Too large: at most 1 MB can be sent at once."])
        self.assertIn("refusal.replaceChildren(...(r.ok ? [] : refusalNodes(r, body)))", JS)


@unittest.skipUnless(NODE, "node is not installed")
class CopyAndAnnounceTest(unittest.TestCase):
    def test_w18_the_copy_button_sits_beside_the_code_block_not_in_it(self):
        """W18: the Copy button was inside the <pre>, which scrolls sideways: it scrolled away with the code."""
        out = node(PRELUDE + r"""
const icons = { copy: "<svg/>" };
const make = (tag) => ({ tag, children: [], dataset: {}, classList: { contains(c) { return this.owner.className === c; } },
  setAttribute() {}, append(...n) { n.forEach(x => { x.parentNode && x.parentNode.children.splice(x.parentNode.children.indexOf(x), 1);
    x.parentNode = this; x.parentElement = this; this.children.push(x); }); },
  insertBefore(n, ref) { n.parentNode = this; n.parentElement = this; this.children.splice(this.children.indexOf(ref), 0, n); } });
const document = { createElement: t => { const n = make(t); n.classList.owner = n; return n; } };
const md = make("div"); md.classList.owner = md;
const pre = make("pre"); pre.classList.owner = pre; md.append(pre);
const root = { querySelectorAll: () => [pre] };
""" + function("decorate") + r"""
decorate(root); decorate(root);                                    // twice: one button
const wrap = md.children[0];
console.log(JSON.stringify({ wrap: wrap.className, kids: wrap.children.map(c => c.tag), inPre: pre.children.length }));
""")
        self.assertEqual(json.loads(out), {"wrap": "prewrap", "kids": ["pre", "button"], "inPre": 0})
        self.assertIn(".prewrap > .copy { position: absolute;", CSS)
        self.assertNotIn("pre > .copy", CSS)

    def test_w18_two_announcements_read_only_the_newer(self):
        """W18: announce() cleared the region and set it 50 ms later; two at once both fired, the older read last's
        place."""
        out = node(PRELUDE + r"""
const timers = new Map(); let n = 0; const said = [];
const setTimeout = (fn) => { timers.set(++n, fn); return n; }, clearTimeout = id => timers.delete(id);
const box = { set textContent(v) { if (v) said.push(v); }, get textContent() { return ""; } };
const region = () => box;
""" + function("announce") + r"""
announce("wren replied: one"); announce("kant replied: two");
for (const fn of [...timers.values()]) fn();
console.log(JSON.stringify(said));
""")
        self.assertEqual(json.loads(out), ["kant replied: two"])


@unittest.skipUnless(NODE, "node is not installed")
class LeaveWarningJsTest(unittest.TestCase):
    HARNESS = PRELUDE + r"""
const storage = new Map(JSON.parse(process.argv[2])), handlers = {}, win = {};
const note = { textContent: "" }, sha = { type: "hidden", value: "s2", defaultValue: "s2" };
const box = { tagName: "TEXTAREA", type: "textarea", value: "be useful", defaultValue: "be useful" };
const form = { dataset: { keep: "drive.wren", guard: "", post: "/action/drive-set" }, isConnected: true,
  querySelector: q => ({ textarea: box, "textarea[name=text]": box, "input[name=sha]": sha, "[data-keep-note]": note })[q] || null,
  querySelectorAll: q => q === "input, textarea, select" ? [sha, box] : [], closest: () => null };
box.closest = q => q === "form[data-keep]" ? form : null;
const document = { readyState: "loading", visibilityState: "visible", body: { appendChild() {}, classList: { toggle() {}, contains: () => false } },
  documentElement: { style: { setProperty() {} } },
  addEventListener(k, fn) { (handlers[k] ||= []).push(fn); }, querySelector: () => null,
  querySelectorAll: q => ["form[data-guard]", "form[data-keep]"].includes(q) ? [form] : [],
  getElementById: () => null, createElement: () => ({ setAttribute() {} }) };
const answers = [];
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, URL, navigator: {}, URLSearchParams,
  AbortController, Event, FormData: class { *[Symbol.iterator]() {} },
  DOMParser: class { parseFromString(t) { return { body: { textContent: t, childNodes: [] } }; } },
  fetch: async () => answers.shift(),
  window: { addEventListener(k, fn) { (win[k] ||= []).push(fn); }, innerHeight: 800, getSelection: () => null },
  localStorage: { getItem: k => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, v), removeItem: k => storage.delete(k) },
  setTimeout() {}, clearTimeout() {}, setInterval() {} });
const unload = () => { const ev = { preventDefault() { this.prevented = true; } }; for (const f of win.beforeunload || []) f(ev);
  return !!ev.prevented; };
for (const f of handlers.DOMContentLoaded) f();
"""

    def test_w14_a_drive_edit_asks_before_leaving_and_survives_a_reload(self):
        """W14: drive, config and settings edits had no draft and no leave warning: an iOS back-swipe lost a long
        drive.md edit."""
        out = node(self.HARNESS + r"""
const clean = unload();
box.value = "be useful, and brief"; for (const f of handlers.input) f({ target: box });
console.log(JSON.stringify({ clean, dirty: unload(), kept: JSON.parse(storage.get("shaft.draft.drive.wren")) }));
""", str(STATIC / "web.js"), "[]")
        self.assertEqual(json.loads(out), {"clean": False, "dirty": True,
                                           "kept": {"text": "be useful, and brief", "sha": "s2"}})
        out = node(self.HARNESS + r"""
console.log(JSON.stringify({ text: box.value, note: note.textContent, sha: sha.value, dirty: unload() }));
""", str(STATIC / "web.js"), json.dumps([["shaft.draft.drive.wren", json.dumps({"text": "mine", "sha": "s1"})]]))
        self.assertEqual(json.loads(out), {"text": "mine", "sha": "s2", "dirty": True,
                                           "note": "Unsaved edit restored — begun on an older version: check the diff "
                                                   "before Apply"})

    def test_a_saved_edit_no_longer_reads_as_unsaved(self):
        """Grok review of 8735c62 (S2): after a successful save that did not replace the form (no saved row came back),
        the fields still differed from what the page was drawn with: leaving warned, and the next input kept the
        "draft" again. A save that went through makes the form's values its saved ones."""
        out = node(self.HARNESS + r"""
box.value = "saved now"; for (const f of handlers.input) f({ target: box });
answers.push({ ok: true, status: 200, headers: { get: () => null }, text: async () => "<p>drive.md saved</p>" });
(async () => {
  for (const f of handlers.submit) await f({ target: { closest: q => q === "form[data-post]" ? form : null },
                                            preventDefault() {}, submitter: null });
  console.log(JSON.stringify({ dirty: unload(), kept: storage.has("shaft.draft.drive.wren"), base: box.defaultValue }));
})();
""", str(STATIC / "web.js"), "[]")
        self.assertEqual(json.loads(out), {"dirty": False, "kept": False, "base": "saved now"})


@unittest.skipUnless(NODE, "node is not installed")
class AnchorTest(unittest.TestCase):
    def test_w10_an_anchor_never_cuts_an_emoji_in_half(self):
        """W10: the 32-character context around a comment's anchor was cut by UTF-16 offset, inside an emoji's
        surrogate pair: half a character, which the server could not store."""
        out = node(PRELUDE + r"""
const window = {};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { window });
const text = "x".repeat(10) + "😀" + "a".repeat(31) + "PICK" + "b".repeat(31) + "😀" + "tail";
const start = text.indexOf("PICK"), a = window.shaftComments.anchorOf(text, start, start + 4);
for (const part of [a.prefix, a.exact, a.suffix]) encodeURIComponent(part);   // throws on half a character
const many = "x" + "😀".repeat(1500), long = window.shaftComments.anchorOf(many, 0, many.length);
encodeURIComponent(long.exact);
console.log(JSON.stringify({ exact: a.exact, prefix: a.prefix.length, suffix: a.suffix.length, start: a.start,
  long: long.exact.length }));
""", str(STATIC / "comments.js"))
        self.assertEqual(json.loads(out), {"exact": "PICK", "prefix": 31, "suffix": 31, "start": 43, "long": 1999})


if __name__ == "__main__":
    unittest.main()
