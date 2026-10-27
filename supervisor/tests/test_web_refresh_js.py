"""Issues #26/#27: run shipped refresh code on a small DOM with replaceable nodes."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor import web_pages, web_views

JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"
NODE = shutil.which("node")

HARNESS = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const handlers = {}, ids = {}, queries = {};
let childReads = 0;
class Node {
  constructor(key, classes = '', version = '') {
    this.dataset = key ? {k: key} : {}; this.className = classes; this.version = version;
    this.nodes = []; this.parts = []; this.scrollTop = 0; this.open = false; this.isConnected = true;
    this.classList = {contains: c => this.className.split(' ').includes(c),
      add: c => this.className += ' ' + c, remove: c => this.className = this.className.split(' ').filter(x => x !== c).join(' '),
      toggle() {}};
  }
  get children() { childReads++; return this.nodes; }
  get firstElementChild() { return this.nodes[0] || null; }
  get nextElementSibling() { return this.parentNode?.nodes[this.parentNode.nodes.indexOf(this) + 1] || null; }
  get previousElementSibling() { return this.parentNode?.nodes[this.parentNode.nodes.indexOf(this) - 1] || null; }
  get outerHTML() { return JSON.stringify([this.dataset.k, this.className, this.version]); }
  get innerHTML() { return this.version; }
  set innerHTML(v) {
    this.version = v;
    if (this.rebuild) {
      if (this.contains(document.activeElement)) document.activeElement = document.body;
      this.rebuild();
    }
  }
  insertBefore(n, c) { n.remove(); const i = c ? this.nodes.indexOf(c) : this.nodes.length; this.nodes.splice(i, 0, n); n.parentNode = this; }
  appendChild(n) { this.insertBefore(n, null); if (n.id) ids[n.id] = n; }
  remove() { if (this.parentNode) this.parentNode.nodes.splice(this.parentNode.nodes.indexOf(this), 1); this.parentNode = null; if (this.id) delete ids[this.id]; }
  replaceWith(n) { this.parentNode.insertBefore(n, this); this.remove(); }
  addEventListener() {}
  setAttribute(k, v) { this[k] = v; }
  getAttribute(k) { return this[k] || null; }
  contains(n) { return this.parts.includes(n) || this.nodes.includes(n); }
  matches(s) { return s === 'img' ? this.tag === 'img' : s === '[data-scroll]' ? !!this.scrollBox
    : s === 'button, a[href], summary' ? this.kind === 'control' : false; }
  closest(s) { return s === '[data-scroll]' ? this.box || null : s === '[data-follow]' ? this.feed || null : null; }
  focus() { document.activeElement = this; }
  getClientRects() { return [1]; }
  getBoundingClientRect() { return {top: this.top || 0, bottom: 300}; }
  querySelectorAll(s) {
    if (s === '.turn[data-k]') return this.nodes.filter(n => n.dataset.k && n.classList.contains('turn'));
    if (s === '.sending') return this.nodes.filter(n => n.classList.contains('sending'));
    if (s.startsWith('details[data-key]')) return this.parts.filter(n => n.kind === 'details' && n.dataset.key &&
      (s.includes(':not') ? !n.open : s.includes('[open]') ? n.open : true));
    if (s === '[data-scroll-key]') return this.parts.filter(n => n.dataset.scrollKey);
    if (s === '.elapsed[data-since]') return this.parts.filter(n => n.dataset.since);
    if (s === 'button, a[href], summary') return this.parts.filter(n => n.kind === 'control');
    if (s === '[data-poll]') return this.parts.filter(n => n.dataset.poll);
    return [];
  }
  querySelector(s) { return this.querySelectorAll(s)[0] || null; }
  dispatchEvent(e) { e.target = this; for (const f of handlers[e.type] || []) f(e); }
}
const document = {readyState: 'loading', visibilityState: 'visible', body: new Node(),
  addEventListener(t, f) { (handlers[t] ||= []).push(f); },
  querySelector: s => queries[s] || null, querySelectorAll: () => [], getElementById: id => ids[id] || null,
  createElement: () => new Node(), importNode: n => n};
const window = {addEventListener() {}, getSelection: () => null, innerHeight: 300, scrollY: 0, scrollTo() {}};
let response = {status: 200, body: 'new'}, error = null, parsed = [];
const context = {document, window, navigator: {onLine: true}, console, URL, AbortController,
  CustomEvent: class {constructor(type, options) {this.type = type; Object.assign(this, options);}},
  setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  DOMParser: class {parseFromString() { return {body: {children: parsed, textContent: 'server error'}}; }},
  fetch: async () => {if (error) throw Error(error); return {status: response.status, ok: response.status === 200,
    headers: {get: () => ''}, text: async () => response.body};}};
let source = fs.readFileSync(process.argv[1], 'utf8');
source = source.replace(/\}\)\(\);\s*$/, 'window.test = {poll, morph, docSaved, toBottom}; })();');
vm.runInNewContext(source, context);
const {poll, morph, docSaved, toBottom} = window.test;
function panel() { const p = new Node(); p.dataset.poll = '/panel'; document.body.appendChild(p); return p; }
function part(kind, dataset) { const n = new Node(); n.kind = kind; n.dataset = dataset; return n; }
function turn(k, v) { const n = new Node(k, 'turn me', v); n.top = 500; return n; }
(async () => {
//CASE//
})().catch(e => {console.error(e); process.exitCode = 1;});
'''


@unittest.skipUnless(NODE, "node is not installed")
class LiveRefreshTest(unittest.TestCase):
    def run_js(self, script):
        result = subprocess.run([NODE, "-e", HARNESS.replace("//CASE//", script), str(JS)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_26_each_failed_panel_must_recover(self):
        """Reverting #26 lets a successful rail poll clear a failed feed's stale state and warning."""
        self.run_js(r'''
const a = panel(), b = panel(); poll(a); poll(b);
error = 'offline'; await a._tick(); await b._tick();
error = null; response.status = 304; await b._tick();
assert.equal(a.classList.contains('stale'), true);
assert.equal(b.classList.contains('stale'), false);
assert.ok(ids.netnote, 'connection warning must remain');
assert.match(ids.netnote.textContent, /Cannot reach SHAFT/);
response.status = 500; await a._tick();
assert.ok(ids.netnote, 'connection warning must remain');
assert.match(ids.netnote.textContent, /Cannot reach SHAFT/);
response.status = 200; await a._tick();
assert.equal(a.classList.contains('stale'), false); assert.equal(ids.netnote, undefined);
''')

    def test_issue_26_newbelow_requires_a_new_turn_below_view(self):
        """Reverting #26 shows New below for activity edits, separators, or visible insertions."""
        self.run_js(r'''
const p = panel(), box = new Node(), nb = {hidden: true};
p.dataset.keyed = '1'; p.dataset.follow = '1'; p.box = box;
box.scrollHeight = 1000; box.clientHeight = 300; box.scrollTop = 0;
queries['[data-newbelow]'] = nb;
p.appendChild(turn('a1', 'old')); poll(p);
parsed = [turn('a1', 'changed')]; await p._tick(); assert.equal(nb.hidden, true);
response.body = 'separator'; parsed = [turn('a1', 'changed'), new Node('day', 'chat-day')];
await p._tick(); assert.equal(nb.hidden, true);
response.body = 'visible'; const visible = turn('a2', 'new'); visible.top = 100;
parsed = [turn('a1', 'changed'), visible]; await p._tick(); assert.equal(nb.hidden, true);
response.body = 'below'; parsed = [turn('a1', 'changed'), turn('a2', 'new'), turn('a3', 'new')];
await p._tick(); assert.equal(nb.hidden, false);
''')

    def test_issue_27_activity_scroll_details_clock_and_keyed_identity(self):
        """Reverting #27 resets activity scroll and clock text when a changed turn is replaced."""
        activity = str(web_views._activity(["one", "two"], "live", True))
        self.run_js('const activityHTML = ' + json.dumps(activity) + r''';
const scrollKey = /data-scroll-key="([^"]+)"/.exec(activityHTML)?.[1];
const p = panel(), old = turn('live', 'old'), stable = turn('a1', 'same');
const details = part('details', {key: 'live'}), scroll = part('scroll', {scrollKey}), clock = part('clock', {since: 'start'});
details.open = false; scroll.scrollTop = 123; clock.textContent = ' · 2:34';
old.parts = [details, scroll, clock]; old._src = old.outerHTML; stable._src = stable.outerHTML;
p.appendChild(old); p.appendChild(stable);
const next = turn('live', 'new'); next.parts = [part('details', {key: 'live'}), part('scroll', {scrollKey}), part('clock', {since: 'start'})];
next.parts[0].open = true; parsed = [turn('a1', 'same'), next]; morph(p, 'new');
assert.equal(next.parts[1].scrollTop, 123); assert.equal(next.parts[0].open, false);
assert.equal(p.children[0], stable); assert.equal(p.children[1], next);
assert.equal(next.parts[2].textContent, ' · 2:34');
// Count child-list scans on a synthetic long conversation; no production data is read.
const big = panel(); for (let i = 0; i < 1000; i++) {const t = turn('a' + i, 'same'); t._src = t.outerHTML; big.appendChild(t);}
parsed = big.nodes.map(n => turn(n.dataset.k, 'same')); childReads = 0; morph(big, 'unchanged');
assert.ok(childReads < 10, `child-list scans: ${childReads}`);
''')

    def test_w7_a_sending_copy_goes_once_its_turn_is_in_the_feed(self):
        """W7: a poll that landed between the server saving the turn and the send returning brought the turn while the
        copy was not "done" yet: kept, then every later refresh was a 304 — a "sending…" duplicate for good."""
        self.run_js(r'''
const p = panel(), mine = turn('u0', 'hi'), copy = new Node(null, 'turn me sending');
copy.dataset.mid = 'm-1'; mine._src = mine.outerHTML; p.appendChild(mine); p.appendChild(copy);
const other = new Node(null, 'turn me sending'); other.dataset.mid = 'm-2'; p.appendChild(other);
const saved = turn('u1', 'hello'); saved.dataset.mid = 'm-1';
parsed = [turn('u0', 'hi'), saved]; morph(p, 'with its turn');
assert.deepEqual(p.children.map(n => n.dataset.k || n.dataset.mid), ['u0', 'u1', 'm-2']);   // its own copy only
''')

    def test_w12_a_poll_inside_a_refreshed_part_starts_polling(self):
        """W12: the running reply's activity is a poll of its own inside the feed's live turn; one that arrives in a
        refresh (a new reply began) has to start, and one gone from the page has to stop."""
        self.run_js(r'''
const p = panel(); let inner;
p.rebuild = () => { inner = part('poll', {poll: '/fragment/chat/wren/live', every: '1500'}); inner.isConnected = true;
  p.parts = [inner]; };
poll(p); await p._tick();
assert.equal(typeof inner._tick, 'function', 'the poll that came with the refresh runs');
inner.isConnected = false; await inner._tick();               // gone with the next refresh: stops, quietly
''')

    def test_w13_a_selection_in_the_feed_no_longer_stops_its_refresh(self):
        """W13: text left selected in the feed made busyIn() true, and the feed stopped refreshing for good, though the
        keyed morph can leave the selected message as it is and refresh the rest."""
        self.run_js(r'''
const p = panel(); p.dataset.keyed = '1';
const selected = turn('a1', 'old'), text = part('text', {}); selected.parts = [text]; p.appendChild(selected);
window.getSelection = () => ({isCollapsed: false, rangeCount: 1, getRangeAt: () => ({commonAncestorContainer: text})});
poll(p); response.body = 'new'; parsed = [turn('a1', 'changed'), turn('a2', 'new')];
await p._tick();
assert.equal(p.children[0], selected, 'the selected message stays as it is');
assert.equal(p.children[1].dataset.k, 'a2', 'the rest is refreshed');
assert.equal(p.dataset.last, undefined, 'and the next refresh brings it, once the selection lets go');
window.getSelection = () => null; parsed = [turn('a1', 'changed'), turn('a2', 'new')];
await p._tick();
assert.notEqual(p.children[0], selected); assert.equal(p.dataset.last, 'new');
''')

    def test_the_live_activity_keeps_its_place_and_follows_its_end(self):
        """Grok review of 8735c62 (S2): the reply's activity, polled on its own, came back without a data-k, so morph
        replaced it whole each second and its log jumped to the top. Keyed, it keeps a reader who scrolled up where
        they are, and one at the end follows the new lines."""
        from supervisor import web_views
        chat = {"state": "replying", "reply": {"activity": ["one", "two"]}}
        html = str(web_views.live_activity(chat))
        self.assertRegex(html, r'^<details class="activity" data-k="[^"]+" data-key="live"')
        self.run_js(r'''
const p = panel(); p.dataset.keyed = '1';
function act(version, top, height) { const n = new Node('act', 'activity', version);
  const box = part('scroll', {scrollKey: 'live'}); box.scrollTop = top; box.clientHeight = 100; box.scrollHeight = height;
  n.parts = [box]; return [n, box]; }
const [first, up] = act('v1', 50, 400); first._src = first.outerHTML; p.appendChild(first);
let [next, box] = act('v2', 0, 420); parsed = [next]; morph(p, 'v2');
assert.equal(p.children[0], next); assert.equal(box.scrollTop, 50, 'scrolled up: stays');
box.scrollTop = 320; let [last, end] = act('v3', 0, 460); parsed = [last]; morph(p, 'v3');
assert.equal(end.scrollTop, 460, 'at the end: follows');
''')

    def test_the_end_of_a_reply_keeps_its_activity_and_brings_the_answer_at_once(self):
        """Grok review of 8735c62 (S3): once the reply ended, /live answered nothing while the feed still showed the
        live turn: its activity blanked for up to 1.5 s. It answers the finished reply's activity, marked ended, and
        that asks the feed for the answer at once."""
        from supervisor import web_views
        chat = {"state": "idle", "turns": [{"role": "user", "content": "q"},
                                            {"role": "assistant", "content": "a", "activity": ["one", "two"]}]}
        html = str(web_views.live_activity(chat))
        self.assertIn("<p>two</p>", html)
        self.assertIn("data-ended", html)
        self.assertEqual(str(web_views.live_activity({"state": "idle", "turns": []})), "")
        self.run_js(r'''
const feed = panel(); let asked = 0; feed._tick = () => { asked++; };
const live = panel(); live.feed = feed; let ended = false;
live.rebuild = () => { live.parts = ended ? [part('ended', {ended: ''})] : []; };
live.querySelector = s => s === '[data-ended]' ? live.parts.find(n => n.kind === 'ended') || null : null;
poll(live); response.body = 'running'; await live._tick(); assert.equal(asked, 0);
ended = true; response.body = 'ended'; await live._tick(); assert.equal(asked, 1);
''')

    def test_issue_27_manage_collapse_and_link_focus_survive(self):
        """Reverting #27's rendered Manage key reopens the collapsed menu during a rail refresh."""
        rail = str(web_pages.rail(["wren"]))
        self.run_js('const railHTML = ' + json.dumps(rail) + r''';
const key = /<details class="manage"[^>]*data-key="([^"]+)"/.exec(railHTML)?.[1];
const p = panel(); let menu, link;
p.rebuild = () => {menu = part('details', {key}); menu.open = true;
  link = part('control', {}); link.href = '/i/wren/chat'; p.parts = [menu, link];};
p.rebuild(); menu.open = false; link.focus(); poll(p); await p._tick();
assert.equal(p.innerHTML, 'new'); assert.equal(menu.open, false); assert.equal(document.activeElement, link);
''')

    def test_issue_27_save_focus_survives_document_refresh(self):
        """Reverting #27's Edit focus key loses focus when Save refreshes the document panel."""
        html = str(web_views.docs_panel("wren", [], "note.md", "hello", "sha", set()))
        self.run_js('const docHTML = ' + json.dumps(html) + r''';
const key = /data-edit-doc data-focus="([^"]+)"/.exec(docHTML)?.[1];
const p = panel(); ids.docs = p; let edit;
p.rebuild = () => {edit = part('control', {focus: key}); p.parts = [edit];}; p.rebuild(); poll(p);
const view = new Node();
view.querySelector = s => s === '[data-edit-doc]' ? edit : s === 'textarea.raw' ? {value: ''} : {};
// the editor docSaved reads since #5/#22: the saved revision, the draft note, the box (unchanged during Save)
const fields = {'input[name=sha]': {value: 'old'}, '[data-doc-draft]': {textContent: ''},
  'textarea': {value: 'hello'}, 'input[name=path]': {value: 'note.md'}};
const form = {closest: () => view, dataset: {post: '/docs/wren/save'}, querySelector: s => fields[s] || {}};
// Wait for the actual poll started by Save.
const tick = p._tick; let refreshed; p._tick = () => refreshed = tick();
docSaved(form, null, 'hello', 'new-sha'); await refreshed;
assert.equal(p.innerHTML, 'new'); assert.equal(document.activeElement, edit); assert.notEqual(document.activeElement, document.body);
''')

    def test_issue_27_images_follow_only_while_at_bottom(self):
        """Reverting #27 leaves the latest message out of view after a late image load."""
        self.run_js(r'''
const p = panel(), box = new Node(), img = new Node();
p.box = box; p.dataset.follow = '1'; img.tag = 'img'; img.feed = p;
box.scrollBox = true; box.clientHeight = 300; box.scrollHeight = 1000;
box.querySelector = () => p;
toBottom(p); box.scrollHeight = 1200;
for (const f of handlers.load || []) f({target: img}); assert.equal(box.scrollTop, 1200);
for (const f of handlers.wheel || []) f({target: {closest: () => box}});   // the reader scrolls up (I8: only that stops it)
box.scrollTop = 100;
for (const f of handlers.scroll || []) f({target: box});
box.scrollHeight = 1400;
for (const f of handlers.load || []) f({target: img}); assert.equal(box.scrollTop, 100);
''')

    def test_issue_27_both_thumbnail_sources_reserve_dimensions(self):
        """Reverting #27 removes the space reserved before chat and sent-message images load."""
        from html.parser import HTMLParser

        class Images(HTMLParser):
            def __init__(self):
                super().__init__()
                self.images = []

            def handle_starttag(self, tag, attrs):
                if tag == "img":
                    self.images.append(dict(attrs))

        chat = {"id": "c1", "state": "idle", "turns": [
            {"role": "user", "content": "photo", "ts": "2026-10-20T10:00", "attachments": [{"name": "a.png"}]}]}
        parser = Images()
        parser.feed(str(web_views.chat_feed("wren", chat, [
            {"file": "f", "ts": "2026-10-20T10:01", "text": "photo", "attachments": [{"name": "b.png"}]}])))
        self.assertEqual([(i.get("width"), i.get("height")) for i in parser.images], [("192", "144"), ("192", "144")])


if __name__ == "__main__":
    unittest.main()
