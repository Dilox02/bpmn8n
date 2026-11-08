"""BPMN gateways: decided by Laya (reads text) or by conditions on the process variables.
Parallel gateways need no decision: n8n fans out natively."""
from typing import Any, Literal

from ninja import Router, Schema

from ..core.cases import CaseData
from .errors import upstream
from ..clients.laya import LayaClient
from ..core.observability import profiled, record
from ..core.variables import MISSING, ConditionError

router = Router(tags=["gateways"])


class GatewayIn(Schema):
    question: str = ""  # {{variable}} templates allowed
    branches: dict[str, str]  # branch name -> description, shown to Laya
    state: dict | str | None = None  # what Laya reads; empty: all the process variables
    min_confidence: float | None = None
    item: dict[str, Any] = {}  # the n8n item that reached the node


class ConditionsIn(Schema):
    kind: Literal["exclusive", "inclusive"]
    conditions: dict[str, str | bool]  # branch -> e.g. "{{amount}} > 1000 and not {{vip}}"
    item: dict[str, Any] = {}


class GatewayOut(Schema):
    branches: list[str]  # branches to take; empty -> BPMN default flow
    scores: dict[str, float | None]  # Laya confidence / P(yes) per evaluated branch


@router.post("/exclusive", response=GatewayOut, openapi_extra={"x-bpmn-gateway": "exclusiveGateway"})
@profiled
def exclusive_gateway(request, data: GatewayIn):
    """Exactly one branch: a Laya `choice` question over the branches."""
    case = CaseData(request, data.item)
    question, seen = case.text(data.question), state(case, data)
    with upstream():
        branch, confidence = LayaClient().choose(seen, question, data.branches)
    taken = confidence is None or confidence >= (data.min_confidence or 0)
    return decided("laya", {"question": question, "state": seen, "options": data.branches},
                   [branch] if taken else [], {branch: confidence})


@router.post("/inclusive", response=GatewayOut, openapi_extra={"x-bpmn-gateway": "inclusiveGateway"})
@profiled
def inclusive_gateway(request, data: GatewayIn):
    """One or more branches: a Laya yes/no per branch ("<question> <description>"), all in one
    forward pass. Takes every branch with P(yes) >= min_confidence (default 0.5)."""
    case = CaseData(request, data.item)
    question, seen = case.text(data.question), state(case, data)
    questions = {name: f"{question} {desc}".strip() for name, desc in data.branches.items()}
    with upstream():
        scores = LayaClient().yes_no(seen, questions)
    threshold = 0.5 if data.min_confidence is None else data.min_confidence
    return decided("laya", {"question": question, "state": seen, "options": data.branches},
                   [n for n, p in scores.items() if p >= threshold], scores)


@router.post("/conditions", response={200: GatewayOut, 422: dict},
             openapi_extra={"x-bpmn-gateway": "exclusiveGateway|inclusiveGateway"})
@profiled
def conditions_gateway(request, data: ConditionsIn):
    """Decided on data: the branches whose condition holds on the process variables (exclusive:
    the first one). No branch -> the BPMN default flow."""
    case = CaseData(request, data.item)
    try:
        taken = []
        for branch, condition in data.conditions.items():
            if case.holds(condition):
                taken.append(branch)
                if data.kind == "exclusive":
                    break
    except ConditionError as e:
        return 422, {"detail": str(e)}
    return 200, decided("conditions", {"state": case.variables, "options": data.conditions},
                        taken, {})


def decided(decider, seen, branches, scores):
    """The gateway's reply, with the decision kept in the event log (decision mining, training)."""
    record(decider=decider, input=seen, output={"branches": branches, "scores": scores})
    return {"branches": branches, "scores": scores}


def state(case, data):
    """What Laya reads: the node's State (templates filled), or all the process variables."""
    rendered = case.render(data.state) if data.state not in (None, "", {}) else None
    return case.variables if rendered in (None, "") or rendered is MISSING else rendered
