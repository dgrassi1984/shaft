"""PWA market correctness regressions, running the shipped handlers in Node."""
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor import web_pages
from supervisor.tests.test_web_review_js import function

NODE = shutil.which("node")
STATIC = Path(__file__).resolve().parents[1] / "web_static"


@unittest.skipUnless(NODE, "node is not installed")
class MarketJsTest(unittest.TestCase):
    def node(self, script, source):
        result = subprocess.run([NODE, "-e", script, source], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(result.stdout.strip(), "ok", "the async assertions must finish")

    def test_b8_comment_keeps_text_typed_while_sending(self):
        """B8: sending a comment closed the editor over text typed during the request."""
        src = (STATIC / "comments.js").read_text()
        handler = src[src.index('    form.addEventListener("submit",'):src.index('    box = form;')]
        self.node(r'''
const assert = require('node:assert/strict');
(async () => {
for (const changed of [true, false]) {
  let submit, finish, closed = false, refreshed = 0;
  const textarea = {value: 'first version'}, save = {}, result = {}, fields = {pid: 'p-1'};
  const draftKey = 'draft', mode = 'new', name = 'wren', when = () => 'now';
  const storage = new Map(), localStorage = {getItem:k=>storage.get(k), removeItem:k=>storage.delete(k)};
  const draftValue = () => JSON.stringify({text:textarea.value});
  const form = {dataset:{}, addEventListener:(type, fn)=>{submit=fn;},
    querySelector:q=>q === 'button[type=submit]' ? save : result,
    _saveDraft:()=>storage.set(draftKey, draftValue())};
  let box = form;
  const close = () => {closed=true; box=null;};
  const window = {shaftWeb:{csrf:()=>'', pairAgain:()=>false, textOf:s=>s, toast(){}, refreshDocs:()=>refreshed++}};
  const fetch = async (url, opts) => {
    assert.equal(opts.body.get('text'), 'first version');
    await new Promise(resolve=>{finish=resolve;});
    return {ok:true, text:async()=> 'Sent'};
  };
  eval(process.argv[1]);
  const pending = submit({preventDefault(){}});
  assert.equal(save.disabled, true);
  if (changed) {textarea.value = 'later version'; form._saveDraft();}
  finish(); await pending;
  assert.equal(closed, !changed, 'later text must stay in the open editor');
  assert.equal(textarea.value, changed ? 'later version' : 'first version');
  if (changed) {
    assert.match(result.textContent, /earlier version.*sent/i);
    assert.equal(JSON.parse(storage.get(draftKey)).text, 'later version');
  } else assert.equal(storage.has(draftKey), false);
  assert.equal(save.disabled, false);
  assert.equal(refreshed, 1);
}
})().then(()=>console.log("ok")).catch(e=>{console.error(e);process.exitCode=1;});
''', handler)

    def test_m10_covered_feed_stays_unseen_and_does_not_watch(self):
        """M10: a sheet over the chat consumed unseen replies and polled as a push watcher."""
        self.node(r'''
const assert = require('node:assert/strict');
let sheet = {kind:'docs'}, seen = {}, headers;
const item = {dataset:{seenInst:'wren', seenReply:'2026-10-27T10:00:00'},
  getClientRects:()=>[1], closest:()=>null, getBoundingClientRect:()=>({top:10,bottom:20})};
const document = {visibilityState:'visible', querySelectorAll:()=>[item], addEventListener(){}};
const window = {innerHeight:800, addEventListener(){}};
const seenGet = () => ({...seen}), seenSet = v => {seen=v;};
const POLL_LIMIT_MS = 15000, net = {down:new Set()}, navigator = {onLine:true};
const busyIn=()=>false, checkVersion=()=>{}, pairAgain=()=>false, fresh=()=>{}, netNote=()=>{};
const el = {dataset:{poll:'/fragment/chat/wren',follow:'1'}, isConnected:true,
  querySelectorAll:()=>[], closest:()=>null};
const fetch = async (url, opts) => {headers=opts.headers; return {status:304, headers:{get:()=>''}};};
const setTimeout = () => 1, clearTimeout = () => {};
eval(process.argv[1]);
(async()=>{
  poll(el);
  for (const kind of ['docs','nav']) {
    sheet = {kind}; seenFeed();
    assert.deepEqual(seen, {}, 'covered replies must remain unseen');
    await el._tick();
    assert.equal(headers['X-Shaft-Watching'], '0', 'covered polling must not silence push');
    assert.ok(Number(headers['X-Shaft-At']) > 0);
  }
  sheet = null; seenFeed();
  assert.equal(seen.wren, '2026-10-27T10:00:00');
  await el._tick();
  assert.notEqual(headers['X-Shaft-Watching'], '0');
})().then(()=>console.log("ok")).catch(e=>{console.error(e);process.exitCode=1;});
''', '\n'.join(function(n) for n in ('poll', 'seenFeed', 'onScreen')))

    def test_m17_push_deadline_shows_generic_notification_inside_wait_until(self):
        """M17: an unbounded /push/latest fetch prevented the required push notification."""
        self.node(r'''
const assert = require('node:assert/strict'), vm = require('node:vm');
(async()=>{
for (const mode of ['fetch stalls', 'body stalls', 'success']) {
  const handlers = {}, timers = new Map(), shown = [];
  let signal, pending;
  const self = {location:{origin:'https://shaft.test'}, navigator:{},
    addEventListener:(kind, fn)=>{handlers[kind]=fn;},
    registration:{showNotification:async(title, opts)=>shown.push([title,opts])}};
  const stall = () => new Promise((resolve, reject)=>signal?.addEventListener('abort', ()=>reject(signal.reason)));
  const fetch = async(url, opts)=>{
    assert.equal(url, '/push/latest'); signal=opts.signal;
    if (mode === 'fetch stalls') return stall();
    return {ok:true, json:async()=>{
      if (mode === 'body stalls') return stall();
      return {events:[{title:'wren',body:'A reply',url:'/i/wren/chat',tag:'reply'}]};
    }};
  };
  vm.runInNewContext(process.argv[1], {self, fetch, URL, AbortController,
    setTimeout:(fn, ms)=>{timers.set(1,{fn,ms});return 1;}, clearTimeout:id=>timers.delete(id)});
  handlers.push({waitUntil:p=>{pending=p;}});
  assert.ok(pending && typeof pending.then === 'function', 'push lifetime must cover the notification');
  for(let i=0;i<10;i++) await Promise.resolve();
  if (mode !== 'success') {
    assert.equal(timers.size, 1, 'stalled push must have a deadline');
    const timer = timers.get(1);
    assert.ok(timer.ms > 0 && timer.ms <= 4000, 'short deadline');
    timer.fn();
  }
  await pending;
  assert.equal(timers.size, 0, 'clear the deadline on success and failure');
  assert.equal(shown.length, 1);
  assert.equal(shown[0][0], mode === 'success' ? 'wren' : 'SHAFT');
  assert.equal(shown[0][1].body, mode === 'success' ? 'A reply' : 'Something new');
}
})().then(()=>console.log("ok")).catch(e=>{console.error(e);process.exitCode=1;});
''', web_pages.service_worker())
