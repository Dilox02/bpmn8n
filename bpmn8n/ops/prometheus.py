"""Prometheus metrics (GET /api/metrics/prometheus, text exposition format 0.0.4), read from the database
so they cover every worker process: the endpoints' calls, errors and time (the event log's aggregates, as
/api/metrics), the cases, the work waiting for people and messages, and Studio's projects."""
from django.db.models import Count, Q, Sum

from ..core import goals
from ..models import Case, Event, MessageSubscription, Project, UserTask

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def number(value):
    value = float(value or 0)
    return str(int(value)) if value.is_integer() else repr(value)


def family(name, kind, help_text, samples):
    """# HELP and # TYPE, then one line per (suffix, labels, value)."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for suffix, labels, value in samples:
        text = ",".join(f'{key}="{escape(v)}"' for key, v in labels.items())
        lines.append(f"{name}{suffix}{{{text}}} {number(value)}" if text else f"{name}{suffix} {number(value)}")
    return lines


def counts(queryset, *fields):
    return [({f: row[f] for f in fields}, row["n"])
            for row in queryset.values(*fields).annotate(n=Count("pk")).order_by(*fields)]


def goal_samples():
    """J9: 1 when a goal of a project's evaluations.json is met, 0 when missed (none while nothing is measured)."""
    try:
        return [("", {"process": g["process"], "goal": g["name"]}, g["ok"]) for g in goals.evaluate() if g["ok"] is not None]
    except goals.GoalError:  # an invalid goal is check_goals' to report, not the scrape's
        return []


def exposition():
    # ponytail: aggregates the whole Event table per scrape, as /api/metrics does; past a few million
    # events keep running counters instead (G10)
    endpoints = list(Event.objects.exclude(status="running").values("endpoint").annotate(
        calls=Count("pk"), errors=Count("pk", filter=Q(status="error")), ms=Sum("duration_ms")).order_by("endpoint"))
    lines = [
        *family("bpmn8n_endpoint_calls_total", "counter", "Measured calls per endpoint (tasks, gateways, waits).",
                [("", {"endpoint": e["endpoint"]}, e["calls"]) for e in endpoints]),
        *family("bpmn8n_endpoint_errors_total", "counter", "Measured calls per endpoint that failed.",
                [("", {"endpoint": e["endpoint"]}, e["errors"]) for e in endpoints]),
        *family("bpmn8n_endpoint_duration_seconds", "summary", "Time spent in each endpoint.",
                [sample for e in endpoints for sample in (("_sum", {"endpoint": e["endpoint"]}, (e["ms"] or 0) / 1000),
                                                          ("_count", {"endpoint": e["endpoint"]}, e["calls"]))]),
        *family("bpmn8n_cases_started_total", "counter", "Cases (process instances) per process.",
                [("", labels, n) for labels, n in counts(Case.objects, "process")]),
        *family("bpmn8n_user_tasks_waiting", "gauge", "Open and claimed user and manual tasks per lane.",
                [("", labels, n) for labels, n in counts(UserTask.objects.waiting(), "lane", "status")]),
        *family("bpmn8n_message_subscriptions_open", "gauge", "Receive tasks and event gateways waiting, per message.",
                [("", {"message": labels["name"]}, n) for labels, n in counts(MessageSubscription.objects.waiting(), "name")]),
        *family("bpmn8n_goal_ok", "gauge", "1 when a process goal (evaluations.json) is met, 0 when missed.", goal_samples()),
        *family("bpmn8n_studio_projects", "gauge", "Studio projects per status.",
                [("", labels, n) for labels, n in counts(Project.objects, "status")]),
    ]
    return "\n".join(lines) + "\n"
