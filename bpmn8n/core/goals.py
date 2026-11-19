"""J8/J9, goals and alerts: a project's `evaluations.json` may declare process goals, KPIs of the event log
over a window of days with a target:

    {"goals": [{"name": "Quick approvals", "kpi": "p90_cycle_time", "target": 7200, "window_days": 30}]}

`process` (the n8n workflow's name) defaults to the project's workflow.json name; `direction` ("lower" or
"higher") to "lower": the value has to stay at or under the target. KPIs: mean_cycle_time and p90_cycle_time
(seconds, closed cases), error_rate (cases with a failed step), override_rate (reviewed decisions a person
changed: core/overrides.py), sla_breaches (user tasks past their due time, finished late or still open).
`manage.py check_goals`, GET /api/goals, `bpmn8n_goal_ok{process,goal}` in Prometheus and
`manage.py eval_gate --goals` evaluate them."""
import json
from datetime import timedelta

import numpy as np
from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from ..analytics import Log, Scope, stats
from ..models import UserTask

KPIS = ("mean_cycle_time", "p90_cycle_time", "error_rate", "override_rate", "sla_breaches")


class GoalError(ValueError):
    pass


def declared(root=None, project=None):
    """[{project, name, process, kpi, target, direction, window_days}] from every project's evaluations.json
    (only `project`'s when given); raises GoalError for a goal that is not valid."""
    from pathlib import Path
    root, goals = Path(root or settings.BASE_DIR / "projects"), []
    for path in sorted(root.glob("*/evaluations.json")):
        if project and path.parent.name != project:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8")).get("goals") or []
            process = json.loads((path.parent / "workflow.json").read_text(encoding="utf-8")).get("name", "")
        except (ValueError, OSError, AttributeError):
            continue
        for i, goal in enumerate(raw):
            where = f"{path.parent.name} goal {i + 1}"
            if not isinstance(goal, dict) or goal.get("kpi") not in KPIS:
                raise GoalError(f"{where}: kpi is one of {', '.join(KPIS)}")
            if not isinstance(goal.get("target"), int | float) or isinstance(goal.get("target"), bool):
                raise GoalError(f"{where}: target is a number")
            days, direction = goal.get("window_days", 30), goal.get("direction", "lower")
            if not isinstance(days, int | float) or days <= 0 or direction not in ("lower", "higher"):
                raise GoalError(f"{where}: window_days is a positive number, direction 'lower' or 'higher'")
            goals.append({"project": path.parent.name, "name": str(goal.get("name") or goal["kpi"]),
                          "process": goal.get("process") or process, "kpi": goal["kpi"], "target": goal["target"],
                          "direction": direction, "window_days": days})
    return goals


def value(goal, now=None):
    """(the KPI over the goal's window, the cases it covers); None for a KPI with nothing to measure yet."""
    now = now or timezone.now()
    since = now - timedelta(days=goal["window_days"])
    if goal["kpi"] == "sla_breaches":
        tasks = UserTask.objects.filter(process=goal["process"], created__gte=since, due__isnull=False)
        return tasks.filter(Q(completed__gt=F("due")) | Q(completed__isnull=True, due__lt=now)).count(), tasks.count()
    log = Log.load(Scope(process=goal["process"], since=since), now)
    if not log.traces:
        return None, 0
    kpi, closed = goal["kpi"], [t.duration for t in log.traces if not t.open]
    if kpi == "error_rate":
        return sum(any(s.status == "error" for s in t.steps) for t in log.traces) / len(log.traces), len(log.traces)
    if kpi == "override_rate":
        reviewed = [bool(d["overridden"]) for _, d in stats.decision_rows(log) if "overridden" in d]
        return (sum(reviewed) / len(reviewed) if reviewed else None), len(reviewed)
    if not closed:
        return None, 0
    return float(np.mean(closed) if kpi == "mean_cycle_time" else np.percentile(closed, 90)), len(closed)


def evaluate(root=None, project=None, process=None, now=None):
    """Every declared goal with its `value`, the `cases` behind it and `ok` (None: nothing to measure yet)."""
    out = []
    for goal in declared(root, project):
        if process and goal["process"] != process:
            continue
        got, cases = value(goal, now)
        ok = None if got is None else got <= goal["target"] if goal["direction"] == "lower" else got >= goal["target"]
        out.append({**goal, "value": None if got is None else round(got, 4), "cases": cases, "ok": ok})
    return out
