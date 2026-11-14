"""Choice cards in both web feeds and native form posts through the answer route."""
import re
import unittest
import urllib.parse
from html.parser import HTMLParser
from unittest import mock

from supervisor import questions, web, web_pages, web_views
from supervisor.tests import test_app_api as fixtures


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("type") == "hidden":
            self.values[attrs["name"]] = attrs.get("value", "")


class QuestionCardTest(unittest.TestCase):
    def item(self, **extra):
        return {"key": "sq", "type": "sent", "file": 'q"<.md', "ts": "2026-11-03T14:02:00",
                "kind": "question", "read": False, "text": "When?", "attachments": [], "rating": None,
                "choices": {"options": [{"label": '<img src=x onerror="alert(1)">', "detail": "<soon>"},
                                         {"label": "Later", "detail": None}], "multiple": False},
                "answer": None, "open": True, **extra}

    def render(self, item, cid='web-"<id>'):
        return str(web_views.feed_html({"instance": "wisp", "chat": {"id": cid}, "archived": False,
                                       "earlier": 0, "items": [item], "reply": None,
                                       "queued": [], "pending_feedback": [], "pending_edits": []}))

    def test_single_and_multiple_markup_and_escaping(self):
        item = self.item()
        html = self.render(item)
        self.assertEqual(html.count('type="radio"'), 2)
        self.assertIn('name="picked" value="0"', html)
        self.assertIn('class="dim question-detail">&lt;soon&gt;</span>', html)
        self.assertIn('Other…<input type="text" name="other" maxlength="2000">', html)
        self.assertIn('method="post" action="/chat/wisp/answer"', html)
        self.assertIn('data-post="/chat/wisp/answer" data-answer="1"', html)
        self.assertIn('<button type="submit">Send</button>', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('&lt;img src=x onerror=&quot;alert(1)&quot;&gt;', html)
        self.assertIn('value="q&quot;&lt;.md"', html)
        self.assertIn('value="web-&quot;&lt;id&gt;"', html)
        fields = Fields(html).values
        self.assertEqual(fields["file"], item["file"])
        self.assertEqual(fields["cid"], 'web-"<id>')
        self.assertEqual(fields["mid"], "", "the page's JS sets the id at Send: the same HTML at every poll")
        self.assertEqual(self.render(item), html)
        item["choices"]["multiple"] = True
        html = self.render(item)
        self.assertEqual(html.count('type="checkbox"'), 2)
        self.assertNotIn('type="radio"', html)
        self.assertIn('name="picked[]" value="1"', html)

    def test_answered_card_escapes_labels_and_other(self):
        html = self.render(self.item(answer={"picked": [0, 1], "other": "<tomorrow>"}, open=False))
        self.assertIn('You answered: &lt;img src=x onerror=&quot;alert(1)&quot;&gt;; Later; Other: &lt;tomorrow&gt;', html)
        self.assertNotIn('data-answer="1"', html)
        self.assertNotIn('<img src=x', html)
        other_only = self.render(self.item(answer={"picked": [], "other": "Tomorrow"}, open=False))
        self.assertIn("You answered: Other: Tomorrow", other_only)

    def test_closed_and_plain_questions_have_no_form(self):
        html = self.render(self.item(open=False))
        self.assertIn("No longer waiting for an answer.", html)
        self.assertNotIn('data-answer="1"', html)
        html = self.render(self.item(choices=None))
        self.assertNotIn("No longer waiting", html)
        self.assertNotIn('data-answer="1"', html)


class QuestionRoutesTest(fixtures.AppTest):
    choice_question = fixtures.ChoiceAnswerTest.choice_question

    def setUp(self):
        super().setUp()
        self.cookie = self.paired()
        patch = mock.patch.object(web, "_docs_panel", return_value=("", web_pages.Html("")))
        patch.start()
        self.addCleanup(patch.stop)

    def html(self, path):
        response = self.req(path, cookie=self.cookie)
        self.assertEqual(response.status, 200, response.body)
        return response.body.decode()

    def card(self, html):
        return re.search(r'<form class="question-card".*?</form>', html, re.S)[0]

    def post(self, fields, headers=None):
        return self.req("/chat/wisp/answer", "POST", cookie=self.cookie,
                        body=urllib.parse.urlencode(fields, doseq=True).encode(),
                        headers={"content-type": "application/x-www-form-urlencoded",
                                 "sec-fetch-site": "same-origin", **(headers or {})})

    def test_thread_and_from_pages_and_fragments_render_cards(self):
        chat = self.chat([])
        self.choice_question(file="thread.md", thread=chat["id"])
        self.choice_question(file="from.md", talk=None)
        for suffix, cid, file in ((f'?id={chat["id"]}', chat["id"], "thread.md"), ("", "from", "from.md")):
            for prefix in ("/i/wisp/chat", "/fragment/chat/wisp"):
                with self.subTest(path=prefix + suffix):
                    fields = Fields(self.card(self.html(prefix + suffix))).values
                    self.assertEqual((fields["cid"], fields["file"]), (cid, file))
                    self.assertNotIn("csrf", fields, "the token goes as the header, as every fetch sends it")

    def test_native_form_post_stores_answer_and_second_mid_conflicts(self):
        self.choice_question(talk=None, multiple=True)
        fields = Fields(self.card(self.html("/i/wisp/chat"))).values
        fields.update({"picked[]": [0, 1], "other": "After lunch"})
        fields["mid"] = "web-answer-1"                  # what the page's JS sets at Send
        token = {"x-csrf-token": self.csrf(self.cookie)}
        response = self.post(fields, token)
        self.assertEqual(response.status, 200, response.body)
        saved = questions.answers()["choice.md"]["answer"]
        self.assertEqual((saved["picked"], saved["other"]), ([0, 1], "After lunch"))
        self.assertIn("You answered: Now; Later; Other: After lunch", self.html("/fragment/chat/wisp"))
        self.assertEqual(self.post(fields, token).status, 200)  # lost response: the same mid is safe to retry
        response = self.post({**fields, "mid": "another-answer"}, token)
        self.assertEqual(response.status, 409, response.body)
        self.assertIn(b"already answered", response.body)
        self.assertEqual(questions.answers()["choice.md"]["answer"], saved)

    def test_native_form_keeps_csrf_and_origin_checks(self):
        self.choice_question(talk=None)
        fields = Fields(self.card(self.html("/i/wisp/chat"))).values
        fields.update(picked="0", mid="web-answer-1")
        self.assertEqual(self.post(fields).status, 403)
        self.assertEqual(self.post(fields, {"x-csrf-token": "wrong"}).status, 403)
        self.assertEqual(self.post(fields, {"sec-fetch-site": "cross-site"}).status, 403)
        self.assertNotIn("choice.md", questions.answers())
        self.assertEqual(self.post(fields, {"x-csrf-token": self.csrf(self.cookie)}).status, 200)
