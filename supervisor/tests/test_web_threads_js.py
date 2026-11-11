"""Topic threads in the shipped web.js, run in Node: New thread opens the thread the server made, Rename closes its menu,
From's composer only ever sends for the next cycle, a document saved beside a thread names that thread, and a thread's
page (?id=) or From says when it goes away, so a reply there is pushed to this device again."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor.tests.node_helpers import NODE_TIMEOUT_S
from supervisor.tests.test_web_review_js import JS, function

NODE = shutil.which("node")
WEB_JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"

HARNESS = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {}, sent = [];
let reloads = 0;
const location = { href: "https://shaft.test/i/wisp/chat", reload() { reloads++; } };
const cidField = { value: process.argv[2] === "thread" ? "web-a" : "" };
const document = {
  readyState: "loading", visibilityState: "visible", title: "",
  body: { scrollHeight: 0, appendChild() {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false } },
  documentElement: { style: { setProperty() {} } },
  addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
  dispatchEvent(ev) { (handlers[ev.type] || []).forEach(fn => fn(ev)); },
  querySelector: q => q === "form.composer input[name=cid]" && cidField.value ? cidField : null,
  querySelectorAll: () => [],
  getElementById: () => ({ set textContent(v) {}, setAttribute() {}, classList: { add() {}, remove() {} } }),
  createElement: () => ({ className: "", style: {}, setAttribute() {}, appendChild() {}, classList: { add() {} } }),
};
class DOMParser {
  parseFromString(html) {
    const m = /data-cid="([^"]+)"><a href="([^"]+)"/.exec(html);
    return { body: { textContent: html.replace(/<[^>]*>/g, ""), childNodes: [] },
             querySelector: q => q === "[data-cid] a[href]" && m ? { getAttribute: () => m[2] } : null };
  }
}
class FormData {
  constructor(form) { this.map = new Map(Object.entries(form.fields())); }
  has(k) { return this.map.has(k); } set(k, v) { this.map.set(k, v); } get(k) { return this.map.get(k); }
}
class URLSearchParams { constructor(fd) { this.fd = fd; } }
const sandbox = {
  document, location, DOMParser, FormData, URLSearchParams, URL, console,
  window: { addEventListener() {}, confirm: () => true, scrollTo() {}, scrollY: 0, innerHeight: 800, innerWidth: 1200,
            getSelection: () => ({ removeAllRanges() {} }) },
  navigator: { onLine: true }, AbortController: class { constructor() { this.signal = {}; } abort() {} },
  Event: class { constructor(type) { this.type = type; } }, CustomEvent: class { constructor(type) { this.type = type; } },
  setTimeout: (f, ms) => { if (ms === 300) f(); return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {}, crypto: { randomUUID: () => "mid-1" },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async (url, opts) => { sent.push([url, opts.body.fd || opts.body]); return { ok: true, status: 200,
    headers: { get: () => null }, text: async () => answer }; },
};
sandbox.globalThis = sandbox;
let answer = "<p>done</p>";
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
function formOf(post, after, inputs = {}, details = null) {
  return { dataset: { post, ...(after ? { after } : {}) },
    querySelector: sel => inputs[sel] || null, querySelectorAll: () => [],
    closest: q => q === "details" ? details : null,
    fields() { return Object.fromEntries(Object.entries(inputs).filter(([k]) => k.startsWith("input[name="))
      .map(([k, v]) => [k.slice(11, -1), v.value])); } };
}
async function submit(form, submitter = null) {
  const ev = { target: { closest: sel => sel === "form[data-post]" ? form : null }, preventDefault() {}, submitter };
  for (const fn of handlers.submit) await fn(ev);
}
(async () => {
  // New thread: the answer names the thread; the page goes there
  answer = '<p data-cid="web-new"><a href="/i/wisp/chat?id=web-new">New thread</a></p>';
  await submit(formOf("/chat/wisp/new", "open-thread"));
  assert.equal(location.href, "/i/wisp/chat?id=web-new");
  // Rename: its menu closes once saved
  answer = "<p>thread renamed</p>";
  const menu = { open: true };
  await submit(formOf("/chat/wisp/rename", "close", { "input[name=cid]": { value: "web-a" } }, menu));
  assert.equal(menu.open, false);
  // From's composer: Enter (no submitter) still sends for the next cycle
  const bg = { value: "1", dataset: { always: "1" } };
  await submit(formOf("/chat/wisp/send", "", { "input[name=background]": bg }));
  assert.equal(sent.at(-1)[1].get("background"), "1", "From sends only for the next cycle");
  // the chat's composer: Enter is not Next cycle
  const chatBg = { value: "", dataset: {} };
  await submit(formOf("/chat/wisp/send", "", { "input[name=background]": chatBg }));
  assert.equal(sent.at(-1)[1].get("background"), "", "a chat's Enter sends now");
  // a document saved beside a thread names it; on the plain page, none (the server takes the current thread)
  await submit(formOf("/docs/wisp/save", "", { "input[name=path]": { value: "a.md" } }));
  assert.equal(sent.at(-1)[1].get("cid"), process.argv[2] === "thread" ? "web-a" : undefined);
  // End and Clear on a thread's own page, which cannot write to it once ended; the plain page follows by itself
  const thread = process.argv[2] === "thread";
  location.href = "here";
  await submit(formOf("/action/chat-end", "thread-ended", { "input[name=cid]": { value: "web-a" } }));
  assert.equal(reloads, thread ? 1 : 0, "End reloads a thread's page (it shows as an earlier chat)");
  await submit(formOf("/chat/wisp/clear", "thread-cleared", { "input[name=cid]": { value: "web-a" } }));
  assert.equal(location.href, thread ? "/i/wisp/chat" : "here", "Clear goes to the current thread");
  console.log("ok");
})().catch(e => { console.error(e); process.exitCode = 1; });
"""


@unittest.skipUnless(NODE, "node is not installed")
class ThreadsFormsJsTest(unittest.TestCase):
    def run_page(self, page: str) -> None:
        result = subprocess.run([NODE, "-e", HARNESS, str(WEB_JS), page], capture_output=True, text=True,
                                timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("ok", result.stdout)

    def test_on_a_threads_page(self):
        self.run_page("thread")

    def test_on_the_plain_chat_page(self):
        self.run_page("plain")


@unittest.skipUnless(NODE, "node is not installed")
class AwayFromAThreadTest(unittest.TestCase):
    def test_a_threads_page_and_from_say_they_went_away(self):
        """away() returned on any feed polled with ?id= (before threads that was only a past chat's page): a thread's
        own page never said it went away, so a reply landing there was not pushed to this device for 6 s."""
        script = r"""
const sent = [];
let poll;
const document = { visibilityState: "hidden",
  querySelector: q => q === ".feed[data-follow][data-poll]" ? { dataset: { poll } } : null };
const Date = { now: () => 1234 };
const fetch = (u, o) => { sent.push(u); return Promise.resolve(); };
""" + function("away") + r"""
for (poll of ["/fragment/chat/wisp?id=web-a", "/fragment/from/wisp"]) away({ type: "pagehide" });
console.log(JSON.stringify(sent));
"""
        out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), ["/fragment/chat/wisp?id=web-a&away=1&at=1234",
                                                  "/fragment/from/wisp?away=1&at=1234"])


@unittest.skipUnless(NODE, "node is not installed")
class SeenMarksTest(unittest.TestCase):
    def test_legacy_seen_marks_are_sent_to_each_known_conversation(self):
        """A legacy instance mark migrates to each known conversation before browser storage is removed."""
        script = r'''
const assert = require("node:assert/strict");
let seenMigrating = false, removed = false; const posted = [];
const localStorage = {getItem: () => JSON.stringify({example:"2026-10-20T10:00:00"}), removeItem: () => {removed=true;}};
const rail = {querySelectorAll: () => [{dataset:{inst:"example",lasts:JSON.stringify({"web-a":"",from:""})}}]};
const sendSeen = async mark => { posted.push(mark); return true; };
''' + re.search(r'^  async function migrateSeen\(.*?^  \}\n', JS, re.S | re.M)[0] + r'''
(async () => {await migrateSeen(rail);
assert.deepEqual(posted.map(m => [m.inst,m.key,m.last]), [
  ["example","web-a","2026-10-20T10:00:00"], ["example","from","2026-10-20T10:00:00"]]);
assert.equal(removed,true); })().catch(e => {console.error(e);process.exitCode=1;});
'''
        out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(out.returncode, 0, out.stderr)
