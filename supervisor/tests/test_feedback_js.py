"""Run the shipped feedback controls against small DOM stubs, as the other web JS tests do."""
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor.tests.node_helpers import NODE_TIMEOUT_S

NODE = shutil.which("node")
JS = (Path(__file__).resolve().parents[1] / "web_static" / "web.js").read_text()


@unittest.skipUnless(NODE, "node is not installed")
class FeedbackJsTest(unittest.TestCase):
    def test_down_sheet_cancel_keep_and_delegation_after_refresh(self):
        source = JS[JS.index('  // Feedback stays in the supervisor.'):JS.index("  // A message's actions as a menu")]
        script = r'''
const assert = require('node:assert/strict');
const listeners = {}, requests = [], dialogs = [];
let refreshes = 0, focused = 0, modal = null, finish, fail = false;
const feed = {_tick:()=>refreshes++};
const csrf = ()=>'token', pairAgain = ()=>false, textOf = t=>t, toast = ()=>{}, syncSheets = ()=>{};
const document = {
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  querySelector(sel) {return sel === '.feed[data-poll]' ? feed : modal;},
  body:{appendChild(d){modal=d;dialogs.push(d);}},
  createElement(tag) {
    assert.equal(tag, 'dialog');
    const handlers = {}, save = {}, reason = {value:'',focus(){focused++;}}, cancel = {};
    const form = {addEventListener(t,fn){handlers[t]=fn;},querySelector(){return save;}};
    cancel.addEventListener = (t,fn)=>{handlers.cancel=fn;};
    return {handlers, reason, save, setAttribute(){},
      querySelector(sel){return sel==='form'?form:sel==='textarea'?reason:cancel;},
      addEventListener(t,fn){handlers[t]=fn;},showModal(){},
      close(){handlers.close();},remove(){modal=null;}};
  }
};
const fetch = async (url, opts) => {
  requests.push({url,opts});
  await new Promise(resolve=>{finish=resolve;});
  return {ok:!fail,status:fail?409:200,text:async()=>fail?'bad target':'Feedback kept.'};
};
function controls(pressed='false') {
  const group = {dataset:{feedbackName:'wren',feedbackTarget:JSON.stringify({kind:'chat',cid:'web-abc',k:1}),
    feedbackId:'f-1',feedbackReason:'earlier reason'},querySelectorAll(){return [up,down];}};
  const button = thumb=>({dataset:{thumb},getAttribute(){return pressed;},closest(){return group;},
    isConnected:true,focus(){focused++;}});
  const up=button('up'),down=button('down');
  return {group,up,down};
}
const click = b=>listeners.click.forEach(fn=>fn({target:{closest:()=>b}}));
const settle = async()=>{finish();await new Promise(resolve=>setImmediate(resolve));};
eval(process.argv[1]);
(async()=>{
  assert.equal(listeners.click.length,1,'one delegated binding on the stable document');
  let {down} = controls();
  click(down);
  assert.match(modal.innerHTML,/What was wrong\? \(optional\)/);
  assert.match(modal.innerHTML,/Just keep it/);
  assert.match(modal.innerHTML,/Cancel/);
  assert.doesNotMatch(modal.innerHTML,/Send to/);
  assert.equal(modal.reason.value,'earlier reason');
  assert.equal(requests.length,0,'opening the sheet stores nothing');
  modal.handlers.cancel();
  assert.equal(modal,null);
  assert.equal(requests.length,0,'Cancel stores nothing');
  click(down);
  modal.reason.value='wrong answer';
  const dialog=modal;
  const sending=dialog.handlers.submit({preventDefault(){}});
  await dialog.handlers.submit({preventDefault(){}});
  assert.equal(requests.length,1,'double submit produces one post');
  assert.equal(dialog.save.disabled,true);
  assert.equal(requests[0].url,'/feedback/wren');
  assert.equal(requests[0].opts.headers['X-CSRF-Token'],'token');
  assert.equal(requests[0].opts.body.get('thumb'),'down');
  assert.equal(requests[0].opts.body.get('reason'),'wrong answer');
  assert.equal(requests[0].opts.body.get('cid'),'web-abc');
  assert.equal(requests[0].opts.body.get('k'),'1');
  await settle();await sending;
  assert.equal(modal,null);
  assert.equal(refreshes,1);
  // New nodes from a keyed feed refresh use the same listener.
  let {up} = controls();
  click(up);
  click(controls().up); // a refresh during the request must not duplicate it
  assert.equal(requests.length,2);
  assert.equal(requests[1].opts.body.get('op'),'rate');
  assert.equal(requests[1].opts.body.get('thumb'),'up');
  await settle();
  click(controls('true').down);
  assert.equal(modal,null,'tapping a lit down clears without a sheet');
  assert.equal(requests[2].opts.body.get('op'),'clear');
  assert.equal(requests[2].opts.body.get('id'),'f-1');
  await settle();
  fail=true;
  click(controls().down);
  const refused=modal;
  const saving=refused.handlers.submit({preventDefault(){}});
  await settle();await saving;
  assert.equal(modal,refused,'a refused rating keeps the reason editable');
  assert.equal(refused.save.disabled,false);
  assert.equal(listeners.click.length,1);
})().then(()=>console.log('ok')).catch(e=>{console.error(e);process.exitCode=1;});
'''
        result = subprocess.run([NODE, "-e", script, source], capture_output=True, text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_message_action_menu_dispatches_both_thumbs(self):
        from supervisor.tests.test_web_review_js import function
        script = r'''
const assert = require('node:assert/strict');
let clicks = [], buttons = [];
const act = (dataset,pressed='false')=>({dataset,innerHTML:'icon',
  getAttribute:()=>pressed,click:()=>clicks.push(dataset)});
const acts = [act({copy:''}),act({thumb:'up'}),act({thumb:'down'},'true')];
const turn={querySelectorAll:selector=>{
  assert.match(selector,/\.who \[data-thumb\]/,'sent-card controls must enter the menu');return acts;
},classList:{add(){}}};
const menu={style:{},offsetWidth:100,offsetHeight:100,appendChild:b=>buttons.push(b)};
const document={createElement:()=>({setAttribute(){},append(t){this.label=t;},
  addEventListener(t,fn){this.click=fn;}}),createTextNode:t=>t};
const msgMenuBox=()=>menu,closeMsgMenu=()=>{},touchOnly=()=>false;
const window={innerWidth:800,innerHeight:600};
eval(process.argv[1]);
assert.equal(openMsgMenu(turn,10,10,false),true);
assert.deepEqual(buttons.map(b=>b.label),['Copy','thumbs up','Clear thumbs down']);
buttons[1].click();buttons[2].click();
assert.deepEqual(clicks,[{thumb:'up'},{thumb:'down'}]);
console.log('ok');
'''
        result = subprocess.run([NODE, "-e", script, function("openMsgMenu")], capture_output=True,
                                text=True, timeout=NODE_TIMEOUT_S)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(result.stdout.strip(), "ok")
