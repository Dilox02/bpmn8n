"""Staffing ([BS] p.45–51, 90–96): the fewest people per resource that still meet a target (mean or p90
cycle time, or the total time of the simulated cases, the course's "total duration"), with at most
`max_per_resource` each and, optionally, `total` in all.

As in the course: the target must hold with every resource at its maximum; then a greedy descent
removes, one person at a time, the person whose absence costs least, while the target holds; the
sensitivity table says what removing one more person of each resource would cost. Every candidate is
confirmed by replications; the analytic model screens first, so few simulations are needed:
- the work of N cases needs time: a resource can never finish it faster than N × work / capacity, so
  for a total-time target capacities below that bound are never simulated;
- with a steady stream of cases and a mean target, the descent starts where the closed forms say the
  target is still met, and climbs back (adding a person where utilisation is highest) if the
  simulation disagrees.
Pure Python, numpy and scipy, no Django."""
import math

import numpy as np

from .analytic import analyse, work
from .des import Run
from .scenarios import expected_visits
from .spec import SimModel, SpecError

METRICS = {"mean": "cycle_time", "p90": "cycle_time_p90", "total": "simulated_time"}


def staffing(model: SimModel, target, metric="mean", max_per_resource=20, total=None, replications=5, seed=None,
             budget=200):
    if metric not in METRICS:
        raise SpecError(f"metric: one of {', '.join(METRICS)}")
    key = METRICS[metric]
    resources = sorted(r for r, c in model.capacity.items() if not math.isinf(c) and r in model.resource.values())
    if not resources:
        raise SpecError("no resource with a capacity to staff")
    runs = {}  # capacities -> (mean metric, mean utilisation per resource)

    def simulated(caps):
        k = tuple(caps[r] for r in resources)
        if k not in runs:
            if len(runs) >= budget:
                raise _Budget
            headlines = [Run(model.with_capacity(caps), i, seed).headline for i in range(replications)]
            values = [h[key] for h in headlines]
            runs[k] = (math.inf if None in values else float(np.mean(values)),
                       {r: float(np.mean([h[f"utilisation {r}"] or 0 for h in headlines])) for r in resources})
        return runs[k][0]

    lower = dict.fromkeys(resources, 1)
    count = model.spec.arrivals.count
    if metric == "total" and count:
        for r, w in work(model, expected_visits(model)).items():
            if r in lower:
                lower[r] = min(max_per_resource, max(1, math.ceil(count * w / target)))
    top = dict.fromkeys(resources, max_per_resource)
    try:
        if simulated(top) > target:
            return {"metric": metric, "target": target, "feasible": False, "capacity": top, "value": simulated(top),
                    "simulations": len(runs), "note": f"the target is not met even with {max_per_resource} per resource"}
        caps = _analytic_start(model, top, lower, target) if metric == "mean" and \
            model.spec.arrivals.rate_and_scv() else dict(lower) if metric == "total" else dict(top)
        while simulated(caps) > target:  # climb where people are busiest
            busy = runs[tuple(caps[r] for r in resources)][1]
            short = [r for r in resources if caps[r] < max_per_resource]
            if not short:
                break
            caps[max(short, key=busy.get)] += 1
        while True:  # descend: remove the person whose absence costs least
            trials = [(simulated(c), c) for r in resources if caps[r] - 1 >= lower[r]
                      for c in [{**caps, r: caps[r] - 1}]]
            trials = [t for t in trials if t[0] <= target]
            if not trials:
                break
            caps = min(trials, key=lambda t: t[0])[1]
        value = simulated(caps)
        sensitivity = [{"resource": r, "capacity": caps[r] - 1, "value": v, "delta": v - value, "meets_target": v <= target}
                       for r in resources if caps[r] > 1 for v in [simulated({**caps, r: caps[r] - 1})]]
        note = ""
    except _Budget:
        feasible = [(sum(k), k) for k, (v, _) in runs.items() if v <= target]
        caps = dict(zip(resources, min(feasible)[1])) if feasible else top
        value, sensitivity, note = runs[tuple(caps[r] for r in resources)][0], [], f"stopped after {budget} simulations"
    headcount = sum(caps.values())
    return {"metric": metric, "target": target, "feasible": total is None or headcount <= total, "capacity": caps,
            "headcount": headcount, "value": value, "simulations": len(runs), "sensitivity": sensitivity,
            "note": note or (f"needs {headcount} people, more than {total}" if total is not None and headcount > total else "")}


class _Budget(Exception):
    """Too many simulations."""


def _analytic_start(model, top, lower, target):
    """The fewest people the closed forms say keep the mean cycle time within the target."""
    def cycle(caps):
        a = analyse(model.with_capacity(caps), scenario_limit=200)
        return a["cycle_time"] if a.get("stable") and a["cycle_time"] is not None else math.inf

    caps = dict(top)
    while True:
        trials = [(cycle(c), c) for r in caps if caps[r] > lower[r] for c in [{**caps, r: caps[r] - 1}]]
        trials = [t for t in trials if t[0] <= target]
        if not trials:
            return caps
        caps = min(trials, key=lambda t: t[0])[1]
