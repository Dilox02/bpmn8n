"""Check Agreement: an activity of Erasmus learning agreement (bpmn8n Studio). Edit freely."""
from ninja import Schema
from pydantic import Field

from bpmn8n.api import task

MIN_ECTS, MAX_ECTS, MAX_SUBMISSIONS = 24, 33, 3
# TODO: the department's table of pre-approved equivalences: (host course, our course), lower case
PRE_APPROVED = {("machine learning", "machine learning"), ("databases ii", "basi di dati"),
                ("spanish b1", "lingua straniera")}


class CheckLearningAgreementInput(Schema):
    courses: str = Field(description="One mapping per line: host course | ECTS | our course")
    submission: int = Field(0, ge=0, description="Submissions checked so far (0 the first time)")


class CheckLearningAgreementResult(Schema):
    total_ects: float = Field(description="ECTS of all the host courses")
    ects_ok: bool = Field(description="Is the total between 24 and 33 and every line well formed?")
    unmatched_count: int = Field(description="Mappings not in the pre-approved table")
    unmatched_mappings: str = Field(description="Those mappings, one per line")
    needs_opinions: bool = Field(description="Must professors give an opinion?")
    submission: int = Field(description="This submission's number")
    attempts_left: bool = Field(description="Can the student still resubmit?")
    check_report: str = Field(description="What was checked and what is wrong, for the student")


@task("businessRuleTask")
def check_learning_agreement(request, data: CheckLearningAgreementInput) -> CheckLearningAgreementResult:
    """Totals the ECTS (24-33 per semester), matches every mapping against the pre-approved
    equivalence table and counts the submission (at most 3)."""
    total, unmatched, problems = 0.0, [], []
    lines = [line.strip() for line in data.courses.splitlines() if line.strip()]
    for number, line in enumerate(lines, 1):
        parts = [part.strip() for part in line.split("|")]
        try:
            host, ects, ours = parts
            credits = float(ects.replace(",", "."))
            if not 0 < credits <= 30:
                raise ValueError
        except ValueError:
            problems.append(f"line {number} is not 'host course | ECTS | our course' with ECTS between 1 and 30: {line!r}")
            continue
        total += credits
        if (host.lower(), ours.lower()) not in PRE_APPROVED:
            unmatched.append(f"{host} ({credits:g} ECTS) -> {ours}")
    if not lines:
        problems.append("no course given")
    if not MIN_ECTS <= total <= MAX_ECTS:
        problems.append(f"{total:g} ECTS: a semester needs {MIN_ECTS} to {MAX_ECTS}")
    submission = data.submission + 1
    report = "; ".join(problems) if problems else f"{total:g} ECTS, {len(lines) - len(unmatched)} mappings pre-approved"
    if unmatched:
        report += f"; {len(unmatched)} need a professor's opinion"
    return CheckLearningAgreementResult(
        total_ects=total, ects_ok=not problems, unmatched_count=len(unmatched), unmatched_mappings="\n".join(unmatched),
        needs_opinions=bool(unmatched), submission=submission, attempts_left=submission < MAX_SUBMISSIONS,
        check_report=f"Submission {submission} of {MAX_SUBMISSIONS}: {report}")
