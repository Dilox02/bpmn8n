"""Live function invocations, distinct across concurrent branches of an execution."""
from django.db.models import F, Max
from ninja import Router
from ninja.errors import HttpError

from ..core.evaluations import EvaluationStore
from ..models import Event
from .conditional import conditional, revision
from .evaluations import evaluation_read_auth
from .tasks import TASKS
from .stream import ChangeStream

router = Router(tags=["executions"])


@router.get("/{workflow_id}/{execution_id}/stream", auth=evaluation_read_auth)
def stream(request, workflow_id: str, execution_id: str):
    try:
        EvaluationStore.identifier(workflow_id)
        EvaluationStore.identifier(execution_id)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    def snapshot():
        events = Event.objects.filter(case_id=f"{workflow_id}:{execution_id}")
        return [list(events.filter(status="running").values_list("id", flat=True)),
                events.exclude(status="running").aggregate(last=Max("ended"), id=Max("id")),
                EvaluationStore().version(workflow_id, execution_id)]
    return ChangeStream(snapshot).response(request)


@router.get("/{workflow_id}/{execution_id}/activity", auth=evaluation_read_auth)
def activity(request, workflow_id: str, execution_id: str):
    try:
        EvaluationStore.identifier(workflow_id)
        EvaluationStore.identifier(execution_id)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    events = Event.objects.filter(case_id=f"{workflow_id}:{execution_id}")
    events = events.annotate(invocation=F("data__invocation")).defer("data")
    active = list(events.filter(status="running").order_by("id")[:100])
    completed = events.exclude(status="running")
    tag = revision([[(e.pk, e.invocation) for e in active], completed.aggregate(last=Max("ended"), id=Max("id"))])
    if request.headers.get("If-None-Match") == tag:
        return conditional(request, {}, tag)

    def as_row(event):
        task = TASKS.get(event.endpoint)
        return dict(id=event.pk, node_name=event.activity, function=event.endpoint, status=event.status,
                    started=event.started, ended=event.ended if event.status != "running" else None,
                    duration_ms=event.duration_ms, invocation=event.invocation,
                    error=event.error, source=task.location if task else None)

    return conditional(request, dict(active=[as_row(e) for e in active], active_count=events.filter(status="running").count(),
                                     recent=[as_row(e) for e in completed.order_by("-ended", "-id")[:100]]), tag)


def preview(value, depth=0):
    """A bounded copy of an event's data, to show: 5 levels, 2000 characters, 20 items, 50 keys."""
    if depth >= 5:
        return "[deeper levels: open n8n's inspector]"
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + f"… [{len(value)} characters]"
    if isinstance(value, list):
        return [preview(v, depth+1) for v in value[:20]] + ([f"… {len(value)-20} more items"] if len(value) > 20 else [])
    if isinstance(value, dict):
        result = {k: preview(v, depth+1) for k, v in list(value.items())[:50]}
        if len(value) > 50:
            result["…"] = f"{len(value)-50} more keys"
        return result
    return value


@router.get("/{workflow_id}/{execution_id}/activity/{event_id}", auth=evaluation_read_auth)
def invocation(request, workflow_id: str, execution_id: str, event_id: int):
    event = Event.objects.filter(pk=event_id, case_id=f"{workflow_id}:{execution_id}").first()
    if not event:
        raise HttpError(404, "Invocation not found")
    return {"input": preview(event.data.get("input")), "output": preview(event.data.get("output")), "error": event.error}
