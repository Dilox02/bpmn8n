"""People's pages: the inbox, one page per human task (what the link in n8n's User Task node opens),
the activities this server runs, and the documentation."""
import inspect
import re
import zlib
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_POST

from .. import conf
from ..api.tasks import TASK_KINDS, TASKS
from ..clients.http import ServiceUnavailable
from ..models import Event, TransitionError, UserTask
from ..ops import health
from . import scope
from .paging import paginate
from .similar import similar_cases
from .text import to_html

ACTIONS = {  # what a person can do on the task page; the model allows it or raises TransitionError
    "claim": (lambda task, request: task.claim(request.user), "You claimed “{task}”."),
    "release": (lambda task, request: task.release(request.user), "You released “{task}”."),
    "complete": (lambda task, request: task.complete(request.user, task.clean_result(request.POST)), "“{task}” completed."),
    "suspend": (lambda task, request: task.suspend(request.user),
                "“{task}” is suspended: the process still waits, and nobody can claim it until it is resumed."),
    "resume": (lambda task, request: task.resume(request.user), "“{task}” is back in its lane."),
    "reassign": (lambda task, request: task.reassign(request.user, person(request)), "“{task}” is now {task.assignee}’s."),
    "delegate": (lambda task, request: task.delegate(request.user, person(request)),
                 "You handed “{task}” to {task.assignee}."),
}
FILTERS = {  # the inbox's tabs (?show=): which waiting tasks it lists
    "": ("All", lambda tasks, user: tasks.workable()),
    "mine": ("Mine", lambda tasks, user: tasks.workable().filter(assignee=user)),
    "lanes": ("My lanes", lambda tasks, user: tasks.workable().filter(lane__in=user.groups.values("name"))),
    "overdue": ("Overdue", lambda tasks, user: tasks.workable().overdue()),
    "suspended": ("Suspended", lambda tasks, user: tasks.filter(status=UserTask.SUSPENDED)),
}
INPUTS = {"number": "number", "date": "date", "email": "email"}  # form field type -> <input type>
TASKS_PAGE = 50  # the inbox's rows per page
ACTIVITIES_PAGE = 25  # activities per page: each opens on its code


@dataclass(frozen=True)
class Nav:
    """A sidebar entry: a page by URL name, or a setting that holds an address outside this server."""
    target: str
    label: str
    icon: str  # templates/bpmn8n/icons/<icon>.svg
    staff: bool = False
    group: str = ""  # the sidebar's heading above it


# the sidebar, in groups: the work people do, the workflows and their cases (each workflow's own pages are tabs under
# /workflows/<id>/), what the log says across workflows, and what builds them. An entry shows once its URL name
# resolves, so a page added later appears without editing base.html
NAV = (Nav("inbox", "Inbox", "inbox", group="Work"), Nav("start_case", "Start a case", "start", group="Work"),
       Nav("workload", "Workload", "workload", group="Work"),
       Nav("workflows", "Workflows", "workflows", group="Workflows"), Nav("cases", "Cases", "cases", staff=True, group="Workflows"),
       Nav("insights", "Insights", "insights", group="Analyse"),
       Nav("simulation", "Simulation", "simulation", staff=True, group="Analyse"),
       Nav("studio", "Studio", "studio", staff=True, group="Build"), Nav("activities", "Activities", "activities", group="Build"),
       Nav("landscape", "Landscape", "landscape", group="Build"),
       Nav("variables", "Variables", "variables", staff=True, group="Build"))
NAV_BOTTOM = (Nav("docs", "Docs", "docs"), Nav("api-1:openapi-view", "API", "api"),
              Nav("N8N_PUBLIC_URL", "Editor", "editor", staff=True), Nav("admin:index", "Admin", "admin", staff=True))
THEMES, THEME_COOKIE = (("system", "System"), ("light", "Light"), ("dark", "Dark")), "b8-theme"


def nav_entries(items, request, counts=None):
    """The entries this person may see and this server has, with their address and whether they are open."""
    shown = []
    for item in items:
        if item.staff and not request.user.is_staff:
            continue
        if external := item.target.isupper():
            href = getattr(conf.settings, item.target, "")
        else:
            try:
                href = reverse(item.target)
            except NoReverseMatch:  # a page this server does not have (yet)
                continue
        if href:
            shown.append({"href": href, "label": item.label, "icon": f"bpmn8n/icons/{item.icon}.svg", "group": item.group,
                          "external": external, "count": (counts or {}).get(item.target),
                          "current": not external and request.path.startswith(href)})
    return shown


def theme_of(request):
    """The theme the person chose (the b8-theme cookie): system, light or dark."""
    chosen = request.COOKIES.get(THEME_COOKIE)
    return chosen if chosen in dict(THEMES) else "system"


def navigation(request):
    """Context processor: the theme on every page; for a person also the sidebar, with the inbox count
    (suspended tasks wait for nobody), and their stable hue."""
    context = {"theme": theme_of(request), "themes": THEMES}
    if not request.user.is_authenticated:
        return context
    waiting = UserTask.objects.workable().for_user(request.user).count()
    dots = health.dots()
    down = [d["name"] for d in dots if d["state"] == "down"]
    summary = {"text": f"{', '.join(down).capitalize()} down" if down else "All services up", "state": "down" if down else "up"}
    return {**context, "status_dots": dots, "status_summary": summary, "waiting_count": waiting,
            "user_hue": zlib.crc32(request.user.username.encode()) % 360,
            "nav": nav_entries(NAV, request, {"inbox": waiting}), "nav_bottom": nav_entries(NAV_BOTTOM, request)}


@require_POST
def theme(request):
    """The theme switch: kept a year in the b8-theme cookie (n8n's hooks may read it: not HttpOnly), then back."""
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(back, {request.get_host()}, request.is_secure()):
        back = "/"
    response = redirect(back)
    if (chosen := request.POST.get("theme")) in dict(THEMES):
        response.set_cookie(THEME_COOKIE, chosen, max_age=365 * 24 * 3600, samesite="Lax")
    return response


def person(request):
    """The person a reassign or a delegation hands the task to (the form's `username`)."""
    if user := get_user_model().objects.filter(username=request.POST.get("username", ""), is_active=True).first():
        return user
    raise TransitionError("choose the person to hand the task to")


def inbox_url(**query):
    query = {k: v for k, v in query.items() if v}
    return reverse("inbox") + (f"?{urlencode(query)}" if query else "")


@login_required
def inbox(request):
    """Waiting tasks in the person's lanes, most urgent first: overdue, then by priority, then the earliest
    due, then the oldest. Tabs narrow it (?show=mine|lanes|overdue|suspended), and so does ?priority=.
    ?workflow=&execution= (the link in n8n's node) narrows it to one case, and opens the task when it
    is the only one."""
    UserTask.objects.expire_due()
    visible = UserTask.objects.waiting().for_user(request.user).select_related("assignee")
    show = request.GET.get("show", "") if request.GET.get("show", "") in FILTERS else ""
    priority = request.GET.get("priority", "") if request.GET.get("priority") in UserTask.PRIORITIES else ""
    workflow, execution, workflow_choices = request.GET.get("workflow", ""), request.GET.get("execution", ""), []
    if workflow and "execution" in request.GET:  # the link in n8n's node: that execution's tasks (with no execution known,
        # the workflow's), the task itself when it is one
        tasks = visible.filter(case_id=f"{workflow}:{execution}") if execution.isdigit() else \
            visible.filter(case_id__startswith=f"{workflow}:")
        if len(found := list(tasks[:2])) == 1:
            return redirect("task", found[0].pk)
    else:  # one workflow at a time, or all of them: counted in the tab and priority chosen
        visible = visible.filter(priority=priority) if priority else visible
        workflow_choices = scope.choices(request, Counter(
            scope.workflow_of(c) for c in FILTERS[show][1](visible, request.user).values_list("case_id", flat=True)))
        visible = visible.filter(case_id__startswith=f"{workflow}:") if workflow else visible
        tasks = FILTERS[show][1](visible, request.user)
    tasks = list(tasks.by_urgency())
    tabs = [{"label": label, "url": inbox_url(show=key, priority=priority), "current": key == show,
             "count": pick(visible, request.user).count()} for key, (label, pick) in FILTERS.items()]
    priorities = [{"label": label, "url": inbox_url(show=show, priority=p), "current": p == priority}
                  for p, label in (("", "Any priority"), *((p, p.capitalize()) for p in reversed(UserTask.PRIORITIES)))]
    done = UserTask.objects.filter(status=UserTask.COMPLETED).for_user(request.user).select_related("assignee")
    page, pager = paginate(request, tasks, TASKS_PAGE)
    filtered = "execution" if workflow and "execution" in request.GET else ""
    return render(request, "bpmn8n/inbox.html", {
        "tasks": page, "pager": pager, "count": len(tasks), "first": tasks[0] if tasks else None,
        "workflow_choices": workflow_choices,
        "oldest": min(tasks, key=lambda t: t.created, default=None),
        "overdue": sum(t.overdue for t in tasks), "tabs": tabs, "priorities": priorities, "show": FILTERS[show][0],
        "narrowed": bool(show or priority or workflow), "filtered": filtered,
        "done": done.order_by("-completed")[:5]})


def form_fields(task, values=None, errors=None):
    """The task's form as the page draws it: defaults filled in (older forms have only name, label and
    type), with what was submitted and the error of each field."""
    values, errors, fields = values or {}, errors or {}, []
    for i, f in enumerate(task.form, 1):
        kind, error = f.get("type", "string"), " ".join(errors.get(f["name"], []))
        fields.append({**f, "id": f"field-{i}", "label": f.get("label") or f["name"], "type": kind,
                       "input": INPUTS.get(kind, "text"), "required": kind != "boolean" and f.get("required", True),
                       "options": f.get("options", []), "help": f.get("help", ""), "value": values.get(f["name"], ""),
                       "error": error, "described": " ".join(
                           id_ for id_, shown in ((f"help-{i}", f.get("help")), (f"error-{i}", error)) if shown)})
    return fields


def task_page(request, task, values=None, errors=None, status=200):
    trail = Event.objects.filter(case_id=task.case_id, started__lt=task.created).order_by("started") if task.case_id else []
    allowed = task.allowed(request.user)
    similar, similar_how = similar_cases(task, request.user)
    return render(request, "bpmn8n/task.html", {
        "similar": similar, "similar_how": similar_how,
        "task": task, "mine": task.assignee_id == request.user.pk, "trail": trail, "allowed": allowed,
        "barred": task.barred(request.user) if task.status in task.WORKABLE else "",
        "fields": form_fields(task, values, errors),
        "candidates": task.candidates().exclude(pk=task.assignee_id) if {"reassign", "delegate"} & set(allowed) else []},
        status=status)


@login_required
def task(request, pk):
    UserTask.objects.filter(pk=pk).expire_due()
    return task_page(request, get_object_or_404(UserTask.objects.for_user(request.user).select_related("assignee"), pk=pk))


@login_required
@require_POST
def act(request, pk, action):
    if action not in ACTIONS:
        raise Http404(action)
    task = get_object_or_404(UserTask.objects.for_user(request.user), pk=pk)
    do, done = ACTIONS[action]
    try:
        do(task, request)
        messages.success(request, done.format(task=task))
    except ValidationError as e:  # the form again, each error next to its field
        messages.error(request, "Some fields need another look: see below.")
        return task_page(request, task, request.POST, e.message_dict, status=422)
    except TransitionError as e:  # someone else was faster, or n8n no longer waits (Cancelled)
        messages.warning(request, str(e))
    except ServiceUnavailable:
        messages.error(request, "The workflow engine is unreachable; the task is still open, try again.")
    return redirect("task", pk)


@login_required
def activities(request):
    """Every registered @task with its decorator and code, its inputs and its result, by node kind."""
    kind = request.GET.get("kind") if request.GET.get("kind") in TASK_KINDS else None
    every = sorted(TASKS.values(), key=lambda t: (TASK_KINDS.index(t.kind), t.name))
    q, project = request.GET.get("q", "").strip().lower(), request.GET.get("project", "")
    matching = [t for t in every if kind in (None, t.kind) and (not q or q in f"{t.name} {t.description} {t.module}".lower())]
    counts = Counter(project_of(t) for t in matching)
    chosen, pager = paginate(request, [t for t in matching if project in ("", project_of(t))], ACTIVITIES_PAGE)
    rows = [{"name": t.name, "kind": t.kind, "label": label(t.kind), "description": t.description, "project": project_of(t),
             "module": t.module.removesuffix(".tasks"), "inputs": t.inputs, "output": t.output is not None, "outputs": t.outputs,
             "source": source(t.fn)} for t in chosen]
    def with_project(name):  # this page's query, on another project's activities
        query = request.GET.copy()
        query.pop("page", None)
        query.pop("project", None)
        if name:
            query["project"] = name
        return f"?{query.urlencode()}" if query else request.path

    projects = [{"id": "", "name": "All projects", "count": len(matching), "current": not project, "url": with_project("")},
                *({"id": name, "name": name, "count": n, "current": name == project, "url": with_project(name)}
                  for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))]
    return render(request, "bpmn8n/activities.html", {
        "tasks": rows, "pager": pager, "q": q, "workflow_choices": projects, "filter_label": "Project",
        "kind": kind, "total": len(every), "modules": {t.module for t in every},
        "kinds": [(k, label(k), sum(t.kind == k for t in every)) for k in TASK_KINDS]})


def project_of(task):
    """The project an activity's code lives in: "acme" for projects.acme, "framework" for bpmn8n's own."""
    parts = task.module.split(".")
    return parts[1] if parts[0] == "projects" and len(parts) > 1 else "framework"


def label(kind):
    """"businessRuleTask" -> "Business rule task" """
    return re.sub(r"([A-Z])", r" \1", kind).capitalize()


def source(fn):
    try:
        return inspect.getsource(fn).strip()
    except (OSError, TypeError):  # no source file (a compiled module)
        return f"@task\ndef {fn.__name__}(request, data): ..."


GUIDE = Path(settings.BASE_DIR) / "docs" / "GUIDE.md"


@lru_cache(maxsize=2)
def guide_sections(mtime):
    """docs/GUIDE.md as (its introduction, [{slug, title, html, children}], {anchor: section}), one section per "## "
    heading; cached until the file changes (`mtime`)."""
    text = GUIDE.read_text()
    if text.startswith("# "):  # the page has its own title
        text = text.split("\n", 1)[1]
    html, toc = to_html(text, trusted=True, toc=True)  # our own guide
    intro, *chunks = re.split(r'(?=<h2 id=")', html)
    sections = [{"slug": h["id"], "title": h["name"], "html": chunk, "children": h["children"]} for h, chunk in zip(toc, chunks)]
    anchors = {anchor: s["slug"] for s in sections for anchor in re.findall(r'id="([^"]+)"', s["html"])}
    return intro, sections, anchors


@login_required
def docs(request, section=None):
    """The guide, docs/GUIDE.md (the one source of the documentation): an index of its sections at /docs/, one section
    per page at /docs/<section>/, with the next and the previous one."""
    if not GUIDE.exists():
        return render(request, "bpmn8n/docs.html", {"intro": mark_safe("<p>docs/GUIDE.md is not deployed here.</p>"), "sections": []})
    intro, sections, anchors = guide_sections(GUIDE.stat().st_mtime)
    context = {"sections": sections, "intro": mark_safe(intro)}  # noqa: S308 (our own guide)
    if section is not None:
        at = next((i for i, s in enumerate(sections) if s["slug"] == section), None)
        if at is None:
            raise Http404(section)

        def link(match):  # an anchor of another section: that section's page
            anchor = match.group(1)
            return f'href="{reverse("docs_section", args=[anchors[anchor]])}#{anchor}"' \
                if anchors.get(anchor, section) != section else match.group(0)

        context |= {"current": sections[at], "body": mark_safe(re.sub(r'href="#([^"]+)"', link, sections[at]["html"])),  # noqa: S308
                    "previous": sections[at - 1] if at else None, "next": sections[at + 1] if at + 1 < len(sections) else None}
    return render(request, "bpmn8n/docs.html", context)
