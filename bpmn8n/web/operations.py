"""Pages for operating processes with what n8n sells by subscription: global variables (staff), the search
over cases (staff: it shows process data), the workflows bpmn8n has seen and their history."""
import json
from datetime import date, datetime, time
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..clients.n8n import N8nError
from ..core import history, shared
from ..core.cases import OPERATORS, STATUSES, CaseSearch, SearchError
from ..models import AuditEntry, Case, WorkflowVersion
from ..workflows.changes import changes

PAGE = 50  # cases per page
VERSIONS = 50  # versions shown on a workflow's page


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
def cases(request):
    """The cases, newest first, filtered like GET /api/cases: process, version, start day, status, variables."""
    q = request.GET
    try:
        criteria = CaseSearch.criteria(q) + [CaseSearch.criterion(p.strip(), o, v) for p, o, v in
                                             zip(q.getlist("path"), q.getlist("op"), q.getlist("value")) if p.strip()]
        search = CaseSearch(q.get("process", ""), q.get("version", ""), day(q.get("since")), day(q.get("until"), end=True),
                            q.get("status", ""), criteria)
        found = search.cases()
        page = max(int(q.get("page", 1)) if q.get("page", "").isdigit() else 1, 1)
        rows = [{"case": c, "matched": search.matched(c)} for c in found[(page - 1) * PAGE:page * PAGE]]
        count = found.count()
    except SearchError as e:
        messages.error(request, str(e))
        criteria, rows, count, page = [], [], 0, 1

    def link(to):
        return "?" + urlencode([*((k, v) for k, v in q.items(multi=True) if k != "page"), ("page", to)]) if q else f"?page={to}"

    return render(request, "bpmn8n/cases.html", {
        "rows": rows, "count": count, "q": q, "statuses": STATUSES, "operators": OPERATORS,
        "processes": Case.objects.exclude(process="").values_list("process", flat=True).distinct().order_by("process"),
        "criteria": [*criteria, ("", "", "")][:CaseSearch.MAX_CRITERIA], "paths": [p for p, _, _ in criteria],
        "newer": link(page - 1) if page > 1 else "", "older": link(page + 1) if page * PAGE < count else ""})


@login_required
def workflows(request):
    """The workflows bpmn8n has seen, through their cases or snapshots of their versions."""
    seen = history.workflow_cases()
    kept = {r["workflow_id"]: r for r in WorkflowVersion.objects.values("workflow_id").annotate(
        versions=Count("id"), saved=Max("created"))}
    names = dict(WorkflowVersion.objects.order_by("created", "id").values_list("workflow_id", "name"))  # the newest wins
    rows = [{"id": i, "name": names.get(i) or seen.get(i, {}).get("process") or i,
             "cases": seen.get(i, {}).get("cases", 0), "last_case": seen.get(i, {}).get("last"),
             "versions": kept.get(i, {}).get("versions", 0), "saved": kept.get(i, {}).get("saved")}
            for i in {*seen, *kept}]
    rows.sort(key=lambda r: max(filter(None, (r["last_case"], r["saved"]))), reverse=True)
    return render(request, "bpmn8n/workflows.html", {"workflows": rows})


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
    return redirect("workflow_history", workflow_id)
