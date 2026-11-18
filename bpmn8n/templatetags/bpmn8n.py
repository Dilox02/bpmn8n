"""Template filters of bpmn8n's pages."""
from django import template
from django.utils.safestring import mark_safe
from django.utils import timezone
from django.utils.timesince import timesince, timeuntil

from ..web.text import to_html

register = template.Library()


@register.filter
def prose(text):
    """Text an LLM wrote (a brief, a question), its markdown rendered and its HTML escaped."""
    return mark_safe(to_html(text)[0])  # noqa: S308 (escaped by to_html)


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
