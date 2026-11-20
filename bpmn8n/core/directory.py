"""Which workflows exist, one answer for every page. n8n is the source of truth for a workflow: its current name,
whether it is published, a draft or archived. bpmn8n's own records (cases, events, versions) add the workflows it
has seen and n8n no longer has: deleted there, they keep their history here and are marked so. Without n8n's API
(no key, n8n down) the states are unknown and the names are the recorded ones.

Every page keys a workflow by its n8n id, as n8n's own URLs do (/workflow/<id>/executions/<execution>)."""
import zlib
from dataclasses import dataclass
from datetime import datetime

from django.core.cache import cache

from ..clients import n8n
from ..conf import settings
from ..models import Event, WorkflowVersion
from . import history

TTL = 30  # seconds: a workflow created in n8n shows here within half a minute


def cache_key():
    """One answer per n8n (its address and key): a test, or a second instance, never reads another's."""
    return f"b8:n8n-workflows:{zlib.crc32(f'{settings.N8N_URL}|{settings.N8N_API_KEY}'.encode())}"


@dataclass(frozen=True)
class WorkflowRef:
    id: str
    name: str
    state: str  # published, draft, archived (in n8n), deleted (only here), unknown (n8n unreadable)
    cases: int = 0
    last: datetime | None = None

    @property
    def in_n8n(self):
        return self.state in ("published", "draft", "archived")

    @property
    def process(self):
        """The name its cases ran under in the event log (what Insights and the simulation read), else its name."""
        return (Event.objects.filter(case_id__startswith=f"{self.id}:").exclude(process="")
                .order_by("-started").values_list("process", flat=True).first()) or self.name


def from_n8n():
    """{id: (name, state)} as n8n has them now, or None when its API cannot be read; cached TTL seconds."""
    cached = cache.get(key := cache_key())
    if cached is not None:
        return cached or None  # False: unreadable, kept as briefly as an answer
    try:
        found = {w["id"]: (w.get("name") or w["id"],
                           "archived" if w.get("isArchived") else "published" if w.get("active") else "draft")
                 for w in n8n.pages("/workflows")}
    except Exception:  # noqa: BLE001 - whatever n8n answers, a page lists what bpmn8n recorded rather than fail
        found = None
    cache.set(key, found if found is not None else False, TTL)
    return found


def workflows():
    """Every workflow n8n has or bpmn8n has seen, newest activity first."""
    live, seen = from_n8n(), history.workflow_cases()
    recorded = dict(WorkflowVersion.objects.order_by("created", "id").values_list("workflow_id", "name"))  # the newest wins
    refs = []
    for wid in {*(live or {}), *seen, *recorded}:
        if live is not None and wid in live:
            name, state = live[wid]
        else:
            name, state = recorded.get(wid) or seen.get(wid, {}).get("process") or wid, "unknown" if live is None else "deleted"
        refs.append(WorkflowRef(wid, name, state, seen.get(wid, {}).get("cases", 0), seen.get(wid, {}).get("last")))
    return sorted(refs, key=lambda r: (r.last is None, -(r.last.timestamp() if r.last else 0), r.name.lower()))


def get(workflow_id):
    """The workflow, or None when neither n8n nor bpmn8n knows it."""
    return next((r for r in workflows() if r.id == workflow_id), None)
