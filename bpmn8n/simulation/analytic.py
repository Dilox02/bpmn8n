"""The workflow as a queueing network, in closed form ([QT] §3–4, [BS] p.45–51): an answer at once, and
the oracle the simulator is tested against.

- λ of a step = case arrival rate × its expected visits per case (loops included: scenarios.py);
- the steps that share a resource (a lane) form one G/G/c station: their λ add up, the mean work is
  the visit-weighted mean, cs² comes from the mixture of their times; c is the resource's capacity;
- a step with no resource (an automated task, a timer, a message) is a delay: it takes its time and
  nobody waits for it (unless the spec gives it a resource with a capacity);
- a step's response time W = the station's mean wait Wq + its own mean time.
The mean cycle time plays every scenario with W as each step's time and weights the time each case
takes by its probability: sequential steps add up, parallel branches take the longest one.
Approximations, stated: every station sees the process's arrival variability ca² (the departures of
one station are not propagated to the next, as Whitt's QNA would); Allen–Cunneen for G/G/c; the
mean of the longest parallel branch is taken as the longest of their means (an underestimate when
branches vary); work schedules are ignored (servers always there). Pure Python, no Django."""
import math

from . import queueing
from .scenarios import expected_visits, scenarios
from .spec import SimModel


def stations(model: SimModel, visits, rate, ca2):
    """resource -> (Station, its nodes, cs²) for the resources the workflow visits."""
    groups = {}
    for node, resource in model.resource.items():
        if resource and visits.get(node, 0) > 0:
            groups.setdefault(resource, []).append(node)
    found = {}
    for resource, nodes in groups.items():
        rates = {n: rate * visits[n] for n in nodes}
        lam = sum(rates.values())
        means = {n: model.duration[n].expected() for n in nodes}
        es = sum(rates[n] * means[n] for n in nodes) / lam
        es2 = sum(rates[n] * (1 + model.duration[n].scv()) * means[n] ** 2 for n in nodes) / lam
        cs2 = es2 / es ** 2 - 1 if es else 0.0
        c = model.capacity[resource]
        found[resource] = (queueing.mminf(lam, es) if math.isinf(c) else queueing.ggc(lam, es, c, ca2, max(cs2, 0.0)),
                           nodes, cs2)
    return found


def work(model: SimModel, visits):
    """resource -> the work one case brings it (Σ visits × mean time)."""
    total = {}
    for node, resource in model.resource.items():
        if resource:
            total[resource] = total.get(resource, 0.0) + visits.get(node, 0.0) * model.duration[node].expected()
    return total


def analyse(model: SimModel, scenario_limit=500):
    spec = model.spec
    visits = expected_visits(model)
    per_case = work(model, visits)
    result = {"unit": spec.unit, "visits": visits, "work": per_case, "warnings": list(model.warnings)}
    if model.schedules:
        result["warnings"].append("work schedules are ignored here: the closed forms assume servers always there")
    if spec.arrivals.count:  # no resource can finish the work of `count` cases faster than this
        result["busy_bound"] = max((spec.arrivals.count * w / model.capacity[r] for r, w in per_case.items()
                                    if not math.isinf(model.capacity[r])), default=0.0)
    stream = spec.arrivals.rate_and_scv()
    if stream is None:
        return {**result, "steady": False, "note": "a peak of cases at once has no steady state: simulate it"}
    rate, ca2 = stream
    found = stations(model, visits, rate, ca2)
    wait = {}
    for station, nodes, _ in found.values():
        wait.update(dict.fromkeys(nodes, station.waiting_time))
    times = {n: wait.get(n, 0.0) + model.mean_time(n) for n in visits}
    stable = all(s.stable for s, _, _ in found.values())

    def time(node, outputs):
        if node in model.timeout and outputs:
            message = min(model.duration[node].expected(), model.timeout[node])
            return model.timeout[node] if outputs == ("timeout",) else message
        return times[node]

    cycle, method = None, None
    if stable:
        table = scenarios(model, 1.0, threshold=1e-6, limit=scenario_limit, time=time)
        covered = sum(r["probability"] for r in table["rows"])
        if covered > 0.99:
            cycle, method = sum(r["probability"] * r["time"] for r in table["rows"]) / covered, "scenarios"
        else:  # too many paths to play: add up every visit (parallel branches summed, an upper bound)
            cycle, method = sum(visits[n] * times[n] for n in visits), "visits"
    def finite(v):
        return v if v is not None and math.isfinite(v) else None

    return {**result, "steady": True, "rate": rate, "ca2": ca2, "stable": stable,
            "bottleneck": max(found, key=lambda r: found[r][0].utilisation, default=None),
            "cycle_time": cycle, "cycle_time_method": method, "in_system": finite(cycle and rate * cycle),
            "stations": {r: {**s.as_dict(), "cs2": cs2, "ca2": ca2, "nodes": nodes} for r, (s, nodes, cs2) in found.items()},
            "nodes": {n: {"visits": visits[n], "arrival_rate": rate * visits[n], "resource": model.resource[n],
                          "service_time": model.mean_time(n), "waiting_time": finite(wait.get(n, 0.0)),
                          "response_time": finite(times[n])} for n in visits if visits[n] > 0}}
