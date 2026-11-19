"""N4 people's pages: the workload of the lanes (H7), what a person is told (H8), starting a case (H9)."""
import json
from collections import defaultdict
from datetime import timedelta
from statistics import mean

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from ..clients.http import ServiceUnavailable, post_json
from ..conf import settings
from ..models import Preference, UserTask
from ..models.safety import AuditEntry
from . import charts
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
    """The lanes' workload: staff see every lane, others the lanes they are in (and tasks of no lane)."""
    now = timezone.now()
    days = int(request.GET["days"]) if request.GET.get("days", "").isdigit() and int(request.GET["days"]) in WINDOWS else 30
    since = now - timedelta(days=days)
    visible = UserTask.objects.all() if UserTask.manages(request.user) or request.user.is_superuser \
        else UserTask.objects.for_user(request.user)
    tasks = list(visible.waiting().values("lane", "status", "due", "created", "claimed", "completed")) + list(
        visible.filter(status=UserTask.COMPLETED, completed__gte=since).values("lane", "status", "due", "created", "claimed", "completed"))
    lanes = lane_stats(tasks, since, now)
    people = defaultdict(lambda: {"claimed": 0, "completed": 0})
    for who in visible.filter(status=UserTask.CLAIMED, assignee__isnull=False).values_list("assignee__username", flat=True):
        people[who]["claimed"] += 1
    for who in visible.filter(status=UserTask.COMPLETED, completed__gte=since, assignee__isnull=False).values_list(
            "assignee__username", flat=True):
        people[who]["completed"] += 1
    late = visible.waiting().filter(due__lt=now).select_related("assignee").order_by("due")[:10]
    return render(request, "bpmn8n/workload.html", {
        "lanes": lanes, "people": sorted(people.items()), "late": late, "days": days, "windows": WINDOWS.items(),
        "open_chart": charts.bars(lanes, label="Waiting tasks by lane", value=lambda r: r["waiting"],
                                  name=lambda r: r["lane"], edges=[r["lane"] for r in lanes]),
        "late_chart": charts.bars(lanes, label="Overdue tasks by lane", value=lambda r: r["overdue"],
                                  name=lambda r: r["lane"], edges=[r["lane"] for r in lanes])})


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
    and from n8n's workflows when its API answers."""
    found = {}
    for node in known_workflows():
        for start in node.contract.starts if node.contract else ():
            if start["kind"] == "webhook" and start["path"] and start["method"] == "POST":
                found[start["path"]] = {"path": start["path"], "name": node.name, "workflow_id": node.workflow_id,
                                        "fields": start_fields(node.contract)}
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


@login_required
def start_case(request):
    """The processes a webhook starts, each with a form generated from its contract's start data."""
    return render(request, "bpmn8n/start.html", {"processes": list(starts().values())})


@login_required
@require_POST
def start_run(request, hook):
    """Posts the form's JSON to n8n's production webhook, then shows n8n's answer and the case when it names one."""
    process = starts().get(hook)
    if process is None:
        raise Http404(hook)
    body, errors = clean_start(process["fields"], request.POST)
    context = {"processes": list(starts().values()), "chosen": hook, "values": request.POST, "errors": errors}
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
    context |= {"answer": json.dumps(answer, indent=2, ensure_ascii=False)[:4000], "case_id": case_id, "values": {}}
    return render(request, "bpmn8n/start.html", context)

