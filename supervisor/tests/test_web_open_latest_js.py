"""Opening a chat lands on its last message, and stays there while what is below the jump grows.

The operator, 2026-10-26: clicking an instance must go to the last message. It opened at the first unread one instead
(a new browser or one with nothing unread went to the end), and nothing kept it at the end once there: the draft growing
the composer, the documents sheet or an image loading after the jump left the last message below the fold."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor import web_views

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"
SEEN, ASKED, SENT, LATEST = "2026-10-21T09:00:00", "2026-10-21T10:00:00", "2026-10-21T10:05:00", "2026-10-21T10:30:00"


def feed(archived=False):
    """A reply already seen, a question, a message the instance sent meanwhile, and the newest reply."""
    chat = {"id": "old" if archived else "current", "state": "ended" if archived else "idle", "ended": LATEST,
            "turns": [{"role": "assistant", "content": "Seen answer", "ts": SEEN},
                      {"role": "user", "content": "and then?", "ts": ASKED},
                      {"role": "assistant", "content": "Newest answer", "ts": LATEST}]}
    sent = [{"ts": SENT, "file": "m9.md", "text": "a note meanwhile", "kind": "note", "read": False}]
    return str(web_views.chat_feed("wren", chat, sent, archived=archived))


HARNESS = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const input = JSON.parse(process.argv[2]), handlers = {};
let stored = input.seen === null ? null : JSON.stringify({wren: input.seen});
const classes = (...initial) => { const s = new Set(initial);
  return { contains: k => s.has(k), add: k => s.add(k), remove: k => s.delete(k), toggle(k, on) { on ? s.add(k) : s.delete(k); } }; };
// Each feed item is 100px tall, in document order; the scroll box shows `height` px of it (250 by default).
// scrollTop clamps to the end, as a browser's does
const box = { _top: 0, get scrollTop() { return this._top; },
  set scrollTop(v) { this._top = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight)); },
  clientHeight: input.height || 150, scrollHeight: 0,
  getBoundingClientRect() { return { top: 0, bottom: this.clientHeight }; } };
const items = [...input.html.matchAll(/<(?:div|p) class="[^"]*"([^>]*)>/g)].map(m =>
  Object.fromEntries([...m[1].matchAll(/data-([\w-]+)="([^"]*)"/g)].map(a => [a[1], a[2]]))).filter(a => a.k)
  .map((a, i) => ({ key: a.k, dataset: { seenInst: a["seen-inst"], seenReply: a["seen-reply"], sentAt: a["sent-at"] },
                    top: i * 100, getClientRects: () => [1], closest: q => q === "[data-scroll]" ? box : null,
                    getBoundingClientRect() { return { top: this.top - box.scrollTop, bottom: this.top + 100 - box.scrollTop }; } }));
box.scrollHeight = items.length * 100;
const feed = { dataset: { poll: "/fragment/chat/wren", every: "1500", follow: "1" }, isConnected: true,
  classList: classes("feed"), children: [], getClientRects: () => [1], closest: q => q === "[data-scroll]" ? box : null,
  querySelector: () => null,
  querySelectorAll: q => q === "[data-seen-inst]" ? items.filter(n => n.dataset.seenInst) : [],
  dispatchEvent() {} };
const newbelow = { hidden: true };
const document = { readyState: "loading", visibilityState: "visible", title: "wren · chat · SHAFT",
  body: { scrollHeight: 0, classList: classes(), appendChild() {} },
  addEventListener(k, f) { (handlers[k] ||= []).push(f); },
  querySelector: q => q === "[data-newbelow]" ? newbelow : null,
  querySelectorAll: q => q === "[data-poll]" || q === "[data-follow]" ? [feed] :
    q === ".feed [data-seen-reply], .feed [data-sent-at]" ? items.filter(n => n.dataset.seenReply || n.dataset.sentAt) :
    q === ".feed [data-seen-reply]" ? items.filter(n => n.dataset.seenReply) : [],
  getElementById: () => ({ setAttribute() {}, remove() {} }) };
const observers = [];
class ResizeObserver { constructor(f) { this.f = f; this.seen = []; observers.push(this); } observe(n) { this.seen.push(n); } }
let clock = 1e12;                                    // Date.now, moved on by hand: a gesture counts for 1.5 s
class FakeDate extends Date { static now() { return clock; } }
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, navigator: {}, ResizeObserver, Date: FakeDate,
  window: { addEventListener() {}, innerHeight: 0, scrollY: 0, scrollTo() {}, getSelection: () => ({ isCollapsed: true }) },
  localStorage: { getItem: k => k === "shaft.seen" ? stored : null, setItem(k, v) { if (k === "shaft.seen") stored = v; } },
  setTimeout: () => 1, clearTimeout() {}, setInterval() {}, AbortController,
  CustomEvent: class { constructor(type) { this.type = type; } }, fetch: () => new Promise(() => {}) });
for (const f of handlers.DOMContentLoaded || []) f();
const end = () => box.scrollHeight - box.clientHeight;
assert.equal(box.scrollTop, end(), "opened at the last message");
assert.equal(newbelow.hidden, true, "nothing new below the last message");
if (input.seen !== null && !input.archived) assert.equal(JSON.parse(stored).wren, input.after, "what is on screen is seen");
// Pinned: the feed's box and the feed itself are watched; growth after the jump goes back to the end ...
const pin = observers.find(o => o.seen.includes(box) && o.seen.includes(feed));
assert.ok(pin, "the feed and its box are watched for size changes");
box.matches = q => q === "[data-scroll]"; box.querySelector = () => feed;
const scrolled = () => { for (const f of handlers.scroll || []) f({ target: box }); };
const resized = () => { pin.f(); scrolled(); };    // the observer, then the scroll event its jump causes
// The composer grows with a restored draft right after the jump: the box is shorter, and the jump's own scroll event
// arrives after that — the end is out of view, but nobody scrolled up (live, 2026-10-26: 198 px short of the end).
box.clientHeight -= 100; scrolled(); resized();
assert.equal(box.scrollTop, end(), "the box got shorter after the jump: still at the last message");
box.scrollHeight += 400; resized();
assert.equal(box.scrollTop, end(), "content added after the jump: still at the last message");
// I8 (iPhone review 2026-10-27): the keyboard closing after Send moves the box up with no reader gesture; the feed
// kept following only if nobody scrolled — your own message waited under "New below". Not the reader: still followed.
box.scrollTop = end() - 300; scrolled();
box.scrollHeight += 400; resized();
assert.equal(box.scrollTop, end(), "moved up by a layout change, not the reader: still at the last message");
// ... unless the reader scrolled up: then it stays where they left it.
const read = () => { for (const f of handlers.wheel || []) f({ target: { closest: () => box } }); };
read(); box.scrollTop = end() - 20; scrolled();    // up a little (review, Codex 2026-10-26): within 40 px, still reading
box.scrollHeight += 400; resized();
assert.equal(box.scrollTop, end() - 400 - 20, "scrolled up a little: not pulled back down");
// Codex review: a scrollbar held down, then dragged after the 1.5 s a gesture counts for, still counts
box.scrollTop = end(); scrolled();                 // back at the end: following again
assert.equal(feed._atBottom, true);
// A tap (Retry) is not a scroll: the feed shortening after it moves the box up, and it still follows (found live)
clock += 5000;                                      // the wheel above is long over
for (const f of handlers.pointerdown || []) f({ target: { closest: () => box } });
for (const f of handlers.pointerup || []) f({});
box.scrollTop = end() - 300; scrolled();
box.scrollHeight += 400; resized();
assert.equal(box.scrollTop, end(), "a tap, then a layout change: still at the last message");
for (const f of handlers.pointerdown || []) f({ target: { closest: () => box } });
clock += 5000;                                      // held for 5 s, then dragged
box.scrollTop = 20; scrolled();
box.scrollHeight += 400; resized();
assert.equal(box.scrollTop, 20, "scrolled up: not pulled back down");
'''


@unittest.skipUnless(NODE, "node is not installed")
class OpenAtLastMessageTest(unittest.TestCase):
    def opens(self, seen, archived=False, after=LATEST, height=250):
        result = subprocess.run([NODE, "-e", HARNESS, str(JS), json.dumps({
            "html": feed(archived), "seen": seen, "archived": archived, "after": after, "height": height})],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_opens_at_the_last_message_even_with_unread_ones_above(self):
        """Regression (2026-10-26): with a seen mark older than the instance's message, the chat opened at that
        message, two turns above the end. It must open at the last message."""
        self.opens(SEEN)

    def test_opens_at_the_last_message_when_everything_was_seen_or_nothing_ever_was(self):
        self.opens(LATEST)
        self.opens(None)                    # a new browser: no mark

    def test_w8_a_reply_whose_start_is_above_the_fold_is_not_seen(self):
        """W8: seen means its start is on screen. A box too short for the newest reply's start above the actions row:
        opened at the end, nothing new is marked seen."""
        self.opens(SEEN, after=SEEN, height=150)

    def test_an_earlier_chat_opens_at_the_end(self):
        """An archived chat carries no seen marks, and it does not mark the current chat seen."""
        self.opens(SEEN, archived=True)
