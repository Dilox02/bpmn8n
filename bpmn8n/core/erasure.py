"""Erasure by case (GDPR art. 17): what a case knows about its data subject goes, what process mining needs stays.

Kept: the case id, process and version, every step's activity, times, status and resource (a staff username, a
system or a message name: the people who worked the case are not its data subject), the type of a step's error,
a gateway's decider and branches, the user tasks' lanes, states, assignees and times. Cleared: the case's
variables, every step's input and output, the user tasks' title (back to the activity's name), description,
context and result, the messages' payload and correlation key, the results kept for retries (TaskRun). The audit
log holds no process data by design; evaluation samples neither (synthetic, written by the Metrics node)."""
from django.db import transaction

from ..models import AuditEntry, Case, Event, MessageSubscription, TaskRun, UserTask

ERASED = "erased"


class StillWaiting(Exception):
    """The case still waits for people or messages, or a step still runs."""


class NoSuchCase(LookupError):
    pass


def kept(data):
    """What process mining reads in a step's data and is not about the person: the invocation, a gateway's decider,
    and the branches it took with their scores (they name the workflow's branches)."""
    if not isinstance(data, dict):
        return {}
    out = {k: data[k] for k in ("invocation", "decider") if k in data}
    if "decider" in data and isinstance(data.get("output"), dict):
        out["output"] = {k: data["output"][k] for k in ("branches", "scores") if k in data["output"]}
        out["input"] = {}  # the state it decided on is gone: no rules are learnt from this decision
    return out


def erase(case_id, force=False, actor=None):
    """Erases case `case_id` and returns what changed, per model. A case that still waits is refused (StillWaiting)
    unless `force`, which cancels its waits first (n8n's execution is not stopped: stop it in n8n)."""
    tasks, messages = UserTask.objects.filter(case_id=case_id), MessageSubscription.objects.filter(case_id=case_id)
    events = Event.objects.filter(case_id=case_id)
    if not (Case.objects.filter(pk=case_id).exists() or events.exists() or tasks.exists() or messages.exists()):
        raise NoSuchCase(f"no case {case_id!r}")
    with transaction.atomic():
        waiting = [*tasks.waiting(), *messages.waiting()]
        running = events.filter(status="running").count()
        if (waiting or running) and not force:
            raise StillWaiting(f"case {case_id} still waits: {len(waiting)} user tasks or messages open, {running} steps "
                               "running. Finish or cancel them, or erase with force (it cancels the waits)")
        for wait in waiting:
            wait.cancel("the case was erased", actor=actor)
        done = {"case": Case.objects.filter(pk=case_id).update(variables={}),
                "events": 0, "user_tasks": 0, "messages": 0, "task_runs": TaskRun.objects.filter(case_id=case_id).delete()[0]}
        for event in events.only("pk", "data", "error"):
            event.data, event.error = kept(event.data), event.error.partition(":")[0]
            event.save(update_fields=["data", "error"])
            done["events"] += 1
        for task in tasks.only("pk", "activity"):
            done["user_tasks"] += UserTask.objects.filter(pk=task.pk).update(
                title=task.activity[:200], description="", context={}, result=None)
        done["messages"] = messages.update(payload=None, correlation_key=ERASED)
        AuditEntry.write("case.erase", actor=actor, object_type="case", object_id=case_id, case_id=case_id,
                         forced=bool(force and (waiting or running)), **done)
    return done
