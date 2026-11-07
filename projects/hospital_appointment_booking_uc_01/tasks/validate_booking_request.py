"""Validate request and hospital requirements: an activity of Hospital appointment booking UC-01 (bpmn8n
Studio). Edit freely.

An internal bpmn8n step: the hospital's rules on a complete request. Its report holds no identifiers: it is
all Agent B (the classifier) reads."""
from datetime import date
from typing import Literal

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task
# the hospital's service directory and scheduling data (synthetic demo slots, as the brief asks)
from projects.hospital.models import DemoSlot


class ValidateBookingRequestInput(Schema):
    service: str = Field(description="Medical specialty to book")
    earliest: str = Field(description="First acceptable date, YYYY-MM-DD")
    latest: str = Field(description="Last acceptable date, YYYY-MM-DD")
    registered: bool = Field(False, description="Is the patient registered at the hospital (Patient Database)?")
    referral_required: bool = Field(False, description="Does the service need a referral for this patient?")
    referral_present: bool = Field(False, description="Is a valid referral on file?")


class ValidateBookingRequestResult(Schema):
    validation_report: str = Field(description="Concise report of every check, without identifiers")
    failed_check: Literal["none", "registration", "referral", "service", "dates"] = Field(
        description="The first check that failed")
    service: str = Field(description="The service as the directory names it")


def as_date(value):
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


@task("businessRuleTask")
def validate_booking_request(request, data: ValidateBookingRequestInput) -> ValidateBookingRequestResult:
    """Checks registration, referral, the service directory and the dates (valid, first <= last, not past)."""
    # TODO: read registration and referral from the Patient Database instead of the trusted start data
    known = DemoSlot.objects.filter(service__iexact=data.service.strip()).values_list("service", flat=True).first()
    service = known or data.service.strip()
    first, last = as_date(data.earliest), as_date(data.latest)
    dates_ok = bool(first and last and first <= last and last >= date.today())
    referral_ok = data.referral_present or not data.referral_required
    checks = {"registration": data.registered, "referral": referral_ok, "service": bool(known), "dates": dates_ok}
    failed = [name for name, ok in checks.items() if not ok]
    referral = "not required" if not data.referral_required else "required, " + ("on file" if data.referral_present else "missing")
    report = (f"Registered patient: {'yes' if data.registered else 'no'}. Referral: {referral}. "
              f"Service {service}: {'bookable' if known else 'not in the service directory'}. "
              f"Dates {data.earliest.strip() or '?'} to {data.latest.strip() or '?'}: {'valid' if dates_ok else 'invalid'}. "
              + ("Result: all checks passed." if not failed else
                 f"Result: {len(failed)} check{'s' if len(failed) > 1 else ''} failed ({', '.join(failed)})."))
    return ValidateBookingRequestResult(validation_report=report, failed_check=failed[0] if failed else "none",
                                        service=service)
