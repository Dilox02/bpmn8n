"""The simulation page (staff): queueing theory and the event simulation, for people. Pick a project or
an ACME example, or a process of the event log; read the capacity in closed form at once, try a what-if,
find the staffing, count the scenarios' tokens, simulate with confidence intervals, calibrate from the
log and compare the model with reality. Its state is the query string, so a view is a link to bookmark
and compare; every form is a plain GET, only saving the spec posts."""
import json
import math
import time
from pathlib import Path
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from ..analytics import Log, Scope
from ..analytics import stats as logstats
from ..analytics.log import duration_text
from ..api.simulation import MAX_CASES, STAFFING_BUDGET, cases, project_folder, source, sources
from ..core import directory
from ..models import AuditEntry
from ..simulation import analytic, des, optimise, scenarios
from ..simulation.calibrate import calibrate, sim_model
from ..simulation.spec import UNITS, SimSpec, SpecError, load
from . import charts
from .insights import day, link

LIGHT = ("project", "process", "since", "until", "source", "load", "tokens", "replications", "seed", "level",
         "metric", "max", "total")
HEAVY = ("run", "target")  # seconds of work: only their own form keeps them, so every other answer is instant
LEVELS = ("90", "95", "99")
SHOWN_ROWS = 40  # scenario rows before "show more"
METRICS = {"cycle_time": ("Mean cycle time", "time"), "cycle_time_p90": ("p90 cycle time", "time"),
           "cycle_time_max": ("Longest cycle time", "time"), "waiting_time": ("Mean waiting time", "time"),
           "completed": ("Completed cases", "count"), "unfinished": ("Unfinished cases", "count"),
           "throughput": ("Throughput", "rate"), "in_system": ("Cases in the system", "count"),
           "cost_per_case": ("Cost per case", "money"),
           "cost_people_per_case": ("  of which people", "money"), "cost_models_per_case": ("  of which models", "money"),
           "total_cost": ("Total cost", "money"),
           "simulated_time": ("Simulated time", "time")}


def span(value, unit):
    """A time in the spec's unit, in words: "12 min", "3 h 20 min"."""
    return "0" if value == 0 else duration_text(None if value is None else value * UNITS[unit] * 60)


def show(kind, value, unit):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "—"
    if kind == "time":
        return span(value, unit)
    if kind == "share":
        return f"{value * 100:.1f} %"
    if kind == "rate":
        return f"{value * 60 / UNITS[unit]:,.3g} /h"
    if kind == "money":
        return f"{value:,.2f}"
    return f"{value:,.1f}".removesuffix(".0")


def number(text, default, cast=float, low=None, high=None):
    """A query-string number within [low, high]; `default` when absent or unreadable."""
    try:
        value = cast(text)
    except (TypeError, ValueError):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return min(max(value, low if low is not None else value), high if high is not None else value)


def hidden(state, *drop, keep=()):
    """The state a form carries along: everything but the fields it sets, and no heavy run but its own."""
    return [(k, v) for k, v in state.items()
            if v and k not in drop and (k not in HEAVY or k in keep) and not ("c:*" in drop and k.startswith("c:"))]


def describe(d, unit):
    """A distribution in words, its times in the spec's unit."""
    s = lambda v: span(v, unit)  # noqa: E731
    if d.type == "fixed":
        return f"fixed {s(d.value)}"
    if d.type == "exponential":
        return f"exponential · mean {s(d.mean)}"
    if d.type in ("normal", "lognormal", "gamma"):
        return f"{d.type} · mean {s(d.mean)}, sd {s(d.sd)}"
    if d.type == "triangular":
        return f"triangular · {s(d.min)}, {s(d.mode)}, {s(d.max)}"
    if d.type == "uniform":
        return f"uniform · {s(d.min)} to {s(d.max)}"
    return f"empirical · {len(d.values)} values, mean {s(d.expected())}"


def faster(arrivals, factor):
    """The same stream `factor` times as fast: rate × factor, gaps and times ÷ factor, a peak × factor."""
    a = arrivals
    if a.type == "poisson":
        return a.model_copy(update={"rate": a.rate * factor})
    if a.type == "interval":
        d = a.every
        stretched = {k: v / factor for k in ("value", "mean", "sd", "min", "mode", "max") if (v := getattr(d, k)) is not None}
        if d.values:
            stretched["values"] = [v / factor for v in d.values]
        return a.model_copy(update={"every": d.model_copy(update=stretched)})
    if a.type == "list":
        return a.model_copy(update={"times": [t / factor for t in a.times]})
    return a.model_copy(update={"count": max(1, round(a.count * factor))})


def dump(spec):
    """A spec as JSON data: None left out, except an unlimited capacity (null)."""
    data = spec.model_dump(mode="json", exclude_none=True)
    for name, resource in spec.resources.items():
        if resource.capacity is None:
            data["resources"][name]["capacity"] = None
    return data


def effective(model):
    """The spec a model runs on with every default written out (each step's time and lane, each lane's
    people, each choice's shares, the what-if included): SimSpec reads it back to the same model."""
    g, spec = model.graph, model.spec
    tasks = {}
    for node in g.flow_nodes:
        task = {"duration": model.duration[node].model_dump(exclude_none=True)}
        if model.resource[node] or g.element(node).human:
            task["resource"] = model.resource[node] or ""
        task.update({k: v for k, v in (("cost", model.cost[node]), ("cost_per_call", model.model_cost[node]),
                                       ("priority", model.priority[node])) if v})
        tasks[node] = task
    resources = {r: {"capacity": None if math.isinf(c) else int(c), "cost_per_hour": model.cost_per_hour[r],
                     **({"schedule": spec.resources[r].schedule} if r in spec.resources and spec.resources[r].schedule else {})}
                 for r, c in model.capacity.items()}
    gateways = {}
    for node, options in model.options.items():
        if g.element(node).kind == "inclusiveGateway":
            gateways[node] = {b: sum(p for taken, p in options if b in taken) for b in g.outputs(node)[:-1]}
        else:
            gateways[node] = {taken[0]: p for taken, p in options}
    return dump(SimSpec.model_validate({**spec.model_dump(exclude={"tasks", "gateways", "resources"}), "tasks": tasks,
                                        "gateways": gateways, "resources": resources}))


def save(folder, text):
    """Writes a spec to projects/<p>/simulation.json once it parses and fits the workflow it names."""
    if folder is None:
        raise SpecError("an example has no project folder: download its spec instead")
    spec = SimSpec.model_validate_json(text)
    base, projects = Path(settings.BASE_DIR).resolve(), (Path(settings.BASE_DIR) / "projects").resolve()
    target = (folder / "simulation.json").resolve()
    if target.parent.parent != projects:
        raise SpecError("a spec is saved only in its project's folder under projects/")
    workflow = (folder / (spec.workflow or "workflow.json")).resolve()
    if not workflow.is_relative_to(base) or not workflow.is_file():
        raise SpecError(f"no workflow {spec.workflow or 'workflow.json'} for this project")
    sim_model(json.loads(workflow.read_text()), spec)  # it must fit the workflow
    target.write_text(json.dumps(dump(spec), indent=2, ensure_ascii=False) + "\n")
    return target.relative_to(base)


def headline(slug):
    """Studio's Capacity card: the bottleneck and its utilisation, None while the project has no workflow."""
    try:
        workflow, spec = load(project_folder(slug))
        a = analytic.analyse(sim_model(workflow, spec))
    except (ValueError, OSError):
        return None
    station = a.get("stations", {}).get(a.get("bottleneck")) or {}
    return {"url": reverse("simulation") + "?" + urlencode({"project": slug}), "bottleneck": a.get("bottleneck"),
            "utilisation": station.get("utilisation"), "stable": a.get("stable"), "steady": a["steady"],
            "cycle_time": span(a.get("cycle_time"), a["unit"]), "note": a.get("note", "")}


def sentence(a, unit):
    """The bottleneck in one sentence."""
    if not a["steady"]:
        return f"{a['note'].capitalize()}."
    if not any(s["servers"] for s in a["stations"].values()):
        return (f"No step waits for people: every step is automated, a delay or a lane without a limit, so a case takes "
                f"{span(a['cycle_time'], unit)} on average.")
    b = a["bottleneck"]
    s = a["stations"][b]
    people = "1 person" if s["servers"] == 1 else f"{s['servers']:g} people"
    if not s["stable"]:
        return (f"{b} cannot keep up: work arrives faster than {people} can do it (ρ = {s['utilisation'] * 100:.0f} %), "
                "so its queue grows without bound.")
    return (f"The bottleneck is {b}: its {people} {'is' if s['servers'] == 1 else 'are'} busy {s['utilisation'] * 100:.0f} % "
            f"of the time, and a task waits {span(s['waiting_time'], unit)} for a free one on average.")


def capacity(model):
    a = analytic.analyse(model)
    a.setdefault("stations", {})
    stations = sorted(({"resource": r, **s} for r, s in a["stations"].items()), key=lambda s: -(s["utilisation"] or 0))
    steps = [{"node": n, **row} for n, row in a.get("nodes", {}).items()]
    return {"a": a, "stations": stations, "steps": steps, "sentence": sentence(a, model.unit)}


def what_if(workflow, spec, model, state):
    """(the what-if model, the arrival multiplier, the changed capacities): `load` × arrivals, `c:<lane>` people."""
    factor = number(state["load"], 1.0, low=0.05, high=50)
    caps = {}
    for r, c in model.capacity.items():
        v = None if math.isinf(c) else number(state.get(f"c:{r}"), None, int, 1, 1000)
        if v is not None and v != c:
            caps[r] = v
    if factor == 1 and not caps:
        return model, factor, caps
    other = sim_model(workflow, spec.model_copy(update={"arrivals": faster(spec.arrivals, factor)})) if factor != 1 else model
    return other.with_capacity(caps), factor, caps


def compare(now, then):
    """Per lane and for the cycle time, now against the what-if."""
    rows = []
    for s in now["stations"]:
        w = then["a"]["stations"].get(s["resource"], {})
        rows.append({"resource": s["resource"], "servers": (s["servers"], w.get("servers")),
                     "utilisation": (s["utilisation"], w.get("utilisation")), "stable": w.get("stable", True),
                     "wait": (s["waiting_time"], w.get("waiting_time")),
                     "delta": None if s["waiting_time"] is None or w.get("waiting_time") is None
                     else w["waiting_time"] - s["waiting_time"]})
    a, b = now["a"].get("cycle_time"), then["a"].get("cycle_time")
    return {"rows": rows, "cycle": (a, b), "cycle_delta": None if a is None or b is None else b - a}


def staffing(model, state):
    target = number(state["target"], None, low=0.0)
    if not target:
        raise SpecError("give a positive target")
    metric = state["metric"] if state["metric"] in ("mean", "p90") else "mean"
    if 5 * cases(model) * STAFFING_BUDGET > MAX_CASES * 10:
        raise SpecError("too many cases for a search on the server: use manage.py simulate --staffing")
    started = time.perf_counter()
    result = optimise.staffing(model, target, metric, number(state["max"], 20, int, 1, 100),
                               number(state["total"], None, int, 1, 100_000), replications=5, budget=STAFFING_BUDGET)
    others = {k: v for k, v in state.items() if k not in HEAVY and not k.startswith("c:")}
    return {**result, "seconds": time.perf_counter() - started,
            "try": link(others, **{f"c:{r}": c for r, c in result["capacity"].items()})}


def table(model, tokens):
    t = scenarios.scenarios(model, tokens)
    for row in t["rows"]:
        row["ends"] = list(dict.fromkeys(row["ends"]))
    return {**t, "shown": t["rows"][:SHOWN_ROWS], "more": t["rows"][SHOWN_ROWS:], "unrolled_share": t["unrolled"] / tokens}


def simulate(model, state, a):
    """The replications, read for people: each headline metric at the chosen level, the charts, the steps,
    and whether the closed form agrees."""
    unit = model.unit
    n = number(state["replications"], model.spec.replications, int, 1, 1000)
    seed = number(state["seed"], model.spec.seed, int, 0, 2 ** 31)
    if n * cases(model) > MAX_CASES:
        raise SpecError(f"{n} replications of {cases(model):.0f} cases exceed {MAX_CASES:,} for a run on the server: "
                        "use manage.py simulate")
    started = time.perf_counter()
    result = des.simulate(model, n, seed)
    seconds = time.perf_counter() - started
    level = state["level"] if state["level"] in LEVELS else "95"
    rows = []
    for key, m in result["metrics"].items():
        label, kind = METRICS.get(key) or (f"Utilisation of {key.removeprefix('utilisation ')}", "share")
        half = m["ci"][level]
        rows.append({"label": label, "mean": show(kind, m["mean"], unit), "half": show(kind, half, unit),
                     "low": show(kind, None if half is None else max(0.0, m["mean"] - half), unit),  # none is negative
                     "high": show(kind, None if half is None else m["mean"] + half, unit)})
    bins = result["cycle_time_histogram"]
    histogram = charts.bars(bins, label="Cycle time of the completed cases", value=lambda b: b["count"],
                            name=lambda b: f"{span(b['from'], unit)} – {span(b['to'], unit)}", fmt=lambda v: f"{v:g}",
                            edges=[span(bins[0]["from"], unit), span(bins[len(bins) // 2]["from"], unit),
                                   span(bins[-1]["to"], unit)] if bins else [])
    busy = [charts.series(r["over_time"], label=f"{name}: utilisation over time", value=lambda p: p[1] or 0,
                          when=lambda p: span(p[0], unit), fmt=lambda v: f"{v * 100:.0f} %")
            for name, r in result["resources"].items() if r["capacity"] is not None]
    nodes = [{"node": n, **r} for n, r in result["nodes"].items()]
    return {"result": result, "seconds": seconds, "level": level, "replications": n, "seed": seed, "rows": rows,
            "cases": sum(result["cases"].values()),
            "histogram": histogram, "busy": busy, "nodes": nodes, "check": agreement(model, a, result["metrics"], level)}


def agreement(model, a, metrics, level):
    """E8 in small: the closed form's mean cycle time against the simulation's interval and, when it falls
    outside, what the closed form assumes that this model does not do."""
    if a.get("steady") and not a.get("stable"):
        return {"unstable": True, "lane": a["bottleneck"]}
    m = metrics.get("cycle_time") or {}
    mean, half = m.get("mean"), (m.get("ci") or {}).get(level)
    if not a.get("steady") or a.get("cycle_time") is None or mean is None or half is None:
        return None
    check = {"analytic": span(a["cycle_time"], model.unit), "simulated": span(mean, model.unit),
             "half": span(half, model.unit), "agree": abs(a["cycle_time"] - mean) <= half}
    if check["agree"]:
        return check
    g, spec = model.graph, model.spec
    reasons = ["every lane sees the arrivals as they enter the process: what one step's queue does to the stream "
               "is not passed on to the next (no propagation between stations)",
               "Allen–Cunneen's G/G/c formula, exact only for Poisson arrivals and exponential work"]
    if any(g.element(n).splits for n in g.flow_nodes):
        reasons.append("parallel branches take the longest of their mean times; the simulation takes the longest of "
                       "their actual times, which is longer when they vary")
    if model.schedules:
        reasons.append("people always at work: the simulation follows their work schedules")
    if spec.arrivals.count:
        reasons.append(f"a steady state: the simulation starts empty and plays {spec.arrivals.count} cases, so the first "
                       "ones wait less, and a busy lane may not settle in that time")
    if any((s.get("utilisation") or 0) >= 0.85 for s in a["stations"].values()):
        reasons.append("a lane near capacity, where the mean wait is very sensitive and slow to settle: more cases or "
                       "replications narrow the interval")
    return {**check, "reasons": reasons}


def from_log(state, workflow, unit):
    """The spec calibrated from the event log of the chosen process and window, read for people."""
    try:
        spec, report = calibrate(state["process"], day(state["since"]), day(state["until"], end=True), workflow, unit)
    except ValueError as e:
        return {"error": str(e)}
    activities = []
    for name, r in report["activities"].items():
        aic = r.get("aic") or {}
        activities.append({"name": name, "n": r["n"], "mean": span(r["mean"], unit), "fitted": r.get("fitted", "—"),
                           "params": describe(spec.tasks[name].duration, unit), "resource": spec.tasks[name].resource,
                           "aic": aic.get(r.get("fitted")),
                           "others": ", ".join(f"{k} {v:,.0f}" for k, v in aic.items() if k != r.get("fitted"))})
    gateways = [{"name": g, "decisions": r["decisions"],
                 "branches": [(b, n, n / r["decisions"] if r["decisions"] else 0) for b, n in r["branches"].items()]}
                for g, r in report["gateways"].items()]
    every = spec.arrivals.every if spec.arrivals and spec.arrivals.type == "interval" else None
    return {"spec": spec, "report": report, "activities": activities, "gateways": gateways,
            "arrivals": {"rate": show("rate", report.get("rate"), unit), "ca2": report.get("ca2"),
                         "gaps": describe(every, unit) if every else "—"},
            "people": [(lane, names, spec.resources[lane].capacity) for lane, names in report["people"].items()]}


def reality(state, result, unit):
    """Per activity, the log's mean duration and wait against the simulation's (queue + work), with the
    relative error, and the mean cycle time on both sides."""
    log = Log.load(Scope(state["process"], since=day(state["since"]), until=day(state["until"], end=True)))
    if not log.traces:
        return None
    seconds, observed = UNITS[unit] * 60, {a["activity"]: a for a in logstats.activities(log)}
    simulated_cases = sum(result["cases"].values()) or 1
    rows = []
    for node, sim in result["nodes"].items():
        if not (obs := observed.get(node)):
            continue
        queue = sim["queue_time"] * seconds if sim["queue_time"] is not None else None
        took = (sim["queue_time"] or 0) * seconds + (sim["work_time"] or 0) * seconds
        mean = obs["duration"]["mean"]
        rows.append({"activity": node, "executions": obs["executions"],
                     "visits": (obs["executions"] / len(log.traces), sim["visits"] / simulated_cases),
                     "observed": mean, "simulated": took, "error": (took - mean) / mean if mean else None,
                     "wait": obs["wait"], "queue": queue})
    # the fit: the time the steps are off, over the time they take (a sub-second step 20× off weighs nothing)
    spent = sum(r["executions"] * r["observed"] for r in rows if r["observed"])
    m = result["metrics"].get("cycle_time") or {}
    cycle = logstats.overview(log)["case_duration"] or {}
    observed_cycle, simulated_cycle = cycle.get("mean"), m.get("mean") and m["mean"] * seconds
    return {"rows": rows, "cases": len(log.traces),
            "fit": sum(r["executions"] * abs(r["simulated"] - r["observed"]) for r in rows if r["observed"]) / spent
            if spent else None,
            "cycle": (observed_cycle, simulated_cycle),
            "cycle_error": (simulated_cycle - observed_cycle) / observed_cycle if observed_cycle and simulated_cycle else None}


def origin(node, file_spec, calibrated):
    if calibrated is not None and node in calibrated.tasks:
        return "log"
    task = file_spec.tasks.get(node)
    return "spec" if task and (task.duration or task.resource is not None or task.cost) else "default"


@staff_member_required
def simulation(request):
    state = {k: request.GET.get(k, "").strip() for k in (*LIGHT, *HEAVY)}
    state.update({k: v.strip() for k, v in request.GET.items() if k.startswith("c:")})
    catalogue, processes = sources(), logstats.processes()
    if not (state["project"] or state["process"]) and not getattr(request, "b8_workflow", None):
        return choose(request, catalogue)
    # ponytail: a log's process is simulated on the project or example named like it, else only calibrated.
    # Upgrade: read its workflow from n8n by the case ids' workflow id (clients.n8n.workflow), as /overlay does.
    if not state["project"] and state["process"]:
        state["project"] = next((k for k, name in catalogue.items() if name == state["process"]), "")
    context = {"state": state, "sources": catalogue, "processes": processes, "levels": LEVELS, "unit": "minutes",
               "here": request.get_full_path()}
    workflow = folder = None
    if state["project"]:
        try:
            workflow, file_spec, folder = source(state["project"])
            context.update(name=catalogue.get(state["project"], state["project"]), unit=file_spec.unit)
        except ValueError as e:
            messages.error(request, f"Cannot read {state['project']}: {e}")
            workflow = None
    unit = context["unit"]
    log = from_log(state, workflow, unit) if state["process"] else None
    context["log"] = log
    if workflow is None:
        return render(request, "bpmn8n/simulation.html", context)

    calibrated = log.get("spec") if log and state["source"] == "log" else None
    spec = file_spec if calibrated is None else calibrated.model_copy(
        update={"workflow": file_spec.workflow, "replications": file_spec.replications, "seed": file_spec.seed})
    try:
        now_model = sim_model(workflow, spec)
        what_model, factor, caps = what_if(workflow, spec, now_model, state)
        now = capacity(now_model)
        then = capacity(what_model) if what_model is not now_model else now
    except ValueError as e:
        messages.error(request, f"This workflow cannot be simulated as it is: {e}")
        return render(request, "bpmn8n/simulation.html", context)
    data = effective(what_model)
    if request.GET.get("download"):
        response = HttpResponse(json.dumps(data, indent=2, ensure_ascii=False) + "\n", content_type="application/json")
        response["Content-Disposition"] = 'attachment; filename="simulation.json"'
        return response
    text, status = json.dumps(data, indent=2, ensure_ascii=False), 200
    if request.method == "POST":
        text = request.POST.get("spec", "")
        try:
            path = save(folder, text)
            AuditEntry.write("simulation.save", object_type="project", object_id=folder.name, path=str(path))
            messages.success(request, f"Saved {path}: the page now reads it.")
            return redirect(request.get_full_path())
        except ValueError as e:
            context["spec_error"], status = str(e), 422

    context.update(
        calibrated=calibrated is not None, now=now, then=then, factor=factor, caps=caps,
        changed=what_model is not now_model, delta=compare(now, then) if what_model is not now_model else None,
        inputs=[(s["resource"], int(what_model.capacity[s["resource"]]), int(s["servers"]))
                for s in now["stations"] if s["servers"] is not None],
        tokens=number(state["tokens"], 100, int, 1, 10 ** 9), steady=now["a"]["steady"],
        defaults={"replications": spec.replications, "seed": spec.seed},
        spec={"text": text, "file": (folder / "simulation.json").read_text() if folder and (folder / "simulation.json").exists() else None,
              "path": f"projects/{folder.name}/simulation.json" if folder else None,
              "download": link(state, download="1"), "arrivals": spec.arrivals,
              "rows": [{"node": n, "duration": describe(what_model.duration[n], unit), "resource": what_model.resource[n],
                        "cost": what_model.cost[n], "from": origin(n, file_spec, calibrated)} for n in what_model.graph.flow_nodes]},
        forms={"what": hidden(state, "load", "c:*"), "staffing": hidden(state, "target", "metric", "max", "total"),
               "tokens": hidden(state, "tokens"), "run": hidden(state, "replications", "seed", "level", "run"),
               "reset": link({k: v for k, v in state.items() if k not in HEAVY and k != "load" and not k.startswith("c:")})},
        simulate_with_log=link({k: v for k, v in state.items() if k not in HEAVY}, source="log", run="1"),
        without_log=link({k: v for k, v in state.items() if k not in HEAVY}, source=""))
    context["scenarios"] = table(now_model, context["tokens"])
    if state["target"]:
        try:
            context["staffing"] = staffing(what_model, state)
        except ValueError as e:
            context["staffing_error"] = str(e)
    if state["run"]:
        try:
            context["run"] = simulate(what_model, state, then["a"])
            if state["process"] and what_model is now_model:  # a what-if is not what happened
                context["reality"] = reality(state, context["run"]["result"], unit)
        except ValueError as e:
            context["run_error"] = str(e)
    return render(request, "bpmn8n/simulation.html", context, status=status)


def choose(request, catalogue):
    """/simulation/ with nothing chosen: the workflows n8n has that have a model (a project's simulation.json, or its
    elements' defaults), each opening its Simulation tab; then the models of projects not in n8n."""
    rows, modelled = [], set()
    for ref in (r for r in directory.workflows() if r.in_n8n):
        key = next((k for k, name in catalogue.items() if name in (ref.name, ref.process)), None)
        if key:
            modelled.add(key)
            rows.append({"name": ref.name, "url": reverse("workflow_simulation", args=[ref.id]), "meta": ref.state})
    others = [{"name": name, "url": f"?{urlencode({'project': key})}", "meta": "a model, not in n8n"}
              for key, name in catalogue.items() if key not in modelled]
    return render(request, "bpmn8n/chooser.html", {
        "title": "Simulation", "sub": "How long a case takes, where it waits and how many people each lane needs: choose a workflow.",
        "workflows": rows, "empty": "No workflow in n8n has a simulation model yet.", "others": others,
        "others_title": "Models of projects not in n8n", "others_note": "their simulation.json, before they are published"})
