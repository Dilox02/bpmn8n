"""The Insights page: what the event log says about a process, filtered as Disco does, with queueing
theory on what really happened. Its whole state is the query string (process, window, the filter stack,
the comparison cohort, the target wait), so a view is a link to share; adding or removing a filter is a
plain GET form and a redirect, with no script."""
from datetime import datetime, time, timedelta
from datetime import timezone as tz
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from ..analytics import FilterError, FilterStack, Log, Scope, queues, stats
from ..analytics.log import duration_text
from ..core import directory
from ..models import Snapshot
from . import charts, processmap

STATE = ("process", "since", "until", "filters", "vs", "target", "acts", "paths", "map")
SNAPSHOTS = 20  # listed on the page
UNITS = {"s": 1, "min": 60, "h": 3600, "d": 86400}
BUCKET_NAMES = {60: "minute", 300: "5 minutes", 900: "15 minutes", 3600: "hour", 3 * 3600: "3 hours",
                6 * 3600: "6 hours", 86400: "day", 7 * 86400: "week", 30 * 86400: "30 days"}


def day(text, end=False):
    """A date input (YYYY-MM-DD) as a UTC instant: its start, or for `until` the next day's start."""
    try:
        d = datetime.combine(datetime.strptime(text, "%Y-%m-%d").date(), time(), tz.utc)
    except (TypeError, ValueError):
        return None
    return d + timedelta(days=1) if end else d


def amount(get, name):
    """A number field with its unit select (`<name>_unit`) as seconds; None when empty."""
    raw = get.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw) * UNITS.get(get.get(f"{name}_unit", "s"), 1)
    except ValueError:
        raise FilterError(f"{name} must be a number") from None


def submitted(get):
    """The filter the "add filter" form describes, as the stack's JSON."""
    kind, value = get.get("add"), lambda name: get.get(name, "").strip() or None  # noqa: E731
    if kind == "timeframe":
        start, end = day(get.get("start")), day(get.get("end"), end=True)
        return {"type": kind, "mode": value("mode"), "start": start and start.isoformat(), "end": end and end.isoformat()}
    if kind == "variants":
        return {"type": kind, value("by") or "top": value("n")}
    if kind == "performance":
        return {"type": kind, "measure": value("measure"), "from": value("from"), "to": value("to"),
                "min": amount(get, "min"), "max": amount(get, "max")}
    if kind == "endpoints":
        return {"type": kind, "start": [s for s in [value("start")] if s], "end": [e for e in [value("end")] if e]}
    if kind == "attribute":
        return {"type": kind, "key": value("key"), "values": [value("value")] if value("value") else [], "mode": value("mode")}
    if kind == "follower":
        return {"type": kind, "from": value("from"), "to": value("to"), "relation": value("relation"),
                "within": amount(get, "within"), "mode": value("mode")}
    raise FilterError(f"unknown filter {kind!r}")


def link(state, **changes):
    """The page's URL with some of its state changed."""
    query = {k: v for k, v in {**state, **changes}.items() if v not in (None, "")}
    return "?" + urlencode(query) if query else "?"


@login_required
def insights(request):
    get = request.GET
    state = {k: get.get(k, "") for k in STATE}
    if "add" in get:  # the "add filter" form: validate, append to its stack, back to the canonical URL
        target = "vs" if get.get("into") == "vs" else "filters"
        try:
            found = {k: v for k, v in submitted(get).items() if v not in (None, [])}
            stack = FilterStack.parse(state[target]).plus(FilterStack.parse([found]).filters[0])
            state[target] = stack.query()
        except FilterError as e:
            messages.error(request, f"The filter was not added: {e}")
        return redirect(request.path + link(state))  # this page: /insights/, or a workflow's Insights tab

    processes = stats.processes()
    if not state["process"] and not getattr(request, "b8_workflow", None):
        return choose(request, processes)
    process = state["process"] or (processes[0]["process"] if processes else "")
    state["process"] = process
    stacks = {}
    for key in ("filters", "vs"):
        try:
            stacks[key] = FilterStack.parse(state[key])
        except FilterError as e:
            messages.error(request, f"Filters ignored: {e}")
            stacks[key], state[key] = FilterStack(), ""
    try:
        target = float(state["target"]) * 60 if state["target"] else None
    except ValueError:
        target = None
    base = Log.load(Scope(process or None, since=day(state["since"]), until=day(state["until"], end=True)))
    log, steps = base.filter(stacks["filters"])
    context = {"processes": processes, "process": process, "state": state, "base_cases": len(base.traces),
               "chips": chips(state, "filters", stacks["filters"], steps), "names": names(base), "target": state["target"],
               "snapshots": [{"name": n.name, "author": n.author, "created": n.created, "url": reverse("snapshot", args=[n.pk])}
                             for n in Snapshot.objects.all()[:SNAPSHOTS]]}
    if stacks["vs"]:
        cohort, vs_steps = base.filter(stacks["vs"])
        context.update(vs_chips=chips(state, "vs", stacks["vs"], vs_steps), comparison=stats.compare(log, cohort),
                       vs_cases=len(cohort.traces))
    if log.traces:
        context.update(page(log, target), pmap=processmap.section(log, state))
    return render(request, "bpmn8n/insights.html", context)


@login_required
@require_POST
def save_snapshot(request):
    """Names the page's current state (its hidden fields) and opens it by its own URL."""
    state = {k: v for k in STATE if (v := request.POST.get(k, "").strip())}
    name = request.POST.get("name", "").strip()[:120]
    if not name:
        messages.error(request, "Give the snapshot a name.")
        return redirect(reverse("insights") + link(state))
    snapshot = Snapshot.objects.create(name=name, state=state, author=request.user.get_username())
    messages.success(request, f"Saved {name}: reopen it from the list or by its link.")
    return redirect("snapshot", snapshot.pk)


@login_required
def open_snapshot(request, pk):
    """A saved analysis: the Insights page with that state, recomputed on the log as it is now."""
    return redirect(reverse("insights") + link(get_object_or_404(Snapshot, pk=pk).state))


def chips(state, key, stack, steps):
    """The stack as removable chips: each says what it kept and links to the page without it."""
    return [{"label": s["label"], "before": s["before"], "kept": s["kept"],
             "remove": link(state, **{key: stack.without(i).query()})} for i, s in enumerate(steps)]


def names(log):
    """What the add-filter forms offer: the activities, resources, statuses and processes seen."""
    seen = {"activity": set(), "resource": set(), "status": set(), "process": {t.process for t in log.traces}}
    for t in log.traces:
        for s in t.steps:
            seen["activity"].add(s.activity)
            seen["resource"].add(s.resource)
            seen["status"].add(s.status)
    return {k: sorted(v - {""}) for k, v in seen.items()}


def page(log, target):
    """Every section's figures, and the charts drawn from them."""
    overview, timeline, bins = stats.overview(log), stats.timeline(log), stats.histogram(log)
    bucket = timeline["bucket"]
    stamp = "%d %b %H:%M" if bucket < 86400 else "%d %b %Y"
    when = lambda p: datetime.fromisoformat(p["start"]).strftime(stamp)  # noqa: E731
    activities, variants = stats.activities(log), stats.variants(log, 10)
    q = queues.queues(log, target)
    edges = [duration_text(bins[0]["from"]), duration_text(bins[len(bins) // 2]["from"]), duration_text(bins[-1]["to"])] \
        if bins else []
    return {
        "overview": overview, "activities": activities, "variants": variants, "queues": q,
        "most_executions": max((a["executions"] for a in activities), default=0),
        "longest_wait": max((a["wait"] or 0 for a in activities), default=0),
        "resources": stats.resources(log)[:12], "handoffs": stats.handoffs(log), "decisions": stats.decisions(log),
        "bucket": BUCKET_NAMES.get(bucket, duration_text(bucket)),
        "events_chart": charts.series(timeline["points"], label=f"Events per {BUCKET_NAMES.get(bucket, 'bucket')}",
                                      value=lambda p: p["events"], when=when, fmt=lambda v: f"{v:g}", area=False),
        "active_chart": charts.series(timeline["points"], label="Cases in progress", value=lambda p: p["active"],
                                      when=when, fmt=lambda v: f"{v:g}"),
        "histogram_chart": charts.bars(bins, label="Closed cases by duration", value=lambda b: b["cases"],
                                       name=lambda b: f"{duration_text(b['from'])} – {duration_text(b['to'])}",
                                       fmt=lambda v: f"{v:g}", edges=edges),
    }


def choose(request, processes):
    """/insights/ with no process: the workflows n8n has whose cases are in the event log, each opening its Insights
    tab; then the logs of no workflow in n8n now (imported ones, deleted workflows), by their process name."""
    cases = {p["process"]: p["cases"] for p in processes}
    rows, named = [], set()
    for ref in (r for r in directory.workflows() if r.in_n8n and r.cases):
        named.add(process := ref.process)
        rows.append({"name": ref.name, "url": reverse("workflow_insights", args=[ref.id]),
                     "meta": f"{cases.get(process, ref.cases):,} case{'s' if cases.get(process, ref.cases) != 1 else ''}"})
    others = [{"name": p["process"], "url": f"?process={quote(p['process'])}", "meta": f"{p['cases']:,} cases"}
              for p in processes if p["process"] not in named]
    return render(request, "bpmn8n/chooser.html", {
        "title": "Insights", "sub": "What the event log says about a workflow: choose one.", "workflows": rows,
        "empty": "No case in the event log yet. Run a process in n8n (every bpmn8n step is logged), import a log, or see "
                 "a demo with manage.py seed_events." if not processes else "No workflow in n8n has a case in the event log yet.",
        "others": others,
        "others_title": "Other event logs", "others_note": "imported, or of workflows n8n no longer has",
        "action": {"url": reverse("insights_import"), "label": "Import a log"} if request.user.is_staff else None})
