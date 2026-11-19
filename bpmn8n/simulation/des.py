"""The event simulator ([BS] p.26–28, 62–64, 85): cases arrive as the spec says and run on the
TokenEngine with sampled times and choices; people are resources with a capacity and a FIFO queue.
Parallel branches run in parallel and arrivals are native: no "emulation" model, no timer faked in a
one-instance lane ([BS] p.65–68, 73–75).

One run gives the course's outputs: completed cases, total cost, simulated time; cycle and waiting
times (mean, p50, p90, max); utilisation per resource, overall and over time; cost per case; per step
the visits, queue time, work time and cost; per scenario the first and last case out, how many and
their mean cycle time. `simulate` repeats it on independent seeds and gives every headline metric
with its confidence interval; `compare` runs two specs on common random numbers. Pure Python and
numpy, no Django."""
import itertools
import math
import zlib
from functools import cached_property

import numpy as np

from ..engine import (
    COMPLETED,
    ERROR,
    LOOP_LIMIT,
    Outcome,
    Resolver,
    Resources,
    TokenEngine,
)
from . import stats
from .spec import UNITS, SimModel

BUCKETS = 20  # utilisation over time


def _rng(seed, replication, key):
    """One random stream per seed, replication and purpose: two specs that agree on a step draw the same
    numbers for it (common random numbers)."""
    return np.random.default_rng([seed, replication, zlib.crc32(key.encode())])


class _Stream:
    def __init__(self, rng, draw):
        self.rng, self.draw, self.buffer, self.i = rng, draw, [], 0

    def next(self):
        if self.i == len(self.buffer):
            self.buffer, self.i = self.draw(self.rng, 256).tolist(), 0
        self.i += 1
        return self.buffer[self.i - 1]


class Sampled(Resolver):
    """Draws each visit's time from its step's distribution and each choice from its options."""

    def __init__(self, model: SimModel, seed, replication):
        self.model, self.seed, self.replication, self._streams = model, seed, replication, {}

    def _next(self, key, draw):
        if key not in self._streams:
            self._streams[key] = _Stream(_rng(self.seed, self.replication, key), draw)
        return self._streams[key].next()

    def resolve(self, visit):
        node, m = visit.node, self.model
        outputs, options = None, m.options.get(node)
        if options:
            outputs = options[-1][0]
            if len(options) > 1:
                u, total = self._next(f"route:{node}", lambda rng, n: rng.random(n)), 0.0
                for taken, p in options:
                    total += p
                    if u < total:
                        outputs = taken
                        break
        if node in m.timeout and outputs == ("timeout",):
            return Outcome(m.timeout[node], outputs)
        dist = m.duration[node]
        duration = dist.value if dist.type == "fixed" else self._next(f"time:{node}", dist.sample)
        return Outcome(min(duration, m.timeout[node]) if node in m.timeout else duration, outputs)


class Run:
    """One replication."""

    def __init__(self, model: SimModel, replication=0, seed=None):
        spec = model.spec
        self.model, self.seed = model, spec.seed if seed is None else seed
        self.resources = Resources(model.capacity, model.resource, model.priority, model.schedules)
        self.engine = TokenEngine(model.graph, Sampled(model, self.seed, replication), self.resources, spec.visit_limit)
        for t in spec.arrivals.arrival_times(_rng(self.seed, replication, "arrivals"), spec.horizon):
            self.engine.start(at=t)
        self.engine.run(spec.horizon or math.inf)
        everything = self.engine.cases
        self.cases = [c for c in everything if c.started >= spec.warmup]
        self.done = [c for c in self.cases if c.status == COMPLETED]
        self.begin = spec.warmup or min((c.started for c in everything), default=0.0)
        self.end = max(self.engine.now, self.begin)

    @property
    def length(self):
        return self.end - self.begin

    def costs(self, visit):
        """(people, models, other) of one visit: the worker's time at the resource's hourly cost, an LLM agent's
        call at the model's price, and the per-execution cost of the step."""
        if visit.started is None or visit.ended is None:
            return 0.0, 0.0, 0.0
        hours = (visit.ended - visit.started) * UNITS[self.model.unit] / 60
        return (self.model.cost_per_hour.get(visit.resource, 0.0) * hours if visit.resource else 0.0,
                self.model.model_cost[visit.node], self.model.cost[visit.node])

    def cost(self, visit):
        return sum(self.costs(visit))

    def case_cost(self, case):
        return sum(self.cost(v) for v in case.trace)

    def case_costs(self, case):
        return tuple(map(sum, zip(*(self.costs(v) for v in case.trace), strict=False))) or (0.0, 0.0, 0.0)

    def utilisation(self, resource, begin=None, end=None):
        begin, end = self.begin if begin is None else begin, self.end if end is None else end
        c = self.model.capacity[resource]
        if end <= begin or math.isinf(c):
            return None
        return self.resources.busy_time(resource, begin, end) / (c * (end - begin))

    @cached_property
    def headline(self):
        """The metrics replications are compared on."""
        t, cycle = self.length, [c.cycle_time for c in self.done]
        in_system = sum(max(0.0, min(c.ended if c.ended is not None else self.end, self.end) - max(c.started, self.begin))
                        for c in self.cases)
        p = stats.summary(cycle)
        return {"completed": len(self.done), "unfinished": len(self.cases) - len(self.done),
                "simulated_time": t, "total_cost": sum(self.case_cost(c) for c in self.cases),
                "cost_per_case": float(np.mean([self.case_cost(c) for c in self.done])) if self.done else None,
                "cost_people_per_case": float(np.mean([self.case_costs(c)[0] for c in self.done])) if self.done else None,
                "cost_models_per_case": float(np.mean([self.case_costs(c)[1] for c in self.done])) if self.done else None,
                "cycle_time": p["mean"], "cycle_time_p90": p["p90"], "cycle_time_max": p["max"],
                "waiting_time": stats.summary([c.waiting_time for c in self.done])["mean"],
                "throughput": len(self.done) / t if t > 0 else None, "in_system": in_system / t if t > 0 else None,
                **{f"utilisation {r}": self.utilisation(r) for r in self.model.capacity
                   if not math.isinf(self.model.capacity[r])}}

    def tables(self):
        """Per step, per resource and per scenario."""
        visits = {}
        for case in self.cases:
            for v in case.trace:
                visits.setdefault(v.node, []).append(v)
        nodes = {node: {"visits": len(vs), "queue_time": _mean(v.queued for v in vs if v.resource),
                        "work_time": _mean(v.ended - v.started for v in vs if v.ended is not None and v.started is not None),
                        "cost": sum(self.cost(v) for v in vs)} for node, vs in visits.items()}
        edges = np.linspace(self.begin, self.end, BUCKETS + 1)
        resources = {r: {"capacity": None if math.isinf(c) else c, "utilisation": self.utilisation(r),
                         "over_time": [[float(a), self.utilisation(r, a, b)] for a, b in itertools.pairwise(edges)]}
                     for r, c in self.model.capacity.items()}
        scenarios = {}
        for case in self.done:
            scenarios.setdefault(case.label, []).append(case)
        scenarios = {label: {"count": len(cs), "first_out": min(c.ended for c in cs), "last_out": max(c.ended for c in cs),
                             "cycle_time": _mean(c.cycle_time for c in cs)} for label, cs in scenarios.items()}
        statuses = {s: sum(c.status == s for c in self.cases) for s in (COMPLETED, ERROR, LOOP_LIMIT)}
        return {"cases": {**statuses, "other": len(self.cases) - sum(statuses.values())}, "nodes": nodes,
                "resources": resources, "scenarios": scenarios}


def _mean(values):
    values = [v for v in values if v is not None]
    return float(np.mean(values)) if values else None


def _average(tables, count=("visits", "count", "cost")):
    """Rows averaged over replications; `count` fields average over every run (absent = 0)."""
    rows = {}
    for table in tables:
        for key, row in table.items():
            rows.setdefault(key, []).append(row)
    return {key: {f: (sum(r.get(f) or 0 for r in rs) / len(tables) if f in count else _mean(r.get(f) for r in rs))
                  for f in rs[0] if not isinstance(rs[0][f], list)} for key, rs in rows.items()}


def simulate(model: SimModel, replications=None, seed=None):
    spec = model.spec
    seed = spec.seed if seed is None else seed
    runs = [Run(model, r, seed) for r in range(replications or spec.replications)]
    tables = [run.tables() for run in runs]
    resources = _average([t["resources"] for t in tables])
    for r, row in resources.items():
        row["over_time"] = tables[0]["resources"][r]["over_time"]  # from the first replication
    return {"unit": spec.unit, "replications": len(runs), "seed": seed, "warnings": list(model.warnings),
            "metrics": stats.replicated([run.headline for run in runs]),
            "cases": {k: _mean(t["cases"][k] for t in tables) for k in tables[0]["cases"]},
            "nodes": _average([t["nodes"] for t in tables]), "resources": resources,
            "scenarios": _average([t["scenarios"] for t in tables]),
            # every completed case of every replication
            "cycle_time": stats.summary(c.cycle_time for run in runs for c in run.done),
            "cycle_time_histogram": stats.histogram([c.cycle_time for run in runs for c in run.done]),
            "waiting_time": stats.summary(c.waiting_time for run in runs for c in run.done),
            "case_cost": stats.summary(run.case_cost(c) for run in runs for c in run.done)}


def compare(a: SimModel, b: SimModel, replications=None, seed=None):
    """Two alternatives on common random numbers: each one's metrics and the paired difference b − a."""
    seed = a.spec.seed if seed is None else seed
    n = replications or a.spec.replications
    ra = [Run(a, r, seed).headline for r in range(n)]
    rb = [Run(b, r, seed).headline for r in range(n)]
    return {"replications": n, "seed": seed, "a": stats.replicated(ra), "b": stats.replicated(rb),
            "difference": stats.paired(ra, rb)}
