"""bpmn8n's own activities: the LLM agent's prompt (the workflow builder's llm_agent step), the agentic
patterns' guardrail, audit sample and critique, and OCR."""
import hashlib
import json
import re

from ninja import Schema
from pydantic import Field, field_validator

from .api import task
from .api.errors import upstream
from .clients.http import ModelClient
from .conf import settings
from .core.observability import Trace
from .workflows.builder import GUARDRAIL_CHECKS


class PromptInput(Schema):
    prompt: str = Field(description="What the LLM agent is asked, with the {{variables}} it needs filled in")


class PromptResult(Schema):
    prompt: str = Field(description="The prompt as the LLM agent receives it")


@task("scriptTask")
def compose_prompt(request, data: PromptInput) -> PromptResult:
    """Fills an LLM agent's prompt with the case's variables (the llm_agent step's first node): only the
    ones it names reach the LLM, and the event log keeps what it was sent."""
    return PromptResult(prompt=data.prompt)


# --- guardrails (J3): personal data by its shape, no model needed ---------------------------------------

def iban_ok(text):
    """ISO 13616's mod-97 check: an IBAN, not any code of that shape."""
    code = text.replace(" ", "")
    return int("".join(str(int(c, 36)) for c in code[4:] + code[:4])) % 97 == 1


def luhn_ok(text):
    """The Luhn check of card numbers: a card, not any 13-19 digit number (an order, an invoice)."""
    digits = [int(c) for c in reversed(re.sub(r"\D", "", text))]
    return sum(d if i % 2 == 0 else d * 2 - 9 * (d > 4) for i, d in enumerate(digits)) % 10 == 0


# kind, pattern, check: earlier kinds win where matches overlap (an IBAN holds digit runs that look like phones).
# ponytail: IBANs in capitals only (a lower-case word after one would join it); a fiscal code by its shape only,
# without its check letter, because one with a typo still names a person; any run of 9-13 digits counts as a
# phone number: recall before precision, as the hospital project's pseudonymisation does.
PERSONAL_DATA = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), None),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b"), iban_ok),
    ("fiscal_code", re.compile(r"\b[A-Z]{6}[\dLMNP-V]{2}[A-EHLMPR-T][\dLMNP-V]{2}[A-Z][\dLMNP-V]{3}[A-Z]\b", re.I), None),
    ("card", re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)"), luhn_ok),
    ("phone", re.compile(r"(?<![\w+-])(?:\+\d{1,3}[ .-]?)?\(?\d{2,4}\)?(?:[ .-]?\d{2,4}){1,4}(?![\w-])"),
     lambda s: 9 <= sum(c.isdigit() for c in s) <= 13),
)
FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)  # LLMs wrap JSON in a code fence


def personal_data(text):
    """[(kind, start, end)] of the personal data in text, in order, without overlaps."""
    spans = []
    for kind, pattern, valid in PERSONAL_DATA:
        for m in pattern.finditer(text):
            if (valid is None or valid(m.group())) and not any(s < m.end() and m.start() < e for _, s, e in spans):
                spans.append((kind, m.start(), m.end()))
    return sorted(spans, key=lambda span: span[1])


def schema_problems(text, keys):
    try:
        data = json.loads(FENCE.sub(r"\1", text))
    except ValueError:
        return ["not JSON"]
    if not isinstance(data, dict):
        return ["not a JSON object"]
    return [f"no key {key!r}" for key in keys if key not in data]


def names(value):
    return [v.strip() for v in value.split(",") if v.strip()]


class GuardrailInput(Schema):
    text: str = Field("", description="The text to check, with the {{variables}} it holds filled in")
    checks: str = Field("pii", description=f"The checks, comma-separated: {', '.join(GUARDRAIL_CHECKS)}")
    max_length: int = Field(4000, description="length: the most characters the text may have")
    blocklist: str = Field("", description="blocklist: the words that block the text, comma-separated")
    keys: str = Field("", description="schema: the keys the text, a JSON object, must have, comma-separated")

    @field_validator("text", mode="before")
    @classmethod
    def as_text(cls, value):  # a {{variable}} holding data (parallel_llm's answers): checked as its JSON
        return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)

    @field_validator("checks")
    @classmethod
    def known(cls, value):
        if unknown := set(names(value)) - set(GUARDRAIL_CHECKS):
            raise ValueError(f"unknown checks {sorted(unknown)}: use {', '.join(GUARDRAIL_CHECKS)}")
        return value


class GuardrailFinding(Schema):
    check: str = Field(description="The check that found it: pii, length, blocklist or schema")
    what: str = Field(description="What it found: the kind of personal data, the word, the missing key")
    where: str = Field("", description="Its characters in the text, start-end; empty for the whole text")


class GuardrailResult(Schema):
    passed: bool = Field(description="Whether no check found anything: the text may go on")
    findings: list[GuardrailFinding] = Field(description="What the checks found")
    redacted: str = Field(description="The text with its personal data replaced by placeholders such as [EMAIL]")


@task("scriptTask")
def check_guardrails(request, data: GuardrailInput) -> GuardrailResult:
    """Checks a text before it goes on (the guardrail step): personal data (emails, phone numbers, IBANs,
    Italian fiscal codes, card numbers), its length, blocked words, or that it is a JSON object with given
    keys. Whatever the checks, it also returns the text with the personal data redacted."""
    checks, text, findings = names(data.checks), data.text, []
    spans = personal_data(text)
    if "pii" in checks:
        findings += [GuardrailFinding(check="pii", what=kind, where=f"{s}-{e}") for kind, s, e in spans]
    if "length" in checks and len(text) > data.max_length:
        findings.append(GuardrailFinding(check="length", what=f"{len(text)} characters, more than {data.max_length}"))
    if "blocklist" in checks:
        findings += [GuardrailFinding(check="blocklist", what=word, where=f"{m.start()}-{m.end()}")
                     for word in names(data.blocklist) for m in re.finditer(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.I)]
    if "schema" in checks:
        findings += [GuardrailFinding(check="schema", what=problem) for problem in schema_problems(text, names(data.keys))]
    for kind, start, end in reversed(spans):
        text = f"{text[:start]}[{kind.upper()}]{text[end:]}"
    return GuardrailResult(passed=not findings, findings=findings, redacted=text)


# --- oversight (H10) and reflection (J1, J4) ------------------------------------------------------------

def audit_share(case_id):
    """Where a case falls in [0, 1): from its id alone, so the same case always gets the same answer."""
    return int(hashlib.sha256(case_id.encode()).hexdigest()[:8], 16) / 16 ** 8


class AuditInput(Schema):
    rate: float = Field(gt=0, le=1, description="The share of the cases a person audits, e.g. 0.1")
    risk: str = Field("not declared", description="The step's declared risk: only 'low' allows auditing a sample")


class AuditResult(Schema):
    audited: bool = Field(description="Whether a person audits this case")


@task("scriptTask")
def draw_audit_sample(request, data: AuditInput) -> AuditResult:
    """Human-on-the-loop (an llm_agent with oversight="audit"): whether a person audits this case's answer,
    drawn from the case id, so about `rate` of the cases are audited and the event log keeps which. Outside
    a case (a direct call) everything is audited."""
    case = Trace.of(request).case_id
    return AuditResult(audited=not case or audit_share(case) < data.rate)


class CritiqueInput(Schema):
    critique: str = Field("", description="The critic's answer: PASS or REVISE first, then why")
    reflection_round: int = Field(0, description="The drafts made before the one judged")
    max_rounds: int = Field(2, ge=1, description="The most drafts to make")


class CritiqueResult(Schema):
    passed: bool = Field(description="Whether the critic passed the draft")
    again: bool = Field(description="Whether to draft again: not passed, and rounds left")
    reflection_round: int = Field(description="The drafts made so far")


@task("scriptTask")
def read_critique(request, data: CritiqueInput) -> CritiqueResult:
    """Reads a reflection's critic (PASS or REVISE) and counts the round: the draft is redone only while it
    does not pass and fewer than max_rounds drafts were made, so the loop always ends."""
    passed, done = bool(re.match(r"\W*PASS\b", data.critique, re.I)), data.reflection_round + 1
    return CritiqueResult(passed=passed, again=not passed and done < data.max_rounds, reflection_round=done)


class DocumentImage(Schema):
    # exclude: the event log does not keep the image (a referral, an ID card: personal, often health data)
    image: str = Field(description="Photo or scan of the document (JPEG, PNG or HEIC), base64 or a data URL",
                       exclude=True)


class DocumentText(Schema):
    document_text: str = Field(description="The text read from the document, line by line, top to bottom")
    ocr_confidence: float = Field(description="How sure the reading is, 0 to 1: low for a blurred or dark photo")


@task("serviceTask")
def read_document_text(request, data: DocumentImage) -> DocumentText:
    """Reads the text of a photographed or scanned document (OCR, with macOS's Vision on this server: the
    image never leaves it). A following task extracts its fields with Input From: Text, "{{document_text}}"."""
    with upstream():
        reading = ModelClient(settings.OCR_URL, settings.MODELS_API_KEY, timeout=30).call({"image": data.image})
    return DocumentText(document_text=reading["text"], ocr_confidence=reading["confidence"])
