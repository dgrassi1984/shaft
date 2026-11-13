"""Issue #21: run the shipped composer in Node with a stub page."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

from supervisor.tests.node_helpers import NODE_TIMEOUT_S

NODE = shutil.which("node")
JS = Path(__file__).resolve().parents[1] / "web_static" / "web.js"


@unittest.skipUnless(NODE, "node is not installed")
class ComposerJsTest(unittest.TestCase):
    def run_composer(self, scenario):
        script = r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const storage=new Map(); let serial=0;
const plain=v=>JSON.parse(JSON.stringify(v));
function page(mids=[], touch=false, feed=[], bubble=false) {   // feed: the message ids the feed shows (data-mid);
                                                                 // bubble: a feed the "sending…" copy goes in
  const handlers={}, win={}, elements={}, viewportHandlers={}, viewport={height:900,addEventListener(k,fn){viewportHandlers[k]=fn;}}; let resolve, reject, payload;
  const node=()=>({dataset:{},style:{},children:[],setAttribute(){},querySelectorAll(q){return this.children.flatMap(n=>[...(q==='textarea.quoteanswer'&&n.className==='quoteanswer'?[n]:[]),...n.querySelectorAll(q)]);},
    appendChild(n){this.children.push(n);},append(...ns){this.children.push(...ns);},remove(){},
    replaceChildren(...ns){this.children=ns;},
    addEventListener(k,fn){if(k==='focus')this.onFocus=fn;else this[k]=fn;},classList:{add(){},remove(){},toggle(){},contains(){return false;}}});
  const sendButton={dataset:{queue:''}}, queueButton={dataset:{queue:'1'}};
  const mid={value:''}, queue={value:''}, files={type:'file',files:[],closest:()=>form}, chips=node();
  const box={value:'',placeholder:'Write here',tagName:'TEXTAREA',name:'text',style:{},scrollHeight:120,selectionStart:0,selectionEnd:0,
    closest:q=>['form[data-files]','form[data-draft]'].includes(q)?form:null,focused:0,focus(){this.focused++;},
    setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;},
    dispatchEvent(ev){for(const fn of handlers[ev.type]||[]) fn({target:box});}};
  const form={dataset:{post:'/chat/wisp/send',draft:'wisp',files:'',multipart:'1',reset:'1',sentMids:JSON.stringify(mids)},
    closest:()=>null,querySelectorAll:q=>q==='textarea.quoteanswer'?(form.chip?.querySelectorAll(q)||[]):[sendButton,queueButton],querySelector:q=>({'textarea':box,'textarea[name=text]':box,'input[name=mid]':mid,'input[name=queue]':queue,
      'input[type=file]':files,'[data-picked]':chips,'button[type=submit]':sendButton,'.quotechips':form.chip})[q]||null,
    insertBefore(n){this.chip=n;n.remove=()=>{this.chip=null;};},reset(){box.value='';mid.value='';files.files=[];}};
  const feedNode={...node(),closest:()=>null};
  const selection={text:'selected quote',isCollapsed:false,rangeCount:1,toString(){return this.text;},removeAllRanges(){},
    getRangeAt:()=>({commonAncestorContainer:{nodeType:1,closest:()=>({dataset:{}})},
      getBoundingClientRect:()=>({top:100,bottom:120,left:10})})};
  const timers=[];
  const document={readyState:'loading',visibilityState:'hidden',body:node(),documentElement:{style:{setProperty(){}}},
    addEventListener(k,fn){(handlers[k]||=[]).push(fn);},dispatchEvent(){},
    querySelector:q=>q==='form.composer textarea[name=text]'?box:['form[data-files]','form[data-draft]'].includes(q)?form
      :q==='.feed[data-follow]'&&bubble?feedNode:null,
    querySelectorAll:q=>q==='.feed [data-mid]'?feed.map(m=>({dataset:{mid:m},classList:{contains:()=>false}})):[],
    getElementById:id=>elements[id]||null,
    createElement(tag){const n=node();
      n.tagName=tag.toUpperCase();n.focused=0;n.focus=function(){this.focused++;if(this.onFocus)this.onFocus();};
      n.scrollIntoView=()=>{n.scrolled=true;};n.scrollHeight=120;
      n.closest=q=>['form[data-files]','form[data-draft]','form.composer'].includes(q)?form:null;
      n.setSelectionRange=(a,b)=>{n.selectionStart=a;n.selectionEnd=b;};
      n.dispatchEvent=ev=>{for(const fn of handlers[ev.type]||[])fn({target:n});};
      n.setAttribute=(k,v)=>{(n.attrs||={})[k]=v;};
      const classes=new Set();n.classList.toggle=k=>{if(classes.has(k)){classes.delete(k);return false;}classes.add(k);return true;};
      Object.defineProperty(n,'id',{set(id){elements[id]=n;}});return n;}};
  const context={document,URL,URLSearchParams,Event,AbortController,File:require("node:buffer").File,navigator:{},
    crypto:{randomUUID:()=>`m-${++serial}`},location:{href:'https://shaft.test/i/wisp/chat'},
    window:{innerHeight:900,visualViewport:viewport,scrollTo(){},addEventListener(k,fn){(win[k]||=[]).push(fn);},getSelection:()=>selection,
      matchMedia:()=>({matches:touch})},
    localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
    setTimeout(fn){timers.push(fn);},clearTimeout(){},setInterval(){},
    DataTransfer:class {constructor(){this.files=[];this.items={add:f=>this.files.push(f)};}},
    FormData:class {set(k,v){this[k]=v;}constructor(){this.text=box.value;this.mid=mid.value;this.queue=queue.value;this.files=[...files.files];}},
    DOMParser:class {parseFromString(t){return {body:{textContent:t}};}},
    fetch(u,o){payload=o.body;return new Promise((a,b)=>{resolve=a;reject=b;});}};
  vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
  const emit=async(k,target,extra={})=>{for(const fn of handlers[k]||[]) await fn({target,preventDefault(){},...extra});};
  form.requestSubmit=()=>{form.sending=emit('submit',{closest:()=>form});};
  return {box,mid,queue,files,form, chips, sendButton,queueButton, selection, payload:()=>payload,
    key:(extra,target=box)=>emit('keydown',target,{key:'Enter',...extra}),
    answerBox:i=>form.chip.children[i].children[2],
    typeAnswer:async(i,t)=>{const a=form.chip.children[i].children[2];a.value=t;await emit('input',a);},
    async lineBreak(inputType='insertLineBreak',target=box){let cancelled=false;await emit('beforeinput',target,{inputType,preventDefault(){cancelled=true;}});return cancelled;},
    resize:height=>{viewport.height=height;viewportHandlers.resize();},
    load:()=>emit('DOMContentLoaded'),type:async t=>{box.value=t;await emit('input',box);},
    pick:async(...fs)=>{files.files=fs;await emit('change',files);},
    async pasteText(text,fs=[]) {let prevented=false;await emit('paste',box,{clipboardData:{files:fs,getData:()=>text},
      preventDefault(){prevented=true;}});return prevented;},
    paste:(...fs)=>emit('paste',box,{clipboardData:{files:fs}}),
    drop:(...fs)=>emit('drop',box,{dataTransfer:{types:['Files'],files:fs}}),
    remove:k=>emit('click',{closest:q=>q==='[data-unpick]'?{dataset:{unpick:String(k)},closest:()=>form}:null}),
    reply:()=>emit('click',{closest:q=>q==='[data-reply-message]'?{closest:()=>({dataset:{raw:'whole quote'}})}:null}),
    async quote(text){if(text) selection.text=text;const n=timers.length;await emit('selectionchange');
      timers.splice(n).forEach(fn=>fn());   // the button placed once the selection settles
      await emit('scroll',{});elements['quote-reply'].click();},
    quoteButton:()=>elements['quote-reply'],
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
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_long_paste_attaches_a_text_file_and_short_paste_stays_inline(self):
        self.run_composer(r"""
const p=page();const text='é'.repeat(2001);
assert.equal(await p.pasteText(text),true);assert.equal(p.box.value,'');
assert.equal(p.files.files.length,1);assert.equal(await p.files.files[0].text(),text);
assert.equal(p.files.files[0].type,'text/plain');
assert.match(p.files.files[0].name,/^pasted-text-\d{8}-\d{6}[.]txt$/);
assert.match(p.chips.children[0].children[0].textContent,/pasted-text-/);
for(const short of ['a'.repeat(2000),'line\n'.repeat(20),'line\r\n'.repeat(20),' '.repeat(5000),'👩‍💻'.repeat(2000)]) {
 const q=page();assert.equal(await q.pasteText(short),false);assert.equal(q.files.files.length,0);
}
const q=page();assert.equal(await q.pasteText(Array(21).fill('line').join('\n')),true);
const r=page();await r.pasteText(text,[A]);assert.deepEqual(r.files.files,[A]);
""")

    def test_pasted_text_draft_survives_reload_and_removal_updates_storage(self):
        self.run_composer(r"""
const text='saved'.repeat(500);const p=page();await p.pasteText(text);
const records=JSON.parse(storage.get('shaft.pasted.wisp'));
assert.deepEqual(records,[{name:p.files.files[0].name,text}]);
const q=page();await q.load();assert.equal(q.box.value,'');
assert.equal(await q.files.files[0].text(),text);assert.equal(q.files.files[0].name,records[0].name);
await q.remove(0);assert.equal(storage.get('shaft.pasted.wisp'),undefined);
const r=page();await r.load();assert.equal(r.files.files.length,0);
""")

    def test_pending_pasted_text_restores_and_only_real_files_need_reattaching(self):
        self.run_composer(r"""
const text='pending'.repeat(400);const p=page();await p.pasteText(text);await p.pick(A);
const sending=p.send();await Promise.resolve();const id=p.payload().mid;p.fail();await sending;
const attempt=JSON.parse(storage.get('shaft.pending.wisp'));
assert.deepEqual(attempt.files,['a.txt']);assert.equal(attempt.pasted[0].text,text);
// The pending attempt also holds the bytes if the separate draft copy is absent.
storage.delete('shaft.pasted.wisp');
const q=page();await q.load();assert.equal(q.mid.value,id);assert.equal(await q.files.files[0].text(),text);
assert.match(q.toast(),/attach a[.]txt again/);assert.equal(q.toast().includes('pasted-text-'),false);
const retry=q.send();await Promise.resolve();assert.equal(await q.payload().files[0].text(),text);
q.answer();await retry;assert.equal(storage.get('shaft.pasted.wisp'),undefined);
assert.equal(storage.get('shaft.pending.wisp'),undefined);
const r=page();await r.load();assert.equal(r.files.files.length,0);
""")

    def test_delivered_pasted_text_is_not_restored_and_later_pastes_survive_send(self):
        self.run_composer(r"""
const p=page();await p.pasteText('first'.repeat(500));let sending=p.send();await Promise.resolve();
const id=p.payload().mid;p.fail();await sending;
const q=page([id]);await q.load();assert.equal(q.files.files.length,0);
assert.equal(storage.get('shaft.pasted.wisp'),undefined);
storage.clear();const r=page();await r.pasteText('first'.repeat(500));sending=r.send();await Promise.resolve();
await r.pasteText('later'.repeat(500));r.answer();await sending;
const t=page();await t.load();assert.equal(t.files.files.length,1);assert.equal(await t.files.files[0].text(),'later'.repeat(500));
""")

    def test_unicode_white_space_pastes_stay_inline_but_zero_width_spaces_attach(self):
        self.run_composer(r"""
const p=page();assert.equal(await p.pasteText('\u0085'.repeat(5000)),false);
assert.equal(p.files.files.length,0);
const text='\u200b'.repeat(2001);assert.equal(await p.pasteText(text),true);
assert.equal(await p.files.files[0].text(),text);
""")

    def test_inflight_paste_is_not_restored_after_text_or_attachment_edits(self):
        self.run_composer(r"""
for (const edit of ['text','file']) {
 storage.clear();const p=page();const text='sending'.repeat(400);await p.pasteText(text);
 const sending=p.send();await Promise.resolve();
 assert.equal(JSON.parse(storage.get('shaft.pending.wisp')).pasted[0].text,text);
 if(edit==='text')await p.type('next');else await p.pasteText('next'.repeat(600));
 assert.equal(storage.get('shaft.pending.wisp'),undefined);
 const saved=JSON.parse(storage.get('shaft.pasted.wisp')||'[]');
 assert.deepEqual(saved.map(f=>f.text),edit==='text'?[]:['next'.repeat(600)]);
 const q=page();await q.load();assert.equal(q.files.files.length,edit==='text'?0:1);
 if(edit==='file')assert.equal(await q.files.files[0].text(),'next'.repeat(600));
 p.answer();await sending;
}
""")

    def test_failed_inflight_paste_returns_to_the_persisted_draft(self):
        self.run_composer(r"""
const p=page();const text='retry'.repeat(500);await p.pasteText(text);
const sending=p.send();await Promise.resolve();
assert.equal(storage.get('shaft.pasted.wisp'),undefined);
await p.type('edited');p.fail();await sending;
assert.equal(await p.files.files[0].text(),text);
assert.deepEqual(JSON.parse(storage.get('shaft.pasted.wisp')).map(f=>f.text),[text]);
const q=page();await q.load();assert.equal(await q.files.files[0].text(),text);
""")

    def test_issue_21_quotes_save_grow_and_reset_identity(self):
        """Reverting #21 loses the restored quote or keeps the old retry identity. iPhone review I17:
        the quote is a chip above the field, not "> " lines typed into it; Send puts it first, as the same quote."""
        self.run_composer("""
for(const kind of ['reply','quote']) {
 storage.clear();const p=page();p.mid.value='old';await p[kind]();
 assert.equal(p.mid.value,'');assert.equal(p.box.value,'');
 assert.ok(p.form.chip,'a chip shows the quote');assert.ok(storage.get('shaft.quote.wisp').includes('quote'));
 const q=page();await q.load();assert.ok(q.form._quotes[0].q.includes('quote'),'kept with the draft');
 await q.type('my answer');const sending=q.send();await Promise.resolve();
 const nl=String.fromCharCode(10),t=q.payload().text;assert.ok(/^> (whole|selected) quote/.test(t)&&t.endsWith('quote'+nl+nl+'my answer'),t);q.answer();await sending;
 assert.equal(storage.get('shaft.quote.wisp'),undefined);
}
""")

    def test_several_quotes_go_in_one_message_each_its_own_quote(self):
        """Quoting a second piece glued it onto the first (one chip, one ×, one merged "> " block): the operator could
        neither see nor drop one piece, and a quoted question merged with another piece no longer matched
        messages.quoted_questions, so it went unanswered. Each piece is its own chip and its own quote."""
        self.run_composer("""
const nl=String.fromCharCode(10);
const p=page();await p.quote('first piece');
assert.equal(p.quoteButton().textContent,'↩ Reply');
await p.quote('second piece');await p.reply();
assert.equal(p.quoteButton().textContent,'+ Quote','the button says it adds to the reply');
assert.deepEqual(plain(p.form._quotes),[{q:'> first piece',a:''},{q:'> second piece',a:''},{q:'> whole quote',a:''}]);
assert.equal(p.form.chip.children.length,3,'a chip for each quote');
await p.quote('second piece');assert.equal(p.form._quotes.length,3,'the same piece twice is one quote');
p.mid.value='old';p.form.chip.children[0].children[1].click();
assert.deepEqual(plain(p.form._quotes),[{q:'> second piece',a:''},{q:'> whole quote',a:''}],'x takes out its own quote only');
assert.equal(p.mid.value,'','another message: a new id');
assert.deepEqual(plain(JSON.parse(storage.get('shaft.quote.wisp'))),[{q:'> second piece',a:''},{q:'> whole quote',a:''}]);
const q=page();await q.load();assert.deepEqual(plain(q.form._quotes),[{q:'> second piece',a:''},{q:'> whole quote',a:''}],'kept with the draft');
await q.type('my answer');const sending=q.send();await Promise.resolve();
assert.equal(q.payload().text,'> second piece'+nl+nl+'> whole quote'+nl+nl+'my answer','each its own quote, in order');
assert.equal(q.form.chip,null,'the chips go at Send');
q.answer();await sending;assert.equal(storage.get('shaft.quote.wisp'),undefined);
storage.clear();storage.set('shaft.quote.wisp','> kept before several quotes');
const r=page();await r.load();assert.deepEqual(plain(r.form._quotes),[{q:'> kept before several quotes',a:''}],'a quote kept by the old page');
const t=page([],true);await t.quote('on a phone');assert.equal(t.box.focused,0,'no keyboard over the chat on a phone');
await t.reply();assert.equal(t.answerBox(1).focused,1,'a whole-message Reply brings the keyboard, to its answer field');
""")

    def test_answers_could_not_be_tied_to_quotes(self):
        self.run_composer(r"""
const p=page();await p.quote('q1');await p.quote('q2');await p.quote('q3');
assert.equal(p.box.placeholder,'Anything else…');
assert.equal(p.answerBox(2).focused,1,'the new answer gets focus');
assert.equal(p.answerBox(2).scrolled,true);
assert.equal(p.answerBox(2).attrs['aria-label'],'Your answer to quote 3 of 3');
p.mid.value='old';storage.set('shaft.pending.wisp','old attempt');
p.answerBox(0).scrollHeight=900;await p.typeAnswer(0,'  answer 1  ');
assert.equal(p.mid.value,'');assert.equal(storage.get('shaft.pending.wisp'),undefined);
assert.equal(p.answerBox(0).style.height,'128px');
await p.typeAnswer(2,'answer 3');await p.type('else');
assert.equal(JSON.parse(storage.get('shaft.quote.wisp'))[0].a,'  answer 1  ');
assert.equal(storage.get('shaft.draft.wisp'),'else','answers do not overwrite the main draft');
const r=page();await r.load();assert.equal(r.answerBox(0).value,'  answer 1  ');
assert.equal(r.answerBox(1).value,'');assert.equal(r.answerBox(2).value,'answer 3');
let sending=r.send();await Promise.resolve();
assert.equal(r.payload().text,'else\n\n> q1\n\nanswer 1\n\n> q2\n\n> q3\n\nanswer 3','anything else first: after the last answer it read as part of it');
assert.equal(r.box.placeholder,'Write here');r.answer();await sending;
storage.clear();const q=page();await q.quote('q1');await q.quote('q2');
sending=q.send();await Promise.resolve();assert.equal(q.payload().text,'> q1\n\n> q2');q.answer();await sending;
""")

    def test_answers_could_not_be_tied_to_quotes_saved_formats(self):
        self.run_composer(r"""
for (const [saved,expected] of [
 [JSON.stringify([{q:'> new',a:'answer'}]),[{q:'> new',a:'answer'}]],
 [JSON.stringify(['> old','> older']),[{q:'> old',a:''},{q:'> older',a:''}]],
 ['> bare',[{q:'> bare',a:''}]]]) {
 storage.clear();storage.set('shaft.quote.wisp',saved);const p=page();await p.load();
 assert.deepEqual(plain(p.form._quotes),expected);
 assert.equal(p.answerBox(0).value,expected[0].a);
}
""")

    def test_answers_could_not_be_tied_to_quotes_keyboard(self):
        self.run_composer(r"""
for(const modifier of ['altKey','shiftKey']) {
 storage.clear();const p=page();await p.quote('q');await p.typeAnswer(0,'abcd');
 const a=p.answerBox(0);a.setSelectionRange(1,3);await p.key({[modifier]:true},a);
 assert.equal(a.value,'a\nd');assert.equal(JSON.parse(storage.get('shaft.quote.wisp'))[0].a,'a\nd');
 assert.equal(p.payload(),undefined);
}
for(const modifier of ['metaKey','ctrlKey',null]) {
 storage.clear();const p=page();await p.quote('q');await p.typeAnswer(0,'answer');
 await p.key(modifier?{[modifier]:true}:{},p.answerBox(0));
 assert.equal(p.payload().text,'> q\n\nanswer');assert.equal(p.payload().queue,modifier?'1':'');
 p.answer();await p.form.sending;
}
storage.clear();let p=page([],true);await p.quote('q');await p.typeAnswer(0,'answer');
let a=p.answerBox(0);await p.key({isComposing:true},a);assert.equal(await p.lineBreak('insertLineBreak',a),false);
assert.equal(p.payload(),undefined);
await p.key({code:'Enter'},a);assert.equal(p.payload(),undefined);
assert.equal(await p.lineBreak('insertLineBreak',a),true);assert.equal(p.payload().text,'> q\n\nanswer');
p.answer();await p.form.sending;
storage.clear();p=page([],true);p.resize(600);await p.quote('q');a=p.answerBox(0);
await p.key({},a);assert.equal(await p.lineBreak('insertParagraph',a),false);assert.equal(p.payload(),undefined);
storage.clear();p=page();await p.quote('q');a=p.answerBox(0);
assert.equal(a.attrs['aria-label'],'Your answer to the quote');
const shown=p.form.chip.children[0].children[0];shown.click();assert.equal(shown.attrs['aria-expanded'],'true');
shown.click();assert.equal(shown.attrs['aria-expanded'],'false');
p.form.chip.children[0].children[1].click();assert.equal(p.box.placeholder,'Write here');
""")

    def test_p8_the_field_empties_as_the_bubble_appears_and_a_failure_puts_the_text_back(self):
        """iPad review P8 (2026-10-27): for a second the text was both in the field and in the "sending…" bubble."""
        self.run_composer("""
const p=page([],false,[],true);await p.type('hello');let sending=p.send();await Promise.resolve();
assert.equal(p.box.value,'','empty while sending');assert.equal(p.payload().text,'hello');
p.answer();await sending;assert.equal(p.box.value,'');assert.equal(storage.get('shaft.draft.wisp'),undefined);
await p.type('again');sending=p.send();await Promise.resolve();assert.equal(p.box.value,'');
await p.type('more');p.fail();await sending;
assert.equal(p.box.value,'again'+String.fromCharCode(10,10)+'more','a failure puts it back, before what came after');
""")

    def test_issue_21_picker_adds_and_send_keeps_later_files(self):
        """Reverting #21 replaces earlier picks and clears files added during a send."""
        self.run_composer("""
const p=page();await p.type('hello');await p.pick(A);await p.pick(B);
assert.deepEqual(p.files.files,[A,B]);const sending=p.send();await Promise.resolve();
assert.deepEqual(p.payload().files,[A,B]);await p.remove(1);await p.pick({...B});
await p.type('next');p.answer();await sending;
assert.equal(p.box.value,'next');assert.deepEqual(p.files.files,[B]);
assert.equal(storage.get('shaft.draft.wisp'),'next');
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
assert.equal(storage.get('shaft.draft.wisp'),undefined);
""")

    def test_w11_a_sent_message_leaves_no_retry_behind(self):
        """W11 (review 2026-10-24) said sent() left shaft.pending.<instance> behind. It did, but the reset after a send
        cleared it anyway (setFiles -> composerChanged), so this passed before too: it guards that sent() now clears
        it itself, whatever the reset does."""
        self.run_composer("""
const p=page();await p.type('hello');const sending=p.send();await Promise.resolve();
assert.ok(storage.get('shaft.pending.wisp'));p.answer();await sending;
assert.equal(storage.get('shaft.pending.wisp'),undefined);assert.equal(storage.get('shaft.draft.wisp'),undefined);
""")

    def test_w11_a_lost_send_the_feed_shows_is_let_go(self):
        """W11: data-sent-mids is drawn once with the page and the poll never refreshes it, so a send whose answer was
        lost but which landed stayed in the composer (and came back after a reload). The feed's turns (data-mid) say."""
        self.run_composer("""
const p=page([],false,[]);await p.type('hello');let sending=p.send();await Promise.resolve();const id=p.payload().mid;
p.fail();await sending;assert.equal(p.box.value,'hello');assert.equal(p.mid.value,id);
const q=page([],false,[id]);await q.load();assert.equal(q.box.value,'');assert.equal(q.mid.value,'');
assert.equal(storage.get('shaft.pending.wisp'),undefined);
storage.clear();const r=page([],false,[]);await r.type('again');sending=r.send();await Promise.resolve();
const rid=r.payload().mid;r.fail();await sending;
await r.swapped();assert.equal(r.box.value,'again');assert.equal(r.mid.value,rid);   // not in the feed yet: kept
r.feed.push(rid);await r.swapped();                                 // the next refresh brings its turn
assert.equal(r.box.value,'');assert.equal(r.mid.value,'');assert.equal(storage.get('shaft.pending.wisp'),undefined);
""")

    def test_w11_a_reload_says_the_files_of_an_unsent_attempt_are_gone(self):
        """W11: an attempt with files, restored after a reload, would be sent again without them, unsaid."""
        self.run_composer("""
const p=page();await p.type('see these');await p.pick(A,B);const sending=p.send();await Promise.resolve();p.fail();await sending;
assert.deepEqual(JSON.parse(storage.get('shaft.pending.wisp')).files,['a.txt','b.txt']);
const q=page();await q.load();assert.equal(q.box.value,'see these');
assert.match(q.toast(),/its files could not be kept: attach a[.]txt, b[.]txt again/);
""")

    def test_issue_21_edits_during_send_survive_reload(self):
        """Reverting #21 loses retry identity; reconciliation must also preserve edits made during a send."""
        self.run_composer("""
const p=page();await p.type('hello');const sending=p.send();await Promise.resolve();const id=p.payload().mid;
const pending=JSON.parse(storage.get('shaft.pending.wisp'));assert.equal(pending.mid,id);assert.equal(pending.text,'hello');
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
        parser.feed(str(chat_page("wisp", chat, [], [], {}, ["wisp"], "csrf")))
        self.assertEqual(json.loads(parser.attrs["data-sent-mids"]), ['m-"1'])

    def test_issue_70_newlines_and_queue_keys(self):
        """Reverting #70 sends Alt-Enter text and sends modifier messages as interjections."""
        self.run_composer("""
for(const modifier of ['altKey','shiftKey']) {
 storage.clear();const p=page();await p.type('abcd');p.box.setSelectionRange(1,3);
 await p.key({[modifier]:true});assert.equal(p.box.value,'a\\nd');assert.equal(p.payload(),undefined);
 assert.equal(p.box.selectionStart,2);assert.equal(storage.get('shaft.draft.wisp'),'a\\nd');
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
// M18 (review C6): a floating iPad keyboard leaves the page its height; its Return has no physical code: a new line
let p=page([],true);await p.type('floating');await p.key({});assert.equal(await p.lineBreak(),false);
assert.equal(p.payload(),undefined,'a Return with no hardware key behind it does not send');
p=page([],true);await p.type('hello');await p.key({code:'Enter'});
assert.equal(p.payload(),undefined, 'nothing is sent at keydown: the autocorrection comes after it');
assert.equal(await p.lineBreak('insertReplacementText'),false);     // autocorrect lands first, the Return still sends
assert.equal(await p.lineBreak(),true, 'the line break is cancelled');
assert.ok(p.payload(), 'Return with a hardware keyboard must send');assert.equal(p.payload().queue,'');
p.answer();await p.form.sending;
p=page([],true);await p.type('para');await p.key({code:'Enter'});assert.equal(await p.lineBreak('insertParagraph'),true);assert.ok(p.payload());
for (const modifier of ['shiftKey','altKey']) {
 p=page([],true);await p.type('two lines');await p.key({[modifier]:true,code:'Enter'});
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
            buttons.feed(str(chat_page('wisp', chat, [], [], {}, ['wisp'], 't')))
            self.assertEqual(buttons.labels, [['1', 'Queue after reply', queue_hidden], ['', 'Send now', False]], state)

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
  window: { addEventListener() {} }, location: { href: "https://shaft.test/i/wisp/chat" } });
const swap = () => { for (const f of handlers["shaft:swapped"]) f({ target: feed, detail: {} }); };
live = true; swap(); assert.equal(queue.hidden, false, "a reply started: offer to queue");
live = false; swap(); assert.equal(queue.hidden, true, "the reply ended: only Send now");
"""
        result = subprocess.run([NODE, "-e", script, str(JS)], text=True, capture_output=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_opening_keyboard_shrinks_and_closing_restores_long_draft(self):
        self.run_composer("""
const p=page([],true);p.box.scrollHeight=800;await p.type('long draft');
assert.equal(p.box.style.height,'300px');
p.resize(450);assert.equal(p.box.style.height,'150px');assert.equal(p.box.value,'long draft');
p.resize(900);assert.equal(p.box.style.height,'300px');assert.equal(p.box.value,'long draft');
""")
