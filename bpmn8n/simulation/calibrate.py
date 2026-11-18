"""Simulate from reality: a SimSpec read from the event log of a process ([DI] §1.2, 4.2; [BS] p.83).

- arrivals: the start of each case (its first event); the gaps between starts are fitted like a
  duration, so the stream keeps its variability (rate and ca² are in the report);
- per activity: its durations fitted with exponential, lognormal and gamma by maximum likelihood, the
  lowest AIC wins; with fewer than MIN_FIT samples the samples themselves (empirical), all equal a
  fixed time;
- per gateway: the branches its decisions took (Event.data.output.branches; none = "default"): an
  exclusive gateway's shares, an inclusive gateway's share per branch;
- per lane: as many people as distinct `resource` values completed its tasks.
Django: it reads bpmn8n.models.Event."""
import itertools
import math

import numpy as np
from scipy import stats

from ..models import Event
from ..workflows.graph import ProcessGraph
from .spec import UNITS, Arrivals, Distribution, Resource, SimSpec, SpecError, Task

MIN_FIT = 10
GATEWAYS = ("exclusive_gateway", "inclusive_gateway", "conditions_gateway")  # api/gateways.py
HUMAN = ("userTask", "manualTask")


def fit(values):
    """(Distribution, report) for samples (in the spec's unit)."""
    x = np.asarray(values, dtype=float)
    report = {"n": len(x), "mean": float(x.mean()) if len(x) else None}
    if not len(x):
        return None, report
    if np.ptp(x) == 0:
        return Distribution(value=float(x[0])), {**report, "fitted": "fixed"}
    if len(x) < MIN_FIT:
        return Distribution(type="empirical", values=[float(v) for v in x]), {**report, "fitted": "empirical"}
    positive = np.maximum(x, 1e-9)  # lognormal and gamma live on (0, ∞)
    candidates = {}
    scale = stats.expon.fit(x, floc=0)[1]
    candidates["exponential"] = (1, stats.expon.logpdf(x, 0, scale).sum(), Distribution(type="exponential", mean=scale))
    s, _, scale = stats.lognorm.fit(positive, floc=0)
    mean = scale * math.exp(s * s / 2)
    candidates["lognormal"] = (2, stats.lognorm.logpdf(positive, s, 0, scale).sum(),
                               Distribution(type="lognormal", mean=mean, sd=mean * math.sqrt(math.expm1(s * s))))
    a, _, scale = stats.gamma.fit(positive, floc=0)
    candidates["gamma"] = (2, stats.gamma.logpdf(positive, a, 0, scale).sum(),
                           Distribution(type="gamma", mean=a * scale, sd=math.sqrt(a) * scale))
    aic = {name: 2 * k - 2 * ll for name, (k, ll, _) in candidates.items() if math.isfinite(ll)}
    best = min(aic, key=aic.get)
    return candidates[best][2], {**report, "fitted": best, "aic": aic}


def calibrate(process, since=None, until=None, workflow=None, unit="minutes"):
    """(SimSpec, report) from the events of `process` (Event.process: the n8n workflow's name) whose cases
    started at/after `since` and events before `until`. With the `workflow`, only its steps are kept
    and gateways, event-based gateways and lanes are read from it."""
    graph = ProcessGraph(workflow) if workflow else None
    events = Event.objects.cases(since).filter(process=process)
    if until:
        events = events.filter(started__lt=until)
    rows = list(events.values("case_id", "activity", "endpoint", "started", "duration_ms", "resource", "data"))
    if not rows:
        raise SpecError(f"no events of the process {process!r} in that window")
    ms = 60_000 * UNITS[unit]
    starts = {}
    for row in rows:
        starts[row["case_id"]] = min(starts.get(row["case_id"], row["started"]), row["started"])
    starts = sorted(starts.values())
    gaps = [(b - a).total_seconds() * 1000 / ms for a, b in itertools.pairwise(starts)]
    # ponytail: UserTask keeps no claim time, so a human task's time is its whole open time. Upgrade: a
    # claimed timestamp on UserTask, then queue and work apart.
    report = {"process": process, "unit": unit, "cases": len(starts), "events": len(rows), "notes": [
        ("a human task's time runs from its creation to its completion: the log has no claim time, so it "
         "includes the wait for a free person, which the simulation adds again when the lane is busy")]}
    arrivals = None
    if gaps and sum(gaps) > 0:
        every, report["arrivals"] = fit(gaps)
        report["rate"] = len(gaps) / sum(gaps)
        report["ca2"] = float(np.var(gaps, ddof=1) / np.mean(gaps) ** 2) if len(gaps) > 1 else 0.0
        arrivals = Arrivals(type="interval", every=every, count=len(starts))

    known = set(graph.flow_nodes) if graph else None
    by_activity = {}
    for row in rows:
        by_activity.setdefault(row["activity"], []).append(row)
    report["not_in_workflow"] = sorted(a for a in by_activity if known is not None and a not in known)
    tasks, people, report["activities"] = {}, {}, {}
    for activity, events_ in by_activity.items():
        if activity in report["not_in_workflow"]:
            continue
        duration, report["activities"][activity] = fit([e["duration_ms"] / ms for e in events_])
        human = [e for e in events_ if e["endpoint"] in HUMAN]
        lane = None
        if human or (graph and graph.element(activity).human):
            params = graph.nodes[activity].get("parameters") or {} if graph else {}
            lane = params.get("lane") or activity
            people.setdefault(lane, set()).update(e["resource"] for e in human if e["resource"] not in ("", "api"))
        tasks[activity] = Task(duration=duration, **({"resource": lane} if lane else {}))
    resources = {lane: Resource(capacity=max(1, len(names))) for lane, names in people.items()}
    report["people"] = {lane: sorted(names) for lane, names in people.items()}

    gateways, report["gateways"] = {}, {}
    for activity, events_ in by_activity.items():
        decided = [e["data"]["output"]["branches"] for e in events_ if e["endpoint"] in GATEWAYS
                   and isinstance((e["data"] or {}).get("output", {}).get("branches"), list)]
        if not decided or activity in report["not_in_workflow"]:
            continue
        inclusive = graph.element(activity).kind == "inclusiveGateway" if graph else any(len(b) > 1 for b in decided)
        counts = {}
        for branches in decided:
            for b in branches or (["default"] if not inclusive else []):
                counts[b] = counts.get(b, 0) + 1
        if graph:
            outputs = graph.outputs(activity)
            counts = {b: n for b, n in counts.items() if b in outputs}
        report["gateways"][activity] = {"decisions": len(decided), "branches": counts}
        gateways[activity] = {b: n / len(decided) for b, n in counts.items() if not (inclusive and b == "default")}
    for node in graph.of(timeout_first=True) if graph else []:
        # ponytail: a timeout writes no event, so its share is what the messages leave of the cases that
        # reached the gateway (counted at its logged predecessors). Upgrade: log the timeout.
        messages = [e["resource"].removeprefix("message:") for e in by_activity.get(node, [])
                    if e["resource"].startswith("message:")]
        reached = sum(len(by_activity.get(p, [])) for p in graph.into[node])
        if messages and reached >= len(messages):
            names = [m for m in messages if m in graph.outputs(node)]
            gateways[node] = {m: names.count(m) / reached for m in set(names)}
            report["gateways"][node] = {"decisions": reached, "branches": {m: names.count(m) for m in set(names)}}
    spec = SimSpec(unit=unit, arrivals=arrivals, resources=resources, tasks=tasks, gateways=gateways)
    return spec, report
