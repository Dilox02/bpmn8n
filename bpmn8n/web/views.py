"""People's pages: the inbox, one page per human task (what the link in n8n's User Task node opens),
the activities this server runs, and the documentation."""
import inspect
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib import messages
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
from .text import to_html

ACTIONS = {  # what a person can do on the task page; the model allows it or raises TransitionError
    "claim": (lambda task, request: task.claim(request.user), "You claimed “{task}”."),
    "release": (lambda task, request: task.release(request.user), "You released “{task}”."),
    "complete": (lambda task, request: task.complete(request.user, task.clean_result(request.POST)), "“{task}” completed."),
}


@dataclass(frozen=True)
class Nav:
    """A sidebar entry: a page by URL name, or a setting that holds an address outside this server."""
    target: str
    label: str
    icon: str  # templates/bpmn8n/icons/<icon>.svg
    staff: bool = False


# the sidebar; an entry shows once its URL name resolves, so a page added later appears without editing base.html
NAV = (Nav("inbox", "Inbox", "inbox"), Nav("cases", "Cases", "cases"), Nav("insights", "Insights", "insights"),
       Nav("studio", "Studio", "studio", staff=True), Nav("activities", "Activities", "activities"),
       Nav("workflows", "Workflows", "workflows"), Nav("variables", "Variables", "variables", staff=True),
       Nav("docs", "Docs", "docs"))
NAV_BOTTOM = (Nav("api-1:openapi-view", "API", "api"), Nav("N8N_PUBLIC_URL", "Editor", "editor", staff=True),
              Nav("admin:index", "Admin", "admin", staff=True))
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
            shown.append({"href": href, "label": item.label, "icon": f"bpmn8n/icons/{item.icon}.svg",
                          "external": external, "count": (counts or {}).get(item.target),
                          "current": not external and request.path.startswith(href)})
    return shown


def theme_of(request):
    """The theme the person chose (the b8-theme cookie): system, light or dark."""
    chosen = request.COOKIES.get(THEME_COOKIE)
    return chosen if chosen in dict(THEMES) else "system"


def navigation(request):
    """Context processor: the theme on every page; for a person also the sidebar, with the inbox count, and
    their stable hue."""
    context = {"theme": theme_of(request), "themes": THEMES}
    if not request.user.is_authenticated:
        return context
    waiting = UserTask.objects.waiting().for_user(request.user).count()
    return {**context, "waiting_count": waiting, "user_hue": zlib.crc32(request.user.username.encode()) % 360,
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


@login_required
def inbox(request):
    """Waiting tasks in the person's lanes, oldest first. ?workflow=&execution= (the link in n8n's node)
    narrows it to one case, and opens the task when it is the only one."""
    tasks = UserTask.objects.waiting().for_user(request.user).select_related("assignee").order_by("created")
    if workflow := request.GET.get("workflow"):
        execution = request.GET.get("execution", "")
        tasks = tasks.filter(case_id=f"{workflow}:{execution}") if execution.isdigit() else \
            tasks.filter(case_id__startswith=f"{workflow}:")
        if len(found := list(tasks[:2])) == 1:
            return redirect("task", found[0].pk)
    tasks = list(tasks)
    done = UserTask.objects.filter(status=UserTask.COMPLETED).for_user(request.user).select_related("assignee")
    return render(request, "bpmn8n/inbox.html", {"tasks": tasks, "oldest": tasks[0] if tasks else None,
                                                 "filtered": bool(workflow), "done": done.order_by("-completed")[:5]})


@login_required
def task(request, pk):
    task = get_object_or_404(UserTask.objects.for_user(request.user).select_related("assignee"), pk=pk)
    trail = Event.objects.filter(case_id=task.case_id, started__lt=task.created).order_by("started") if task.case_id else []
    return render(request, "bpmn8n/task.html", {"task": task, "mine": task.assignee_id == request.user.pk,
                                                "trail": trail})


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
    except ValidationError as e:
        messages.error(request, "; ".join(f"{k}: {', '.join(v)}" for k, v in e.message_dict.items()))
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
    rows = [{"name": t.name, "kind": t.kind, "label": label(t.kind), "description": t.description,
             "module": t.module.removesuffix(".tasks"), "inputs": t.inputs, "output": t.output is not None, "outputs": t.outputs,
             "source": source(t.fn)} for t in every if kind in (None, t.kind)]
    return render(request, "bpmn8n/activities.html", {
        "tasks": rows, "kind": kind, "total": len(every), "modules": {t.module for t in every},
        "kinds": [(k, label(k), sum(t.kind == k for t in every)) for k in TASK_KINDS]})


def label(kind):
    """"businessRuleTask" -> "Business rule task" """
    return re.sub(r"([A-Z])", r" \1", kind).capitalize()


def source(fn):
    try:
        return inspect.getsource(fn).strip()
    except (OSError, TypeError):  # no source file (a compiled module)
        return f"@task\ndef {fn.__name__}(request, data): ..."


@login_required
def docs(request):
    """The guide: docs/GUIDE.md, the one source of the documentation, with an index of its sections."""
    readme = Path(settings.BASE_DIR) / "docs" / "GUIDE.md"
    if not readme.exists():
        return render(request, "bpmn8n/docs.html", {"guide": mark_safe("<p>docs/GUIDE.md is not deployed here.</p>"), "toc": []})
    text = readme.read_text()
    if text.startswith("# "):  # the page has its own title
        text = text.split("\n", 1)[1]
    guide, toc = to_html(text, trusted=True, toc=True)  # our own guide
    return render(request, "bpmn8n/docs.html", {"guide": mark_safe(guide), "toc": toc})  # noqa: S308
