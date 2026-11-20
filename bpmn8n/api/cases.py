"""What ends or undoes a case: a Terminate End Event (C5) and a compensation (C9).

Terminate: n8n's public API stops an execution (POST /api/v1/executions/<id>/stop), so the terminate node
makes bpmn8n record it, cancel the case's waiting tasks and messages, and ask n8n to stop the execution.
Whatever n8n does with that request, bpmn8n answers 409 "terminated" to every later call of the case (middleware)."""
import logging

from django.http import JsonResponse
from django.utils import timezone
from ninja import Router, Schema

from ..clients import n8n
from ..core import saga
from ..core.observability import CASE_HEADER, Trace, measure, record
from ..models import AuditEntry, Case, MessageSubscription, TransitionError, UserTask

log = logging.getLogger("bpmn8n")
router = Router(tags=["cases"])


class TerminateIn(Schema):
    reason: str = ""


def stop_execution(case_id):
    """Asks n8n to stop the case's execution: (whether it did, why not). The case is "<workflow id>:<execution id>"."""
    execution = case_id.rpartition(":")[2]
    if not execution.isdigit():
        return False, "no n8n execution in the case id"
    try:
        n8n.request("POST", f"/executions/{execution}/stop")
    except n8n.N8nError as e:
        log.warning("terminate %s: n8n did not stop it: %s", case_id, e)
        return False, str(e)
    return True, ""


@router.post("/cases/{case_id}/terminate", response=dict)
def terminate_case(request, case_id: str, data: TerminateIn):
    """Ends the case: records it as terminated (every later call of the case answers 409), cancels what it
    still waits on (tasks, messages, signals) and asks n8n to stop the execution. Idempotent."""
    trace = Trace.of(request)
    with measure("terminateEndEvent", trace if trace.case_id == case_id else Trace(case_id=case_id)):
        record(reason=data.reason)
        case, _ = Case.objects.get_or_create(case_id=case_id)
        first = Case.objects.filter(pk=case_id, terminated__isnull=True).update(terminated=timezone.now())
        cancelled = 0
        for model in (UserTask, MessageSubscription):
            for waiting in model.objects.filter(case_id=case_id).waiting():
                try:
                    waiting.cancel(f"the case was terminated{': ' + data.reason if data.reason else ''}")
                    cancelled += 1
                except TransitionError:
                    pass  # it completed meanwhile
        stopped, why = stop_execution(case_id)
        if first:
            AuditEntry.write("case.terminate", case, case_id=case_id, reason=data.reason, cancelled=cancelled,
                             stopped=stopped)
    return {"case_id": case_id, "terminated": True, "cancelled": cancelled, "stopped": stopped, "detail": why}


@router.post("/cases/{case_id}/compensate", response=dict)
def compensate_case(request, case_id: str):
    """Runs the undo of the case's completed tasks that declared one (`@task(compensate=...)`), newest first,
    each with its original input and output; once per task run (see core/saga.py)."""
    return saga.compensate(case_id, request)


def middleware(get_response):
    """A call that belongs to a terminated case (the X-Bpmn8n-Case header) answers 409: the case is over."""
    def middleware(request):
        case_id = request.headers.get(CASE_HEADER, "")
        if case_id and request.path.startswith("/api/") and not request.path.endswith("/terminate"):
            case_id = Trace.of(request).case_id  # the node sends it URI-encoded
            if Case.objects.filter(pk=case_id, terminated__isnull=False).exists():
                return JsonResponse({"detail": f"case {case_id} was terminated by a Terminate End Event"}, status=409)
        return get_response(request)
    return middleware
