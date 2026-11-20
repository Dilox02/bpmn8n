"""Simulation and capacity (bpmn8n/simulation/): the analytic model and scenario table, the event
simulator with confidence intervals, staffing, and a spec calibrated from the event log. A workflow
comes inline (n8n JSON) or as a project (projects/<p>/: its workflow and simulation.json). /overlay
feeds the canvas's Capacity mode (n8n-editor/insights.js), with the editor's n8n session."""
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode

from django.conf import settings
from ninja import Router, Schema

from ..clients import n8n
from ..models import Project
from ..simulation import analytic, des, optimise, scenarios
from ..simulation import calibrate as calibration
from ..simulation.calibrate import sim_model
from ..simulation.spec import UNITS, SimSpec, SpecError, load
from .analytics import overlay_auth

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


def sources():
    """What can be simulated, key -> workflow name: every project with a workflow, then the ACME
    examples (examples/workflows/, key "example:<file>", simulated on the defaults)."""
    found = {}
    for folder in sorted((Path(settings.BASE_DIR) / "projects").iterdir()):
        if folder.is_dir() and re.fullmatch(r"[A-Za-z0-9_-]+", folder.name):
            try:
                found[folder.name] = load(folder)[0].get("name") or folder.name
            except (ValueError, OSError):  # no workflow, or a spec that does not parse
                continue
    for path in sorted((Path(settings.BASE_DIR) / "examples" / "workflows").glob("*.json")):
        found[f"example:{path.stem}"] = json.loads(path.read_text()).get("name") or path.stem
    return found


def source(key):
    """(workflow, spec, project folder) of a key of sources(); the folder is None for an example."""
    if key.startswith("example:"):
        name = key.removeprefix("example:")
        path = Path(settings.BASE_DIR) / "examples" / "workflows" / f"{name}.json"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not path.is_file():
            raise SpecError(f"no example {name!r}")
        return json.loads(path.read_text()), SimSpec(), None
    folder = project_folder(key)
    return (*load(folder), folder)


def model_of(data, spec=None):
    if data.workflow:
        workflow, project_spec = data.workflow, None
    elif data.project:
        workflow, project_spec = load(project_folder(data.project))
    else:
        raise SpecError("give a workflow or a project")
    spec = data.spec if spec is None else spec
    return sim_model(workflow, SimSpec.model_validate(spec) if spec is not None else project_spec)


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


def canvas_model(workflow_id):
    """(SimModel, source key) of a workflow open in n8n: the Studio project imported as it, else the
    workflow read from n8n with the spec of the project or example of the same name (its defaults when
    that spec does not fit). None when neither knows it."""
    if project := Project.objects.filter(n8n_id=workflow_id).first():
        try:
            workflow, spec, _ = source(project.slug)
            return sim_model(workflow, spec), project.slug
        except ValueError:
            pass
    if not (workflow := n8n.workflow(workflow_id)):
        return None
    key = next((k for k, name in sources().items() if name == workflow.get("name")), None)
    if key:
        try:
            return sim_model(workflow, source(key)[1]), key
        except ValueError:
            pass
    return sim_model(workflow), key


@router.get("/overlay", auth=overlay_auth, response={200: dict, 404: dict, 422: dict})
def overlay(request, workflow_id: str):
    """Per node of a canvas, the closed forms: its lane's utilisation ρ and servers, the expected wait and
    response (seconds), its visits per case; the bottleneck, and the simulation page of its source."""
    try:
        found = canvas_model(workflow_id) if workflow_id.strip() else None  # "" would match every unimported project
        if found is None:
            return 404, {"detail": f"no project or n8n workflow {workflow_id!r} to simulate"}
        model, key = found
        a = analytic.analyse(model)
    except ValueError as e:
        return 422, {"detail": str(e)}
    seconds = UNITS[model.unit] * 60

    def secs(value):
        return None if value is None or not math.isfinite(value) else value * seconds

    stations = a.get("stations", {})
    nodes = {}
    for name, row in a.get("nodes", {}).items():
        station = stations.get(row["resource"]) or {}
        nodes[name] = {"visits": row["visits"], "resource": row["resource"], "utilisation": station.get("utilisation"),
                       "servers": station.get("servers"), "stable": station.get("stable", True),
                       "wait": secs(row["waiting_time"]), "response": secs(row["response_time"])}
    return 200, {"workflow_id": workflow_id, "source": key, "steady": a["steady"], "stable": a.get("stable"),
                 "bottleneck": a.get("bottleneck"), "cycle_time": secs(a.get("cycle_time")),
                 "page": f"/simulation/?{urlencode({'project': key})}" if key else None, "nodes": nodes,
                 "note": a.get("note", ""), "warnings": a["warnings"]}
