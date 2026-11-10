"""Lost answers and the Drive diff, run from the shipped web.js in Node (#20, #33).

#20: after no answer, only a chat send (input[name=mid]) keeps "sending it again is safe". Every other form says the
outcome is unknown and names the page to check, and it waits data-timeout seconds — the server's limit — not 30s.
#33: Show the diff and save shows the diff with Apply and Back. It does not call confirm, and nothing is saved until
Apply.
"""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor.tests.node_helpers import NODE_TIMEOUT_S

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"

HARNESS = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const handlers = {};
const toasts = [];
const confirms = [];
const timeouts = [];
const toastBox = {
  className: "",
  set textContent(v) { this._text = v; toasts.push(v); },
  get textContent() { return this._text || ""; },
  setAttribute() {},
};
const document = {
  readyState: "loading",
  visibilityState: "visible",
  title: "",
  body: { scrollHeight: 0, appendChild() {}, classList: { add() {}, remove() {}, toggle() {}, contains: () => false } },
  documentElement: { style: { setProperty() {} } },
  addEventListener(type, fn) { (handlers[type] ||= []).push(fn); },
  dispatchEvent(ev) { (handlers[ev.type] || []).forEach(fn => fn(ev)); },
  querySelector: () => null,
  querySelectorAll: () => [],
  getElementById: (id) => id === "toast" ? toastBox : null,
  createElement: () => ({ className: "", style: {}, setAttribute() {}, appendChild() {}, classList: { add() {} } }),
};
const window = {
  addEventListener() {},
  confirm(text) { confirms.push(text); return false; },
  scrollTo() {}, scrollY: 0, innerHeight: 800, innerWidth: 1200,
  getSelection: () => ({ removeAllRanges() {} }),
};
class DOMParser {
  parseFromString(html) {
    const text = String(html).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
    return { body: { textContent: text, childNodes: [] } };
  }
}
class FormData { constructor(form) { this.form = form; } }
class URLSearchParams { constructor(body) { this.body = body; } toString() { return "body"; } }
class AbortController { constructor() { this.signal = {}; } abort() {} }
class Event { constructor(type) { this.type = type; } }
const sandbox = {
  document, window, navigator: { onLine: true }, location: { href: "https://shaft.test/", reload() {} },
  DOMParser, FormData, URLSearchParams, AbortController, Event, URL,
  setTimeout(fn, ms) { timeouts.push(ms); return timeouts.length; },
  clearTimeout() {},
  setInterval() {},
  clearInterval() {},
  crypto: { randomUUID: () => "mid-1" },
  console,
};
sandbox.globalThis = sandbox;
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
// web.js runs in the sandbox, so a case sets this fetch, not the outer one.
function formOf(opts) {
  const midInput = { value: "" };
  const dataset = { post: opts.post || "/action/service" };
  if (opts.check) dataset.check = opts.check;
  if (opts.timeout) dataset.timeout = String(opts.timeout);
  if (opts.multipart) dataset.multipart = "1";
  if (opts.diff) dataset.diff = opts.diff;
  if (opts.confirm) dataset.confirm = opts.confirm;
  return {
    dataset, midInput,
    querySelector(sel) {
      if (sel === "input[name=mid]") return opts.mid ? midInput : null;
      if (sel === "button[type=submit]") return { disabled: false };
      return null;
    },
    querySelectorAll: () => [],
    closest: () => null,
  };
}
async function submit(form, submitter) {
  const before = timeouts.length;
  const ev = {
    target: { closest: (sel) => sel === "form[data-post]" ? form : null },
    preventDefault() {},
    submitter: submitter || null,
  };
  for (const fn of handlers.submit) await fn(ev);
  return timeouts.slice(before);
}
//CASE//
"""


@unittest.skipUnless(NODE, "node is not installed")
class FormJsTest(unittest.TestCase):
    def run_js(self, body: str) -> subprocess.CompletedProcess:
        script = HARNESS.replace("//CASE//", body)
        return subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=NODE_TIMEOUT_S)

    def test_w17_cancelling_a_queued_message_never_says_it_is_safe_to_repeat(self):
        """W17: the queued message's Cancel carries the message's id (input[name=mid]), so a lost answer took it for a
        chat send: "sending it again is safe", for an action that may have happened. Only the composer's send is."""
        result = self.run_js(r"""
sandbox.fetch = async () => { const e = new Error("The operation was aborted"); e.name = "AbortError"; throw e; };
(async () => {
  const cancel = formOf({ mid: true, check: "the chat (is it still queued?)", post: "/chat/wisp/unqueue" });
  cancel.midInput.value = "q-1";
  await submit(cancel);
  assert.match(toasts.at(-1), /it may still have happened — check the chat \(is it still queued\?\)/);
  assert.doesNotMatch(toasts.at(-1), /safe/);
  assert.equal(cancel.midInput.value, "q-1");                        // its id is the queued message's, kept
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_20_a_lost_answer_is_safe_to_repeat_only_for_a_chat_send(self):
        """#20: every form used to say "sending it again is safe" or "Not sent" when the answer was lost. That is true
        only for a chat send, which carries input[name=mid]. Reverting lostAnswer to the one message fails this:
        a service restart or a trial would invite a second run."""
        result = self.run_js(r"""
sandbox.fetch = async () => { const e = new Error("The operation was aborted"); e.name = "AbortError"; throw e; };
(async () => {
  const chat = formOf({ mid: true, multipart: true, post: "/chat/wisp/send" });
  await submit(chat);
  assert.equal(toasts.at(-1), "No answer: sending it again is safe (it is not sent twice)");
  assert.equal(chat.midInput.value, "mid-1");          // the id is filled before the send, so a retry is the same one
  toasts.length = 0;
  sandbox.fetch = async () => { const e = new Error("Failed to fetch"); e.name = "TypeError"; throw e; };
  await submit(chat);
  assert.match(toasts.at(-1), /Not sent: Failed to fetch — sending it again is safe/);

  toasts.length = 0;
  sandbox.fetch = async () => { const e = new Error("The operation was aborted"); e.name = "AbortError"; throw e; };
  const service = formOf({ check: "Services", timeout: 105, post: "/action/service" });
  const waited = await submit(service);
  assert.equal(waited[0], 105000);                     // data-timeout seconds, not the old 30s
  assert.equal(toasts.at(-1),
    "No answer within 105s: it may still have happened — check Services before trying again");
  assert.doesNotMatch(toasts.at(-1), /Not sent|sending it again is safe/);

  toasts.length = 0;
  sandbox.fetch = async () => { const e = new Error("Failed to fetch"); e.name = "TypeError"; throw e; };
  const trial = formOf({ check: "Trials", timeout: 75, post: "/action/aa-trial" });
  await submit(trial);
  assert.equal(toasts.at(-1),
    "No answer (Failed to fetch): it may still have happened — check Trials before trying again");
  assert.doesNotMatch(toasts.at(-1), /Not sent|sending it again is safe/);
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_20_the_wait_comes_from_data_timeout(self):
        """#20: the browser aborted every plain form at 30s, including a service action the server allows 90s.
        data-timeout is that limit in seconds. Reverting waitMsFor to the fixed 30s/180s fails this."""
        result = self.run_js(r"""
sandbox.fetch = async () => { const e = new Error("aborted"); e.name = "AbortError"; throw e; };
(async () => {
  const plain = await submit(formOf({ check: "Overview" }));
  assert.equal(plain[0], 30000);
  assert.match(toasts.at(-1), /within 30s/);
  const files = await submit(formOf({ multipart: true, post: "/chat/wisp/send", mid: true }));
  assert.equal(files[0], 180000);                       // a chat with files keeps its longer wait
  const over = await submit(formOf({ multipart: true, timeout: 10, check: "Services" }));
  assert.equal(over[0], 10000);                         // a stated data-timeout wins over either default
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_33_the_diff_is_shown_before_apply_and_confirm_is_not_used(self):
        """#33: the diff was fetched and then window.confirm ran on top of it, so a long diff could not be read, and
        accepting saved at once. Reverting to confirm() after the fetch, or making the first press save, fails this."""
        result = self.run_js(r"""
const calls = [];
sandbox.fetch = async (url) => {
  calls.push(String(url));
  if (String(url).includes("/preview/")) {
    return { ok: true, status: 200, headers: { get: () => "" }, text: async () => "<pre>+a new line</pre>" };
  }
  return { ok: true, status: 200, headers: { get: () => "" }, text: async () => "<p>saved</p>" };
};
const out = { textContent: "" };
const step = { hidden: true };
const ask = { hidden: false };
const showBtn = { disabled: false, hasAttribute: () => false };
const applyBtn = { disabled: false, hasAttribute: (name) => name === "data-diff-apply" };
const backBtn = { hasAttribute: (name) => name === "data-diff-back" };
const box = { name: "text", tagName: "TEXTAREA", value: "the new drive" };
const form = {
  dataset: { post: "/action/drive-set", diff: "/preview/drive/wisp", confirm: "Replace the drive?" },
  querySelector(sel) {
    return ({
      "[data-diff-out]": out, "[data-diff-step]": step, "[data-diff-ask]": ask,
      "[data-diff-ask] button": showBtn, "button[type=submit]": showBtn,
      "textarea[name=text]": box,
    })[sel] || null;
  },
  querySelectorAll: () => [],
  closest: () => null,
};
backBtn.closest = (sel) => (sel === "form" || sel === "form[data-post]") ? form : null;
box.closest = (sel) => sel === "form[data-diff]" ? form : null;
(async () => {
  await submit(form, showBtn);
  assert.deepEqual(confirms, []);                      // the diff is not covered by a dialog
  assert.deepEqual(calls, ["/preview/drive/wisp"]);    // nothing saved
  assert.equal(out.textContent, "+a new line");
  assert.equal(step.hidden, false);
  assert.equal(ask.hidden, true);
  assert.equal(showBtn.disabled, true);
  assert.equal(box.value, "the new drive");

  for (const fn of handlers.click) await fn({ target: { closest: (sel) => sel === "[data-diff-back]" ? backBtn : null } });
  assert.equal(step.hidden, true);                     // Back
  assert.equal(out.textContent, "");
  assert.equal(box.value, "the new drive");            // the text is kept
  assert.deepEqual(calls, ["/preview/drive/wisp"]);
  assert.deepEqual(confirms, []);

  await submit(form, showBtn);
  assert.equal(step.hidden, false);
  for (const fn of handlers.input) {
    await fn({ target: box });                         // editing dismisses the diff; Apply cannot save a different text
  }
  assert.equal(step.hidden, true);
  assert.equal(box.value, "the new drive");

  calls.length = 0;
  await submit(form, showBtn);
  await submit(form, applyBtn);
  assert.deepEqual(confirms, []);                      // Apply does not ask either
  assert.deepEqual(calls, ["/preview/drive/wisp", "/action/drive-set"]);
  assert.equal(step.hidden, true);                     // saved: back to the text, still kept
  assert.equal(box.value, "the new drive");
})().catch(e => { console.error(e); process.exitCode = 1; });
""")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()


class AfterHandlersTest(unittest.TestCase):
    def test_every_data_after_a_page_uses_has_a_handler_in_web_js(self):
        """#17 removed web.js's data-after="reload" branch with Settings' last use of it, but the Unpair form still
        declares it: unpairing then stayed on a page it could no longer use. A data-after value without a handler does
        nothing, silently."""
        root = Path(__file__).resolve().parents[1]
        used = set()
        for py in root.glob("*.py"):
            used |= set(re.findall(r'data-after=\\?"([a-z-]+)', py.read_text()))
        js = (root / "web_static" / "web.js").read_text()
        handled = set(re.findall(r'dataset\.after === "([a-z-]+)"', js))
        self.assertIn("reload", used)                      # the Unpair form: the case that broke
        self.assertEqual(used - handled, set())
