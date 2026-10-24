"""The web UI's Markdown (web_md.py): what instances write reads as they meant it, and never runs as markup."""
from __future__ import annotations

import re
from pathlib import Path
import unittest

from supervisor import web_md


def md(text: str) -> str:
    out = str(web_md.render(text))
    assert out.startswith('<div class="md">') and out.endswith("</div>")
    return out[len('<div class="md">'):-len("</div>")]


class NeverMarkupTest(unittest.TestCase):
    EVIL = [
        "<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "[x](javascript:alert(1))",
        "[x](JaVaScRiPt:alert(1))", "[x]( javascript:alert(1) )", "![a](javascript:alert(1))", "[x](data:text/html,hi)",
        '<https://a.example/"onmouseover="alert(1)>', 'https://a.example/"onmouseover="alert(1)',
        '[x](https://a.example/"onmouseover="alert(1))', '```"><script>alert(1)</script>\ncode\n```',
        "| <b>h</b> |\n|---|\n| <i>c</i> |", "> <script>q</script>", "- [ ] <script>t</script>",
        "**<script>b</script>**", "`<script>c</script>`", "\ue0000\ue001 \ue000" + "1" + "\ue001",
        "<a href='javascript:x'>a</a>", "[a](https://ok.example) <style>body{}</style>",
    ]

    def test_nothing_written_runs_as_markup(self):
        for text in self.EVIL:
            out = md(text)
            self.assertNotRegex(out, r"<(script|img|style|iframe)\b", text)
            for tag in re.findall(r"<[a-z][^>]*>", out):         # real tags only: escaped text is text
                self.assertNotRegex(tag, r"\son[a-z]+\s*=", (text, tag))
            self.assertNotIn("javascript:", re.sub(r"&[a-z]+;", "", " ".join(re.findall(r'href="([^"]*)"', out))).lower(),
                             text)
            for href in re.findall(r'href="([^"]*)"', out):
                self.assertTrue(href.startswith(("http://", "https://", "mailto:")), (text, href))
            for attr in re.findall(r'\s[a-z-]+="([^"]*)"', out):
                self.assertNotIn("<", attr, text)

    def test_deep_nesting_never_breaks_the_page(self):
        """Codex review: 1100 nested quotes raised RecursionError, and the stored answer broke every later render."""
        for text in (">" * 1100 + " text", "\n".join("  " * k + "- item" for k in range(600)), "> - " * 400 + "x"):
            out = md(text)
            self.assertIn("text" if text.endswith("text") else "item" if "item" in text else "x", out)

    def test_a_forged_placeholder_is_only_text(self):
        out = md("[a](https://ok.example) and \ue0000\ue001")
        self.assertEqual(out.count("<a "), 1)


class BlocksTest(unittest.TestCase):
    def test_headings_start_at_h2_and_drop_closing_hashes(self):
        self.assertEqual(md("# Title #\n## Sub\n###### six"), "<h2>Title</h2><h3>Sub</h3><h6>six</h6>")
        self.assertEqual(md("#hashtag"), "<p>#hashtag</p>")

    def test_paragraphs_join_lines_and_keep_hard_breaks(self):
        self.assertEqual(md("one\ntwo\n\nthree"), "<p>one two</p><p>three</p>")
        self.assertEqual(md("one  \ntwo\\\nthree"), "<p>one<br>two<br>three</p>")

    def test_numbered_lists_are_numbered_from_where_they_start(self):
        self.assertEqual(md("1. a\n2. b"), "<ol><li>a</li><li>b</li></ol>")
        self.assertEqual(md("3) c\n4) d"), '<ol start="3"><li>c</li><li>d</li></ol>')

    def test_bullets_nest_and_mix_with_numbers(self):
        out = md("- a\n  1. one\n  2. two\n- b\n  - deep\n    - deeper")
        self.assertEqual(out, "<ul><li>a<ol><li>one</li><li>two</li></ol></li><li>b<ul><li>deep<ul><li>deeper</li>"
                              "</ul></li></ul></li></ul>")

    def test_a_list_item_wrapping_onto_the_next_line_stays_one_item(self):
        self.assertEqual(md("- one long item\n  that wraps\n- two"), "<ul><li>one long item that wraps</li><li>two</li></ul>")
        self.assertEqual(md("- lazy\ncontinuation"), "<ul><li>lazy continuation</li></ul>")

    def test_a_loose_list_keeps_its_paragraphs(self):
        self.assertEqual(md("- a\n\n- b\n\n  more b"), "<ul><li><p>a</p></li><li><p>b</p><p>more b</p></li></ul>")

    def test_task_lists_show_their_boxes(self):
        out = md("- [ ] to do\n- [x] done\n- [X] done too")
        self.assertEqual(out.count('<span class="task" aria-label="to do">☐</span>'), 1)
        self.assertEqual(out.count('<span class="task done" aria-label="done">☑</span>'), 2)
        self.assertIn("☐</span> to do", out)

    def test_issue_16_task_items_do_not_share_checkbox_width_or_colour(self):
        """Reverting #16 gives the li the checkbox's narrow width and dim colour."""
        text = "Review the proposal and send the summary to the team before Friday"
        out = md(f"- [ ] {text}\n- [x] finished")
        self.assertIn(f'<li class="task-item"><span class="task" aria-label="to do">☐</span> {text}</li>', out)
        self.assertIn('<li class="task-item"><span class="task done" aria-label="done">☑</span> finished</li>', out)
        css = (Path(__file__).parents[1] / "web_static" / "web.css").read_text()
        self.assertRegex(css, r"\.md li\.task-item\s*\{[^}]*list-style: none;")
        self.assertRegex(css, r"\.md span\.task\s*\{[^}]*width: 1.1rem;")
        self.assertRegex(css, r"\.md span\.task\.done\s*\{[^}]*color: var\(--good\);")
        self.assertNotRegex(css, r"\.md li\.task-item\s*\{[^}]*(?:width|color):")

    def test_a_list_right_after_a_paragraph_starts_without_a_blank_line(self):
        self.assertEqual(md("Steps:\n- a\n- b"), "<p>Steps:</p><ul><li>a</li><li>b</li></ul>")
        self.assertEqual(md("In 2026\n3. not a list"), "<p>In 2026 3. not a list</p>")   # only 1. interrupts

    def test_fenced_code_keeps_everything_and_names_its_language(self):
        out = md("```python\nif a < b:\n    print('**no**')\n```\nafter")
        self.assertEqual(out, '<pre class="code" data-lang="python"><code>if a &lt; b:\n    print(&#x27;**no**&#x27;)'
                              "</code></pre><p>after</p>")
        self.assertEqual(md("~~~\nx\n~~~"), '<pre class="code"><code>x</code></pre>')
        self.assertEqual(md("````\n```\ninner\n```\n````"), '<pre class="code"><code>```\ninner\n```</code></pre>')
        self.assertEqual(md("```\nnever closed"), '<pre class="code"><code>never closed</code></pre>')

    def test_rules_and_quotes(self):
        self.assertEqual(md("a\n\n---\n\nb"), "<p>a</p><hr><p>b</p>")
        self.assertEqual(md("* * *"), "<hr>")
        self.assertEqual(md("> one\n> > two"), "<blockquote><p>one</p><blockquote><p>two</p></blockquote></blockquote>")

    def test_tables_align_and_format_their_cells(self):
        out = md("| **Name** | Qty | Note |\n|:--|--:|:-:|\n| `a\\|b` | 3 | *x* |\n| c |")
        self.assertIn('<th class="al-l"><b>Name</b></th><th class="al-r">Qty</th><th class="al-c">Note</th>', out)
        self.assertIn('<td data-label="Name" class="al-l"><code>a|b</code></td>', out)
        self.assertIn('<td data-label="Note" class="al-c"><i>x</i></td>', out)
        self.assertIn('<td data-label="Qty" class="al-r"></td>', out)              # a short row is filled

    def test_issue_35_table_labels_are_visible_heading_text(self):
        """Reverting #35 exposes Markdown delimiters and link destinations in phone labels."""
        headings = '**Name** | *Kind* | `**literal**` | [the **docs**](https://ex.example) | __Bold__ _em_ ~~old~~'
        out = md(f"| {headings} |\n|---|---|---|---|---|\n|a|b|c|d|e|")
        self.assertEqual(re.findall(r'data-label="([^"]*)"', out),
                         ["Name", "Kind", "**literal**", "the docs", "Bold em old"])
        out = md('| **<b> & "x"** |\n|---|\n|value|')
        self.assertIn('data-label="&lt;b&gt; &amp; &quot;x&quot;"', out)
        self.assertNotIn('<b> &', out)



class InlineTest(unittest.TestCase):
    def test_emphasis_in_both_spellings_and_strike(self):
        self.assertEqual(web_md.inline("**b** __b__ *i* _i_ ~~s~~"), "<b>b</b> <b>b</b> <i>i</i> <i>i</i> <s>s</s>")

    def test_underscores_inside_words_and_paths_stay(self):
        self.assertEqual(web_md.inline("snake_case_name and notes/ui_test_file.md"),
                         "snake_case_name and notes/ui_test_file.md")
        self.assertEqual(web_md.inline("2 * 3 * 4"), "2 * 3 * 4")

    def test_backslash_escapes_and_code_spans(self):
        self.assertEqual(web_md.inline(r"\*not em\* and \_no\_"), "*not em* and _no_")
        self.assertEqual(web_md.inline("``a `tick` b`` and `**x**`"), "<code>a `tick` b</code> and <code>**x**</code>")

    def test_links_autolinks_and_bare_addresses(self):
        a = '<a href="{u}" rel="noopener noreferrer" target="_blank">{t}</a>'
        self.assertEqual(web_md.inline("[the **docs**](https://ex.example/a_(b)_c)"),
                         a.format(u="https://ex.example/a_(b)_c", t="the <b>docs</b>"))
        self.assertEqual(web_md.inline("<https://ex.example>"), a.format(u="https://ex.example", t="https://ex.example"))
        self.assertEqual(web_md.inline("see https://ex.example/x_y_z."),
                         "see " + a.format(u="https://ex.example/x_y_z", t="https://ex.example/x_y_z") + ".")
        self.assertEqual(web_md.inline("(https://ex.example/p)"),
                         "(" + a.format(u="https://ex.example/p", t="https://ex.example/p") + ")")
        self.assertEqual(web_md.inline("[mail](mailto:d@ex.example)"), a.format(u="mailto:d@ex.example", t="mail"))
        self.assertEqual(web_md.inline("[a file](notes/docs/plan.md)"), "a file")      # not a link it could follow

    def test_an_image_is_a_link_to_it(self):
        self.assertEqual(web_md.inline("![chart](https://ex.example/c.png)"),
                         '<a href="https://ex.example/c.png" rel="noopener noreferrer" target="_blank">🖼 chart</a>')


def _css_rule_bodies(css: str, selector: str) -> list[str]:
    """Bodies of rules whose selector list includes `selector` exactly, in source order. Comments removed."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    bodies = []
    for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        selectors = [re.sub(r"\s+", " ", part.strip()) for part in match.group(1).split(",")]
        if selector in selectors:
            bodies.append(match.group(2))
    return bodies


def _last_rem(css: str, selector: str, prop: str) -> float:
    found = None
    for body in _css_rule_bodies(css, selector):
        for match in re.finditer(rf"(?:^|;)\s*{prop}\s*:\s*(\d*\.?\d+)rem", body):
            found = float(match.group(1))
    if found is None:
        raise AssertionError(f"{selector} sets no {prop} in rem")
    return found


def _margin_above_below(css: str, selector: str) -> tuple[float, float]:
    found = None
    for body in _css_rule_bodies(css, selector):
        for match in re.finditer(r"(?:^|;)\s*margin\s*:\s*(\d*\.?\d+)(?:rem|em)\s+0\s+(\d*\.?\d+)(?:rem|em)", body):
            found = (float(match.group(1)), float(match.group(2)))
    if found is None:
        raise AssertionError(f"{selector} sets no margin shorthand")
    return found


class MarkdownHeadingScaleTest(unittest.TestCase):
    """#59: markdown headings are a scale, larger than the text they introduce. Page titles outside .md stay put."""

    def test_issue_59_markdown_headings_step_down_and_stay_at_least_body_size(self):
        """Reverting #59 sets .md h2, h3 and h4 to the same .95rem, smaller than the 1rem body."""
        css = (Path(__file__).parents[1] / "web_static" / "web.css").read_text()
        sizes = {level: _last_rem(css, f".md {level}", "font-size") for level in ("h2", "h3", "h4", "h5", "h6")}
        self.assertGreater(sizes["h2"], sizes["h3"])
        self.assertGreater(sizes["h3"], sizes["h4"])
        self.assertGreaterEqual(sizes["h4"], 1.0)          # body is 1rem (html is 15px)
        self.assertGreaterEqual(sizes["h5"], 1.0)
        self.assertGreaterEqual(sizes["h6"], 1.0)
        self.assertGreaterEqual(sizes["h2"], 2.0)          # a clear document title
        for level in ("h2", "h3", "h4", "h5", "h6"):
            above, below = _margin_above_below(css, f".md {level}")
            self.assertGreater(above, below, level)
        for level in ("h5", "h6"):
            weight = None
            for body in _css_rule_bodies(css, f".md {level}"):
                for match in re.finditer(r"(?:^|;)\s*font-weight\s*:\s*([^;]+)", body):
                    weight = match.group(1).strip()
            self.assertEqual(weight, "700", level)
        # SHAFT's own section titles, not instance text
        self.assertIn("h1 { font-size: 1.3rem; margin: 0 0 .8rem; }", css)
        self.assertIn("h2 { font-size: .95rem; margin: 0 0 .5rem; color: var(--dim); font-weight: 600; }", css)
        self.assertIn(".panel h3 { font-size: .9rem; margin: .8rem 0 .2rem; }", css)
        self.assertIn(".earlier h3 { font-size: .8rem; color: var(--dim); margin: 0 0 .2rem; }", css)
        self.assertIn(".chathead h1 { font-size: 1.1rem;", css)
        self.assertRegex(css, r"\.turn, \.md,[^{]*\{[^}]*overflow-wrap: anywhere")



class PlaceholdersInLinksTest(unittest.TestCase):
    """W16 (review 2026-10-24): _LINK's destination did not exclude the placeholders a code span or an autolink leaves,
    so their HTML was expanded inside href="…" and broke out of the attribute (the CSP stopped any script)."""

    def test_w16_a_code_span_in_a_destination_stays_out_of_the_href(self):
        out = md('[x](https://a.example/`q" onclick="x`)')
        for href in re.findall(r'href="([^"]*)"', out):
            self.assertNotIn("<", href)
        self.assertNotIn('href="https://a.example/<code>', out)
        self.assertIn("<code>q&quot; onclick=&quot;x</code>", out)            # still shown, as text

    def test_w16_an_autolink_in_a_destination_stays_out_of_the_href(self):
        out = md("[x](https://a.example/<https://b.example>)")
        self.assertEqual(re.findall(r'href="([^"]*)"', out), ["https://a.example/", "https://b.example"])
        self.assertEqual(out.count("<a "), out.count("</a>"))               # no anchor inside an attribute

    def test_a_code_span_in_a_link_label_renders_instead_of_failing_the_page(self):
        """Found while fixing W16: the label was rendered with a placeholder list of its own, so `code` in a label
        raised IndexError and the whole chat or document failed to render."""
        self.assertEqual(md("[`api`](https://a.example)"),
                         '<p><a href="https://a.example" rel="noopener noreferrer" target="_blank"><code>api</code>'
                         '</a></p>')

    def test_plain_strips_the_block_syntax_of_a_line_too(self):
        """Grok review of 8735c62 (S3): plain() rendered inline Markdown only, so an instance quoting a list item, a
        heading, a quote or a table row kept "- ", "## ", "> " or pipes and never matched the rendered document."""
        cases = {"- [ ] **ship** it": "☐ ship it", "* [x] done": "☑ done", "1. first `step`": "first step",
                 "## The *plan*": "The plan", "> > quoted **twice**": "quoted twice",
                 "| a | **b** | c |": "a\tb\tc", "  - nested": "nested", "plain text": "plain text"}
        for line, shown in cases.items():
            self.assertEqual(web_md.plain(line), shown, line)
        for line, shown in cases.items():                                    # what the page's text holds
            out = re.sub(r"<[^>]*>", "", md(line))
            self.assertIn(shown.split("\t")[0], __import__("html").unescape(out), line)

    def test_plain_is_the_text_the_page_shows(self):
        self.assertEqual(web_md.plain("the **bold** `x < y` and [a link](https://a.example) <b>"),
                         "the bold x < y and a link <b>")


if __name__ == "__main__":
    unittest.main()
