"""N4 people's pages: the workload of the lanes (H7), what a person is told (H8), starting a case (H9)."""
import json
from collections import Counter, defaultdict
from datetime import timedelta
from statistics import mean

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..clients.http import ServiceUnavailable, post_json
from ..conf import settings
from ..core import directory
from ..models import Preference, UserTask
from ..models.safety import AuditEntry
from . import scope
from .landscape import known_workflows

WINDOWS = {7: "7 days", 30: "30 days", 90: "90 days"}  # the window of the means and of the completions


def duration(seconds):
    """"12 min", "3.5 h", "2.0 d"; "—" for no data."""
    if seconds is None:
        return "—"
    return f"{seconds / 60:.0f} min" if seconds < 3600 else f"{seconds / 3600:.1f} h" if seconds < 172800 else f"{seconds / 86400:.1f} d"


def average(values):
    return mean(values) if values else None


def lane_stats(tasks, since, now):
    """One row per lane of `tasks`: what waits now (open, claimed, suspended; overdue and due within a day) and, over
    the window, the mean wait before a claim and the mean work after it."""
    rows = defaultdict(lambda: {"open": 0, "claimed": 0, "suspended": 0, "overdue": 0, "soon": 0, "waits": [], "works": []})
    for t in tasks:
        row = rows[t["lane"]]
        if t["status"] in UserTask.WAITING:
            row[t["status"]] += 1
            if t["due"] and t["due"] < now:
                row["overdue"] += 1
            elif t["due"] and t["due"] < now + UserTask.DUE_SOON:
                row["soon"] += 1
        if t["claimed"] and t["created"] >= since:
            row["waits"].append((t["claimed"] - t["created"]).total_seconds())
        if t["status"] == UserTask.COMPLETED and t["claimed"] and t["completed"] and t["completed"] >= since:
            row["works"].append((t["completed"] - t["claimed"]).total_seconds())
    return [{"lane": lane or "(any lane)", **{k: v for k, v in row.items() if k not in ("waits", "works")},
             "waiting": row["open"] + row["claimed"] + row["suspended"],
             "wait": duration(average(row["waits"])), "work": duration(average(row["works"]))}
            for lane, row in sorted(rows.items())]


@login_required
def workload(request):
    """The work waiting on people, one workflow at a time: for each workflow n8n has, its lanes (the roles its User
    and Manual tasks wait on, each served by the Django group of its name), what waits there and how fast it is taken
    and done over the window, who did it, and what is overdue. Staff see every lane, others the lanes they are in."""
    now = timezone.now()
    days = int(request.GET["days"]) if request.GET.get("days", "").isdigit() and int(request.GET["days"]) in WINDOWS else 30
    since = now - timedelta(days=days)
    visible = UserTask.objects.all() if UserTask.manages(request.user) or request.user.is_superuser \
        else UserTask.objects.for_user(request.user)
    tasks = visible.filter(Q(status__in=UserTask.WAITING) | Q(status=UserTask.COMPLETED, completed__gte=since))
    tasks = tasks.filter(directory.in_n8n())  # as everywhere: n8n's workflows (a deleted one's tasks can never be resumed)
    workflow_choices = scope.choices(request, Counter(scope.workflow_of(c) for c in tasks.values_list("case_id", flat=True)))
    if workflow := request.GET.get("workflow"):
        tasks = tasks.filter(case_id__startswith=f"{workflow}:")
    rows = list(tasks.values("pk", "case_id", "title", "lane", "status", "due", "created", "claimed", "completed",
                             "assignee__username"))
    refs, groups = {r.id: r for r in directory.workflows()}, dict(
        Group.objects.annotate(n=Count("user")).values_list("name", "n"))
    by_workflow = defaultdict(list)
    for row in rows:
        by_workflow[scope.workflow_of(row["case_id"])].append(row)
    sections = []
    for wid, mine in by_workflow.items():
        lanes = lane_stats(mine, since, now)
        for lane in lanes:
            lane["members"] = groups.get(lane["lane"])  # None: no group of that name, only staff see its tasks
        people = defaultdict(lambda: {"claimed": 0, "completed": 0})
        for r in mine:
            if who := r["assignee__username"]:
                if r["status"] == UserTask.CLAIMED:
                    people[who]["claimed"] += 1
                elif r["status"] == UserTask.COMPLETED:
                    people[who]["completed"] += 1
        late = sorted((r for r in mine if r["status"] in UserTask.WAITING and r["due"] and r["due"] < now), key=lambda r: r["due"])
        sections.append({"ref": refs.get(wid) or directory.WorkflowRef(wid, wid, "unknown"), "lanes": lanes,
                         "people": sorted(people.items()), "late": late[:10],
                         "waiting": sum(lane["waiting"] for lane in lanes),
                         "done": sum(r["status"] == UserTask.COMPLETED for r in mine)})
    sections.sort(key=lambda s: (-s["waiting"], s["ref"].name.lower()))
    return render(request, "bpmn8n/workload.html", {
        "sections": sections, "days": days, "windows": WINDOWS.items(), "workflow_choices": workflow_choices,
        "groups_url": reverse("admin:auth_group_changelist") if request.user.is_staff else ""})


@login_required
def notifications(request):
    """What the person is told: an e-mail on each event, and the daily digest."""
    pref = Preference.of(request.user)
    if request.method == "POST":
        pref.email, pref.digest = "email" in request.POST, "digest" in request.POST
        pref.save()
        AuditEntry.write("user.preferences", object_type="user", object_id=request.user.pk, email=pref.email, digest=pref.digest)
        messages.success(request, "Your notification settings are saved.")
        return redirect("notifications")
    return render(request, "bpmn8n/notifications.html", {"pref": pref})


# --- H9 start a case -----------------------------------------------------------------------------------

KINDS = {"string": "string", "number": "number", "integer": "number", "boolean": "boolean", "array": "json", "object": "json"}


def start_fields(contract):
    """The start form of a contract: [{name, kind (string | number | boolean | choice | date | json), required, help,
    options}] from its typed start data; a field no task types is a string."""
    fields = []
    for name, field in contract.fields.items():
        schema = field["schema"] or {}
        kind = "choice" if schema.get("enum") else "date" if schema.get("format") == "date" else \
            KINDS.get(schema.get("type") if isinstance(schema.get("type"), str) else "", "string")
        fields.append({"name": name, "kind": kind, "required": field["required"] and kind != "boolean",
                       "help": schema.get("description", ""), "options": [str(o) for o in schema.get("enum", [])],
                       "integer": schema.get("type") == "integer"})
    return fields


def starts():
    """{webhook path: process} of every process a webhook starts (a POST one: the form sends JSON), from the projects
    and from n8n's workflows when its API answers; cached as long as the directory of workflows (every tab of a
    workflow asks whether it has a Start tab)."""
    key = f"{directory.cache_key()}:starts"
    if (found := cache.get(key)) is None:
        cache.set(key, found := read_starts(), directory.TTL)
    return found


def read_starts():
    """Only what n8n would start: a production webhook answers for a published workflow, not for a draft or a
    project that was never imported (with n8n unreadable, every one: nothing says otherwise)."""
    live, found = directory.from_n8n(), {}
    for node in known_workflows():
        for start in node.contract.starts if node.contract else ():
            if start["kind"] == "webhook" and start["path"] and start["method"] == "POST":
                found[start["path"]] = {"path": start["path"], "name": node.name, "workflow_id": node.workflow_id,
                                        "fields": start_fields(node.contract)}
    if live is not None:
        found = {path: p for path, p in found.items() if live.get(p["workflow_id"], ("", ""))[1] == "published"}
    return found


def clean_start(fields, data):
    """(the JSON body of the fields the contract declares, typed; {name: error}). Anything else in `data` is not forwarded."""
    body, errors = {}, {}
    for f in fields:
        name = f["name"]
        if f["kind"] == "boolean":
            body[name] = data.get(name) in ("on", "true", "1")
        elif not (raw := (data.get(name) or "").strip()):
            if f["required"]:
                errors[name] = "required"
        else:
            try:
                if f["kind"] == "json":
                    body[name] = json.loads(raw)
                else:
                    body[name] = UserTask.coerce(f["kind"], raw, f["options"])
                    if f["integer"]:
                        if body[name] != int(body[name]):
                            raise ValueError("must be a whole number")
                        body[name] = int(body[name])
            except json.JSONDecodeError:
                errors[name] = "must be JSON"
            except ValueError as e:
                errors[name] = str(e)
    return body, errors


def execution_of(answer):
    """The execution id n8n's answer names (executionId, execution_id), else None."""
    if isinstance(answer, dict):
        for key in ("executionId", "execution_id"):
            if str(answer.get(key, "")).isdigit():
                return str(answer[key])
    return None


def forms(processes, chosen="", values=None, errors=None):
    """The processes with what the person submitted and each field's error, for the page (only for the `chosen` one)."""
    values, errors = values or {}, errors or {}
    return [{**p, "fields": [{**f, "value": values.get(f["name"], "") if p["path"] == chosen else "",
                              "checked": p["path"] == chosen and values.get(f["name"]) == "on",
                              "error": errors.get(f["name"], "") if p["path"] == chosen else ""} for f in p["fields"]]}
            for p in processes.values()]


@login_required
def start_case(request, workflow_id=None):
    """The processes a webhook starts, one form open at a time, generated from the contract's start data; one
    workflow's only on its Start tab."""
    processes = {k: p for k, p in starts().items() if workflow_id in (None, p["workflow_id"])}
    return render(request, "bpmn8n/start.html", {"processes": forms(processes), "single": workflow_id is not None})


@login_required
@require_POST
def start_run(request, hook):
    """Posts the form's JSON to n8n's production webhook, then shows n8n's answer and the case when it names one."""
    processes = starts()
    if (process := processes.get(hook)) is None:
        raise Http404(hook)
    body, errors = clean_start(process["fields"], request.POST)
    context = {"processes": forms(processes, hook, request.POST, errors), "errors": errors}
    if errors:
        return render(request, "bpmn8n/start.html", context, status=422)
    try:
        answer = post_json(f"{settings.N8N_URL}/webhook/{hook}", body, timeout=30)
    except ServiceUnavailable as e:
        context["failure"] = f"n8n did not take it: {e}"
        return render(request, "bpmn8n/start.html", context, status=502)
    execution = execution_of(answer)
    case_id = f"{process['workflow_id']}:{execution}" if execution and process["workflow_id"] else ""
    AuditEntry.write("case.start", object_type="process", object_id=process["name"], case_id=case_id, path=hook,
                     fields=sorted(body), execution=execution or "")
    context |= {"answer": json.dumps(answer, indent=2, ensure_ascii=False)[:4000], "case_id": case_id, "chosen": hook,
                "processes": forms(processes)}
    return render(request, "bpmn8n/start.html", context)

