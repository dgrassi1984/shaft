"""Navigation interactions execute the shipped JavaScript against small DOM stubs in Node."""
import re
import shutil
import unittest

from supervisor.tests.test_web_review_js import JS, node
from supervisor.tests import test_web_threads_js


def function(name):
    match = re.search(r'^  (?:async )?function ' + name + r'\(.*?^  \}\n', JS, re.S | re.M)
    assert match, name
    return match[0]


ROWS = r'''
const assert = require('node:assert/strict');
const cl = () => ({ contains: x => x === 'threadrow', add() {}, remove() {} });
const parent = { children: [], dataset: {}, insertBefore(row, anchor) {
  if (row === anchor) return;
  this.children = this.children.filter(x => x !== row);
  const at = anchor ? this.children.indexOf(anchor) : this.children.length;
  this.children.splice(at, 0, row);
}};
for (const id of ['a', 'b', 'c', 'd']) parent.children.push({ dataset: { orderId: id, name: 'example', title: id },
  parentElement: parent, classList: cl(), get nextSibling() { return parent.children[parent.children.indexOf(this) + 1] || null; } });
const ids = () => parent.children.map(x => x.dataset.orderId);
const a = parent.children[0], b = parent.children[1], c = parent.children[2], d = parent.children[3];
'''


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class NavigationJS(unittest.TestCase):
    def test_move_placement_and_authoritative_order(self):
        """Optimistic thread placement converges to the server's order and announces its position."""
        node(ROWS + r'''
let railMoving = false, railGeneration = 0, refreshed = 0, announcement = {};
const region = () => announcement, csrf = () => 'token', toast = () => assert.fail('unexpected failure');
const document = { querySelector: () => ({ dataset: {}, _tick() { refreshed++; } }) };
const fetch = async (url, options) => {
  assert.equal(url, '/chat/example/move'); assert.equal(options.body.get('id'), 'a');
  assert.equal(options.body.get('after'), 'c'); assert.equal(options.headers['X-CSRF-Token'], 'token');
  return { ok: true, text: async () => '' };
};
class DOMParser { parseFromString() { return { querySelector: () => ({ dataset: { ids: '["d","b","c","a"]', rev: '2' } }) }; } }
''' + ''.join(function(n) for n in ['railItems', 'railPlace', 'railAnnounce', 'railRefresh', 'railSave']) + r'''
railPlace(a, 2); assert.deepEqual(ids(), ['b','c','a','d']);
(async () => { await railSave(a, 0); assert.deepEqual(ids(), ['d','b','c','a']);
assert.equal(announcement.textContent, 'Moved a to 4 of 4'); assert.equal(refreshed, 1);
assert.equal(railMoving, false); assert.equal(railGeneration, 2); })().catch(e => { console.error(e); process.exitCode = 1; });
''')

    def test_instance_move_uses_the_instance_endpoint(self):
        """Instance grips post a name and anchor to the shared instance order route."""
        node(ROWS + r'''
let railMoving = false, railGeneration = 0;
const region = () => ({}), csrf = () => 'token', toast = () => assert.fail('unexpected failure');
const document = { querySelector: () => null };
class DOMParser { parseFromString() { return {querySelector: () => null}; } }
const fetch = async (url, options) => {
  assert.equal(url, '/instances/move'); assert.equal(options.body.get('name'), 'c');
  assert.equal(options.body.get('after'), ''); assert.equal(options.body.has('id'), false);
  return {ok:true, text:async()=>''};
};
''' + ''.join(function(n) for n in ['railItems', 'railPlace', 'railAnnounce', 'railRefresh', 'railSave']) + r'''
c.classList.contains = () => false;
railPlace(c, 0);
(async () => {await railSave(c, 2); assert.deepEqual(ids(), ['c','a','b','d']); })()
.catch(e => {console.error(e);process.exitCode=1;});
''')

    def test_failed_move_reverts_but_ambiguous_failure_refetches(self):
        """A refusal restores the previous order; a lost response preserves uncertainty until refetch."""
        node(ROWS + r'''
let railMoving = false, railGeneration = 0, mode = 'refused', refreshes = 0;
const region = () => ({}), csrf = () => 'token', toast = () => {}, textOf = x => x;
const document = { querySelector: () => ({ dataset: {}, _tick() { refreshes++; } }) };
const fetch = async () => { if (mode === 'lost') throw Error('lost'); return { ok: false, text: async () => 'refused' }; };
''' + ''.join(function(n) for n in ['railItems', 'railPlace', 'railAnnounce', 'railRefresh', 'railSave']) + r'''
(async () => { railPlace(a, 3); await railSave(a, 0); assert.deepEqual(ids(), ['a','b','c','d']);
mode = 'lost'; railPlace(a, 3); await railSave(a, 0); assert.deepEqual(ids(), ['b','c','d','a']);
assert.equal(refreshes, 2); })().catch(e => { console.error(e); process.exitCode = 1; });
''')

    def test_disclosure_navigation_overrides_storage_once_then_survives_polls(self):
        """Navigation expands the selected instance once; a manual collapse survives subsequent swaps."""
        node(r'''
const assert = require('node:assert/strict');
const railCollapsed = new Map(); let railNavigationExpanded = false, railEditing = false;
const localStorage = { getItem: () => '1' };
const button = { setAttribute(k,v) { this[k] = v; } }, list = {};
const group = { dataset: { name: 'example' }, classList: { contains: () => true, toggle() {} },
  querySelector: s => s === '[data-disclose]' ? button : list };
const rail = { classList: { toggle() {} }, querySelector: () => null, querySelectorAll: () => [group] };
''' + function('railDisclosure') + r'''
railDisclosure(rail); assert.equal(list.hidden, false); assert.equal(button['aria-expanded'], 'true');
railCollapsed.set('example', true); railDisclosure(rail); assert.equal(list.hidden, true);
assert.equal(button['aria-expanded'], 'false');
''')

    def test_busy_rail_blocks_lift_move_and_menu(self):
        """Rail polls stop during pointer/keyboard lifts, move requests, and row menus."""
        node(r'''
const assert = require('node:assert/strict');
let railInteraction = null, railMoving = false, railMenu = null;
const selectingIn = () => false, document = { activeElement: null };
const el = { classList: { contains: () => true }, querySelectorAll: () => [] };
''' + function('busyIn') + r'''
assert.equal(busyIn(el), false);
railInteraction = {}; assert.equal(busyIn(el), true); railInteraction = null;
railMoving = true; assert.equal(busyIn(el), true); railMoving = false;
railMenu = {}; assert.equal(busyIn(el), true);
''')

    def test_poll_response_from_before_move_is_discarded(self):
        """A completed move invalidates an earlier rail poll even after the busy flag clears."""
        node(r'''
const assert = require('node:assert/strict');
let railGeneration = 0, finish;
const document = { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} };
const window = { addEventListener() {}, removeEventListener() {} };
const el = { dataset: { poll: '/fragment/rail', now: '1' }, isConnected: true,
  classList: { contains: () => true }, getClientRects: () => [1], set innerHTML(v) { assert.fail('stale swap'); } };
const setTimeout = () => 1, clearTimeout = () => {}, POLL_LIMIT_MS = 15000;
const busyIn = () => false, pairAgain = () => false, checkVersion = () => {}, recovered = () => {}, settled = () => {};
const fetch = () => new Promise(resolve => { finish = resolve; });
class AbortController { constructor() { this.signal = {}; } abort() {} }
''' + function('poll') + r'''
poll(el); el._tick(); railGeneration += 2;
finish({ ok: true, status: 200, text: async () => '<p>old order</p>' });
setImmediate(() => assert.equal(el.dataset.last, undefined));
''')

    def test_draft_migration_preserves_collisions_and_pending_destination(self):
        """Main adopts a legacy draft once without overwriting either draft or changing a known send destination."""
        node(r'''
const assert = require('node:assert/strict');
const storage = new Map([['shaft.draft.example', 'legacy'], ['shaft.draft.example/from', 'main'],
 ['shaft.quote.example', '["quote"]'], ['shaft.pending.example', '{"cid":"web-old","mid":"m1"}']]);
const localStorage = { getItem: k => storage.get(k) ?? null, setItem: (k,v) => storage.set(k,v), removeItem: k => storage.delete(k) };
const toast = () => {};
''' + function('migrateMainDraft') + r'''
migrateMainDraft({ dataset: { draft: 'example/from' } });
assert.equal(storage.get('shaft.draft.example'), 'legacy'); assert.equal(storage.get('shaft.draft.example/from'), 'main');
assert.equal(storage.get('shaft.quote.example'), '["quote"]');
assert.equal(storage.get('shaft.quote.example/from'), undefined);
storage.delete('shaft.draft.example/from'); storage.delete('shaft.main-migrated.example');
migrateMainDraft({ dataset: { draft: 'example/from' } });
assert.equal(storage.get('shaft.draft.example/from'), 'legacy');
assert.deepEqual(JSON.parse(storage.get('shaft.pending.example/from')), {cid:'web-old',mid:'m1'});
''')

    def test_keyboard_lift_cancel_drop_and_pointer_threshold(self):
        """Shipped event handlers cancel keyboard moves and require four pixels before committing a pointer drag."""
        harness = test_web_threads_js.HARNESS.split('(async () => {', 1)[0]
        node(harness + r'''
const parent = { children: [], dataset: {}, insertBefore(row, anchor) {
  if (row === anchor) return;
  this.children = this.children.filter(x => x !== row);
  this.children.splice(anchor ? this.children.indexOf(anchor) : this.children.length, 0, row);
}};
for (const [index, id] of ['a','b','c'].entries()) parent.children.push({
  dataset: {orderId:id,name:'example',title:id}, parentElement:parent,
  classList: {contains: x => x === 'threadrow',add(){},remove(){}},
  get nextSibling() { return parent.children[parent.children.indexOf(this)+1] || null; },
  getBoundingClientRect: () => ({top:index*40,height:40}) });
const row = parent.children[0];
const grip = { closest: q => q === '[data-grip]' ? grip : q === '.threadrow, .railinstance' ? row : null,
  setAttribute(){}, setPointerCapture(){}, focus(){} };
const ids = () => parent.children.map(x=>x.dataset.orderId);
document.querySelector = q => q === 'nav' ? {scrollTop:0,getBoundingClientRect:()=>({top:0,bottom:500})} : null;
function fire(type, options={}) { const ev = { target:grip, key:'', code:'', button:0, pointerId:1,
  pointerType:'mouse', clientX:0, clientY:0, preventDefault(){this.defaultPrevented=true;}, ...options };
  (handlers[type] || []).forEach(fn=>fn(ev)); }
fire('keydown',{key:' ',code:'Space'}); fire('keydown',{key:'ArrowDown'});
assert.deepEqual(ids(),['b','a','c']); fire('keydown',{key:'Escape'});
assert.deepEqual(ids(),['a','b','c']); assert.equal(sent.length,0);
fire('pointerdown'); fire('pointermove',{clientY:3}); fire('pointerup',{clientY:3});
assert.deepEqual(ids(),['a','b','c']); assert.equal(sent.length,0);
fire('pointerdown'); fire('pointermove',{clientY:110}); fire('pointerup',{clientY:110});
assert.deepEqual(ids(),['b','c','a']); assert.equal(sent[0][0],'/chat/example/move');
assert.equal(JSON.stringify(sent[0][1]),JSON.stringify({id:'a',after:'c'}));
''', str(test_web_threads_js.WEB_JS), 'thread')

    def test_close_menu_posts_the_existing_action_route(self):
        """The row menu closes the chosen thread through the web's existing action endpoint."""
        node(r'''
const assert = require('node:assert/strict');
const csrf = () => 'token', toast = () => assert.fail('unexpected error'), railRefresh = () => {};
const document = {dispatchEvent() {}}, Event = class {};
const fetch = async (url, options) => { assert.equal(url, '/action/chat-end');
  assert.equal(options.body.get('cid'), 'web-topic'); return {ok:true}; };
const row = {dataset:{name:'example',orderId:'web-topic'},querySelector:()=>null};
''' + function('railAction') + r'''
railAction(row, 'end');
''')

    def test_compose_shortcut_uses_physical_key_and_focused_composer_keeps_option_arrows(self):
        """KeyO composes despite keyboard layout; plain Option arrows keep the caret in a nonempty composer."""
        harness = test_web_threads_js.HARNESS.split('(async () => {', 1)[0]
        node(harness + r'''
let newThreads = 0;
const form = { requestSubmit() { newThreads++; } };
document.querySelector = q => q === '.chathead form.newthread' ? form : null;
const target = { closest: () => null, matches: () => false };
const fire = options => { const ev = { target, key: 'Ø', code: 'KeyO', shiftKey: true, metaKey: true,
  preventDefault() { this.defaultPrevented = true; }, ...options }; handlers.keydown.forEach(fn => fn(ev)); return ev; };
assert.equal(fire({}).defaultPrevented, true); assert.equal(newThreads, 1);
const box = { value: 'draft', closest: q => q === 'input, textarea, select, [contenteditable]' ? box : null,
  matches: () => true };
assert.equal(fire({ target: box, key: 'ArrowDown', code: 'ArrowDown', altKey: true, metaKey: false, shiftKey: false }).defaultPrevented, undefined);
''', str(test_web_threads_js.WEB_JS), 'thread')


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class HeaderPolishJS(unittest.TestCase):
    def test_manage_restores_saved_choice_and_toggle_persists_without_storage_dependency(self):
        """Page defaults apply until a manual choice; initialization and polling do not overwrite it."""
        code = JS[JS.index('  function manageState('):JS.index('  function switchConversation(')]
        node(r'''
const assert = require('node:assert/strict'), handlers = {};
const document = { addEventListener: (type, fn) => handlers[type] = fn };
let saved = null, writes = 0, broken = false;
const localStorage = {getItem() {if (broken) throw Error('blocked'); return saved;},
  setItem(k,v) {assert.equal(k,'shaft.manage.open'); if(broken) throw Error('blocked'); saved=v; writes++;}};
const details = {open:false,matches:()=>true};
const root = {querySelectorAll:()=>[details]};
''' + code + r'''
manageState(root); handlers.toggle({target:details}); assert.equal(writes,0);
details.open=true; handlers.toggle({target:details}); assert.equal(saved,'1');
const next={open:false,matches:()=>true}; manageState({querySelectorAll:()=>[next]});
assert.equal(next.open,true); handlers.toggle({target:next}); assert.equal(writes,1);
next.open=false; handlers.toggle({target:next}); assert.equal(saved,'0');
broken=true; const fallback={open:true,matches:()=>true};
manageState({querySelectorAll:()=>[fallback]}); assert.equal(fallback.open,true);
fallback.open=false; assert.doesNotThrow(()=>handlers.toggle({target:fallback}));
''')

    def test_switcher_mouse_keyboard_compose_rename_and_focus_return(self):
        """The title opens a focused picker, with keyboard movement and the existing compose and rename actions."""
        code = JS[JS.index('  function switchConversation('):JS.index('  function railItems(')]
        node(r'''
const assert=require('node:assert/strict'), handlers={}; let submits=0, renamed=0;
const document={activeElement:null,addEventListener:(type,fn)=>handlers[type]=fn,
 querySelector:()=>menu.hidden?null:menu};
function item() {return {focus(){document.activeElement=this;}};}
const items=[item(),item(),item(),{...item(),closest:()=>null}]; const renameInput={focus(){renamed++;}};
const more={open:false}, rename={open:false,querySelector:()=>renameInput};
const opener={...item(),expanded:'false',setAttribute(k,v){this.expanded=v;},getAttribute(){return this.expanded;},closest:()=>head};
const menu={hidden:true,closest:()=>head,querySelector:()=>items[1],querySelectorAll:()=>items,
 contains:target=>items.includes(target)};
const head={querySelector:q=>({'[data-switch-conversation]':opener,'.conversation-switcher':menu,
 'form.newthread':{requestSubmit(){submits++;}},'.renamemenu':rename,'.chatmenu':more})[q]};
const click=kind=>handlers.click({target:{closest:q=>q===kind?(kind==='[data-switch-conversation]'?opener:{}):null}});
const key=k=>handlers.keydown({key:k,preventDefault(){}});
''' + code + r'''
click('[data-switch-conversation]'); assert.equal(menu.hidden,false); assert.equal(opener.expanded,'true');
assert.equal(document.activeElement,items[1]); key('ArrowDown'); assert.equal(document.activeElement,items[2]);
key('ArrowDown'); assert.equal(document.activeElement,items[3]);
// A Close thread form button stays in the picker tab order; its normal submit owns the confirmation.
handlers.click({target:items[3]}); assert.equal(menu.hidden,false);
key('ArrowDown'); assert.equal(document.activeElement,items[0]); key('End'); assert.equal(document.activeElement,items[3]);
key('Home'); assert.equal(document.activeElement,items[0]); key('Escape');
assert.equal(menu.hidden,true); assert.equal(document.activeElement,opener);
click('[data-switch-conversation]'); click('[data-switch-new]'); assert.equal(submits,1); assert.equal(menu.hidden,true);
click('[data-switch-conversation]'); click('[data-switch-rename]'); assert.equal(renamed,1);
assert.equal(more.open,true); assert.equal(rename.open,true);
click('[data-switch-conversation]'); click('outside'); assert.equal(menu.hidden,true);
''')

    def test_documents_autopen_requires_touched_conversation_or_explicit_open(self):
        """A saved close wins even after a document arrives; a blocked storage API does not break opening."""
        node(r'''
const assert=require('node:assert/strict'); let closed=null, opened=null, broken=false, wide=true, touched=false;
const localStorage={getItem(k){if(broken)throw Error('blocked');return k==='shaft.docs.closed'?closed:opened;}};
const window={matchMedia:()=>({matches:wide})};
const classes=new Set();
const grid={dataset:{conversationDocs:'0'},classList:{contains:x=>classes.has(x),add:x=>classes.add(x)},
 querySelector:()=>touched?{}:null,getBoundingClientRect:()=>({width:1000})};
''' + function('autoDocs') + r'''
autoDocs(grid); assert.equal(classes.has('showdocs'),false);
grid.dataset.conversationDocs='1'; autoDocs(grid); assert.equal(classes.has('showdocs'),true);
classes.clear(); closed='1'; autoDocs(grid); assert.deepEqual([...classes],['hidedocs']);
classes.clear();closed=null;wide=false;autoDocs(grid);assert.equal(classes.has('showdocs'),false);
opened='1'; autoDocs(grid); assert.equal(classes.has('showdocs'),true);
classes.clear();opened=null;wide=true;grid.dataset.conversationDocs='0';touched=true;broken=true;
autoDocs(grid);assert.equal(classes.has('showdocs'),true);
''')
