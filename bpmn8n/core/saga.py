"""C9, compensation (a saga): `@task(compensate="refund_payment")` declares the undo of a task. The event log is the
ledger: the completed runs of such tasks in a case, with the input and output they logged. compensate() runs the
undo of each, newest first, with that input and output, and records each run in the event log (its Event says which
run it undoes) and in the audit log. Once per original run: a run already compensated is skipped, so a retry or a
second call undoes only what is left."""
from django.db import transaction
from django.http import HttpRequest

from ..api.tasks import TASKS, as_data
from ..models import AuditEntry, Case, Event
from .observability import Trace, measure, record


def undo_payload(comp, data):
    """What the undo task is given: the fields of its input Schema found in the original input and output (the
    output wins), and `input` / `output` themselves when it declares them."""
    values = {**(data.get("input") or {}), **(data.get("output") if isinstance(data.get("output"), dict) else {})}
    fields = comp.schema.model_fields
    return {**{k: v for k, v in values.items() if k in fields},
            **{k: data.get(k) for k in ("input", "output") if k in fields}}


def compensable(case_id):
    """The completed runs of the case that declared an undo and were not undone yet, newest first, and how many
    were undone already."""
    undoable = {name for name, task in TASKS.items() if task.compensate}
    undone = {e.data.get("compensates") for e in Event.objects.filter(case_id=case_id, status="complete")
              if isinstance(e.data, dict) and "compensates" in e.data}
    runs = [e for e in Event.objects.filter(case_id=case_id, status="complete", endpoint__in=undoable)
            .order_by("-ended", "-pk") if "compensates" not in (e.data or {})]
    return [e for e in runs if e.pk not in undone], len(undone)


def compensate(case_id, request=None):
    """Undoes the case's completed tasks, newest first. Returns {"compensated": [{task, undo, activity, event}],
    "failed": [{task, activity, event, error}], "skipped": how many were undone before}. A failing undo does not
    stop the others (a saga compensates as much as it can); it is not recorded as done, so a retry runs it again.
    ponytail: one case at a time (its row is locked); the undo tasks run inside that transaction."""
    request = request or HttpRequest()
    done, failed = [], []
    with transaction.atomic():
        Case.objects.select_for_update().get_or_create(case_id=case_id)
        runs, skipped = compensable(case_id)
        for run in runs:
            comp = TASKS.get(TASKS[run.endpoint].compensate)
            row = {"task": run.endpoint, "activity": run.activity, "event": run.pk}
            try:
                if comp is None:
                    raise LookupError(f"no @task {TASKS[run.endpoint].compensate!r}")
                payload = comp.schema(**undo_payload(comp, run.data or {}))
                with measure(comp.name, Trace(case_id=case_id, activity=f"Compensate {run.activity}", process=run.process)):
                    record(input=as_data(payload), compensates=run.pk, compensated=run.endpoint)
                    with transaction.atomic():
                        result = comp.validated(comp.fn(request, payload))
                    record(output=as_data(result))
            except Exception as e:  # whatever the undo does wrong is the finding
                failed.append({**row, "error": f"{type(e).__name__}: {e}"[:500]})
                AuditEntry.write("case.compensate", object_type="case", object_id=case_id, case_id=case_id, status="failed",
                                 **row)
                continue
            done.append({**row, "undo": comp.name})
            AuditEntry.write("case.compensate", object_type="case", object_id=case_id, case_id=case_id, status="done",
                             undo=comp.name, **row)
    return {"compensated": done, "failed": failed, "skipped": skipped}
