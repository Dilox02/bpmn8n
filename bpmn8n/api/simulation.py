"""Simulation and capacity (bpmn8n/simulation/): the analytic model and scenario table, the event
simulator with confidence intervals, staffing, and a spec calibrated from the event log. A workflow
comes inline (n8n JSON) or as a project (projects/<p>/: its workflow and simulation.json)."""
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from django.conf import settings
from ninja import Router, Schema

from ..simulation import analytic, des, optimise, scenarios
from ..simulation import calibrate as calibration
from ..simulation.spec import SimModel, SimSpec, SpecError, load

router = Router(tags=["simulation"])

# ponytail: runs are synchronous, so their size is capped (cases × replications; staffing: × its
# simulations). Upgrade: an async job whose progress streams over SSE (core ChangeStream).
MAX_CASES = 200_000
STAFFING_BUDGET = 40


class Source(Schema):
    project: str | None = None  # a folder under projects/
    workflow: dict | None = None  # or the n8n workflow itself
    spec: dict | None = None  # a SimSpec; replaces the project's simulation.json


class AnalyticIn(Source):
    tokens: int = 100  # start tokens of the scenario table


class RunIn(Source):
    replications: int | None = None
    seed: int | None = None
    compare: dict | None = None  # a second SimSpec, run on common random numbers: the paired difference


class StaffingIn(Source):
    target: float
    metric: Literal["mean", "p90", "total"] = "mean"
    max_per_resource: int = 20
    total: int | None = None
    replications: int = 5


def project_folder(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name or ""):
        raise SpecError(f"no project {name!r}")
    folder = Path(settings.BASE_DIR) / "projects" / name
    if not folder.is_dir():
        raise SpecError(f"no project {name!r}")
    return folder


def model_of(data, spec=None):
    if data.workflow:
        workflow, project_spec = data.workflow, None
    elif data.project:
        workflow, project_spec = load(project_folder(data.project))
    else:
        raise SpecError("give a workflow or a project")
    spec = data.spec if spec is None else spec
    return SimModel(workflow, SimSpec.model_validate(spec) if spec is not None else project_spec)


def cases(model):
    a, spec = model.spec.arrivals, model.spec
    if a.type == "list":
        return len(a.times)
    if a.count is not None:
        return a.count
    rate = (a.rate_and_scv() or (0, 0))[0]
    return rate * spec.horizon if spec.horizon else 0


def answer(handler, data):
    """Errors of the spec or the workflow (SpecError, pydantic's ValidationError) answer 422 with the reason."""
    try:
        return 200, handler(data)
    except ValueError as e:
        return 422, {"detail": str(e)}


@router.post("/analytic", response={200: dict, 422: dict})
def analytic_model(request, data: AnalyticIn):
    """At once, without a simulation: expected visits per step, the queueing network (per resource ρ, Wq,
    W, stable; the bottleneck; the mean cycle time) and the scenario table for `tokens` start tokens."""
    def handler(d):
        model = model_of(d)
        return {"analytic": analytic.analyse(model), "scenarios": scenarios.scenarios(model, d.tokens)}
    return answer(handler, data)


@router.post("/run", response={200: dict, 422: dict})
def run(request, data: RunIn):
    """The event simulation, replicated: every headline metric with its 90/95/99 % confidence interval,
    per step, per resource and per scenario. With `compare`, both specs on common random numbers."""
    def handler(d):
        model = model_of(d)
        n = d.replications or model.spec.replications
        if n * cases(model) > MAX_CASES:
            raise SpecError(f"{n} replications of {cases(model):.0f} cases exceed {MAX_CASES} for a synchronous run: "
                            "use manage.py simulate")
        if d.compare is not None:
            return des.compare(model, model_of(d, d.compare), n, d.seed)
        return des.simulate(model, n, d.seed)
    return answer(handler, data)


@router.post("/staffing", response={200: dict, 422: dict})
def staffing(request, data: StaffingIn):
    """The fewest people per resource that meet `target` (mean or p90 cycle time, or the total time),
    at most `max_per_resource` each and `total` in all; with the sensitivity per person removed."""
    def handler(d):
        model = model_of(d)
        if d.replications * cases(model) * STAFFING_BUDGET > MAX_CASES * 10:
            raise SpecError("too many cases and replications for a synchronous staffing search")
        return optimise.staffing(model, d.target, d.metric, d.max_per_resource, d.total, d.replications,
                                 budget=STAFFING_BUDGET)
    return answer(handler, data)


@router.get("/calibrate", response={200: dict, 422: dict})
def calibrate(request, process: str, since: datetime | None = None, until: datetime | None = None,
              project: str | None = None, unit: str = "minutes"):
    """A SimSpec from the event log of `process` (the n8n workflow's name) in [since, until), with the
    report of what was fitted; with `project`, only its workflow's steps, read with its gateways and lanes."""
    def handler(_):
        workflow = load(project_folder(project))[0] if project else None
        spec, report = calibration.calibrate(process, since, until, workflow, unit)
        return {"spec": spec.model_dump(exclude_none=True), "report": report}
    return answer(handler, None)
