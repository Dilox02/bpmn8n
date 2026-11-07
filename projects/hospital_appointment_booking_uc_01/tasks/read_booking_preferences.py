"""Read booking preferences: an activity of Hospital appointment booking UC-01 (bpmn8n Studio). Edit freely.

Helper of Agent A (chat agent): reads the AI agent's fixed-format answer into fields with plain rules, and
decides whether the request is complete. No model runs here."""
import re
from datetime import date

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task

LINE = re.compile(r"^[^\w\n]*(SERVICE|EARLIEST|LATEST|TIME|DOCTOR|QUESTION)[^\w\n:]*:[ \t]*(.*?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
EMPTY = {"", "none", "any", "-", "n/a", "na", "unknown", "not specified", "null", "yyyy-mm-dd"}
TIMES = {"morning": "morning", "mornings": "morning", "am": "morning", "afternoon": "afternoon",
         "afternoons": "afternoon", "pm": "afternoon", "any": "any", "anytime": "any", "any time": "any"}
QUESTIONS = {"medical specialty": "Which medical specialty or service do you need to book?",
             "preferred dates": "Between which dates would you like the appointment (first and last day)?",
             "time of day": "Do you prefer the morning, the afternoon, or is any time fine?"}


class ReadBookingPreferencesInput(Schema):
    agent_reply: str = Field(description="Agent A's answer, written by an AI model")


class ReadBookingPreferencesResult(Schema):
    service: str = Field(description="Medical specialty to book, e.g. Cardiology")
    earliest: str = Field(description="First acceptable date, YYYY-MM-DD")
    latest: str = Field(description="Last acceptable date, YYYY-MM-DD")
    time_of_day: str = Field(description="morning, afternoon or any (empty: not said)")
    doctor: str = Field(description="Preferred doctor, empty for any")
    information_complete: bool = Field(description="Are specialty, dates and time of day all known?")
    missing_information: str = Field(description="What is still missing, for the patient")
    agent_question: str = Field(description="The follow-up question for the patient")
    request_summary: str = Field(description="The request as Agent A understood it, for the patient")


def iso(value):
    try:
        return date.fromisoformat(value.strip()[:10]).isoformat()
    except ValueError:
        return ""


@task("scriptTask")
def read_booking_preferences(request, data: ReadBookingPreferencesInput) -> ReadBookingPreferencesResult:
    """Reads Agent A's SERVICE, EARLIEST, LATEST, TIME, DOCTOR and QUESTION lines into fields; the request is
    complete when service, valid dates (first <= last) and time of day are all present."""
    found = {}
    for key, value in LINE.findall(data.agent_reply):
        found.setdefault(key.upper(), value.strip().strip("*").strip())  # the first of each: the reasons come after
    value = {k: ("" if v.lower() in EMPTY else v) for k, v in found.items()}
    service = value.get("SERVICE", "")
    service = service[:1].upper() + service[1:].lower() if service else ""
    earliest, latest = iso(value.get("EARLIEST", "")), iso(value.get("LATEST", ""))
    time_of_day = TIMES.get(found.get("TIME", "").strip().lower(), "")  # "any" is an answer here
    doctor = value.get("DOCTOR", "")
    missing = [what for what, ok in (("medical specialty", service),
                                     ("preferred dates", earliest and latest and earliest <= latest),
                                     ("time of day", time_of_day)) if not ok]
    question = value.get("QUESTION", "") or " ".join(QUESTIONS[m] for m in missing)
    summary = (f"{service or 'specialty not given'}, "
               f"{f'from {earliest} to {latest}' if earliest and latest else 'dates not given'}, "
               f"{ {'morning': 'mornings', 'afternoon': 'afternoons', 'any': 'any time'}.get(time_of_day, 'time not given')}, "
               f"{doctor or 'any doctor'}")
    return ReadBookingPreferencesResult(
        service=service, earliest=earliest, latest=latest, time_of_day=time_of_day, doctor=doctor,
        information_complete=not missing, missing_information=", ".join(missing),
        agent_question=question if missing else "", request_summary=summary)
