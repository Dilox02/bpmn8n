"""H9: GLiNER2.5 small fills task inputs from free text (values: names, amounts, counts).
H10: judged fields (yes/no, pick-one) are right more often with Laya than with GLiNER.
H11: Laya verifying GLiNER's values cuts wrong values accepted, at a small cost in missed ones.
     Result: falsified on this set (no fewer wrong values, some right ones lost): verify is opt-in.

Runs the labelled texts through POST /api/tasks/<task>/run (the n8n node's path) and,
for comparison, asks GLiNER alone the judged fields. Results: reports/extraction_eval.{json,md}."""
import json
import os
import statistics
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from conftest import WEB

DATA = json.loads((Path(__file__).parent / "datasets" / "extraction.json").read_text())
REPORTS = Path(__file__).resolve().parent.parent / "reports"
MODELS = os.environ.get("MODELS_URL", "http://127.0.0.1:8001")
JUDGED = {"duplicate_charge": ("Was the customer charged twice for the same thing?", ["yes", "no"]),
          "category": ("Kind of expense", ["meal", "travel", "hotel", "other"])}
# an alternative wording of the yes/no question, to see how much the phrasing matters
REPHRASED = {"duplicate_charge": "Was the same payment taken more than once?"}
ITALIAN = ("Mi ", "Il ", "Vorrei", "Cena", "Biglietto", "Notte")
DEFAULTS = {"attendees": 1, "duplicate_charge": False}  # the Schema defaults: what "not found" means


def post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def same(got, want):
    if isinstance(want, bool) or isinstance(got, bool):
        return got is want
    if isinstance(want, (int, float)):
        return isinstance(got, (int, float)) and abs(got - want) < 0.01
    return str(got).strip().lower() == str(want).strip().lower()


def outcome(name, got, want):
    """correct | wrong (a wrong value accepted: the costly case) | missing (default or a person decides)."""
    effective = DEFAULTS.get(name) if got is None else got
    if want is None:
        return "correct" if got is None else "wrong"
    if same(effective, want):
        return "correct"
    return "missing" if got is None else "wrong"


class Score:
    """Right / total per field and per group (language, variant)."""

    def __init__(self):
        self.right, self.total = {}, {}

    def add(self, key, ok):
        self.right[key] = self.right.get(key, 0) + ok
        self.total[key] = self.total.get(key, 0) + 1

    def rate(self, keys=None):
        keys = [k for k in self.total if keys is None or k in keys]
        total = sum(self.total[k] for k in keys)
        return sum(self.right[k] for k in keys) / total if total else None


@pytest.fixture(scope="module")
def results(laya_ready):
    fields, judged, lang, latencies, errors = Score(), Score(), Score(), [], []
    outcomes = {"verified": {}, "unverified": {}}
    for task, cases in DATA.items():
        for case in cases:
            language = "it" if case["text"].startswith(ITALIAN) else "en"
            for variant, verify in (("verified", True), ("unverified", False)):
                t0 = time.perf_counter()
                _, reply = post(f"{WEB}/api/tasks/{task}/run", {"text": case["text"], "verify": verify})
                if verify:
                    latencies.append((time.perf_counter() - t0) * 1000)
                extracted = reply.get("extracted", {})
                for name, want in case["expected"].items():
                    got = extracted.get(name, {}).get("value")
                    result = outcome(name, got, want)
                    outcomes[variant][result] = outcomes[variant].get(result, 0) + 1
                    if not verify:
                        continue
                    ok = result == "correct"
                    fields.add(name, ok)
                    lang.add(language, ok)
                    if name in JUDGED:
                        judged.add("hybrid (Laya)", ok)
                        judged.add("GLiNER only", outcome(name, gliner_judgment(case["text"], name), want) == "correct")
                        if name in REPHRASED:
                            judged.add("Laya, rephrased", same(laya_bool(case["text"], REPHRASED[name]), want))
                    if not ok:
                        errors.append({"text": case["text"], "field": name, "expected": want, "got": got,
                                       "outcome": result})
    total = sum(outcomes["verified"].values())
    out = {
        "field_accuracy": {k: fields.rate([k]) for k in fields.total},
        "value_fields_accuracy": fields.rate([k for k in fields.total if k not in JUDGED]),
        "judged_accuracy": {k: judged.rate([k]) for k in judged.total},
        "verification": {v: {k: n / total for k, n in o.items()} for v, o in outcomes.items()},
        "by_language": {k: lang.rate([k]) for k in lang.total},
        "latency_ms": {"p50": statistics.median(latencies), "p95": sorted(latencies)[int(len(latencies) * 0.95)]},
        "errors": errors,
    }
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "extraction_eval.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
    (REPORTS / "extraction_eval.md").write_text(markdown(out))
    return out


def gliner_judgment(text, name):
    question, choices = JUDGED[name]
    _, reply = post(f"{MODELS}/v1/extract", {"threshold": 0, "items": [
        {"text": text, "fields": [{"name": name, "description": question, "choices": choices}]}]})
    value = reply["results"][0].get(name, {}).get("value")
    return {"yes": True, "no": False}.get(value, value)


def laya_bool(text, question):
    _, reply = post(f"{MODELS}/v1/systemone", {"state": text, "questions": {"q": {"type": "noul", "instructions": question}}})
    return reply["answers"]["q"]["noul"] >= 0.5


def markdown(out):
    rows = "\n".join(f"| {k} | {v:.2f} |" for k, v in out["field_accuracy"].items())
    judged = "\n".join(f"| {k} | {v:.2f} |" for k, v in out["judged_accuracy"].items())
    verification = "\n".join(f"| {v} | {o.get('correct', 0):.2f} | {o.get('wrong', 0):.2f} | {o.get('missing', 0):.2f} |"
                             for v, o in out["verification"].items())
    return (f"# Extraction evaluation (GLiNER2.5 small + Laya)\n\n| field | accuracy |\n|---|---|\n{rows}\n\n"
            f"| judged fields | accuracy |\n|---|---|\n{judged}\n\n"
            f"| Laya verification | correct | wrong (accepted) | missing |\n|---|---|---|---|\n{verification}\n\n"
            f"by language: {', '.join(f'{k} {v:.2f}' for k, v in out['by_language'].items())} · "
            f"latency p50 {out['latency_ms']['p50']:.0f} ms, p95 {out['latency_ms']['p95']:.0f} ms\n")


def test_h9_gliner_reads_the_values(results):
    assert results["value_fields_accuracy"] >= 0.85, results["errors"]


def test_h10_judgments_are_better_with_laya(results):
    acc = results["judged_accuracy"]
    assert acc["hybrid (Laya)"] > acc["GLiNER only"], acc


def decision_device():
    """Where the models server runs its decisions ("cpu" for the pinned Bekko adapter, else a GPU)."""
    try:
        with urllib.request.urlopen(f"{MODELS}/health", timeout=5) as reply:
            return json.load(reply).get("decision_model", {}).get("device", "")
    except (OSError, ValueError):
        return ""


def test_extraction_is_fast(results):
    """A task's whole run (GLiNER for the values, the decision model for yes/no and choices, Django): about
    20 ms per decision on a GPU, about 100 ms on the CPU decision backend, so the budget follows the device."""
    budget = 400 if decision_device() == "cpu" else 100
    assert results["latency_ms"]["p50"] < budget, (decision_device(), results["latency_ms"])


def test_h11_laya_verification_never_adds_wrong_values(results):
    """Measured: the veto never lets more wrong values through, but did not stop the ones GLiNER
    got wrong here and cost a few right ones, so it is off by default (README, H11)."""
    verified, unverified = results["verification"]["verified"], results["verification"]["unverified"]
    assert verified.get("wrong", 0) <= unverified.get("wrong", 0), results["verification"]
