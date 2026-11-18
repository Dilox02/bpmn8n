"""One platform: the tokens both servers read, the theme switch, the sidebar and the vocabulary."""
import re
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.template import Context, Template
from django.test import TestCase, override_settings

from ..web import views

STATIC = Path(__file__).resolve().parents[1] / "static" / "bpmn8n"
HOOKS = Path(__file__).resolve().parents[2] / "n8n-editor"


def blocks(css):
    """selector -> declarations of tokens.css's custom properties, by block."""
    return {m[1].strip(): dict(re.findall(r"(--b8-[\w-]+):\s*([^;]+);", m[2]))
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL))}


class TokensTest(TestCase):
    def setUp(self):
        self.tokens = (STATIC / "tokens.css").read_text()
        self.defined = set(re.findall(r"(--b8-[\w-]+):", self.tokens))

    def test_the_pages_have_no_palette_of_their_own(self):
        app = (STATIC / "app.css").read_text()
        self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", app), [])
        self.assertLessEqual(set(re.findall(r"var\((--b8-[\w-]+)", app)), self.defined)

    def test_dark_is_one_palette_whether_chosen_or_the_systems(self):
        found = blocks(self.tokens)
        chosen, system = found["[data-theme=dark]"], found[":root:not([data-theme=light])"]
        self.assertEqual(chosen, system)
        light = found[":root, [data-theme=light]"]
        self.assertEqual(set(chosen), set(light))  # every colour has both themes

    def test_the_hooks_use_the_tokens_with_their_old_colour_as_fallback(self):
        per_node = set(re.findall(r"setProperty\('(--b8-[\w-]+)'", (HOOKS / "bpmn.js").read_text()))  # a node's tint
        for hook in ("bpmn.js", "preview.js", "panel.js", "evaluations.css", "executions.css"):
            text = (HOOKS / hook).read_text()
            for token in re.findall(r"var\((--b8-[\w-]+)", text):
                self.assertIn(token, self.defined | per_node, f"{hook}: {token} is not in tokens.css")
            bare = re.sub(r"var\(--b8-[\w-]+,\s*(?:var\([^()]*\)|[^()]*)\)", "", text)
            self.assertEqual(re.findall(r"#[0-9a-fA-F]{3,8}\b", bare), [], f"{hook}: a colour outside the tokens")


class ShellTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("sam", password="pw", is_staff=True)
        self.person = User.objects.create_user("ann", password="pw")

    def test_the_sidebar_shows_the_pages_this_server_has(self):
        self.client.force_login(self.staff)
        ghost = views.Nav("not-built-yet", "Ghost", "docs")
        with patch.object(views, "NAV", (*views.NAV, ghost)), override_settings(N8N_PUBLIC_URL="http://n8n.test"):
            page = self.client.get("/docs/")
        self.assertNotContains(page, "Ghost")
        self.assertContains(page, '<a class="nav-item" href="/docs/" aria-current="page">', count=1)
        self.assertContains(page, 'href="/inbox/"')
        self.assertContains(page, 'href="/studio/"')
        self.assertContains(page, 'href="http://n8n.test" target="_blank" rel="noopener"')  # the editor
        self.client.force_login(self.person)
        page = self.client.get("/inbox/")
        self.assertNotContains(page, 'href="/studio/"')  # staff only
        self.assertNotContains(page, ">Editor<")
        self.assertNotContains(page, "/admin/")

    def test_the_theme_is_a_cookie_set_on_html(self):
        self.client.force_login(self.person)
        self.assertContains(self.client.get("/docs/"), '<html lang="en">')  # the system's
        r = self.client.post("/theme/", {"theme": "dark", "next": "/docs/?x=1"})
        self.assertRedirects(r, "/docs/?x=1", fetch_redirect_response=False)
        self.assertEqual(r.cookies["b8-theme"].value, "dark")
        self.assertFalse(r.cookies["b8-theme"]["httponly"])  # the n8n hooks may read it
        page = self.client.get("/docs/")
        self.assertContains(page, '<html lang="en" data-theme="dark">')
        self.assertContains(page, 'value="dark" aria-pressed="true"')
        r = self.client.post("/theme/", {"theme": "purple", "next": "https://evil.example/"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)  # nowhere else
        self.assertNotIn("b8-theme", r.cookies)
        self.client.cookies["b8-theme"] = "<script>"
        self.assertContains(self.client.get("/docs/"), '<html lang="en">')

    def test_the_sign_in_page_follows_the_theme(self):
        self.client.cookies["b8-theme"] = "light"
        self.assertContains(self.client.get("/login/"), '<html lang="en" data-theme="light">')


class VocabularyTest(TestCase):
    def render(self, text, **context):
        return Template("{% load bpmn8n %}" + text).render(Context(context))

    def test_a_status_has_its_words_and_tone_in_one_place(self):
        self.assertEqual(self.render("{{ s|status_pill }}", s="waiting_person"),
                         '<span class="pill tone-accent">waiting for a person</span>')
        self.assertEqual(self.render("{{ s|status_pill }}", s="completed"), '<span class="pill tone-positive">completed</span>')
        self.assertEqual(self.render("{{ s|status_label }}", s="error"), "failed")
        self.assertEqual(self.render("{{ s|status_pill }}", s="<b>new</b>"),
                         '<span class="pill tone-neutral">&lt;b&gt;new&lt;/b&gt;</span>')

    def test_durations_in_one_or_two_units(self):
        self.assertEqual([self.render("{{ ms|duration }}", ms=ms) for ms in (120, 3400, 250_000, 7_500_000, 300_000_000)],
                         ["120 ms", "3.4 s", "4 min", "2 h 5 min", "3 days"])
