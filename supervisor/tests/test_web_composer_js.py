"""Issue #21: run the shipped composer in Node with a stub page."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"


@unittest.skipUnless(NODE, "node is not installed")
class ComposerJsTest(unittest.TestCase):
    def run_composer(self, scenario):
        script = r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const storage=new Map(); let serial=0;
function page(mids=[], touch=false, feed=[]) {     // feed: the message ids of the turns the feed shows (data-mid)
  const handlers={}, win={}, elements={}, viewportHandlers={}, viewport={height:900,addEventListener(k,fn){viewportHandlers[k]=fn;}}; let resolve, reject, payload;
  const node=()=>({dataset:{},style:{},children:[],setAttribute(){},querySelectorAll:()=>[],
    appendChild(n){this.children.push(n);},append(...ns){this.children.push(...ns);},remove(){},
    replaceChildren(...ns){this.children=ns;},
    addEventListener(k,fn){this[k]=fn;},classList:{add(){},remove(){},toggle(){},contains(){return false;}}});
  const sendButton={dataset:{queue:''}}, queueButton={dataset:{queue:'1'}};
  const mid={value:''}, queue={value:''}, files={type:'file',files:[],closest:()=>form}, chips=node();
  const box={value:'',tagName:'TEXTAREA',name:'text',style:{},scrollHeight:120,selectionStart:0,selectionEnd:0,
    closest:q=>['form[data-files]','form[data-draft]'].includes(q)?form:null,focus(){},
    setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;},
    dispatchEvent(ev){for(const fn of handlers[ev.type]||[]) fn({target:box});}};
  const form={dataset:{post:'/chat/wren/send',draft:'wren',files:'',multipart:'1',reset:'1',sentMids:JSON.stringify(mids)},
    closest:()=>null,querySelectorAll:()=>[sendButton,queueButton],querySelector:q=>({'textarea':box,'textarea[name=text]':box,'input[name=mid]':mid,'input[name=queue]':queue,
      'input[type=file]':files,'[data-picked]':chips,'button[type=submit]':sendButton,'.quotechip':form.chip})[q]||null,
    insertBefore(n){this.chip=n;},reset(){box.value='';mid.value='';files.files=[];}};
  const selection={isCollapsed:false,rangeCount:1,toString:()=> 'selected quote',removeAllRanges(){},
    getRangeAt:()=>({commonAncestorContainer:{nodeType:1,closest:()=>({dataset:{}})}})};
  const document={readyState:'loading',visibilityState:'hidden',body:node(),documentElement:{style:{setProperty(){}}},
    addEventListener(k,fn){(handlers[k]||=[]).push(fn);},dispatchEvent(){},
    querySelector:q=>q==='form.composer textarea'?box:['form[data-files]','form[data-draft]'].includes(q)?form:null,
    querySelectorAll:q=>q==='.feed [data-mid]'?feed.map(m=>({dataset:{mid:m},classList:{contains:()=>false}})):[],
    getElementById:id=>elements[id]||null,
    createElement(){const n=node();Object.defineProperty(n,'id',{set(id){elements[id]=n;}});return n;}};
  const context={document,URL,URLSearchParams,Event,AbortController,navigator:{},
    crypto:{randomUUID:()=>`m-${++serial}`},location:{href:'https://shaft.test/i/wren/chat'},
    window:{innerHeight:900,visualViewport:viewport,addEventListener(k,fn){(win[k]||=[]).push(fn);},getSelection:()=>selection,
      matchMedia:()=>({matches:touch})},
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    setTimeout(){},clearTimeout(){},setInterval(){},
    DataTransfer:class {constructor(){this.files=[];this.items={add:f=>this.files.push(f)};}},
    FormData:class {constructor(){this.text=box.value;this.mid=mid.value;this.queue=queue.value;this.files=[...files.files];}},
    DOMParser:class {parseFromString(t){return {body:{textContent:t}};}},
    fetch(u,o){payload=o.body;return new Promise((a,b)=>{resolve=a;reject=b;});}};
  vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
  const emit=async(k,target,extra={})=>{for(const fn of handlers[k]||[]) await fn({target,preventDefault(){},...extra});};
  form.requestSubmit=()=>{form.sending=emit('submit',{closest:()=>form});};
  return {box,mid,queue,files,form, chips, sendButton,queueButton, payload:()=>payload,
    key:extra=>emit('keydown',box,{key:'Enter',...extra}),
    async lineBreak(inputType='insertLineBreak'){let cancelled=false;await emit('beforeinput',box,{inputType,preventDefault(){cancelled=true;}});return cancelled;},
    resize:height=>{viewport.height=height;viewportHandlers.resize();},
    load:()=>emit('DOMContentLoaded'),type:async t=>{box.value=t;await emit('input',box);},
    pick:async(...fs)=>{files.files=fs;await emit('change',files);},
    paste:(...fs)=>emit('paste',box,{clipboardData:{files:fs}}),
    drop:(...fs)=>emit('drop',box,{dataTransfer:{types:['Files'],files:fs}}),
    remove:k=>emit('click',{closest:q=>q==='[data-unpick]'?{dataset:{unpick:String(k)},closest:()=>form}:null}),
    reply:()=>emit('click',{closest:q=>q==='[data-reply-message]'?{closest:()=>({dataset:{raw:'whole quote'}})}:null}),
    async quote(){await emit('selectionchange');await emit('scroll',{});elements['quote-reply'].click();},
    send:(queued=false)=>emit('submit',{closest:()=>form},{submitter:queued?queueButton:sendButton}),
    answer:()=>resolve({ok:true,status:200,headers:{get:()=>null},text:async()=> 'sent'}),fail:()=>reject(new Error('lost')),
    unload(){const ev={preventDefault(){this.prevented=true;}};for(const fn of win.beforeunload||[])fn(ev);return !!ev.prevented;},
    feed, swapped:()=>emit('shaft:swapped',{dataset:{follow:'1'},querySelectorAll:()=>[],querySelector:()=>null,closest:()=>null}),
    toast:()=>(elements.toast||{}).textContent||''};
}
const A={name:'a.txt',size:1,type:'text/plain'},B={name:'b.txt',size:1,type:'text/plain'};
(async()=>{
""" + scenario + r"""
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_issue_21_quotes_save_grow_and_reset_identity(self):
        """Reverting #21 loses the restored quote or keeps the old retry identity. PLAN-pwa-market (iPhone review I17):
        the quote is a chip above the field, not "> " lines typed into it; Send puts it first, as the same quote."""
        self.run_composer("""
for(const kind of ['reply','quote']) {
 storage.clear();const p=page();p.mid.value='old';await p[kind]();
 assert.equal(p.mid.value,'');assert.equal(p.box.value,'');
 assert.ok(p.form.chip,'a chip shows the quote');assert.ok(storage.get('shaft.quote.wren').includes('quote'));
 const q=page();await q.load();assert.ok(q.form._quote.includes('quote'),'kept with the draft');
 await q.type('my answer');const sending=q.send();await Promise.resolve();
 const nl=String.fromCharCode(10),t=q.payload().text;assert.ok(/^> (whole|selected) quote/.test(t)&&t.endsWith('quote'+nl+nl+'my answer'),t);q.answer();await sending;
 assert.equal(storage.get('shaft.quote.wren'),undefined);
}
""")

    def test_issue_21_picker_adds_and_send_keeps_later_files(self):
        """Reverting #21 replaces earlier picks and clears files added during a send."""
        self.run_composer("""
const p=page();await p.type('hello');await p.pick(A);await p.pick(B);
assert.deepEqual(p.files.files,[A,B]);const sending=p.send();await Promise.resolve();
assert.deepEqual(p.payload().files,[A,B]);await p.remove(1);await p.pick({...B});
await p.type('next');p.answer();await sending;
assert.equal(p.box.value,'next');assert.deepEqual(p.files.files,[B]);
assert.equal(storage.get('shaft.draft.wren'),'next');
""")

    def test_issue_21_attachments_warn_and_change_retry_identity(self):
        """Reverting #21 silently loses files on unload and reuses IDs after attachment edits."""
        self.run_composer("""
const p=page();assert.equal(p.unload(),false);
for(const add of ['pick','paste','drop']) {p.mid.value='old';await p[add](A);assert.equal(p.mid.value,'');}
assert.equal(p.unload(),true);
while(p.files.files.length){p.mid.value='old';await p.remove(0);assert.equal(p.mid.value,'');}
assert.equal(p.unload(),false);
""")

    def test_issue_21_reload_reconciles_lost_answers(self):
        """Reverting #21 gives a restored retry a fresh ID and offers already delivered text again."""
        self.run_composer("""
const p=page();await p.type('hello');let sending=p.send();await Promise.resolve();const id=p.payload().mid;
p.fail();await sending;sending=p.send();await Promise.resolve();assert.equal(p.payload().mid,id);p.fail();await sending;
const q=page();await q.load();assert.equal(q.box.value,'hello');assert.equal(q.mid.value,id);
const r=page([id]);await r.load();assert.equal(r.box.value,'');assert.equal(r.mid.value,'');
assert.equal(storage.get('shaft.draft.wren'),undefined);
""")

    def test_w11_a_sent_message_leaves_no_retry_behind(self):
        """W11 (review 2026-10-24) said sent() left shaft.pending.<instance> behind. It did, but the reset after a send
        cleared it anyway (setFiles -> composerChanged), so this passed before too: it guards that sent() now clears
        it itself, whatever the reset does."""
        self.run_composer("""
const p=page();await p.type('hello');const sending=p.send();await Promise.resolve();
assert.ok(storage.get('shaft.pending.wren'));p.answer();await sending;
assert.equal(storage.get('shaft.pending.wren'),undefined);assert.equal(storage.get('shaft.draft.wren'),undefined);
""")

    def test_w11_a_lost_send_the_feed_shows_is_let_go(self):
        """W11: data-sent-mids is drawn once with the page and the poll never refreshes it, so a send whose answer was
        lost but which landed stayed in the composer (and came back after a reload). The feed's turns (data-mid) say."""
        self.run_composer("""
const p=page([],false,[]);await p.type('hello');let sending=p.send();await Promise.resolve();const id=p.payload().mid;
p.fail();await sending;assert.equal(p.box.value,'hello');assert.equal(p.mid.value,id);
const q=page([],false,[id]);await q.load();assert.equal(q.box.value,'');assert.equal(q.mid.value,'');
assert.equal(storage.get('shaft.pending.wren'),undefined);
storage.clear();const r=page([],false,[]);await r.type('again');sending=r.send();await Promise.resolve();
const rid=r.payload().mid;r.fail();await sending;
await r.swapped();assert.equal(r.box.value,'again');assert.equal(r.mid.value,rid);   // not in the feed yet: kept
r.feed.push(rid);await r.swapped();                                 // the next refresh brings its turn
assert.equal(r.box.value,'');assert.equal(r.mid.value,'');assert.equal(storage.get('shaft.pending.wren'),undefined);
""")

    def test_w11_a_reload_says_the_files_of_an_unsent_attempt_are_gone(self):
        """W11: an attempt with files, restored after a reload, would be sent again without them, unsaid."""
        self.run_composer("""
const p=page();await p.type('see these');await p.pick(A,B);const sending=p.send();await Promise.resolve();p.fail();await sending;
assert.deepEqual(JSON.parse(storage.get('shaft.pending.wren')).files,['a.txt','b.txt']);
const q=page();await q.load();assert.equal(q.box.value,'see these');
assert.match(q.toast(),/its files could not be kept: attach a[.]txt, b[.]txt again/);
""")

    def test_issue_21_edits_during_send_survive_reload(self):
        """Reverting #21 loses retry identity; reconciliation must also preserve edits made during a send."""
        self.run_composer("""
const p=page();await p.type('hello');const sending=p.send();await Promise.resolve();const id=p.payload().mid;
const pending=JSON.parse(storage.get('shaft.pending.wren'));assert.equal(pending.mid,id);assert.equal(pending.text,'hello');
await p.type('new text');p.fail();await sending;
const q=page([id]);await q.load();assert.equal(q.box.value,'new text');assert.equal(q.mid.value,'');
""")

    def test_issue_21_markup_contains_saved_turn_ids(self):
        """Reverting #21 prevents reload recovery from detecting accepted messages in the rendered chat."""
        from html.parser import HTMLParser
        from supervisor.web_views import chat_page

        class Composer(HTMLParser):
            attrs = {}

            def handle_starttag(self, tag, attrs):
                if tag == "form" and dict(attrs).get("class") == "composer":
                    self.attrs = dict(attrs)

        chat = {"id": "web-a", "state": "ended", "created": "2026-10-20T10:00:00", "turns": [
            {"role": "user", "content": "hello", "ts": "2026-10-20T10:00:00", "mid": 'm-"1'}]}
        parser = Composer()
        parser.feed(str(chat_page("wren", chat, [], [], {}, ["wren"], "csrf")))
        self.assertEqual(json.loads(parser.attrs["data-sent-mids"]), ['m-"1'])

    def test_issue_70_newlines_and_queue_keys(self):
        """Reverting #70 sends Alt-Enter text and sends modifier messages as interjections."""
        self.run_composer("""
for(const modifier of ['altKey','shiftKey']) {
 storage.clear();const p=page();await p.type('abcd');p.box.setSelectionRange(1,3);
 await p.key({[modifier]:true});assert.equal(p.box.value,'a\\nd');assert.equal(p.payload(),undefined);
 assert.equal(p.box.selectionStart,2);assert.equal(storage.get('shaft.draft.wren'),'a\\nd');
}
for(const modifier of ['metaKey','ctrlKey',null]) {
 const p=page();await p.type('next');await p.key(modifier?{[modifier]:true}:{});
 assert.equal(p.payload().queue,modifier?'1':'');assert.equal(p.queue.value,'');
 p.answer();await p.form.sending;
}
const p=page([],true);p.resize(600);await p.type('touch');await p.key({});assert.equal(await p.lineBreak(),false);assert.equal(p.payload(),undefined);
const q=page();await q.type('ime');await q.key({isComposing:true});assert.equal(q.payload(),undefined);
""")

    def test_ipad_hardware_keyboard_can_queue(self):
        self.run_composer("""
for (const modifier of ['metaKey','ctrlKey']) {
 const p=page([],true);await p.type('after this reply');await p.key({[modifier]:true});
 assert.ok(p.payload(), 'Modified Enter must submit on an iPad');
 assert.equal(p.payload().queue,'1');p.answer();await p.form.sending;
}
const p=page([],true);p.resize(600);await p.type('touch');await p.key({});assert.equal(await p.lineBreak(),false);assert.equal(p.payload(),undefined);
""")

    def test_ipad_hardware_keyboard_return_sends(self):
        """Return on an iPad with a hardware keyboard (iPadOS reports a touch pointer, but no on-screen keyboard hides the
        page) must send once its line break arrives, after any autocorrection; with the on-screen keyboard up, or with
        Shift/Alt, it stays a new line. Reverting the fix leaves a hardware-keyboard Return typing a new line."""
        self.run_composer("""
let p=page([],true);await p.type('hello');await p.key({});
assert.equal(p.payload(),undefined, 'nothing is sent at keydown: the autocorrection comes after it');
assert.equal(await p.lineBreak('insertReplacementText'),false);     // autocorrect lands first, the Return still sends
assert.equal(await p.lineBreak(),true, 'the line break is cancelled');
assert.ok(p.payload(), 'Return with a hardware keyboard must send');assert.equal(p.payload().queue,'');
p.answer();await p.form.sending;
p=page([],true);await p.type('para');await p.key({});assert.equal(await p.lineBreak('insertParagraph'),true);assert.ok(p.payload());
for (const modifier of ['shiftKey','altKey']) {
 p=page([],true);await p.type('two lines');await p.key({[modifier]:true});
 assert.equal(await p.lineBreak(),false);assert.equal(p.payload(),undefined);
}
p=page([],true);p.resize(600);await p.type('on-screen');await p.key({});
assert.equal(await p.lineBreak(),false);assert.equal(p.payload(),undefined);
p=page([],true);await p.type('later');await p.key({});await p.key({key:'a'});   // another key: that Return is spent
assert.equal(await p.lineBreak(),false);assert.equal(p.payload(),undefined);
p=page([],true);await p.type('ime');await p.key({isComposing:true});assert.equal(await p.lineBreak(),false);
p=page();await p.type('desk');await p.key({});assert.equal(p.payload().queue,'');   // a computer: sent at keydown
""")

    def test_touch_buttons_choose_send_or_queue_without_leaking_mode(self):
        self.run_composer("""
const p=page([],true);await p.type('later');await p.pick(A);
let sending=p.send(true);await Promise.resolve();
assert.equal(p.payload().queue,'1');assert.deepEqual(p.payload().files,[A]);
assert.equal(p.sendButton.disabled,true);assert.equal(p.queueButton.disabled,true);
p.fail();await sending;
assert.equal(p.box.value,'later');assert.equal(p.sendButton.disabled,false);assert.equal(p.queueButton.disabled,false);
sending=p.send(true);await Promise.resolve();assert.equal(p.payload().queue,'1');p.answer();await sending;
await p.type('now');sending=p.send();await Promise.resolve();assert.equal(p.payload().queue,'');p.answer();await sending;
""")

    def test_composer_has_named_touch_send_actions(self):
        from supervisor.web_views import chat_page
        from html.parser import HTMLParser
        class Buttons(HTMLParser):
            # Its name: aria-label (the send arrow is an icon), else its text without the explaining line under it
            def __init__(self):
                super().__init__(); self.labels = []; self.current = None; self.inner = 0
            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'button' and 'data-queue' in attrs:
                    self.current = [attrs['data-queue'], attrs.get('aria-label') or '', 'hidden' in attrs]
                    self.named = bool(attrs.get('aria-label'))
                elif self.current is not None and tag == 'span':
                    self.inner += 1
            def handle_data(self, data):
                if self.current is not None and not self.inner and not self.named: self.current[1] += data
            def handle_endtag(self, tag):
                if tag == 'span' and self.inner:
                    self.inner -= 1
                elif tag == 'button' and self.current is not None:
                    self.labels.append(self.current); self.current = None
        for state, queue_hidden in ((None, True), ('idle', True), ('replying', False)):
            buttons = Buttons()
            chat = {'id': 'web-a', 'state': state, 'turns': [], 'started': '2026-10-22T09:00:00',
                    'created': '2026-10-22T09:00:00'} if state else None
            buttons.feed(str(chat_page('wren', chat, [], [], {}, ['wren'], 't')))
            self.assertEqual(buttons.labels, [['', 'Send now', False], ['1', 'Queue after reply', queue_hidden]], state)

    def test_queue_after_reply_shows_only_while_a_reply_runs(self):
        """Reverting this leaves Queue after reply on screen when nothing is replying, or hidden once a reply starts."""
        script = r"""
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {}, queue = { hidden: true };
let live = false;
const feed = { dataset: { follow: "1" }, querySelectorAll: () => [], closest: () => null,
  querySelector: q => q === '[data-k="live"]' && live ? {} : null };
const document = { readyState: "loading", addEventListener(k, f) { (handlers[k] ||= []).push(f); },
  querySelector: q => q === 'form[data-files] button[data-queue="1"]' ? queue : null,
  querySelectorAll: () => [], getElementById: () => null };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, URL, navigator: {}, setInterval() {},
  window: { addEventListener() {} }, location: { href: "https://shaft.test/i/wren/chat" } });
const swap = () => { for (const f of handlers["shaft:swapped"]) f({ target: feed, detail: {} }); };
live = true; swap(); assert.equal(queue.hidden, false, "a reply started: offer to queue");
live = false; swap(); assert.equal(queue.hidden, true, "the reply ended: only Send now");
"""
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_opening_keyboard_shrinks_and_closing_restores_long_draft(self):
        self.run_composer("""
const p=page([],true);p.box.scrollHeight=800;await p.type('long draft');
assert.equal(p.box.style.height,'300px');
p.resize(450);assert.equal(p.box.style.height,'150px');assert.equal(p.box.value,'long draft');
p.resize(900);assert.equal(p.box.style.height,'300px');assert.equal(p.box.value,'long draft');
""")
