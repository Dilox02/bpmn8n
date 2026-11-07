"""Search doctors and available slots: an activity of Hospital appointment booking UC-01 (bpmn8n Studio).
Edit freely.

Agent C (booker): reads the hospital scheduling database for free slots. It never receives patient
identifiers: only the service, the dates, the time of day and a preferred doctor."""
from datetime import date, timedelta, timezone as tz

from django.utils import timezone
from ninja import Schema
from pydantic import Field

from bpmn8n.api import task
# TODO: the hospital scheduling system's API; the demo reads the synthetic slots of projects/hospital
from projects.hospital.models import DemoSlot

ANY = {"", "any", "-", "none", "no", "no preference", "anyone"}
WIDER_DAYS = 14  # how far around the requested dates alternatives may be
MAX_OPTIONS, MAX_ALTERNATIVES = 10, 5
# TODO: the hospital's time zone; the demo slots are stored as clinic times in UTC (09:00Z is a 09:00 visit)
CLINIC_TZ = tz.utc


class SearchAppointmentSlotsInput(Schema):
    service: str = Field(description="Medical specialty to book")
    earliest: str = Field(description="First acceptable date, YYYY-MM-DD")
    latest: str = Field(description="Last acceptable date, YYYY-MM-DD")
    time_of_day: str = Field("any", description="morning, afternoon or any")
    doctor: str = Field("", description="Preferred doctor; empty or any for any doctor")


class SearchAppointmentSlotsResult(Schema):
    slots_available: bool = Field(description="Is there at least one matching free slot?")
    slot_ids: str = Field(description="Comma-separated ids of the matching free slots, earliest first (up to 10)")
    morning: bool = Field(description="Morning slots only (for the reservation recheck)")
    alternatives_available: bool = Field(description="Are there alternative slots to suggest?")
    alternatives_text: str = Field(description="The alternatives, one per line, with why each differs")
    search_summary: str = Field(description="One sentence on what was searched and found")


def as_date(value):
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def describe(slot):
    local = slot.starts_at.astimezone(CLINIC_TZ)
    return f"{local:%A %d %B %Y, %H:%M} - {slot.doctor}, {slot.location}"


@task("serviceTask")
def search_appointment_slots(request, data: SearchAppointmentSlotsInput) -> SearchAppointmentSlotsResult:
    """Free future slots of the service between the dates, at the time of day and with the preferred doctor;
    when none matches, up to 5 alternatives up to 14 days around the dates, any time, any doctor."""
    with timezone.override(CLINIC_TZ):  # dates and hours as the clinic counts them
        return search(data)


def search(data):
    time_of_day = data.time_of_day.strip().lower()
    time_of_day = time_of_day if time_of_day in ("morning", "afternoon") else "any"
    doctor = "" if data.doctor.strip().lower() in ANY else data.doctor.strip()
    first, last = as_date(data.earliest), as_date(data.latest)
    free = DemoSlot.objects.filter(service__iexact=data.service.strip(), demobooking__isnull=True,
                                   starts_at__gte=timezone.now()).order_by("starts_at", "pk")
    matching = DemoSlot.objects.none()
    if first and last and first <= last:
        matching = free.filter(starts_at__date__range=(first, last))
        if time_of_day == "morning":
            matching = matching.filter(starts_at__hour__lt=12)
        elif time_of_day == "afternoon":
            matching = matching.filter(starts_at__hour__gte=12)
        if doctor:
            matching = matching.filter(doctor__icontains=doctor)
    slots = list(matching[:MAX_OPTIONS])
    service = slots[0].service if slots else data.service.strip()
    wanted = (f"{service} between {data.earliest.strip() or '?'} and {data.latest.strip() or '?'}"
              f"{'' if time_of_day == 'any' else f' in the {time_of_day}'}{f' with {doctor}' if doctor else ''}")
    if slots:
        return SearchAppointmentSlotsResult(
            slots_available=True, slot_ids=",".join(str(s.pk) for s in slots), morning=time_of_day == "morning",
            alternatives_available=False, alternatives_text="",
            search_summary=f"{len(slots)} free slot{'s' if len(slots) > 1 else ''} for {wanted}.")
    wider = free
    if first and last and first <= last:
        wider = free.filter(starts_at__date__range=(first - timedelta(days=WIDER_DAYS), last + timedelta(days=WIDER_DAYS)))
    alternatives = []
    for slot in list(wider[:MAX_ALTERNATIVES]):
        local = slot.starts_at.astimezone(CLINIC_TZ)
        why = []
        if not (first and last and first <= local.date() <= last):
            why.append("other date")
        if time_of_day == "morning" and local.hour >= 12 or time_of_day == "afternoon" and local.hour < 12:
            why.append("other time of day")
        if doctor and doctor.lower() not in slot.doctor.lower():
            why.append("other doctor")
        alternatives.append(f"{describe(slot)} ({', '.join(why) or 'alternative'})")
    return SearchAppointmentSlotsResult(
        slots_available=False, slot_ids="", morning=time_of_day == "morning",
        alternatives_available=bool(alternatives),
        alternatives_text="\n".join(alternatives) or "No alternative slot within two weeks of these dates.",
        search_summary=f"No free slot for {wanted}.")
