"""Markdown as bpmn8n's pages show it: the README (the docs) and the texts an LLM writes (the brief)."""
import html
import re

import markdown
from django.utils.html import escape

# a list right under a line of text, as GitHub reads it: Python-Markdown wants a blank line before it
LIST_AFTER_TEXT = re.compile(r"^((?![ \t]*(?:[-*+]|\d+\.)[ \t])[^\n|`].*)\n([ \t]*(?:[-*+]|\d+\.)[ \t])", re.M)
FENCES = re.compile(r"(^```.*?^```)", re.M | re.S)
URL_ATTR = re.compile(r'(\s(?:href|src)=")([^"]*)(")')
SAFE_SCHEMES = ("http", "https", "mailto")


def safe_url(match):
    """A link or image of untrusted text: javascript:, data: and the other schemes become "#"."""
    url = re.sub(r"[\x00-\x20]", "", html.unescape(match.group(2))).lower()  # java&#9;script: is javascript:
    scheme = re.match(r"([a-z][a-z0-9+.-]*):", url)
    return match.group(0) if not scheme or scheme.group(1) in SAFE_SCHEMES else f"{match.group(1)}#{match.group(3)}"


def to_html(text, *, trusted=False, toc=False):
    """(html, table of contents). Untrusted text (an LLM's) has its HTML escaped first."""
    text = text if trusted else escape(text)
    text = "".join(part if part.startswith("```") else LIST_AFTER_TEXT.sub(r"\1\n\n\2", part)
                   for part in FENCES.split(text))  # code blocks stay as they are
    md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", *(["toc"] if toc else [])])
    converted = md.convert(text)
    return converted if trusted else URL_ATTR.sub(safe_url, converted), getattr(md, "toc_tokens", [])
