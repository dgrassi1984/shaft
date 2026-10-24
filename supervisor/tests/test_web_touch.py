"""docs/plans/PLAN-pwa P0: the web UI on a phone — fields iOS does not zoom into, finger-sized targets, nothing behind hover,
the phone layout held sideways too, and Return as a new line where there is no Shift key."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from supervisor import web_views

STATIC = Path(web_views.__file__).parent / "web_static"
CSS = (STATIC / "web.css").read_text()
JS = (STATIC / "web.js").read_text()


def block(query: str) -> str:
    """Every rule inside the @media blocks with exactly this query."""
    out, at = [], 0
    while (i := CSS.find(f"@media {query} {{", at)) >= 0:
        depth, j = 0, CSS.index("{", i)
        for k in range(j, len(CSS)):
            depth += CSS[k] == "{"
            depth -= CSS[k] == "}"
            if depth == 0:
                out.append(CSS[j + 1:k])
                at = k
                break
    return "\n".join(out)


class TouchTest(unittest.TestCase):
    def test_no_field_ios_zooms_into(self):
        """iOS Safari zooms the page into any field under 16 px when it is tapped (the composer was 15 px)."""
        coarse = block("(pointer: coarse)")
        for sel in ("input", "select", "textarea", "form.composer textarea", "form.docedit textarea"):
            self.assertRegex(coarse, rf"(^|[\s,]){re.escape(sel)}[\s,][^{{]*\{{[^}}]*font-size: 16px", sel)
        self.assertIn("text-size-adjust: 100%", CSS)

    def test_finger_sized_targets(self):
        coarse = block("(pointer: coarse)")
        self.assertRegex(coarse, r"button, button\.small, button\.act[^{]*\{[^}]*min-height: 44px")
        self.assertRegex(coarse, r"\.chip \.unpick \{[^}]*min-width: 44px; min-height: 44px")

    def test_nothing_waits_for_a_hover(self):
        self.assertRegex(block("(hover: none)"), r"\.icon, \.prewrap > \.copy \{ opacity: 1; \}")

    def test_the_phone_layout_holds_sideways(self):
        """A phone held sideways is wider than 720 px: it got the desktop's three columns."""
        self.assertNotIn("@media (max-width: 720px) {", CSS)
        self.assertGreaterEqual(CSS.count("@media (max-width: 720px), (pointer: coarse) and (max-height: 520px)"), 4)

    def test_the_menu_stays_on_screen(self):
        phone = block("(max-width: 720px), (pointer: coarse) and (max-height: 520px)")
        self.assertRegex(phone, r"\.menubody \{ position: fixed; left: \.5rem; right: \.5rem;")

    def test_return_is_a_new_line_where_there_is_no_shift_key(self):
        self.assertIn('window.matchMedia("(hover: none) and (pointer: coarse)")', JS)
        self.assertRegex(JS, r'ev\.key === "Enter" && !ev\.isComposing && \(!touchOnly\(\) \|\| ev\.metaKey \|\| ev\.ctrlKey\)')   # modifiers: #70
        composer = str(web_views.chat_page("wren", None, [], [], {}, ["wren"], "t"))
        self.assertIn('placeholder="Message wren"', composer)              # no "Enter sends" in the field itself
        self.assertIn('<span class="dim keys">Enter sends · Alt-Enter new line · ⌘-Enter queues', composer)


if __name__ == "__main__":
    unittest.main()


class PhoneLayoutTest(unittest.TestCase):
    """docs/plans/PLAN-pwa P2: on a phone the chat is the whole screen."""
    PHONE = "(max-width: 720px), (pointer: coarse) and (max-height: 520px)"

    def test_the_page_is_as_tall_as_the_keyboard_leaves(self):
        phone = block(self.PHONE)
        self.assertIn("body:has(.chatgrid) { height: var(--vvh, 100dvh); overflow: hidden; }", phone)
        self.assertIn('root.setProperty("--vvh", `${Math.round(vv.height)}px`)', JS)
        self.assertIn('window.visualViewport.addEventListener("resize", fitViewport)', JS)
        self.assertNotIn("100dvh - 19rem", CSS)                    # the guess the feed was sized by

    def test_the_rail_and_the_documents_are_sheets(self):
        phone = block(self.PHONE)
        self.assertIn("body:has(.chatgrid) nav { display: none; }", phone)
        self.assertRegex(phone, r"body\.navopen:has\(\.chatgrid\) nav \{ display: flex;[^}]*position: fixed; inset: 0;")
        page = str(web_views.chat_page("wren", None, [], [], {}, ["wren"], "t"))
        self.assertIn('class="navbtn" data-toggle-nav aria-label="Chats and pages"', page)
        self.assertIn("data-close-docs", str(web_views.docs_panel("wren", [], "", None, "none", set())))
        self.assertIn('class="small closenav" data-toggle-nav aria-label="Close the chats"', page)  # no Escape key
        self.assertIn("showDocs(true, a);", JS)                             # a card opens the sheet

    def test_issue_24_notices_above_sheets_and_landscape_insets(self):
        """Reverting #24 hides feedback below sheets and puts sheet/menu content under the landscape notch."""
        sheet_layers = [int(z) for z in re.findall(r'position: fixed; inset: (?:0|var\(--vvtop, 0px\) 0 var\(--vvbottom, 0px\));\s*'
                                                  r'z-index: (\d+)', CSS)]
        self.assertTrue(sheet_layers)
        self.assertEqual(set(sheet_layers), {55, 60})
        for selector in ("#toast", "#netnote"):
            rule = re.search(re.escape(selector) + r" \{([^}]+)\}", CSS).group(1)
            layer = re.search(r'z-index: (\d+)', rule)
            self.assertIsNotNone(layer, selector)
            self.assertGreater(int(layer.group(1)), max(sheet_layers))
        for selector in ("body.navopen:has(.chatgrid) nav", ".chatgrid.showdocs > aside.docs", ".menubody"):
            rules = re.findall(re.escape(selector) + r" \{([^}]+)\}", CSS)
            fixed = [rule for rule in rules if "position: fixed" in rule]
            self.assertTrue(fixed, selector)
            for rule in fixed:
                for side in ("left", "right"):
                    self.assertIn(f"max(1rem, env(safe-area-inset-{side}))", rule)

    def test_the_header_is_one_line(self):
        phone = block(self.PHONE)
        self.assertIn(".chathead { flex-wrap: nowrap;", phone)
        self.assertIn(".chathead .facts, .docslabel { display: none; }", phone)
        page = str(web_views.chat_page("wren", None, [], [], {"model": "m", "state": "idle"}, ["wren"], "t"))
        self.assertIn('<p class="dim menufacts">m · idle</p>', page)        # the facts move into ⋯


class DocumentsCloseTest(unittest.TestCase):
    """Bug (2026-10-20): beside the chat the Documents panel could not be closed — once an instance had a document,
    .chatgrid:has(.doclist a) held it open, 📄 only toggled .showdocs (no effect then) and ✕ was hidden off a phone."""

    def desktop(self) -> str:
        return re.sub(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", CSS)    # the rules outside any @media

    def test_closed_beats_the_documents_holding_it_open(self):
        css = self.desktop()
        self.assertIn(".chatgrid.hidedocs > aside.docs { display: none; }", css)
        self.assertIn(".chatgrid.hidedocs { grid-template-columns: minmax(0, 1fr); }", css)
        # same specificity as the rule holding it open: it must come after it to win
        self.assertGreater(css.index(".chatgrid.hidedocs {"),
                           css.index(".chatgrid:has([data-back-docs]), .chatgrid:has(.doclist a)"))

    def test_the_close_button_shows_beside_the_chat(self):
        hidden = re.search(r"^([^{\n]*)\{ display: none; \}", self.desktop().split(".navbtn.news")[0].split("PLAN-pwa P2:")[1],
                           re.M)
        self.assertNotIn("closedocs", hidden.group(1))
        self.assertIn("data-close-docs", str(web_views.docs_panel("wren", [{"path": "plan.md", "bytes": 3}], "", None,
                                                                    "none", set())))

    def test_documents_and_close_really_close(self):
        self.assertNotIn('grid.classList.toggle("showdocs");', JS)       # the toggle that could not close it
        self.assertIn("showDocs(!docsOn(), button)", JS)                          # 📄 closes what is on screen
        self.assertRegex(JS, r'matches\("\[data-close-docs\]"\)\) \{\s*showDocs\(false\);')
        self.assertIn('grid.classList.toggle("hidedocs", !on);', JS)
        self.assertIn('localStorage.getItem("shaft.docs.closed")', JS)  # closed stays closed on the next page

    def test_the_documents_button_says_whether_the_panel_is_on_screen(self):
        """Found while checking the fix: Escape closed the phone's sheet and left 📄 saying aria-expanded="true"."""
        # Escape and expansion state are exercised by test_web_sheets_js.
        self.assertRegex(JS, r'if \(ev\.target\.id === "docs"\) \{\s*syncSheets\(\);')



class NetworkClientTest(unittest.TestCase):
    """docs/plans/PLAN-pwa P3, in the page: what it sends and what it does when the network fails."""

    def test_a_refresh_asks_for_what_changed_and_gives_up_in_time(self):
        self.assertIn('{ "If-None-Match": el.dataset.etag }', JS)
        self.assertIn("if (r.status === 304) {", JS)                       # unchanged: nothing re-rendered
        self.assertIn("{ fails = 0; recovered(); }", JS)                   # #26: and this panel is fresh again
        self.assertIn("const POLL_LIMIT_MS = 15000;", JS)
        self.assertIn("signal: ctl.signal", JS)

    def test_a_failure_backs_off_is_said_once_and_ends_when_the_network_returns(self):
        self.assertIn("later(Math.min(every * slow * 2 ** Math.min(fails, 4), 15000 * slow));", JS)
        self.assertIn('netNote(navigator.onLine === false ? "Offline', JS)
        self.assertIn("const onOnline = () => { fails = 0; tick(); };", JS)
        self.assertIn('window.addEventListener("online", onOnline);', JS)
        self.assertIn("if (r.status >= 502 && r.status <= 504) {", JS)     # Tailscale's 503: unreachable, said once
        self.assertIn('window.addEventListener("pageshow", onShow);', JS)

    def test_a_send_carries_its_id_and_is_checked_before_uploading(self):
        self.assertIn("if (mid && !mid.value) mid.value = crypto.randomUUID", JS)
        self.assertIn("if (r.ok && mid && mid.value === sentMid) mid.value = \"\";", JS)   # #21: an edit made meanwhile keeps its new id
        self.assertIn("const tooBig = filesRefused(form);", JS)

    def test_the_feed_is_updated_item_by_item(self):
        self.assertIn("const whole = el.dataset.keyed ? morph(el, body) !== false", JS)
        self.assertIn("const cur = keyed.get(n.dataset.k);", JS)          # #27: one keyed map per reconcile
        self.assertIn("else if (!cur || cur._src !== src) {", JS)         # an unchanged item is left alone


class AccessibilityTest(unittest.TestCase):
    """docs/plans/PLAN-pwa P5."""

    def test_a_new_answer_and_every_toast_are_read_out(self):
        self.assertIn('region("toast", "status");', JS)                    # empty from the start: then it speaks
        self.assertIn('region("announce", "status");', JS)
        self.assertIn('box.setAttribute("role", bad ? "alert" : "status");', JS)
        self.assertIn('announce(`${who ? who.textContent.trim() : "It"} replied:', JS)
        self.assertIn('note.setAttribute("role", "status");', JS)            # a panel not refreshed

    def test_icon_only_controls_and_state_dots_have_names(self):
        from supervisor import web_pages
        page = str(web_views.chat_page("wren", None, [], [], {"model": "m", "state": "idle"}, ["wren"], "t"))
        for name in ('aria-label="Chats and pages"', 'aria-label="Documents"', 'aria-label="wren: pages and actions"',
                     'aria-label="Message wren"'):
            self.assertIn(name, page)
        rail = str(web_pages.rail(["wren"], "", {"instances": [{"name": "wren", "state": "in a cycle"}]}))
        self.assertIn('role="img" aria-label="in a cycle"', rail)
        self.assertIn('x.setAttribute("aria-label", `remove ${f.name}`);', JS)

    def test_the_keyboard_reaches_attach_and_reply(self):
        self.assertIn("label.pick input { position: absolute; width: 1px; height: 1px; opacity: 0; }", CSS)
        self.assertNotIn("label.pick input { display: none; }", CSS)
        self.assertIn('b.addEventListener("click", () => quoteSelection());', JS)

    def test_text_follows_the_phones_text_size_and_motion_can_be_off(self):
        coarse = block("(pointer: coarse)")
        self.assertIn("@supports (font: -apple-system-body) { html { font: -apple-system-body; } }", coarse)
        self.assertIn("font-size: max(16px, 1rem)", coarse)
        self.assertIn("body { font: 1rem/1.45", CSS)                         # rem: it follows the root's size
        self.assertIn("transition: none !important", block("(prefers-reduced-motion: reduce)"))


class CommentsTouchTest(unittest.TestCase):
    def test_comment_script_loads_after_web_and_is_cached(self):
        from supervisor import web_pages
        page = str(web_pages.page("test", web_pages.Html("")))
        self.assertLess(page.index('/static/web.js?v='), page.index('/static/comments.js?v='))
        self.assertRegex(page, r'<script src="/static/comments.js\?v=[^"]+" defer></script>')
        shell = re.search(r"const SHELL = (\[.*?\]);", web_pages.SW, re.S).group(1)
        self.assertIn('/static/comments.js?v=@V@', shell)

    def test_comment_controls_are_above_the_phone_documents(self):
        for selector in ('#quote-reply', '#comment-sel', '#comment-box'):
            rule = re.search(re.escape(selector) + r" \{([^}]+)\}", CSS).group(1)
            self.assertGreater(int(re.search(r'z-index: (\d+)', rule).group(1)), 55)

    def test_composer_survives_panel_replacement(self):
        comments = (STATIC / 'comments.js').read_text()
        self.assertIn('form.id = "comment-box";', comments)
        self.assertIn('document.body.appendChild(form);', comments)
        self.assertNotRegex(comments, r'(panel|view|aside)\.append(?:Child)?\(form\)')

    def test_comment_targets_are_finger_sized(self):
        coarse = block('(pointer: coarse)')
        for selector in ('#quote-reply', '#comment-sel', '#comment-box button', '#comment-box .comment-send'):
            self.assertRegex(coarse, re.escape(selector) + r'[^{}]*\{[^}]*min-width: 44px; min-height: 44px')
        self.assertIn('#comment-box textarea { font-size: 16px; }', coarse)
        self.assertRegex(block(PhoneLayoutTest.PHONE),
                         r'#comment-box \{ left: 0; right: 0; bottom: var\(--vvbottom, 0px\); width: 100%;')
