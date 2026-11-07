"""Register Incident: an activity of Personal data breach response (bpmn8n Studio). Edit freely."""
from datetime import datetime, timedelta, timezone

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task

AUTHORITY_HOURS = 72  # GDPR art. 33(1)


class RegisterIncidentInput(Schema):
    detected_at: str = Field(description="When we became aware of it, ISO date-time")
    description: str = Field(description="What happened, as reported")


class RegisterIncidentResult(Schema):
    incident_id: str = Field(description="INC- followed by the detection time")
    aware_at: str = Field(description="Awareness time, ISO, UTC")
    authority_deadline: str = Field(description="Awareness + 72 hours, ISO, UTC")
    detected_at_valid: bool = Field(description="Was the detection time readable (else: now)?")


@task("scriptTask")
def register_incident(request, data: RegisterIncidentInput) -> RegisterIncidentResult:
    """Gives the incident an id and computes the 72-hour deadline for the supervisory authority from the
    detection time. An unreadable or future time counts from now: the deadline is never later than it should be."""
    now = datetime.now(timezone.utc)
    try:
        aware = datetime.fromisoformat(data.detected_at.strip())
        aware = (aware if aware.tzinfo else aware.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        valid = True
    except ValueError:
        aware, valid = now, False
    if aware > now + timedelta(minutes=5):  # a time in the future cannot be when we became aware
        aware, valid = now, False
    # ponytail: the id is the minute of awareness; two incidents in the same minute need a sequence
    return RegisterIncidentResult(incident_id=f"INC-{aware:%Y%m%dT%H%M}", aware_at=aware.isoformat(),
                                  authority_deadline=(aware + timedelta(hours=AUTHORITY_HOURS)).isoformat(),
                                  detected_at_valid=valid)
