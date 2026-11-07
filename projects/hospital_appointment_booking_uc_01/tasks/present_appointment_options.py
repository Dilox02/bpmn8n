"""Present appointment options: an activity of Hospital appointment booking UC-01 (bpmn8n Studio). Edit freely.

Agent C (booker): the matching slots as numbered options for the patient. No patient identifiers."""
from datetime import timezone as tz

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task
# TODO: the hospital scheduling system's API; the demo reads the synthetic slots of projects/hospital
from projects.hospital.models import DemoSlot

# TODO: the hospital's time zone; the demo slots are stored as clinic times in UTC (09:00Z is a 09:00 visit)
CLINIC_TZ = tz.utc


class PresentAppointmentOptionsInput(Schema):
    slot_ids: str = Field(description="Comma-separated ids of the slots to present")


class PresentAppointmentOptionsResult(Schema):
    options_text: str = Field(description="The options, one per line, for the patient")
    option_count: int = Field(description="How many options were presented")


@task("sendTask")
def present_appointment_options(request, data: PresentAppointmentOptionsInput) -> PresentAppointmentOptionsResult:
    """Formats the slots as numbered options (slot number, date, time, doctor, location) for the patient's app."""
    ids = [int(part) for part in data.slot_ids.replace(" ", "").split(",") if part.isdigit()]
    slots = DemoSlot.objects.filter(pk__in=ids).order_by("starts_at", "pk")
    lines = [f"Slot {slot.pk}: {slot.starts_at.astimezone(CLINIC_TZ):%A %d %B %Y, %H:%M} - {slot.doctor}, {slot.location}"
             for slot in slots]
    # TODO: push the options to the patient's session in the hospital app; the patient sees them in the next task too
    return PresentAppointmentOptionsResult(options_text="\n".join(lines) or "No appointment options.",
                                           option_count=len(lines))
