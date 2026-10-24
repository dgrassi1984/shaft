"""Opening a chat starts at the first reply or message this browser has not seen, not at the bottom."""
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
// Each feed item is 100px tall, in document order; the scroll box shows 150px of it.
const box = { scrollTop: 0, clientHeight: input.height || 150, scrollHeight: 0,
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
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, navigator: {},
  window: { addEventListener() {}, innerHeight: 0, scrollY: 0, scrollTo() {}, getSelection: () => ({ isCollapsed: true }) },
  localStorage: { getItem: k => k === "shaft.seen" ? stored : null, setItem(k, v) { if (k === "shaft.seen") stored = v; } },
  setTimeout: () => 1, clearTimeout() {}, setInterval() {}, AbortController,
  CustomEvent: class { constructor(type) { this.type = type; } }, fetch: () => new Promise(() => {}) });
for (const f of handlers.DOMContentLoaded || []) f();
const at = input.expect === "bottom" ? box.scrollHeight : items.find(n => n.key === input.expect).top - 8;
assert.equal(box.scrollTop, at, `opened at ${input.expect}`);
assert.equal(newbelow.hidden, input.expect === "bottom", "New below offered when it did not open at the end");
if (input.seen !== null && !input.archived) assert.equal(JSON.parse(stored).wren, input.after, "what is on screen is seen");
'''


@unittest.skipUnless(NODE, "node is not installed")
class OpenAtFirstUnreadTest(unittest.TestCase):
    def opens(self, seen, expect, archived=False, after=LATEST, height=150):
        result = subprocess.run([NODE, "-e", HARNESS, str(JS), json.dumps({
            "html": feed(archived), "seen": seen, "expect": expect, "archived": archived, "after": after,
            "height": height})],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_opens_at_the_first_message_not_seen_here(self):
        """Reverting this opens every chat at the bottom, past the message the instance sent after the seen reply."""
        self.opens(SEEN, f"sm9.md")

    def test_w8_only_what_is_on_screen_is_seen_and_a_message_it_sent_counts(self):
        """W8: seenFeed() marked every rendered reply seen — one below the fold too — and never a message the instance
        sent (data-sent-at). Opened at its message, with the newest reply below the fold: only the message is seen."""
        self.opens(SEEN, "sm9.md", after=SENT, height=100)

    def test_opens_at_the_newest_reply_when_only_it_is_unread(self):
        self.opens("2026-10-21T10:10:00", "a2")

    def test_opens_at_the_end_when_everything_was_seen_or_nothing_ever_was(self):
        self.opens(LATEST, "bottom")
        self.opens(None, "bottom")          # a new browser: no mark, so no guess that the whole chat is unread

    def test_an_earlier_chat_opens_at_the_end(self):
        """An archived chat carries no seen marks: it never jumps, and it does not mark the current chat seen."""
        self.opens(SEEN, "bottom", archived=True)

    def test_sent_cards_carry_their_time_only_in_the_current_chat(self):
        self.assertIn(f'data-seen-inst="wren" data-sent-at="{SENT}"', feed())
        self.assertNotIn("data-sent-at", feed(archived=True))
