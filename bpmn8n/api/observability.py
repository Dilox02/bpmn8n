"""The event log and the cases, for monitoring and process-mining tools."""
from datetime import datetime
from typing import Any, Literal

from django.http import StreamingHttpResponse
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from ..core.cases import CaseData, CaseSearch, SearchError
from ..core.observability import CASE_HEADER, metrics_snapshot
from ..core.streaming import EventStream, publish
from ..models import Case, Event

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
    status: Literal["complete", "error"] = "complete"
    message: str = ""  # {{variable}} templates allowed
    item: dict[str, Any] = {}


@router.post("/events", response={200: dict, 400: dict})
def record_event(request, data: EventIn):
    """An instant event of the calling case (n8n execution) in the event log, e.g. a BPMN Error
    End Event; the activity is the X-Bpmn8n-Activity header (the n8n node name). Returns the
    message with its variables filled in."""
    case = CaseData(request, data.item)
    if not case.trace.case_id:
        return 400, {"detail": f"{CASE_HEADER} header required"}
    at = timezone.now()
    message = case.text(data.message)
    publish(Event.objects.create(case_id=case.trace.case_id, activity=case.trace.activity or "event",
                                 process=case.trace.process, endpoint="event", started=at, ended=at, duration_ms=0,
                                 status=data.status, resource="system", data={"output": {"message": message}}))
    return 200, {"message": message}


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
