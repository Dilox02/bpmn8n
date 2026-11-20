"""The event log and the cases, for monitoring and process-mining tools; the audit log and erasure by case."""
from datetime import datetime
from typing import Any, Literal

from django.http import StreamingHttpResponse
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from ..core.cases import CaseData, CaseSearch, SearchError
from ..core.erasure import NoSuchCase, StillWaiting, erase
from ..core.observability import CASE_HEADER, metrics_snapshot
from ..core.streaming import EventStream, publish
from ..models import AuditEntry, Case, Event

router = Router(tags=["observability"])

EVENT_LOG_COLUMNS = ("case_id", "activity", "endpoint", "start_timestamp", "end_timestamp", "status", "resource")


@router.get("/metrics")
def get_metrics(request):
    """Calls, errors and timings per task/gateway/wait; `last_profile` after a profiled request."""
    return metrics_snapshot()


@router.get("/eventlog.csv")
def get_event_log(request, since: datetime | None = None):
    """Process-mining event log (PM4Py, Disco, ProM): one row per activity of each case,
    streamed from the database."""
    def rows():
        yield ",".join(EVENT_LOG_COLUMNS) + "\n"
        for e in Event.objects.cases(since).values_list("case_id", "activity", "endpoint", "started", "ended",
                                                        "status", "resource").iterator(chunk_size=2000):
            yield ",".join(csv_cell(v) for v in e) + "\n"
    return StreamingHttpResponse(rows(), content_type="text/csv")


def csv_cell(value):
    text = value.isoformat() if isinstance(value, datetime) else str(value)
    return f'"{text.replace(chr(34), chr(34) * 2)}"' if any(c in text for c in ',"\r\n') else text


class EventIn(Schema):
    status: Literal["running", "complete", "error"] = "complete"
    message: str = ""  # {{variable}} templates allowed
    item: dict[str, Any] = {}
    event: int | None = None  # a running event of this case to finish (its id, as this call returned it)


@router.post("/events", response={200: dict, 400: dict, 404: dict})
def record_event(request, data: EventIn):
    """An event of the calling case (n8n execution) in the event log; the activity is the X-Bpmn8n-Activity
    header (the n8n node name). An instant one (a BPMN Error End Event), or a step that takes time (a Call
    Activity): "running" when it starts, then "complete" or "error" with the id it returned. Returns the message
    with its variables filled in, and the event's id."""
    case = CaseData(request, data.item)
    if not case.trace.case_id:
        return 400, {"detail": f"{CASE_HEADER} header required"}
    at = timezone.now()
    message = case.text(data.message)
    if data.event is not None:  # only the case's own running event: one case never edits another's log
        event = Event.objects.filter(pk=data.event, case_id=case.trace.case_id, status="running").first()
        if not event:
            return 404, {"detail": f"no running event {data.event} in this case"}
        event.status, event.ended = data.status, at
        event.duration_ms = (at - event.started).total_seconds() * 1000
        event.data = {**event.data, "output": {"message": message}} if message else event.data
        event.save(update_fields=["status", "ended", "duration_ms", "data"])
        publish(event)
        return 200, {"message": message, "event": event.pk}
    event = Event.objects.create(case_id=case.trace.case_id, activity=case.trace.activity or "event",
                                 process=case.trace.process, endpoint="event", started=at,
                                 ended=at, duration_ms=0,  # a running one's end moves when it finishes
                                 status=data.status, resource="system", data={"output": {"message": message}})
    if data.status != "running":  # the sinks hear of a step once it has finished
        publish(event)
    return 200, {"message": message, "event": event.pk}


@router.get("/events/stream")
def stream_events(request):
    """Every finished step as a server-sent event (log streaming): `id` is the cursor to send back as
    Last-Event-ID, `data` the event (process, case, activity, status, times, resource, error). The response
    ends after 55 seconds; reconnect with Last-Event-ID to go on where it stopped."""
    return EventStream().response(request)


class CaseRow(Schema):
    case_id: str
    process: str
    process_version: str
    status: str
    created: datetime
    last_event: datetime | None
    matched: dict[str, Any]


class CasePage(Schema):
    count: int
    results: list[CaseRow]


@router.get("/cases", response=CasePage)
def search_cases(request, process: str = "", version: str = "", since: datetime | None = None,
                 until: datetime | None = None, status: str = "", limit: int = 50, offset: int = 0):
    """Cases, newest first, by process (workflow name), version (n8n versionId), start time (since, until),
    status (waiting, error, active, closed) and the values of their variables: `var.<path>=<value>`, with
    `__gt`, `__gte`, `__lt`, `__lte` or `__contains` (text, ignoring case) after the path, e.g.
    `var.amount__gt=1000&var.customer.email__contains=@acme`. A value is JSON when it parses (1000, true,
    "123" quoted), else text. `matched`: the values of the variables the query names."""
    try:
        search = CaseSearch(process, version, since, until, status, CaseSearch.criteria(request.GET))
        cases = search.cases()
        page = list(cases[max(offset, 0):max(offset, 0) + min(max(limit, 1), 200)])
        count = cases.count()
    except SearchError as e:
        raise HttpError(422, str(e)) from None
    return {"count": count, "results": [{**{f: getattr(c, f) for f in ("case_id", "process", "process_version", "status",
                                                                        "created", "last_event")},
                                         "matched": search.matched(c)} for c in page]}


@router.get("/cases/{case_id}", response={200: dict, 404: dict})
def get_case(request, case_id: str):
    """A case's process variables (what {{name}} templates read)."""
    case = Case.objects.filter(case_id=case_id).values("case_id", "process", "variables", "created", "updated").first()
    return (200, case) if case else (404, {"detail": f"no case {case_id!r}"})


@router.post("/cases/{case_id}/erase", response={200: dict, 404: dict, 409: dict})
def erase_case(request, case_id: str, force: bool = False):
    """Erases the case's personal data (GDPR art. 17), as `manage.py erase_case`: its variables, its steps' input
    and output, its user tasks' and messages' data; activities, times and statuses stay for process mining.
    409 while it still waits, unless `force=true` (it cancels the waits first). For staff: the API token is the
    authority."""
    try:
        return 200, erase(case_id, force=force)
    except NoSuchCase as e:
        return 404, {"detail": str(e)}
    except StillWaiting as e:
        return 409, {"detail": str(e)}


class AuditOut(Schema):
    at: datetime
    actor: str
    action: str
    object_type: str
    object_id: str
    case_id: str
    details: dict
    ip: str | None


@router.get("/audit", response=list[AuditOut])
def list_audit(request, case_id: str = "", actor: str = "", action: str = "", since: datetime | None = None,
               limit: int = 100):
    """The audit log, newest first (at most 1000): who did what, on which object and case, from where. `action`
    is exact (usertask.complete) or a prefix ending in a dot (usertask.)."""
    entries = AuditEntry.objects.all()
    if case_id:
        entries = entries.filter(case_id=case_id)
    if actor:
        entries = entries.filter(actor=actor)
    if action:
        entries = entries.filter(action__startswith=action) if action.endswith(".") else entries.filter(action=action)
    if since:
        entries = entries.filter(at__gte=since)
    return entries[:min(max(limit, 1), 1000)]
