"""Template filters of bpmn8n's pages."""
import re
import zlib

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



PROCESS_HUES = (20, 55, 95, 135, 170, 330)  # a process's tint: 35° or more apart, none a pale blue or a violet


def process_hue(name):
    """The workflows n8n has take the hues in turn (sorted by name), so the ones a person sees together never share one;
    any other name (a deleted workflow, an imported log, a project) takes one from its name."""
    from ..core import directory
    live = sorted({n for n, _ in (directory.from_n8n() or {}).values()}, key=str.lower)
    index = live.index(name) if name in live else zlib.crc32(name.encode())
    return PROCESS_HUES[index % len(PROCESS_HUES)]


@register.filter
def process_chip(name):
    """A process (workflow) by name, with its own mark: two letters on a pale tint of a hue fixed by the name, so
    "ACME · Expense approval" and "ACME · Incident response" read apart at a glance in every list."""
    name = str(name or "").strip()
    if not name:
        return mark_safe('<span class="muted">—</span>')
    words = [w for w in re.split(r"[\s_\-:]+", name.split("·")[-1]) if w and w[0].isalnum()] or [name]
    mark = (words[0][0] + (words[1][0] if len(words) > 1 else words[0][1:2])).upper()
    hue = process_hue(name)
    return format_html('<span class="proc" style="--h: {}"><span class="proc-mark" aria-hidden="true">{}</span>'
                       '<span class="proc-name">{}</span></span>', hue, mark, name)


@register.filter
def case_url(case_id):
    """Where a case lives: /workflows/<workflow id>/cases/<execution>/ (bpmn8n/web/workflow.py)."""
    from ..web.workflow import case_url as url
    return url(case_id)


@register.filter
def workflow_url(case_id):
    """The overview of a case's workflow: /workflows/<workflow id>/."""
    from django.urls import reverse
    workflow_id = str(case_id).rpartition(":")[0]
    return reverse("workflow", args=[workflow_id]) if workflow_id else ""
