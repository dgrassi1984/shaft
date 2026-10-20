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
const net = { down: 0 };
const busyIn = () => false, fresh = () => {}, stale = () => {}, netNote = () => {}, checkVersion = () => {};
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

    def test_a_refresh_asked_while_a_poll_is_on_its_way_shows_the_new_state_at_once(self):
        out = self.run_poll()
        self.assertEqual(out["final"], "NEW", out)
        self.assertLess(out["ms"], 1000, out)                 # was ~2 s: the next ordinary poll
        self.assertEqual(out["shown"], ["START", "NEW"], out)  # the in-flight answer (OLD, from before) never shown


if __name__ == "__main__":
    unittest.main()
