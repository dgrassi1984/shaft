"""The notify button in web.js, run in Node against a stub page (docs/plans/PLAN-pwa P4, issue #23).

"Notifications on" has to mean SHAFT accepted this device. A 409 from /push/subscribe used to leave the button on,
because the click handler ignored that answer and the button only asked the browser.
"""
from pathlib import Path
import shutil
import subprocess
import unittest


NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"

# mode "refuse": the browser subscribes, SHAFT answers 409. mode "visit": the browser already has a subscription
# and /push/status says whether SHAFT still holds it (held flips after the first check).
HARNESS = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const mode = process.argv[1];
const handlers = {}, queries = {}, els = [];
const box = { sub: null };
const button = { dataset: {}, hidden: false, disabled: false, textContent: "" };
queries['meta[name="csrf"]'] = { content: "tok" };
queries["[data-notify]"] = button;
const document = {
  readyState: "loading",
  body: { appendChild() {}, classList: { contains: () => false, add() {}, remove() {}, toggle() {} } },
  addEventListener(k, fn) { (handlers[k] ||= []).push(fn); },
  querySelector: (q) => queries[q] || null,
  querySelectorAll: () => [],
  getElementById: (id) => els.find((e) => e.id === id) || null,
  createElement() {
    const el = { id: "", className: "", textContent: "", setAttribute() {}, appendChild() {} };
    els.push(el);
    return el;
  },
};
function DOMParser() {}
DOMParser.prototype.parseFromString = (html) => ({
  body: { textContent: String(html).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim() },
});
const pushManager = {
  async getSubscription() { return box.sub; },
  async subscribe() {
    box.sub = { endpoint: "https://web.push.apple.com/test", async unsubscribe() { box.sub = null; return true; } };
    return box.sub;
  },
};
const Notification = {
  permission: "granted",
  async requestPermission() { return "granted"; },
};
let held = false;
const calls = [];
async function fetch(url) {
  calls.push(String(url));
  if (url === "/push/key") return { ok: true, json: async () => ({ key: "AQID" }) };
  if (url === "/push/status") return { ok: true, json: async () => ({ subscribed: held }) };
  if (url === "/push/subscribe") {
    return { ok: false, status: 409, text: async () => "<p>refused: not a known push service</p>" };
  }
  throw new Error("unexpected " + url);
}
const window = { addEventListener() {}, PushManager: function PushManager() {}, Notification };
const context = {
  document, Notification, DOMParser, fetch, atob: (s) => Buffer.from(s, "base64").toString("binary"),
  setTimeout() { return 0; }, clearTimeout() {}, setInterval() { return 0; }, clearInterval() {},
  navigator: { serviceWorker: { ready: Promise.resolve({ pushManager }), register: () => Promise.resolve() } },
  window,
};
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
const toast = () => (els.find((e) => e.id === "toast") || {}).textContent || "";
(async () => {
  if (mode === "refuse") {
    const target = { closest: (q) => (q === "[data-notify]" ? button : null) };
    for (const fn of handlers.click) await fn({ target, preventDefault() {} });
    assert.equal(button.dataset.state, "off");
    assert.equal(button.textContent, "Notify me on this device");
    assert.equal(box.sub, null);                       // the browser subscription a refusal left behind is gone
    assert.match(toast(), /refused/);                  // the reason is said, not only the button
    assert.ok(calls.includes("/push/subscribe"));
  } else {
    box.sub = { endpoint: "https://web.push.apple.com/test", async unsubscribe() { box.sub = null; } };
    held = false;
    for (const fn of handlers.DOMContentLoaded) fn();
    for (let i = 0; i < 30; i++) await Promise.resolve();
    assert.equal(button.dataset.state, "incomplete");  // the browser has one; SHAFT does not
    assert.equal(button.textContent, "Notifications incomplete — retry");
    assert.ok(calls.includes("/push/status"));
    held = true;
    for (const fn of handlers.DOMContentLoaded) fn();
    for (let i = 0; i < 30; i++) await Promise.resolve();
    assert.equal(button.dataset.state, "on");
    assert.equal(button.textContent, "Notifications on — turn off");
  }
})().catch((e) => { console.error(e); process.exitCode = 1; });
"""


@unittest.skipUnless(NODE, "node is not installed")
class NotifyButtonJsTest(unittest.TestCase):
    def run_mode(self, mode: str) -> None:
        result = subprocess.run([NODE, "-e", HARNESS, mode, str(JS)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_issue_23_a_refused_subscribe_leaves_the_button_off(self):
        """#23: a 409 from /push/subscribe leaves the button off and says why. Ignoring post()'s result, the old
        handler, leaves the browser subscribed and the button reading Notifications on."""
        self.run_mode("refuse")

    def test_issue_23_a_later_visit_is_not_on_unless_shaft_holds_it(self):
        """#23: a later visit asks SHAFT, and says incomplete when SHAFT has dropped this device. pushState reading
        only pushManager.getSubscription() says Notifications on for that visit."""
        self.run_mode("visit")


if __name__ == "__main__":
    unittest.main()
