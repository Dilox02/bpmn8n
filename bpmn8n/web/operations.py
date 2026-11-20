"""Pages for operating processes with what n8n sells by subscription: global variables (staff), the search
over cases (staff: it shows process data), the workflows bpmn8n has seen and their history."""
import json
from collections import Counter
from datetime import date, datetime, time
from functools import reduce
from operator import or_
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..clients.n8n import N8nError
from ..core import directory, history, shared
from ..core.cases import OPERATORS, STATUSES, CaseSearch, SearchError
from ..models import AuditEntry, Case, UserTask, WorkflowVersion
from ..ops import health
from ..workflows.changes import changes
from . import scope
from .cases import steps_of as case_steps
from .paging import paginate

PAGE = 50  # cases per page
VERSIONS = 50  # versions shown on a workflow's page


@staff_member_required
def status(request):
    """What the top bar's dots summarise: every check with its latency and error, and doctor's hints."""
    results = health.run()
    return render(request, "bpmn8n/status.html", {"results": results, "overall": health.overall(results),
                                                  "hints": health.hints()})


@staff_member_required
def variables(request):
    """Every global variable, by key: add, edit and delete them with JSON values."""
    store, prefix = shared.store(), request.GET.get("prefix", "").strip()
    if request.method == "POST":
        key = request.POST.get("key", "").strip()
        try:
            if request.POST.get("action") == "delete":
                if store.delete(key):
                    AuditEntry.write("variable.delete", object_type="variable", object_id=key)
                messages.success(request, f"Deleted {key}.")
            else:
                ttl = request.POST.get("ttl", "").strip()
                store.set(key, json.loads(request.POST.get("value", "")), float(ttl) if ttl else None)
                AuditEntry.write("variable.set", object_type="variable", object_id=key, ttl=float(ttl) if ttl else None)
                messages.success(request, f"Saved {key}.")
        except json.JSONDecodeError:
            messages.error(request, 'The value is not JSON: write 42, "text", true, [1, 2] or {"limit": 3}.')
        except ValueError as e:  # a key, a value or a TTL that is not allowed
            messages.error(request, str(e))
        return redirect(f"{reverse('variables')}?{urlencode({'prefix': prefix})}" if prefix else "variables")
    try:
        rows = store.list(prefix)
    except shared.InvalidKey as e:
        messages.error(request, str(e))
        rows = []
    return render(request, "bpmn8n/variables.html", {
        "prefix": prefix, "redis": shared.connection() is not None,
        "variables": [{"v": v, "json": json.dumps(v.value, ensure_ascii=False),
                       "pretty": json.dumps(v.value, indent=2, ensure_ascii=False)} for v in rows]})


def day(text, end=False):
    """A date from a date field, as the first (or last) instant of that day."""
    try:
        return timezone.make_aware(datetime.combine(date.fromisoformat(text), time.max if end else time.min))
    except (TypeError, ValueError):
        return None


@staff_member_required
def cases(request, workflow=None):
    """The cases, newest first, filtered like GET /api/cases: process, version, start day, status, variables. Every
    workflow's (/cases/) or one workflow's (its Cases tab). Cases of workflows deleted in n8n are left out unless
    asked for (?deleted=1): n8n no longer has them, so a list that agrees with n8n does not either."""
    q = request.GET
    live = directory.from_n8n()
    try:
        criteria = CaseSearch.criteria(q) + [CaseSearch.criterion(p.strip(), o, v) for p, o, v in
                                             zip(q.getlist("path"), q.getlist("op"), q.getlist("value")) if p.strip()]
        search = CaseSearch("" if workflow else q.get("process", ""), q.get("version", ""), day(q.get("since")),
                            day(q.get("until"), end=True), q.get("status", ""), criteria)
        found = search.cases()
        choices = []
        if workflow:
            found = found.filter(case_id__startswith=f"{workflow.id}:")
        else:
            if live is not None and not q.get("deleted"):
                found = found.filter(reduce(or_, (Q(case_id__startswith=f"{w}:") for w in live), Q(pk__in=[])))
            # ponytail: counted in Python over the ids; a GROUP BY on the id's prefix once cases reach the 100,000s
            choices = scope.choices(request, Counter(scope.workflow_of(c) for c in found.values_list("case_id", flat=True)))
            if chosen := q.get("workflow"):
                found = found.filter(case_id__startswith=f"{chosen}:")
        page, pager = paginate(request, found, PAGE)
        steps = case_steps([c.case_id for c in page])
        states = {r.id: r.state for r in directory.workflows()} if not workflow else {}
        rows = [{"case": c, "execution": c.case_id.rpartition(":")[2], "matched": search.matched(c), "steps": steps.get(c.case_id, []),
                 "deleted": states.get(c.case_id.rpartition(":")[0]) == "deleted"} for c in page]
    except SearchError as e:
        messages.error(request, str(e))
        criteria, rows, pager, choices = [], [], {}, []
    advanced = any(q.get(k) for k in ("version", "since", "until")) or any(p for p in q.getlist("path"))
    return render(request, "bpmn8n/cases.html", {
        "rows": rows, "pager": pager, "q": q, "statuses": STATUSES, "operators": OPERATORS, "workflow": workflow,
        "workflow_choices": choices,
        "advanced": advanced, "show_deleted": bool(q.get("deleted")), "knows_n8n": live is not None,
        "processes": Case.objects.exclude(process="").values_list("process", flat=True).distinct().order_by("process"),
        "criteria": [*criteria, ("", "", "")][:CaseSearch.MAX_CRITERIA], "paths": [p for p, _, _ in criteria]})


@login_required
def workflows(request):
    """Every workflow: the ones n8n has now, with their state there (published, draft, archived), and on their own
    tab the ones n8n no longer has, whose cases and versions bpmn8n kept."""
    every = directory.workflows()
    deleted = [r for r in every if r.state == "deleted"]
    show = "deleted" if request.GET.get("show") == "deleted" else ""
    shown = deleted if show else [r for r in every if r.state != "deleted"]
    waiting = Counter(case_id.rpartition(":")[0] for case_id in UserTask.objects.waiting().values_list("case_id", flat=True))
    page, pager = paginate(request, shown, PAGE)
    return render(request, "bpmn8n/workflows.html", {
        "rows": [{"ref": r, "waiting": waiting.get(r.id, 0)} for r in page], "pager": pager, "show": show,
        "tabs": [{"label": "In n8n", "url": reverse("workflows"), "current": not show, "count": len(every) - len(deleted)},
                 {"label": "Deleted in n8n", "url": f"{reverse('workflows')}?show=deleted", "current": bool(show),
                  "count": len(deleted)}],
        "knows_n8n": any(r.state != "unknown" for r in every) or not every})


@login_required
def workflow_history(request, workflow_id):
    """A workflow's versions, newest first, each with what changed since the one before."""
    newest = list(WorkflowVersion.objects.filter(workflow_id=workflow_id).order_by("-created", "-id")[:VERSIONS + 1])
    if not newest and not Case.objects.filter(case_id__startswith=f"{workflow_id}:").exists():
        raise Http404(workflow_id)
    counts = history.cases_per_version(workflow_id)
    versions = [{"v": v, "cases": counts.get(v.version_id, 0), "changes": changes(older.workflow if older else {}, v.workflow),
                 "first": older is None}
                for v, older in zip(newest[:VERSIONS], [*newest[1:], None])]
    seen = history.workflow_cases().get(workflow_id, {})
    return render(request, "bpmn8n/workflow_history.html", {
        "workflow_id": workflow_id, "name": newest[0].name if newest else seen.get("process", workflow_id),
        "versions": versions, "cases": seen.get("cases", 0), "unversioned": seen.get("cases", 0) - sum(counts.values())})


@staff_member_required
@require_POST
def restore_version(request, workflow_id, version_id):
    """Saves an old version back to n8n as the newest one; n8n never activates it on its own."""
    version = get_object_or_404(WorkflowVersion, workflow_id=workflow_id, version_id=version_id)
    try:
        restored = history.restore(version)
        AuditEntry.write("workflow.restore", object_type="workflow", object_id=workflow_id, version=version_id,
                         saved_as=restored.version_id if restored else None)
        messages.success(request, f"Saved version {version_id[:8]} back to n8n"
                                  f"{f' as {restored.version_id[:8]}' if restored else ''}. "
                                  "It is not published: publish it in n8n's editor when it should run.")
    except N8nError as e:
        messages.error(request, f"n8n did not take it: {e}")
    return redirect("workflow_versions", workflow_id)
