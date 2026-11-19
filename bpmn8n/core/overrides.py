"""J6, the learning loop: when a person's form changes what a Laya gateway or an LLM agent produced, the event
log keeps it (Event.data `overridden`, `final`), so that the decisions dataset knows which automatic decisions
a person did not accept, and `manage.py suggest_thresholds` can propose a confidence from it.

The convention that links a form to what it reviews (no configuration):
- a gateway: a form field named like the gateway in snake case ("Route Ticket" -> `route_ticket`) whose value
  is one of the gateway's branches;
- an LLM agent: a form field named like the agent's output variable (the form shows the draft, the person
  edits it)."""
import re

from ..models.cases import Event


def slug(name):
    return re.sub(r"\W+", "_", name.lower()).strip("_")


def capture(case_id, answers):
    """Marks the case's automatic decisions and drafts that `answers` (what a person submitted) reviews:
    `final` is what the person decided, `overridden` whether it differs from the automatic outcome. Each
    decision is reviewed once (the first form that speaks to it)."""
    if not case_id or not isinstance(answers, dict):
        return
    for event in Event.objects.filter(case_id=case_id).order_by("-started"):
        data = event.data or {}
        if "overridden" in data:
            continue
        if data.get("decider") not in (None, "conditions"):  # Laya: a model decided
            taken = (data.get("output") or {}).get("branches") or []
            final = answers.get(slug(event.activity))
            if taken and final in ((data.get("input") or {}).get("options") or {}):
                data |= {"final": final, "overridden": sorted(taken) != [final]}
        elif isinstance(llm := data.get("llm"), dict) and isinstance(final := answers.get(llm.get("variable")), str):
            data |= {"final": final, "overridden": final.strip() != str(llm.get("draft", "")).strip()}
        else:
            continue
        if "overridden" in data:
            Event.objects.filter(pk=event.pk).update(data=data)


def confidence(scores):
    """How sure Laya was: the chosen branch's confidence (exclusive), the least sure yes/no (inclusive)."""
    values = [v for v in (scores or {}).values() if isinstance(v, int | float)]
    if not values:
        return None
    return values[0] if len(values) == 1 else min(max(p, 1 - p) for p in values)


def override_rate(rows):
    """{"reviewed", "overridden", "rate"} of (confidence, overridden) pairs, or None without any."""
    return {"reviewed": len(rows), "overridden": (n := sum(o for _, o in rows)), "rate": round(n / len(rows), 3)} \
        if rows else None


def reviewed_by_gateway(events):
    """{(process, gateway): [(confidence, overridden)]} of Events (any iterable of rows with .process,
    .activity, .data), for the decisions a person reviewed."""
    out = {}
    for e in events:
        d = e.data or {}
        if "decider" in d and "overridden" in d and (c := confidence((d.get("output") or {}).get("scores"))) is not None:
            out.setdefault((e.process, e.activity), []).append((c, bool(d["overridden"])))
    return out


def suggest(pairs, target=0.05, min_decisions=10):
    """The lowest min_confidence whose decisions at or above it keep the override rate at `target` or less, with
    that rate and how many decisions it keeps (so the most automation a person still accepts); None if no
    threshold does, or too few decisions (`min_decisions`) are left to tell."""
    for t in sorted({c for c, _ in pairs}):
        kept = [(c, o) for c, o in pairs if c >= t]
        if len(kept) < min_decisions:
            return None
        if (rate := override_rate(kept)["rate"]) <= target:
            return {"min_confidence": round(t, 3), "override_rate": rate, "decisions": len(kept)}
    return None
