"""Issues #6/#24: run the shipped sheet controls against a small DOM in Node."""
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor.tests.node_helpers import NODE_TIMEOUT_S

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"

HARNESS = r"""
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const handlers = {}, media = [], observers = [], requests = [];
const feed = {dataset:{poll:"/fragment/chat/wisp"}};
let phone = true, narrow = true;
const classes = () => { const s = new Set(); return { contains:k=>s.has(k), add:k=>s.add(k), remove:k=>s.delete(k),
  toggle(k,on){ if(on === undefined) on=!s.has(k); on?s.add(k):s.delete(k); return on; } }; };
class Element {
  constructor(name, parent=null) { this.name=name; this.children=[]; this.attrs={}; this.dataset={};
    this.classList=classes(); this.tabIndex=0; this.isConnected=true; if(parent) parent.appendChild(this); }
  appendChild(el){this.children.push(el);el.parentElement=this;}
  setAttribute(k,v){this.attrs[k]=String(v);}
  getAttribute(k){return this.attrs[k] ?? null;}
  hasAttribute(k){return k in this.attrs;}
  removeAttribute(k){delete this.attrs[k];}
  contains(el){return el===this || this.children.some(c=>c.contains(el));}
  closest(q){return this.matches(q)?this:this.parentElement?.closest(q)||null;}
  matches(q){return q.split(',').some(s=>{
    s=s.trim();
    if(s==='[inert]') return this.hasAttribute('inert');
    return ({'[data-show-docs]':['docbtn'],'[data-toggle-nav]':['navbtn','navclose'],
      '[data-fullscreen-doc]':['expand'],'[data-close-docs]':['docclose'],'nav a':['navlink'],'.closenav':['navclose'],
      '.chathead':['head'],'[data-open-doc]':['card']})[s]?.includes(this.name) || false;
  });}
  querySelectorAll(q){
    if(q.startsWith('button, a[href]')) return this.children.flatMap(c=>[...(c.control?[c]:[]),...c.querySelectorAll(q)]);
    return this.children.flatMap(c=>[...(c.matches(q)?[c]:[]),...c.querySelectorAll(q)]);
  }
  querySelector(q){return this.querySelectorAll(q)[0]||null;}
  getClientRects(){
    if(this===docs) return !grid.classList.contains('hidedocs') && (!phone&&!narrow || grid.classList.contains('showdocs'))?[1]:[];
    if(this===nav) return !phone || body.classList.contains('navopen')?[1]:[];
    if(this===navbtn || this===navclose) return phone?[1]:[];
    return this.parentElement && !this.parentElement.getClientRects().length?[]:[1];
  }
  focus(){if(!this.closest('[inert]')) document.activeElement=this;}
}
const body=new Element('body'), shell=new Element('shell',body), nav=new Element('nav',shell), main=new Element('main',shell);
const header=new Element('header',main), head=new Element('head',header), grid=new Element('grid',main);
const chat=new Element('chat',grid), docs=new Element('docs',grid); docs.id='docs'; docs.dataset.poll='/fragment/docs/wisp';
const control=(name,parent)=>{const el=new Element(name,parent);el.control=true;return el;};
let navbtn=control('navbtn',head), docbtn=control('docbtn',head), navclose=control('navclose',nav);
const navlink=control('navlink',nav), docclose=control('docclose',docs), doclast=control('doclast',docs);
const composer=control('composer',chat), prior=new Element('prior',main); prior.setAttribute('inert','');
const toast=new Element('toast',body), announce=new Element('announce',body); toast.id='toast'; announce.id='announce';
const document={readyState:'loading',body,activeElement:composer,
  addEventListener(k,fn){(handlers[k] ||= []).push(fn);},
  querySelector(q){return ({'.chatgrid':grid,nav,main,'.chathead [data-toggle-nav]':navbtn,
    '[data-show-docs]':docbtn, '.feed[data-follow][data-poll]':feed})[q]||null;},
  querySelectorAll(q){return ({'[data-show-docs]':[docbtn],'[data-toggle-nav]':[navbtn,navclose]})[q]||[];},
  getElementById(id){return ({docs,toast,announce})[id]||body.children.find(el=>el.id===id)||null;}};
const window={addEventListener(){},matchMedia(q){assert.equal(q,'(max-width: 720px), (pointer: coarse) and (max-height: 520px)');
  return {addEventListener(k,fn){assert.equal(k,'change');media.push(fn);}};}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{document,window,URL,location:{href:'https://shaft.test/i/wisp/chat'},history:{replaceState(){}},navigator:{},fetch:(url,opts)=>{requests.push([url,opts]);return Promise.resolve();},setInterval(){},setTimeout(){},clearTimeout(){},
  localStorage:{getItem(){return null;},setItem(){},removeItem(){}},
  getComputedStyle(el){return {position:el===nav?(phone?'fixed':'static'):(phone||narrow||grid.classList.contains('docfullscreen')?'fixed':'static')};},
  ResizeObserver:class {constructor(fn){observers.push(fn);} observe(el){assert.equal(el,main);}}});
async function event(type,props={}){const ev={target:document.activeElement,preventDefault(){this.prevented=true;},...props};
  for(const fn of handlers[type]||[]) await fn(ev);return ev;}
const click=target=>event('click',{target});
const key=(key,shiftKey=false)=>event('keydown',{key,shiftKey});
const resize=()=>{for(const fn of observers) fn();};
const rotate=()=>{for(const fn of media) fn();};
function modal(panel,label){assert.equal(panel.getAttribute('role'),'dialog');assert.equal(panel.getAttribute('aria-modal'),'true');
  assert.equal(panel.getAttribute('aria-label'),label);}
function clear(){for(const el of [main,header,chat,nav]) assert.equal(el.hasAttribute('inert'),false,el.name);
  assert.equal(prior.hasAttribute('inert'),true);}
(async()=>{await event('DOMContentLoaded');
"""


@unittest.skipUnless(NODE, "node is not installed")
class SheetsJsTest(unittest.TestCase):
    def run_sheet(self, scenario):
        result = subprocess.run([NODE, "-e", HARNESS + scenario +
                                 "\n})().catch(e=>{console.error(e);process.exitCode=1;});", str(JS)],
                                text=True, capture_output=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_m10_opening_a_sheet_unwatches_immediately(self):
        """M10: opening Documents or Chats left the device watching until its next poll."""
        self.run_sheet(r"""
for (const opener of [docbtn, navbtn]) {
  requests.length = 0;
  await click(opener);
  assert.equal(requests.length, 1);
  assert.match(requests[0][0], /^\/fragment\/chat\/wisp\?away=1&at=\d+$/);
  assert.equal(requests[0][1].keepalive, true);
  await key('Escape');
}
""")

    def test_issue_6_documents_focus_close_and_poll(self):
        """Reverting #6 leaves focus behind Documents, permits Tab escape, and loses the opener after polling."""
        self.run_sheet(r"""
for(const close of ['escape','button','toggle']) {
  docbtn.focus(); await click(docbtn); modal(docs,'Documents'); assert.equal(document.activeElement,docclose);
  for(const el of [header,chat,nav]) assert.equal(el.hasAttribute('inert'),true,el.name);
  assert.equal(main.hasAttribute('inert'),false); assert.equal(docs.closest('[inert]'),null);
  assert.equal(toast.closest('[inert]'),null); assert.equal(announce.closest('[inert]'),null);
  assert.equal(docbtn.getAttribute('aria-expanded'),'true');
  await key('Tab',true); assert.equal(document.activeElement,doclast);
  await key('Tab'); assert.equal(document.activeElement,docclose);
  await key('/'); assert.equal(document.activeElement,docclose);
  document.activeElement=body; await event('shaft:swapped',{target:docs});
  assert.equal(document.activeElement,docclose); // replacement of the focused document control
  // The header refresh replaces the opener while it is covered.
  docbtn.isConnected=false; head.children=head.children.filter(c=>c!==docbtn); docbtn=control('docbtn',head);
  await event('shaft:swapped',{target:header}); assert.equal(docbtn.getAttribute('aria-expanded'),'true');
  if(close==='escape') await key('Escape'); else await click(close==='button'?docclose:docbtn);
  clear(); assert.equal(document.activeElement,docbtn); assert.equal(docbtn.getAttribute('aria-expanded'),'false');
  assert.equal(docs.getAttribute('role'),null); assert.equal(docs.getAttribute('aria-modal'),null);
}
await click(docbtn);
const comment=new Element('comment',body); comment.id='comment-box';
const input=control('commentinput',comment); input.focus();
await event('shaft:swapped',{target:header}); assert.equal(document.activeElement,input);
assert.equal(comment.closest('[inert]'),null); assert.equal(docs.getAttribute('aria-owns'),'comment-box comment-sel quote-reply');
await key('Tab'); assert.equal(document.activeElement,docclose);
await key('Tab',true); assert.equal(document.activeElement,input);
await key('Escape'); clear();
const card=control('card',chat); card.dataset.openDoc='plan.md';
card.focus(); await click(card); assert.equal(document.activeElement,docclose);
await key('Escape'); clear(); assert.equal(document.activeElement,card);
""")

    def test_w2_reply_on_a_document_selection_works_over_the_sheet(self):
        """W2 (review 2026-10-24), found clicking through it at phone width: the documents sheet made every other child
        of body inert, ↩ Reply (#quote-reply, made on the first selection, before the sheet opened) with them — the
        button showed on a document's selection and a tap did nothing. It is the sheet's own, as 💬 Comment is."""
        self.run_sheet(r"""
const reply=control('reply',body); reply.id='quote-reply';
await click(docbtn); modal(docs,'Documents');
assert.equal(reply.closest('[inert]'),null);
for(const el of [header,chat,nav]) assert.equal(el.hasAttribute('inert'),true,el.name);
await key('Escape'); clear();
""")

    def test_issue_6_chats_focus_and_all_closing_paths(self):
        """Reverting #6 leaves Chats nonmodal and #24 leaves aria-expanded true after Escape."""
        self.run_sheet(r"""
for(const close of ['escape','button','toggle','link']) {
  navbtn.focus(); await click(navbtn); modal(nav,'Chats and pages'); assert.equal(document.activeElement,navclose);
  assert.equal(main.hasAttribute('inert'),true); assert.equal(nav.closest('[inert]'),null);
  assert.equal(navbtn.getAttribute('aria-expanded'),'true');
  await key('Tab',true); assert.equal(document.activeElement,navlink);
  await key('Tab'); assert.equal(document.activeElement,navclose);
  navbtn.isConnected=false; head.children=head.children.filter(c=>c!==navbtn); navbtn=control('navbtn',head);
  await event('shaft:swapped',{target:header}); assert.equal(navbtn.getAttribute('aria-expanded'),'true');
  if(close==='escape') await key('Escape'); else await click({button:navclose,toggle:navbtn,link:navlink}[close]);
  clear(); assert.equal(document.activeElement,navbtn); assert.equal(navbtn.getAttribute('aria-expanded'),'false');
  assert.equal(nav.getAttribute('aria-modal'),null);
}
""")

    def test_issue_24_phone_and_container_breakpoint_changes(self):
        """Reverting #24 leaves expansion stale on resize; reverting #6 leaves a resized sheet without isolation."""
        self.run_sheet(r"""
assert.equal(media.length,1); assert.equal(observers.length,1);
await click(docbtn); phone=false; narrow=false; rotate(); resize();
clear(); assert.equal(docs.getAttribute('aria-modal'),null); assert.equal(docbtn.getAttribute('aria-expanded'),'true');
assert.equal(document.activeElement,docbtn);
await key('Escape'); assert.equal(docbtn.getAttribute('aria-expanded'),'true'); // desktop stays open
narrow=true; resize(); modal(docs,'Documents'); assert.equal(document.activeElement,docclose);
assert.equal(chat.hasAttribute('inert'),true);
await key('Escape'); clear(); assert.equal(document.activeElement,docbtn);
narrow=false; resize();
// A naturally visible desktop panel disappears at the sheet breakpoint until opened.
grid.classList.remove('hidedocs'); resize(); assert.equal(docbtn.getAttribute('aria-expanded'),'true');
narrow=true; resize(); assert.equal(docbtn.getAttribute('aria-expanded'),'false'); clear();
phone=true; rotate(); await click(navbtn); modal(nav,'Chats and pages');
phone=false; rotate(); clear(); assert.equal(nav.getAttribute('aria-modal'),null);
assert.equal(navbtn.getAttribute('aria-expanded'),'false'); assert.equal(document.activeElement,navlink);
phone=true; rotate(); modal(nav,'Chats and pages'); assert.equal(main.hasAttribute('inert'),true);
await key('Escape'); clear();
""")

    def test_fullscreen_document_toggle_and_escape(self):
        self.run_sheet(r"""
phone=false; narrow=false;
const expand=control('expand',docs);
expand.focus();
await click(expand);
assert.equal(grid.classList.contains('docfullscreen'),true);
assert.equal(expand.title,'Exit full screen');
assert.equal(expand.getAttribute('aria-pressed'),'true');
modal(docs,'Documents');
assert.equal(chat.hasAttribute('inert'),true);
await key('Escape');
assert.equal(grid.classList.contains('docfullscreen'),false);
assert.equal(grid.classList.contains('hidedocs'),false);
assert.equal(document.activeElement,expand);
assert.equal(expand.title,'Full screen');
assert.equal(expand.textContent,undefined);   // writing the label as text wiped its icons
clear();
await click(expand);
await click(docclose);
assert.equal(grid.classList.contains('docfullscreen'),false);
assert.equal(grid.classList.contains('hidedocs'),true);
clear();
""")
