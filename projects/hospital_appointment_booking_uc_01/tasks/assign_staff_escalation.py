"""Escalate to hospital staff: an activity of Hospital appointment booking UC-01 (bpmn8n Studio). Edit freely.

Agent 0 (coordinator): process mining on bpmn8n's own event log and open user tasks, so the hospital can
allocate its people dynamically. It sees only the patient's pseudonym and the redacted validation report."""
from datetime import timedelta
from typing import Literal

from django.db.models import Count, Min
from django.utils import timezone
from ninja import Schema
from pydantic import Field

from bpmn8n.api import task
from bpmn8n.core.observability import Trace
from bpmn8n.models import Event, UserTask

STAFF_LANE = "hospital-staff"
FOLLOW_UP = "Ask patient for missing information"  # the node where Agent A's questions reach the patient
DESKS = {"registration": "admissions-desk", "referral": "admissions-desk"}  # synthetic demo desks
BUSY_TASKS, BUSY_MINUTES = 5, 120  # a queue this long needs more people
HISTORY = timedelta(days=30)


class AssignStaffEscalationInput(Schema):
    patient_ref: str = Field(description="The patient's pseudonym")
    failed_check: Literal["none", "registration", "referral", "service", "dates"] = Field(
        "none", description="The first validation check that failed")
    validation_report: str = Field("", description="The redacted validation report")


class AssignStaffEscalationResult(Schema):
    staff_team: str = Field(description="The staff desk that takes the request")
    priority: Literal["normal", "high"] = Field(description="How urgently staff should act")
    queue_length: int = Field(description="Open tasks in the hospital-staff lane")
    oldest_wait_minutes: float = Field(description="How long the oldest open hospital-staff task has waited")
    staffing_advice: str = Field(description="Whether the hospital should move people to this queue")
    escalation_summary: str = Field(description="What staff need to know, without identifiers")


def minutes(delta):
    return round(delta.total_seconds() / 60, 1)


@task("serviceTask")
def assign_staff_escalation(request, data: AssignStaffEscalationInput) -> AssignStaffEscalationResult:
    """Desk, priority and staffing advice from the staff queues (open tasks and waits per lane, handling time
    over 30 days) and from how many follow-up questions this case needed."""
    now, case_id = timezone.now(), Trace.of(request).case_id
    follow_ups = Event.objects.filter(case_id=case_id, activity=FOLLOW_UP, status="complete").count() if case_id else 0
    queues = {row["lane"] or "(no lane)": (row["open"], minutes(now - row["oldest"]))
              for row in UserTask.objects.waiting().values("lane").annotate(open=Count("id"), oldest=Min("created"))}
    queue_length, oldest_wait = queues.get(STAFF_LANE, (0, 0.0))
    handled = UserTask.objects.filter(lane=STAFF_LANE, status=UserTask.COMPLETED, completed__gte=now - HISTORY)
    durations = [minutes(t.completed - t.created) for t in handled.only("created", "completed")[:500]]
    handling = round(sum(durations) / len(durations), 1) if durations else None
    team = DESKS.get(data.failed_check, "booking-desk")
    reasons = [f"the patient already answered {follow_ups} follow-up questions"] if follow_ups >= 3 else []
    if data.failed_check == "none":
        reasons.append("every check passed but the classifier was unsure")
    busy = queue_length > BUSY_TASKS or oldest_wait > BUSY_MINUTES
    advice = (f"Move one more person to {STAFF_LANE}: {queue_length} open tasks, the oldest waiting {oldest_wait:.0f} min."
              if busy else f"{STAFF_LANE} keeps up: {queue_length} open tasks, the oldest waiting {oldest_wait:.0f} min.")
    lanes = "; ".join(f"{lane}: {n} open, oldest {wait:.0f} min" for lane, (n, wait) in sorted(queues.items())) or "no open tasks"
    summary = (f"Patient {data.patient_ref}. Reason: {data.failed_check if data.failed_check != 'none' else 'classifier unsure'}. "
               f"{data.validation_report.strip()} Follow-up questions in this case: {follow_ups}. "
               f"Desk: {team}, priority {'high' if reasons else 'normal'}"
               f"{' (' + '; '.join(reasons) + ')' if reasons else ''}. Queues: {lanes}. "
               f"Average staff handling time (30 days): {f'{handling:.0f} min' if handling is not None else 'no data yet'}. {advice}")
    return AssignStaffEscalationResult(staff_team=team, priority="high" if reasons else "normal",
                                       queue_length=queue_length, oldest_wait_minutes=oldest_wait,
                                       staffing_advice=advice, escalation_summary=summary)
