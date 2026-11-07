"""Compute Signature Deadline: an activity of Erasmus learning agreement (bpmn8n Studio). Edit freely."""
from datetime import date, datetime, time, timedelta, timezone

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task

DAYS_BEFORE = 15


class ComputeSignatureDeadlineInput(Schema):
    departure_date: str = Field(description="Departure date, YYYY-MM-DD")


class ComputeSignatureDeadlineResult(Schema):
    alert_date: str = Field(description="ISO date-time of the alert")
    deadline_valid: bool = Field(description="Was the departure date a valid date?")


@task("scriptTask")
def compute_signature_deadline(request, data: ComputeSignatureDeadlineInput) -> ComputeSignatureDeadlineResult:
    """The moment of the alert: 15 days before the departure date, at 09:00 UTC. An unreadable date
    alerts at once, so that a person looks at it."""
    try:
        departure = date.fromisoformat(data.departure_date.strip())
    except ValueError:
        return ComputeSignatureDeadlineResult(alert_date=datetime.now(timezone.utc).isoformat(), deadline_valid=False)
    alert = datetime.combine(departure - timedelta(days=DAYS_BEFORE), time(9), tzinfo=timezone.utc)
    return ComputeSignatureDeadlineResult(alert_date=alert.isoformat(), deadline_valid=True)
