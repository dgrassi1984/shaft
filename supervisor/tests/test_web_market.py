"""docs/plans/PLAN-pwa-market: the iPhone and iPad app at market standard. One test per finding fixed, named after it."""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from supervisor import web_actions, web_pages, web_views
from supervisor.tests.test_web_touch import CSS, JS, block

NODE = shutil.which("node")
PHONE = "(max-width: 720px), (pointer: coarse) and (max-height: 520px)"
LIVE = {"name": "wren", "model": "m", "state": "idle"}


def replying(answer: str = "", activity: list | None = None) -> dict:
    return {"id": "web-a", "state": "replying", "turns": [{"role": "user", "content": "hi", "ts": "2026-10-27T10:00:00"}],
            "reply": {"started": "2026-10-27T10:00:01", "activity": activity or ["Reading the journal"], "answer": answer},
            "created": "2026-10-27T10:00:00"}


class StreamingTest(unittest.TestCase):
    def test_c11_i9_the_answer_streams_in_as_it_is_written(self):
        """Reviews C11/I9: the server held the partial answer, the page showed "the answer appears when it is ready"."""
        live = str(web_views.live_activity(replying("**Summary.** The sweep fini")))
        self.assertIn('<div class="streaming" data-k="ans"><div class="md"><p><b>Summary.</b> The sweep fini</p>', live)
        self.assertNotIn("the answer appears when it is ready", str(web_views.chat_feed("wren", replying("x"), [])))
        self.assertNotIn("aria-live", live)          # VoiceOver must not re-read the whole reply at every refresh

    def test_before_the_first_word_a_working_line_and_dots(self):
        live = str(web_views.live_activity(replying("", ["Reading the journal", "Calling tool calendar.list"])))
        self.assertIn('<span class="working">Calling tool calendar.list</span> <span class="dim">· 2 steps</span>', live)
        self.assertIn('class="typing" data-k="wait"', live)
        self.assertNotIn(" open", re.search(r"<details[^>]*>", live).group(0))   # one line, not the expanded log


class HeaderTest(unittest.TestCase):
    def header(self, **live) -> str:
        return str(web_views.chat_header("wren", {**LIVE, **live}, {"id": "c1", "state": "idle"}, []))

    def test_i10_no_emoji_as_controls(self):
        """Review I10: ☰ 🕘 📄 ⏸ read as disabled and were blobs in dark mode; one SVG set, in the text's colour."""
        for state in ("idle", "paused by you", "sleeping", "in a cycle"):
            body = self.header(state=state)
            self.assertNotRegex(body, "[⏳💤⏸💬🕘📄☰]", state)
            self.assertIn('class="i"', body)

    def test_i11_three_controls_and_new_chat_not_beside_the_title(self):
        """Review I11: seven bordered boxes, Clear one tap from the title. Now Documents, New chat, ⋯."""
        body = self.header()
        tools = re.search(r'<div class="headtools">(.*)</header>', body, re.S).group(1)
        self.assertEqual(re.findall(r'aria-label="([^"]+)"', tools.split('<div class="menubody">')[0]),
                         ["Documents", "New chat", "wren: pages and actions"])
        self.assertIn('data-key="earlier-chats"', tools.split('<div class="menubody">', 1)[1])   # in ⋯

    def test_i13_kill_last_and_apart_and_not_on_overview_cards(self):
        """Review I13: Kill sat beside Pause on every card and among the ⋯ menu's pages."""
        buttons = str(web_actions.instance_buttons({"name": "wren", "state": "idle"}, delete=True))
        zone = buttons.index('class="actions dangerzone"')
        self.assertLess(buttons.index(">Pause<"), zone)
        self.assertGreater(buttons.index(">Kill<"), zone)
        self.assertGreater(buttons.index(">Delete…<"), zone)
        self.assertNotIn(">Kill<", str(web_actions.instance_buttons({"name": "wren", "state": "idle"}, kill=False)))
        self.assertIn("instance_buttons(i, kill=False)", Path(web_pages.__file__).read_text())
        menu = self.header()
        self.assertLess(menu.index("Reload / update"), menu.index('class="actions dangerzone"'))   # last in ⋯, found live


class ComposerTest(unittest.TestCase):
    def composer(self, state: str | None = None) -> str:
        chat = {"id": "web-a", "state": state, "turns": [], "created": "2026-10-27T10:00:00"} if state else None
        return str(web_views.chat_page("wren", chat, [], [], {}, ["wren"], "t"))

    def test_i7_one_send_arrow_and_the_other_ways_behind_it(self):
        """Review I7: Attach files + Send now / Queue after reply / Next cycle wrapped to two rows; one arrow now."""
        page = self.composer("replying")
        start = page.index('<div class="cbox">')
        bar = page[start:page.index("</form>", start)]
        self.assertIn('<button type="submit" class="send iconbtn" data-queue="" title="Send now" aria-label="Send now">',
                      bar)
        menu = bar[bar.index('class="menu sendmenu"'):]
        self.assertIn('data-queue="1"', menu)
        self.assertIn('data-background="1"', menu)
        self.assertIn('aria-label="More ways to send"', bar)
        self.assertIn('<span class="sr">Attach files</span>', bar)

    def test_c15_prose_field_asks_for_prose(self):
        self.assertIn('autocapitalize="sentences" autocorrect="on" spellcheck="true"', self.composer())

    def test_i2_a_tap_on_send_keeps_the_keyboard_and_sends(self):
        """Review I2: the tap closed the keyboard, the button moved under the finger and nothing was sent (3/3)."""
        self.assertRegex(JS, r'document\.addEventListener\("mousedown", \(ev\) => \{\n\s+const b = ev\.target\.closest && '
                             r'ev\.target\.closest\("form\.composer button\.send')
        self.assertIn("if (b && box && document.activeElement === box) ev.preventDefault();", JS)

    def test_i18_empty_chat_is_a_welcome_not_a_grey_line(self):
        page = self.composer()
        self.assertIn('<div class="emptychat" data-k="empty"><p class="big">Chat with wren</p>', page)


@unittest.skipUnless(NODE, "node is not installed")
class LongPressTest(unittest.TestCase):
    """The send arrow's long press: a tap opened the menu after sending (found live in the simulator, 2026-10-27)."""
    SCRIPT = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const handlers = {}, timers = [];
const menu = { open: false };
const form = { querySelector: q => q === ".sendmenu" ? menu : null };
const send = { closest: q => q === "form.composer button.send" ? send : q === "form.composer" ? form : null };
const document = { addEventListener: (t, f) => (handlers[t] = handlers[t] || []).push(f), readyState: "loading",
  getElementById: () => null,
  querySelector: () => null, querySelectorAll: q => q === "form.composer button.send" ? [send] : [], body: { classList: { contains: () => false, add() {}, toggle() {} } },
  documentElement: { style: { setProperty() {} } } };
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"), { document, navigator: {}, localStorage: {},
  window: { addEventListener() {}, matchMedia: () => ({ matches: true, addEventListener() {} }) },
  setTimeout: (f, ms) => { timers.push({ f, ms, live: true }); return timers.length; },
  clearTimeout: (k) => { if (timers[k - 1]) timers[k - 1].live = false; }, setInterval() {} });
const fire = (type, ev) => { for (const f of handlers[type] || []) f(ev); };
const run = () => timers.filter(t => t.ms === 500 && t.live).forEach(t => { t.live = false; t.f(); });
// a tap: down, up — the timer is cancelled and the menu stays shut
fire("pointerdown", { pointerType: "touch", pointerId: 1, target: send });
fire("pointerup", { pointerId: 1 });
run();
assert.equal(menu.open, false, "a tap does not open the menu");
// a long press: down, 500 ms with the finger still down — the menu opens, and its click does not send
fire("pointerdown", { pointerType: "touch", pointerId: 2, target: send });
run();
assert.equal(menu.open, true, "a long press opens the other ways to send");
let prevented = false;
fire("click", { target: send, preventDefault() { prevented = true; } });
assert.equal(prevented, true, "the long press's own click does not send");
// Codex review: a long press cancelled after the menu opened left its "no send" behind, and ate the next real Send
menu.open = false;
fire("pointerdown", { pointerType: "touch", pointerId: 3, target: send });
run();
fire("pointercancel", { pointerId: 3 });
prevented = false;
fire("click", { target: send, preventDefault() { prevented = true; } });
assert.equal(prevented, false, "after a cancelled long press, the next tap sends");
// Codex review: a long press on a message threw (its target test returned a boolean): the menu never opened
let asked = 0;
const turn = { classList: { contains: () => false }, querySelectorAll: () => { asked += 1; return []; } };
const text = { closest: q => q === ".feed .turn" ? turn : null };
fire("pointerdown", { pointerType: "touch", pointerId: 4, isPrimary: true, clientX: 10, clientY: 10, target: text });
run();
assert.equal(asked, 1, "a long press on a message's text opens its menu");
const link = { closest: q => q === ".feed .turn" ? turn : q.startsWith("a,") ? {} : null };
fire("pointerdown", { pointerType: "touch", pointerId: 5, isPrimary: true, clientX: 10, clientY: 10, target: link });
run();
assert.equal(asked, 1, "a long press on a link inside it is the link's");
'''

    def test_a_tap_never_opens_the_send_menu(self):
        js = Path(web_pages.__file__).parent / "web_static" / "web.js"
        result = subprocess.run([NODE, "-e", self.SCRIPT, str(js)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class MessageActionsTest(unittest.TestCase):
    def feed(self) -> str:
        chat = {"id": "web-a", "state": "idle", "created": "2026-10-27T10:00:00", "turns": [
            {"role": "user", "content": "q1", "ts": "2026-10-27T10:00:00"},
            {"role": "assistant", "content": "a1", "ts": "2026-10-27T10:00:05"},
            {"role": "user", "content": "q2", "ts": "2026-10-27T10:01:00"},
            {"role": "assistant", "content": "a2", "ts": "2026-10-27T10:01:05"}]}
        return str(web_views.chat_feed("wren", chat, []))

    def test_i12_actions_under_the_message_last_answer_marked(self):
        """Review I12: two bordered boxes in every message's header, bigger than short messages."""
        feed = self.feed()
        self.assertEqual(feed.count('<div class="msgactions">'), 4)
        self.assertNotRegex(feed, r'<div class="who">[^<]*(<span[^>]*>[^<]*</span>)?<button')   # not in the header
        self.assertEqual(re.findall(r'data-k="(a\d)"[^>]*data-last', feed), ["a3"])
        hover_none = block("(hover: none)")
        self.assertIn(".turn:not([data-last]) .msgactions:not(:focus-within)", hover_none)

    def test_decision_2_menu_by_long_press_right_click_and_keyboard(self):
        """The operator's call (2026-10-27): long-press / hover, "but ensure this works on desktops too"."""
        self.assertIn('document.addEventListener("contextmenu", (ev) => {', JS)
        self.assertIn("const keyboard = !ev.clientX && !ev.clientY;", JS)          # the context-menu key, Shift-F10
        self.assertIn('if (ev.key === "Escape" || ev.key === "Tab") { ev.preventDefault(); closeMsgMenu(true); }', JS)
        self.assertIn(".turn:hover .msgactions, .turn:focus-within .msgactions", CSS)


class IPadTest(unittest.TestCase):
    """The iPad review of the new build (2026-10-27, iPad Air 11-inch simulator)."""

    def test_p1_a_long_quote_never_widens_the_composer(self):
        """P1: a quote with a long URL made the page 1141 px wide on an 820 px iPad."""
        self.assertIn("form.composer { grid-template-columns: minmax(0, 1fr); }", CSS)
        self.assertIn("form.composer > * { min-width: 0; }", CSS)
        self.assertIn(".quotechip > span { overflow-wrap: anywhere; }", CSS)

    def test_p2_the_rail_toggle_shows_only_where_the_rail_is_a_sheet(self):
        """P2: the toggle and ✕ showed beside the iPad's permanent rail, and did nothing."""
        self.assertIn(".navbtn.iconbtn, nav button.closenav { display: none; }", CSS)
        self.assertIn(".chathead .navbtn.iconbtn, .pagebar .navbtn.iconbtn { display: inline-flex; }", block(PHONE))

    def test_p4_unpair_is_in_settings_not_at_the_foot_of_every_page(self):
        from supervisor import web_settings
        src = Path(web_settings.__file__).read_text()
        self.assertIn('data-post="/unpair"', src)
        self.assertIn("nav form.unpair { display: none; }", CSS)

    def test_p6_p7_shortcuts_line_and_send_toast(self):
        """P6: the shortcuts line stayed with the on-screen keyboard up. P7: "sent to kant" covered the header."""
        self.assertIn('if (keyboardUp()) document.body.classList.toggle("hwkbd", false);', JS)
        self.assertIn("if (!(r.ok && shown)) toast(", JS)

    def test_p13_the_pairing_box_takes_a_bare_token(self):
        page = str(web_pages.unpaired_page())
        self.assertIn('name="link" type="text" inputmode="url"', page)
        self.assertNotIn("--name phone-app</code>", page)


class RetryInPlaceTest(unittest.TestCase):
    def chat(self) -> dict:
        return {"id": "web-a", "state": "idle", "created": "2026-10-27T10:00:00", "turns": [
            {"role": "user", "content": "first", "ts": "2026-10-27T10:00:00"},
            {"role": "assistant", "content": "", "ts": "2026-10-27T10:00:05", "note": "error: CloudError: HTTP 503",
             "note_kind": "model", "note_retry": True, "retry_text": "first"}]}

    def test_p9_retry_folds_the_failed_try_and_the_model_never_sees_it(self):
        """iPad review P9: Retry added a second copy of the question and a second error under the first."""
        from supervisor import web_chat
        chat = self.chat()
        self.assertIn('data-retry-of="1"', str(web_views.chat_feed("wren", chat, [])))
        self.assertFalse(web_chat.mark_retried(chat, 1, "something else"))        # only the text that failed
        self.assertFalse(web_chat.mark_retried(chat, 0, "first"))
        self.assertTrue(web_chat.mark_retried(chat, 1, "first"))
        self.assertFalse(web_chat.mark_retried(chat, 1, "first"))                 # once
        chat["turns"].append({"role": "user", "content": "first", "ts": "2026-10-27T10:01:00"})
        self.assertEqual(web_chat._api_messages(chat), [{"role": "user", "content": "first"}])
        feed = str(web_views.chat_feed("wren", chat, []))
        self.assertEqual(feed.count(">first<"), 1)
        self.assertIn('<p class="dim retried" data-k="a1">The first try failed and was sent again', feed)
        self.assertNotIn("The model could not finish the reply", feed)

    def test_a_failure_that_may_have_run_tools_is_never_folded(self):
        from supervisor import web_chat
        chat = self.chat()
        chat["turns"][1]["note_retry"] = False
        self.assertFalse(web_chat.mark_retried(chat, 1, "first"))


class AlertsTest(unittest.TestCase):
    def test_i15_a_log_line_alert_reads_on_a_phone_in_its_own_words(self):
        """Review I15: raw log lines with Python dicts, all bold red, each a wall of text on a phone."""
        line = ("07:31:38 shaft: end live exit=1 broker_calls={'run': 2, 'web_search': 2} finish_reason='stop', "
                "reasoning_chars=7988 and more words")
        out = str(web_actions.alert_line({"id": "x", "text": line}))
        self.assertIn('<span class="dim">07:31:38</span> <b>shaft</b> <span class="alertwhat">end live exit=1', out)
        self.assertIn("…</span>", out)
        self.assertIn("<summary>Full line</summary><pre>07:31:38 shaft: end live", out)   # nothing lost, nothing reworded
        short = str(web_actions.alert_line({"id": "y", "text": "wren: code change FAILED tests"}))
        self.assertNotIn("Full line", short)
        self.assertIn("wren: code change FAILED tests", short)


class LayoutTest(unittest.TestCase):
    def test_i16_markdown_tables_keep_their_columns_on_a_phone(self):
        """Review I16: the phone's label/value table rule also hit table.md: an answer's columns were lost."""
        phone = block(PHONE)
        self.assertIn("table:not(.md) thead { display: none; }", phone)
        self.assertNotRegex(phone, r"(^|\n)\s*table thead \{ display: none; \}")

    def test_i6_config_rows_and_alerts_fit_the_phone(self):
        phone = block(PHONE)
        self.assertIn("table:not(.md) td > * { min-width: 0; }", phone)
        self.assertRegex(CSS, r"\.alerts \{[^}]*overflow-wrap: anywhere;")

    def test_i5_other_pages_have_a_bar_and_the_rail_is_a_sheet(self):
        """Review I5: in the installed app the journal had no way back and the rail filled half the screen."""
        page = str(web_views.frame("wren · journal", web_pages.Html("<p>x</p>"), ["wren"], "t", current="i:wren"))
        self.assertIn('<div class="pagebar"><a class="iconbtn back" href="/i/wren/chat" aria-label="Back to the chat">', page)
        self.assertIn('data-toggle-nav aria-label="Chats and pages"', page.split('class="pagebar"')[1])
        chat = str(web_views.chat_page("wren", None, [], [], {}, ["wren"], "t"))
        self.assertNotIn('class="pagebar"', chat)
        # every page with the rail: the overview builds its own page (found live: it had no bar)
        self.assertIn('return page("Overview", join([pagebar("Overview"), body])', Path(web_pages.__file__).read_text())
        self.assertIn(".tabs { flex-wrap: nowrap; overflow-x: auto;", CSS)

    def test_i1_the_chat_page_never_scrolls_itself(self):
        """Review I1: focusing the field scrolled the whole page off screen after the last visualViewport event."""
        self.assertIn('if (document.body.classList.contains("chatapp") && window.scrollY && !(vv && vv.scale > 1.01)) '
                      'window.scrollTo(0, 0);', JS)

    def test_c7_pinch_zoom_is_not_a_keyboard(self):
        fit = JS[JS.index("function fitViewport()"):]
        self.assertLess(fit.index("if (vv.scale > 1.01) return;"), fit.index('root.setProperty("--vvh"'))

    def test_i3_landscape_phone_is_compact(self):
        landscape = block("(pointer: coarse) and (max-height: 520px)")
        self.assertIn(".chathead .sub { display: none; }", landscape)

    def test_c12_choosing_a_document_says_it_is_loading_until_it_is_there(self):
        """Review C12: on a phone's network the old document stayed up, looking current, after a tap on another."""
        refresh = JS[JS.index("  function refreshDocs() {"):]
        refresh = refresh[:refresh.index("\n  }\n")]
        self.assertIn('panel.classList.add("loading");', refresh)
        self.assertIn('panel.setAttribute("aria-busy", "true");', refresh)
        # cleared by an answer for the one asked for — a 304 too, which fires no swap (it stuck there, found in review)
        self.assertIn("{ fails = 0; recovered(); settled(el, url); }", JS)
        self.assertIn("aside.docs.loading > :not(.docshead) { opacity: .45; pointer-events: none;", CSS)

    def test_c9_i19_toasts_at_the_top_clear_of_the_composer_and_notch(self):
        rule = CSS[CSS.rindex("#toast { top:"):]
        rule = rule[:rule.index("}")]
        self.assertIn("top: calc(.5rem + env(safe-area-inset-top)); bottom: auto;", rule)
        self.assertIn("env(safe-area-inset-left)", rule)


if __name__ == "__main__":
    unittest.main()
