"""One workflow's pages, under /workflows/<n8n workflow id>/, as n8n keys them (/workflow/<id>/executions/<n>):

    /workflows/<id>/                     overview: how it is doing, what waits, its latest cases
    /workflows/<id>/cases/               its cases               /workflows/<id>/cases/<execution>/   one case
    /workflows/<id>/insights/            what its event log says /workflows/<id>/simulation/          its capacity
    /workflows/<id>/versions/            the versions kept       /workflows/<id>/start/               start a case

Each tab is the framework's page for that subject, given this workflow: the header (its mark, name, state in n8n
and the tabs) is drawn by base.html from request.b8_workflow, so every tab says which workflow it is about."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse

from .. import conf
from ..api.simulation import sources as simulation_sources
from ..core import directory
from ..core.cases import CaseSearch
from ..models import Case, Event, UserTask, WorkflowVersion
from . import cases as case_pages
from . import insights as insights_pages
from . import operations, people
from . import simulation as simulation_pages

TABS = (("workflow", "Overview", False), ("workflow_cases", "Cases", True), ("workflow_insights", "Insights", False),
        ("workflow_simulation", "Simulation", True), ("workflow_versions", "Versions", False), ("workflow_start", "Start a case", False))


def in_workflow(current, **inject):
    """A framework page as one of the workflow's tabs: the workflow resolved (404 if nobody knows it), the header and
    the tabs on the request, and the query the page reads (`inject`: GET name -> the workflow's attribute)."""
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapper(request, workflow_id, *args, **kwargs):
            ref = directory.get(workflow_id)
            if ref is None:
                raise Http404(workflow_id)
            if inject:
                request.GET = request.GET.copy()
                for name, attribute in inject.items():
                    request.GET[name] = getattr(ref, attribute)
            starts = {p["workflow_id"] for p in people.starts().values()}
            modelled = ref.name in (names := set(simulation_sources().values())) or ref.process in names
            request.b8_workflow = {
                "ref": ref, "n8n_url": f"{conf.settings.N8N_PUBLIC_URL.rstrip('/')}/workflow/{ref.id}" if ref.in_n8n else "",
                "tabs": [{"label": label, "url": reverse(name, args=[ref.id]), "current": name == current}
                         for name, label, staff in TABS if (request.user.is_staff or not staff)
                         and (name != "workflow_start" or ref.id in starts)
                         and (name != "workflow_simulation" or modelled)]}
            return view(request, ref, *args, **kwargs)
        return wrapper
    return decorate


@in_workflow("workflow")
def overview(request, ref):
    """How the workflow is doing and what waits: its figures, its open tasks, its latest cases."""
    prefix = f"{ref.id}:"
    cases = Case.objects.filter(case_id__startswith=prefix)
    tasks = UserTask.objects.waiting().for_user(request.user).filter(case_id__startswith=prefix)
    errors = Event.objects.filter(case_id__startswith=prefix, status="error").values("case_id").distinct().count()
    recent = list(CaseSearch("", "", None, None, "", []).cases().filter(case_id__startswith=prefix)[:8])  # with status, last step
    steps = case_pages.steps_of([c.case_id for c in recent])
    return render(request, "bpmn8n/workflow.html", {
        "ref": ref, "case_count": cases.count(), "error_cases": errors,
        "waiting_cases": tasks.values("case_id").distinct().count(), "task_count": tasks.count(),
        "tasks": list(tasks.by_urgency()[:5]), "recent": [{"case": c, "execution": c.case_id.rpartition(":")[2], "steps": steps.get(c.case_id, [])}
                   for c in recent],
        "versions": WorkflowVersion.objects.filter(workflow_id=ref.id).count()})


@in_workflow("workflow_cases")
def cases(request, ref):
    return operations.cases(request, workflow=ref)


@in_workflow("workflow_cases")
def case(request, ref, execution):
    return case_pages.case(request, f"{ref.id}:{execution}")


@in_workflow("workflow_insights", process="process")
def insights(request, ref):
    return insights_pages.insights(request)


@in_workflow("workflow_simulation", process="process")
def simulation(request, ref):
    return simulation_pages.simulation(request)


@in_workflow("workflow_versions")
def versions(request, ref):
    return operations.workflow_history(request, ref.id)


@in_workflow("workflow_start")
def start(request, ref):
    return people.start_case(request, workflow_id=ref.id)


def old_case(request, case_id):
    """/cases/<workflow id>:<execution>/, the address before a case lived under its workflow: links in mails and in
    n8n's nodes still point there."""
    workflow_id, _, execution = case_id.rpartition(":")
    if not workflow_id or not execution:
        raise Http404(case_id)
    return redirect("workflow_case", workflow_id, execution, permanent=True)


def case_url(case_id):
    """Where a case lives: /workflows/<workflow id>/cases/<execution>/."""
    workflow_id, _, execution = str(case_id).rpartition(":")
    return reverse("workflow_case", args=[workflow_id, execution]) if workflow_id and execution else ""
