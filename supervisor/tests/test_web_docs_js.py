"""Drive the shipped delegated controls in Node; no browser or production instance is needed."""
from pathlib import Path
import shutil
import subprocess
import unittest


NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"


@unittest.skipUnless(NODE, "node is not installed")
class DocumentControlsJsTest(unittest.TestCase):
    def test_reply_copy_and_back(self):
        script = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {}, elements = {}, queries = {}, timers = new Map();
let nextTimer = 0, copied = "", asked = "", allow = false, ticks = 0, replaced = "";
const box = { value: "beforeAFTER", selectionStart: 6, selectionEnd: 6,
  focus() { this.focused = true; }, setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
  dispatchEvent(ev) { assert.equal(ev.type, "input"); this.input = true; } };
queries["form.composer textarea"] = box;
const document = { readyState: "loading", addEventListener(k, fn) { (handlers[k] ||= []).push(fn); },
  querySelector: q => queries[q] || null, querySelectorAll: () => [], getElementById: id => elements[id] || null };
const context = { document, navigator: { clipboard: { async writeText(t) { copied = t; } } }, URL, Event,
  location: { href: "https://shaft.test/i/wren/chat?doc=sub%2Fp.md&thread=c1&id=archive" },
  history: { replaceState(a, b, u) { replaced = String(u); } },
  window: { addEventListener() {}, getSelection: () => ({ removeAllRanges() {} }),
    confirm(t) { asked = t; return allow; } },
  setInterval() {}, setTimeout(fn) { timers.set(++nextTimer, fn); return nextTimer; },
  clearTimeout(id) { timers.delete(id); } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), context);
const click = async (selector, button) => {
  const target = { closest: q => q === selector ? button : null };
  for (const fn of handlers.click) await fn({ target, preventDefault() {} });
};
(async () => {
  const message = { dataset: { raw: "**hello**\nsecond line" } };
  await click("[data-reply-message]", { closest: () => message });
  assert.equal(box.value, "before\n\n> **hello**\n> second line\n\nAFTER");
  assert.equal(box.selectionStart, 35);
  assert.equal(box.selectionEnd, 35);
  assert.ok(box.focused && box.input);
  const copy = { closest: q => q === "pre" ? null : message };
  await click("[data-copy]", copy);
  assert.equal(copied, "**hello**\nsecond line");
  assert.match(copy.innerHTML, /m5 12 4 4L19 6/);
  await click("[data-copy]", copy);
  assert.equal(timers.size, 1);                     // repeated copies must still restore the clipboard
  [...timers.values()][0]();
  assert.match(copy.innerHTML, /<rect/);
  assert.doesNotMatch(copy.innerHTML, /Copied/);
  const edit = { value: "unsaved" }, raw = { value: "saved" }, md = {}, form = {dataset: {}};
  const view = { querySelector: q => ({ "form.docedit textarea": edit, "textarea.raw": raw,
      ".docmd": md, "form.docedit": form })[q], classList: { toggle(k, on) { assert.equal(on, false); } } };
  const panel = { dataset: { poll: "/fragment/docs/wren?doc=sub%2Fp.md&thread=c1&id=archive", etag: "old" },
    querySelector: () => view, _tick() { ticks++; } };
  elements.docs = panel;
  queries["#docs .docview.editing"] = view;
  await click("[data-back-docs]", {});
  assert.equal(asked, "Drop your changes?");
  assert.equal(ticks, 0);
  assert.equal(replaced, "");
  allow = true;
  await click("[data-back-docs]", {});
  assert.equal(ticks, 1);
  assert.equal(panel.dataset.poll, "/fragment/docs/wren?id=archive&list=1");   // not bounced back to it
  assert.equal(replaced, "https://shaft.test/i/wren/chat?id=archive");
  assert.equal(panel.dataset.etag, undefined);
  assert.equal(form.hidden, true);
})().catch(e => { console.error(e); process.exitCode = 1; });
"""
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_editor(self, scenario):
        script = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const handlers = {}, win = {}, storage = new Map();
let view, form, box, raw, sha, note, choice, resolve, submitted, pending;
let url = 'https://shaft.test/i/wren/chat?doc=p.md', ticks = 0;
const classes = () => { const s = new Set(); return {contains: k => s.has(k),
  toggle(k, on) { on ? s.add(k) : s.delete(k); }, add:k=>s.add(k), remove:k=>s.delete(k)}; };
const element = () => ({dataset:{}, children:[], setAttribute(){}, focus(){},
  addEventListener(k, fn){this[k]=fn;}, appendChild(n){this.children.push(n);this.lastElementChild=n;},
  remove(){choice=null;}});
const panel = {id:'docs',dataset:{poll:'/fragment/docs/wren?doc=p.md'},
  querySelector:()=>view, querySelectorAll:q=>q==='.docview'?[view]:[], _tick(){ticks++;}};
const document = {readyState:'loading', addEventListener(k, fn){(handlers[k] ||= []).push(fn);},
  querySelector(q){if(q === '#docs .docview.editing') return view.classList.contains('editing') ? view : null;return null;},
  querySelectorAll:()=>[], getElementById:id=>id==='docs'?panel:element(),
  createElement:element, dispatchEvent(){}};
const context = {document, URL, URLSearchParams, AbortController, Event, navigator:{},
  location:{href:url}, history:{replaceState(a,b,u){url=String(u);}},
  window:{addEventListener(k,fn){win[k]=fn;}, innerHeight:800, confirm:()=>true},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  setTimeout(){}, clearTimeout(){}, setInterval(){},
  FormData:class {constructor(f){return [['text',box.value],['sha',sha.value],['path','p.md']];}},
  DOMParser:class {parseFromString(t){return {body:{textContent:t,childNodes:[t]}};}},
  fetch(u, opts){submitted=String(opts.body);return new Promise(r=>resolve=r);}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
function mount(revision='s1', text='saved', instance='wren', doc='p.md') {
  choice=null; raw={value:text}; sha={value:revision}; note={textContent:''};
  box={value:text,style:{},scrollHeight:20,focus(){},closest:()=>form};
  const path={value:doc}, button={};
  form={dataset:{post:`/docs/${instance}/save`,after:'doc-saved'},hidden:true,
    closest:q=>q==='.docview'?view:null,
    querySelector:q=>({'textarea':box,'textarea[name=text]':box,'input[name=sha]':sha,
      'input[name=path]':path,'button[type=submit]':button,'[data-doc-draft]':note,
      '[data-doc-choice]':choice,'[data-refusal]':{replaceChildren(...n){form.refusal=n;}}})[q]||null,
    appendChild(n){choice=n;},requestSubmit(){pending=submit();}};
  view={dataset:{doc:'p.md'},classList:classes(),querySelector:q=>({'form.docedit':form,
    'form.docedit textarea':box,'textarea.raw':raw,'.docmd':{},'[data-edit-doc]':{focus(){}}})[q]||null};
}
async function click(selector, target={closest:()=>view}) {
  for(const fn of handlers.click) await fn({target:{closest:q=>q===selector?target:null},preventDefault(){}});
}
async function type(text){box.value=text;for(const fn of handlers.input) await fn({target:box});}
async function submit(){for(const fn of handlers.submit) await fn({target:{closest:()=>form},preventDefault(){}});}
async function answer(ok=true){resolve({ok,status:ok?200:409,headers:{get:k=>({'X-Doc':'p.md','X-Doc-Sha':ok?'s2':'agent'})[k]||null},text:async()=>ok?'saved':'refused diff'});await pending;}
async function swap(){for(const fn of handlers['shaft:swapped']) await fn({target:{...panel,querySelector:()=>null}});}
function unload(){let prevented=false;if(win.beforeunload) win.beforeunload({preventDefault(){prevented=true;}});return prevented;}
(async()=>{mount();
""" + scenario + r"""
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([NODE, '-e', script, str(JS)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_22_typing_during_save(self):
        """Reverting #22 closes the editor and loses text typed during Save."""
        self.run_editor(r"""
await click('[data-edit-doc]'); await type('sent'); pending=submit();
await type('newer'); await answer();
assert.equal(form.hidden,false); assert.equal(box.value,'newer'); assert.equal(sha.value,'s2');
assert.equal(raw.value,'sent'); assert.equal(JSON.parse([...storage.values()][0]).sha,'s2');
pending=submit(); await answer(); assert.equal(form.hidden,true); assert.equal(storage.size,0);
""")

    def test_issue_22_switch_choices(self):
        """Reverting #22 changes the URL before a choice and leaves the old editor open."""
        self.run_editor(r"""
await click('[data-edit-doc]'); await type('mine');
const open=()=>click('[data-open-doc]',{dataset:{openDoc:'other.md'}});
await open(); assert.equal(url,'https://shaft.test/i/wren/chat?doc=p.md');
assert.equal(panel.dataset.poll,'/fragment/docs/wren?doc=p.md');
assert.deepEqual(choice.children.map(b=>b.textContent),['Save','Discard','Stay']);
choice.children[2].click(); assert.equal(box.value,'mine'); assert.equal(form.hidden,false);
await open(); choice.children[0].click(); await answer(false);
assert.match(url,/doc=p.md$/); assert.equal(form.hidden,false); assert.equal(form.refusal[0],'refused diff');
await open(); choice.children[0].click(); await type('later'); await answer();
assert.match(url,/doc=p.md$/); assert.equal(box.value,'later');
await open(); choice.children[0].click(); await answer(); assert.match(url,/doc=other.md$/);
mount(); await click('[data-edit-doc]'); await type('discard me'); await open();
choice.children[1].click(); assert.equal(storage.size,0); assert.equal(form.hidden,true);
""")

    def test_issue_5_draft_recovery_and_stale_save(self):
        """Reverting #5 loses local drafts and their original compare-and-set revision on reopening."""
        self.run_editor(r"""
await click('[data-edit-doc]'); await type('mine'); assert.equal(storage.size,1);
await click('[data-toggle-nav], [data-close-docs], nav a',{matches:q=>q==='[data-close-docs]'}); assert.equal(storage.size,1);
mount(); await swap(); assert.equal(box.value,'mine'); assert.equal(form.hidden,false);
assert.equal(note.textContent,'Unsaved draft restored');
mount('s1','saved','other'); await swap(); assert.equal(form.hidden,true);
mount('s1','saved','wren','other.md'); await swap(); assert.equal(form.hidden,true);
mount('agent','agent text'); await swap(); assert.equal(box.value,'mine'); assert.equal(sha.value,'s1');
assert.equal(note.textContent,'Unsaved draft restored — started from an older version');
pending=submit(); assert.match(submitted,/sha=s1/); await answer(false);
assert.equal(box.value,'mine'); assert.equal(form.refusal[0],'refused diff');
await type('mine again'); assert.equal(JSON.parse([...storage.values()][0]).sha,'s1');
mount('agent','agent text'); await swap(); assert.equal(sha.value,'s1');
await click('[data-cancel-edit]'); assert.equal(storage.size,0);
mount(); await swap(); assert.equal(form.hidden,true); assert.equal(box.value,'saved');
""")

    def test_issue_5_beforeunload_only_when_dirty(self):
        """Reverting #5 allows reload or leaving without warning about unsaved document edits."""
        self.run_editor(r"""
await click('[data-edit-doc]'); assert.equal(unload(),false);
await type('mine'); assert.equal(unload(),true);
await type('saved'); assert.equal(unload(),false); assert.equal(storage.size,0);
await type('mine'); pending=submit(); await answer(); assert.equal(unload(),false);
""")
