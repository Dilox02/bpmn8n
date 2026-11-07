"""Pseudonymise patient data: an activity of Hospital appointment booking UC-01 (bpmn8n Studio). Edit freely.

The privacy gate of the process: every patient text passes here before Agent A (an external LLM) reads it.
The agents only ever see the pseudonym patient_ref and the redacted conversation."""
import hashlib
import hmac
import re
from datetime import date

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task
from bpmn8n.conf import settings

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
FISCAL_CODE = re.compile(r"\b[A-Z]{6}\d{2}[A-EHLMPR-T]\d{2}[A-Z]\d{3}[A-Z]\b", re.IGNORECASE)
PHONE = re.compile(r"(?<![\w+-])(?:\+\d{1,3}[ .-]?)?\(?\d{2,4}\)?(?:[ .-]?\d{2,4}){1,4}(?![\w-])")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}"
BIRTH = re.compile(rf"(\b(?:born(?: on)?|date of birth|birth ?date|d\.?o\.?b\.?|nat[oa](?: il)?|data di nascita)\s*:?\s*)({DATE})",
                   re.IGNORECASE)


class PseudonymisePatientDataInput(Schema):
    patient_id: str = Field(description="The patient's id in the hospital app (direct identifier)")
    message: str = Field(description="The patient's first request, in natural language")
    patient_name: str = Field("", description="The patient's full name")
    fiscal_code: str = Field("", description="The patient's Italian fiscal code (codice fiscale)")
    email: str = Field("", description="The patient's email address")
    phone: str = Field("", description="The patient's phone number")
    date_of_birth: str = Field("", description="The patient's date of birth, YYYY-MM-DD")
    redacted_conversation: str = Field("", description="The redacted conversation so far (empty on the first round)")
    agent_question: str = Field("", description="The follow-up question Agent A asked last")
    patient_reply: str = Field("", description="The patient's answer to that question")


class PseudonymisePatientDataResult(Schema):
    patient_ref: str = Field(description="Stable pseudonym of the patient, the only patient reference agents see")
    redacted_conversation: str = Field(description="The conversation with direct identifiers replaced by placeholders")
    redactions: int = Field(description="How many identifiers were redacted in this round")
    request_date: str = Field(description="Today's date (YYYY-MM-DD), so Agent A can read 'next week'")


def pseudonym(patient_id):
    """Stable and not reversible without the server's SECRET_KEY."""
    digest = hmac.new(settings.SECRET_KEY.encode(), patient_id.strip().encode(), hashlib.sha256).hexdigest()
    return f"PT-{digest[:12]}"


def birth_variants(value):
    """The date of birth as people write it: 1980-01-01, 01/01/1980, 1/1/1980, 01-01-1980, 01.01.1980."""
    try:
        d = date.fromisoformat(value.strip())
    except ValueError:
        return [value.strip()] if value.strip() else []
    return [d.isoformat(), *(f"{day}{sep}{month}{sep}{d.year}" for sep in "/-." for day, month in
                             ((f"{d.day:02d}", f"{d.month:02d}"), (str(d.day), str(d.month))))]


def redact(text, data, patient_ref):
    """(text without direct identifiers, how many were replaced)."""
    count = 0

    def sub(pattern, placeholder, text, flags=0, check=None):
        nonlocal count

        def replace(m):
            nonlocal count
            if check and not check(m.group(0)):
                return m.group(0)
            count += 1
            return placeholder
        return re.sub(pattern, replace, text, flags=flags)

    # the patient's own identifiers, as the hospital app knows them
    if data.patient_id.strip():
        text = sub(rf"(?<!\w){re.escape(data.patient_id.strip())}(?!\w)", patient_ref, text)
    if data.email.strip():
        text = sub(re.escape(data.email.strip()), "[EMAIL]", text, re.IGNORECASE)
    if data.fiscal_code.strip():
        text = sub(re.escape(data.fiscal_code.strip()), "[FISCAL_CODE]", text, re.IGNORECASE)
    if data.phone.strip():
        text = sub(re.escape(data.phone.strip()), "[PHONE]", text)
    for variant in birth_variants(data.date_of_birth):
        text = sub(rf"(?<![\d./-]){re.escape(variant)}(?![\d./-])", "[DATE_OF_BIRTH]", text)
    text = sub(EMAIL, "[EMAIL]", text)
    text = sub(FISCAL_CODE, "[FISCAL_CODE]", text)
    name = data.patient_name.strip()
    if name:
        text = sub(rf"\b{re.escape(name)}\b", "[NAME]", text, re.IGNORECASE)
        for part in name.split():
            if len(part) >= 3:
                text = sub(rf"\b{re.escape(part)}\b", "[NAME]", text, re.IGNORECASE)
    # anyone's identifiers, by their shape (emails and fiscal codes went before the name: a name is in many emails)
    text, births = BIRTH.subn(lambda m: f"{m.group(1)}[DATE_OF_BIRTH]", text)
    count += births
    text = sub(PHONE, "[PHONE]", text,
               check=lambda s: 9 <= sum(c.isdigit() for c in s) <= 13 and not ISO_DATE.fullmatch(s.strip()))
    return text, count


@task("scriptTask")
def pseudonymise_patient_data(request, data: PseudonymisePatientDataInput) -> PseudonymisePatientDataResult:
    """Replaces the patient id with a stable pseudonym (HMAC-SHA256 with the server's SECRET_KEY) and redacts
    direct identifiers from the patient's text; keeps the redacted conversation, appending the agent's last
    question and the patient's last answer on every follow-up round."""
    patient_ref = pseudonym(data.patient_id)
    if not data.redacted_conversation.strip():  # the first round: the request itself
        text, count = redact(data.message.strip(), data, patient_ref)
        conversation = f"Patient: {text}"
    else:
        conversation, count = data.redacted_conversation.rstrip(), 0
        if data.patient_reply.strip():
            question, asked = redact(data.agent_question.strip(), data, patient_ref)
            reply, answered = redact(data.patient_reply.strip(), data, patient_ref)
            count = asked + answered
            conversation += (f"\nAgent: {question}" if question else "") + f"\nPatient: {reply}"
    return PseudonymisePatientDataResult(patient_ref=patient_ref, redacted_conversation=conversation,
                                         redactions=count, request_date=date.today().isoformat())
