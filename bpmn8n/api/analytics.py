"""Insights on the event log (bpmn8n/analytics): every endpoint takes the same scope, `process` (the
workflow name), `since`/`until` (cases that started in that window) and `filters` (a JSON filter stack),
and says what each filter kept. Times are seconds, rates per hour. /overlay feeds the canvas heatmap in
n8n's editor, so it also accepts the n8n session that may open that workflow."""
from datetime import datetime

from ninja import Query, Router
from ninja.errors import HttpError

from ..analytics import FilterError, FilterStack, Log, Scope, queues, stats
from .evaluations import evaluation_read_auth

router = Router(tags=["analytics"])


def scoped(process=None, since=None, until=None, filters=None, workflow=None):
    """The cases in scope after the filter stack, and what each filter kept; a bad stack is a 422."""
    try:
        stack = FilterStack.parse(filters)
    except FilterError as e:
        raise HttpError(422, str(e)) from None
    if since and until and since >= until:
        raise HttpError(422, "since must come before until")
    log, steps = Log.load(Scope(process, workflow, since, until)).filter(stack)
    return log, {"process": process, "since": since, "until": until, "cases": len(log.traces), "filters": steps}


@router.get("/overview")
def overview(request, process: str | None = None, since: datetime | None = None, until: datetime | None = None,
             filters: str | None = None):
    """Cases (open and closed), events, activities, the window, case durations and their histogram."""
    log, scope = scoped(process, since, until, filters)
    return {"scope": scope, **stats.overview(log), "histogram": stats.histogram(log)}


@router.get("/timeline")
def timeline(request, process: str | None = None, since: datetime | None = None, until: datetime | None = None,
             filters: str | None = None):
    """Events started and cases in progress per time bucket (its size in seconds follows the window)."""
    log, scope = scoped(process, since, until, filters)
    return {"scope": scope, **stats.timeline(log)}


@router.get("/activities")
def activities(request, process: str | None = None, since: datetime | None = None, until: datetime | None = None,
               filters: str | None = None):
    """Per activity: executions, cases, durations, the mean wait before it, errors."""
    log, scope = scoped(process, since, until, filters)
    return {"scope": scope, "activities": stats.activities(log)}


@router.get("/resources")
def resources(request, process: str | None = None, since: datetime | None = None, until: datetime | None = None,
              filters: str | None = None):
    """Per resource: events, cases, busy time and utilisation; and the handoffs between resources."""
    log, scope = scoped(process, since, until, filters)
    return {"scope": scope, "resources": stats.resources(log), "handoffs": stats.handoffs(log)}


@router.get("/variants")
def variants(request, process: str | None = None, since: datetime | None = None, until: datetime | None = None,
             filters: str | None = None, limit: int = Query(50, ge=1, le=1000)):
    """The most frequent sequences of activities, with coverage and mean duration."""
    log, scope = scoped(process, since, until, filters)
    return {"scope": scope, **stats.variants(log, limit)}


@router.get("/decisions")
def decisions(request, process: str | None = None, since: datetime | None = None, until: datetime | None = None,
              filters: str | None = None):
    """Per gateway: branch percentages, who decided, Laya's confidence."""
    log, scope = scoped(process, since, until, filters)
    return {"scope": scope, "decisions": stats.decisions(log)}


@router.get("/queues")
def queue_view(request, process: str | None = None, since: datetime | None = None, until: datetime | None = None,
               filters: str | None = None, target_wait: float | None = Query(None, gt=0)):
    """Per lane and activity: λ, E[S], ca², cs², servers, ρ, observed against predicted (G/G/c) waits,
    Little's law, ± one person, the people a `target_wait` (seconds) needs; the bottlenecks; what the log
    cannot separate."""
    log, scope = scoped(process, since, until, filters)
    return {"scope": scope, **queues.queues(log, target_wait)}


@router.get("/compare")
def compare(request, a: str = "", b: str = "", process: str | None = None, since: datetime | None = None,
            until: datetime | None = None, filters: str | None = None):
    """Two cohorts side by side: `a` and `b` are filter stacks applied after `filters`; b − a per figure."""
    log, scope = scoped(process, since, until, filters)
    try:
        (cohort_a, steps_a), (cohort_b, steps_b) = (log.filter(FilterStack.parse(x)) for x in (a, b))
    except FilterError as e:
        raise HttpError(422, str(e)) from None
    return {"scope": scope, "a": {"cases": len(cohort_a.traces), "filters": steps_a},
            "b": {"cases": len(cohort_b.traces), "filters": steps_b}, **stats.compare(cohort_a, cohort_b)}


def overlay_auth(request):
    """The evaluation views' rule (the API token, or an n8n session that may open the workflow), with the
    workflow from the query string."""
    request.resolver_match.kwargs.setdefault("workflow_id", request.GET.get("workflow_id", ""))
    return evaluation_read_auth(request)


@router.get("/overlay", auth=overlay_auth)
def overlay(request, workflow_id: str, since: datetime | None = None, until: datetime | None = None,
            filters: str | None = None):
    """Per node name of a workflow's canvas: frequency, case frequency, mean and median duration, errors,
    and a 0–1 heat for frequency and for duration."""
    log, scope = scoped(None, since, until, filters, workflow=workflow_id)
    return {"scope": {**scope, "workflow_id": workflow_id}, "nodes": stats.overlay(log)}


REPLAY_STEPS = 200  # steps kept per case: the animation is a picture of the flow, not the whole log


@router.get("/replay", auth=overlay_auth)
def replay(request, workflow_id: str, since: datetime | None = None, limit: int = Query(50, ge=1, le=200)):
    """The latest `limit` cases of a workflow (that started at or after `since`), each its ordered steps as
    {node, start, end} in epoch seconds: what the editor's Replay animates over the canvas."""
    log, scope = scoped(None, since, None, None, workflow=workflow_id)
    traces = sorted(log.traces, key=lambda t: t.start)[-limit:]
    return {"scope": {**scope, "workflow_id": workflow_id}, "cases": [
        {"case_id": t.case_id, "steps": [{"node": s.activity, "start": s.started, "end": s.ended} for s in t.steps[:REPLAY_STEPS]]}
        for t in traces]}
