"""Issues #12 and #25: rendered field names and the shipped accessibility CSS."""
import re
import unittest

from supervisor import web_config, web_views
from supervisor.web_pages import Html
from supervisor.tests.test_web_touch import CSS, block


def declarations(selector, css=CSS):
    """Bodies containing this exact selector, in source order (not a CSS cascade engine)."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    return [body for selectors, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css)
            if selector in [s.strip() for s in selectors.split(',')]]


def contrast(a, b):
    def luminance(color):
        rgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4 for c in rgb]
        return sum(c * weight for c, weight in zip(linear, (.2126, .7152, .0722)))
    low, high = sorted((luminance(a), luminance(b)))
    return (high + .05) / (low + .05)


class FieldNamesTest(unittest.TestCase):
    def test_issue_12_new_document_label(self):
        """Reverting the label leaves the name field dependent on its disappearing placeholder."""
        body = str(web_views.docs_panel('wisp', [], '', None, 'none', set()))
        self.assertRegex(body, r'<label>Document name\s*<input name="path"[^>]+></label>')

    def test_issue_12_document_editor_label(self):
        """Reverting the editor label loses the persistent document name and accessible context."""
        body = str(web_views.docs_panel('wisp', [], 'sub/a&b.md', 'hello', 'sha', set()))
        self.assertIn('<label>Markdown of sub/a&amp;b.md<textarea', body)
        self.assertIn('aria-label="Markdown of sub/a&amp;b.md"', body)

    def test_issue_25_config_value_names(self):
        """Reverting the value names leaves dropdowns unnamed and text fields without 'value'."""
        for choices in (None, ['bonsai']):
            with self.subTest(choices=choices):
                field = dict(key='LLM_MODEL', value='bonsai', set=True, default='bonsai',
                             help='', secret=False, numeric=False, choices=choices)
                body = str(web_config.config_row('wisp', field))
                self.assertIn('<code>LLM_MODEL</code>', body)
                self.assertRegex(body, r'<(?:input|select) name="value"[^>]*aria-label="LLM_MODEL value"')

    def test_issue_25_drive_label(self):
        """Reverting the Drive label leaves no field name identifying the instance being edited."""
        body = str(web_config.drive_page('wisp', 'hello', 'sha', Html('<p>hello</p>')))
        self.assertIn('<label>drive.md for wisp<textarea', body)
        self.assertIn('aria-label="drive.md for wisp"', body)


class StylesTest(unittest.TestCase):
    def test_issue_25_copy_focus_visibility(self):
        """Reverting either focus selector leaves that code Copy button hidden when Tab reaches it."""
        for parent in ('.prewrap',):                  # W18: the button sits on the code block's wrapper, beside it
            with self.subTest(parent=parent):
                # Same ancestor specificity as the hiding rule, plus :focus-visible.
                rules = declarations(parent + ' > .copy:focus-visible')
                self.assertTrue(any('opacity: 1;' in rule for rule in rules))
                self.assertTrue(any('outline: 2px solid var(--accent);' in rule for rule in rules))

    def test_issue_25_field_contrast(self):
        """Reverting either field token drops its border below 3:1 against a page or panel."""
        tokens = {key: re.findall(r'--' + key + r':\s*(#[0-9a-f]{6})', CSS)
                  for key in ('field', 'bg', 'panel')}
        for theme in range(2):
            for surface in ('bg', 'panel'):
                with self.subTest(theme=('light', 'dark')[theme], surface=surface):
                    self.assertGreaterEqual(contrast(tokens['field'][theme], tokens[surface][theme]), 3)
        for selector in ('input', 'select', 'textarea'):
            self.assertTrue(any('border-color: var(--field);' in r for r in declarations(selector)))

    def test_issue_25_new_document_border(self):
        """Reverting the newdoc override paints the faint divider despite the shared field rule."""
        self.assertIn('border: 1px solid var(--field);', declarations('.newdoc input')[0])

    def test_issue_25_disclosure_targets(self):
        """Reverting the touch rules shrinks the activity and overwrite disclosure hit areas."""
        for selector in ('details.activity > summary', '.overwritten summary'):
            with self.subTest(selector=selector):
                rules = declarations(selector, block('(pointer: coarse)'))
                self.assertTrue(any('min-height: 44px;' in r and 'min-width: 44px;' in r for r in rules))

    def test_issue_25_model_targets(self):
        """Reverting the size or inline-block display makes model labels smaller than 44 px."""
        rules = declarations('.models label', block('(pointer: coarse)'))
        self.assertTrue(any(all(prop in r for prop in ('min-height: 44px;', 'min-width: 44px;',
                                                     'display: inline-block;')) for r in rules))

    def test_issue_25_drive_touch_font(self):
        """Reverting the qualified touch selector lets Drive's .82rem rule win and trigger iOS zoom."""
        rules = declarations('form.drive textarea', block('(pointer: coarse)'))
        self.assertTrue(any('font-size: 16px;' in r for r in rules))
        self.assertIn('font-size: max(16px, 1rem);', rules[-1])
        # Equal specificity: both touch declarations must follow the desktop rule.
        desktop = CSS.index('form.drive textarea {')
        for rule in rules:
            self.assertGreater(CSS.index(rule), desktop)
