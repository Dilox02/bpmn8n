"""Template filters of bpmn8n's pages."""
from django import template
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.timesince import timesince, timeuntil

from ..web import vocab
from ..web.text import to_html

register = template.Library()


@register.filter
def prose(text):
    """Text an LLM wrote (a brief, a question), its markdown rendered and its HTML escaped."""
    return mark_safe(to_html(text)[0])  # noqa: S308 (escaped by to_html)


@register.filter
def status_label(status):
    """A status in words, from bpmn8n/web/vocab.py: "waiting_person" -> "waiting for a person"."""
    return vocab.label(status)


@register.filter
def status_pill(status):
    """A status as a <b8-pill> (static/bpmn8n/components.js), its words and tone from bpmn8n/web/vocab.py."""
    return format_html('<b8-pill tone="{}">{}</b8-pill>', vocab.tone(status), vocab.label(status))


@register.filter
def duration(ms):
    """Milliseconds in one or two units: "120 ms", "3.4 s", "4 min", "2 h 5 min", "3 days"."""
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms:.0f} ms"
    if (s := ms / 1000) < 60:
        return f"{s:.1f} s"
    if (m := int(s // 60)) < 60:
        return f"{m} min"
    if (h := m // 60) < 48:
        return f"{h} h {m % 60} min" if m % 60 else f"{h} h"
    return f"{h // 24} days"


@register.filter
def waited(start):
    """How long since `start`, in one unit: "under a minute", "14 minutes", "3 hours"."""
    if not start:
        return "—"
    return "under a minute" if (timezone.now() - start).total_seconds() < 60 else timesince(start, depth=1)


@register.filter
def span(moment):
    """How far `moment` is from now, past or future, in one unit: "a moment", "14 minutes", "3 days"."""
    if not moment:
        return "—"
    now = timezone.now()
    if abs((moment - now).total_seconds()) < 60:
        return "a moment"
    return timesince(moment, now, depth=1) if moment < now else timeuntil(moment, now, depth=1)


@register.filter
def get(mapping, key):
    """mapping[key], "" when there is none: a form's submitted value or error by field name."""
    return mapping.get(key, "") if hasattr(mapping, "get") else ""
