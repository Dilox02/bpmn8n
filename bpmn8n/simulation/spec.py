"""What a simulation needs besides the workflow: `projects/<p>/simulation.json`, next to workflow.json.
Its sections follow the course's process simulation specification ([BS] p.83–85):

    PSS  arrivals    how cases arrive: a peak of N at once, one every interval, Poisson, a list
    RES  resources   capacity (people), cost per hour, the work schedule they follow
    WS   schedules   working days and hours
    TSK  tasks       per step (any node, by name): duration distribution, cost per execution, resource
    GTW  gateways    per choice: the probability of each output

plus the unit of time (minutes by default), replications, seed, horizon and warm-up. What the spec
leaves out comes from the element (`SimModel`): human tasks are done by their lane, timers wait for
their parameters, automated steps take no time and wait for nobody, so any workflow can be simulated
without a spec. Pure Python, no Django."""
import copy
import itertools
import json
import math
import re
from pathlib import Path
from typing import Any, ClassVar, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..engine import Schedule
from ..workflows.graph import ProcessGraph

UNITS = {"seconds": 1 / 60, "minutes": 1.0, "hours": 60.0, "days": 1440.0, "weeks": 10080.0}  # in minutes
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# what an element takes when the spec says nothing, in minutes (an exponential: people vary)
HUMAN_MINUTES, MESSAGE_MINUTES = 10.0, 60.0
OUTPUT_TOKENS = 300  # an LLM agent's answer, when nothing observed says otherwise


class SpecError(ValueError):
    """A simulation spec that does not fit its workflow."""


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Distribution(Strict):
    """A time in the spec's unit. A bare number is a fixed time. fixed: `value`; exponential: `mean`;
    normal, lognormal, gamma: `mean` and `sd` (of the time itself, not of its log); triangular: `min`,
    `mode`, `max`; uniform: `min`, `max`; empirical: `values`, one drawn at random."""

    type: Literal["fixed", "exponential", "normal", "lognormal", "gamma", "triangular", "uniform", "empirical"] = "fixed"
    value: float | None = None
    mean: float | None = None
    sd: float | None = None
    min: float | None = None
    mode: float | None = None
    max: float | None = None
    values: list[float] | None = None

    NEEDS: ClassVar[dict] = {"fixed": ("value",), "exponential": ("mean",), "normal": ("mean", "sd"), "lognormal": ("mean", "sd"),
             "gamma": ("mean", "sd"), "triangular": ("min", "mode", "max"), "uniform": ("min", "max"),
             "empirical": ("values",)}

    @model_validator(mode="before")
    @classmethod
    def _number(cls, value):
        return {"type": "fixed", "value": value} if isinstance(value, (int, float)) and not isinstance(value, bool) else value

    @model_validator(mode="after")
    def _check(self):
        if missing := [k for k in self.NEEDS[self.type] if getattr(self, k) in (None, [])]:
            raise ValueError(f"a {self.type} distribution needs {', '.join(missing)}")
        numbers = [v for v in (self.value, self.mean, self.sd, self.min, self.mode, self.max, *(self.values or []))
                   if v is not None]
        if any(v < 0 or not math.isfinite(v) for v in numbers):
            raise ValueError("times must be finite and not negative")
        if self.type in ("lognormal", "gamma") and self.mean == 0:
            raise ValueError(f"a {self.type} distribution needs a positive mean")
        if self.type in ("triangular", "uniform") and not self.min <= (self.mode if self.type == "triangular" else self.min) <= self.max:
            raise ValueError("needs min <= mode <= max")
        return self

    def expected(self):
        t = self.type
        if t == "fixed":
            return self.value
        if t in ("exponential", "normal", "lognormal", "gamma"):
            return self.mean
        if t == "triangular":
            return (self.min + self.mode + self.max) / 3
        if t == "uniform":
            return (self.min + self.max) / 2
        return float(np.mean(self.values))

    def variance(self):
        t = self.type
        if t == "fixed":
            return 0.0
        if t == "exponential":
            return self.mean ** 2
        if t in ("normal", "lognormal", "gamma"):
            return self.sd ** 2
        if t == "triangular":
            a, c, b = self.min, self.mode, self.max
            return (a * a + b * b + c * c - a * b - a * c - b * c) / 18
        if t == "uniform":
            return (self.max - self.min) ** 2 / 12
        return float(np.var(self.values))

    def scv(self):
        """Squared coefficient of variation var/mean² (0 fixed, 1 exponential)."""
        mean = self.expected()
        return self.variance() / mean ** 2 if mean else 0.0

    def sample(self, rng, size):
        t = self.type
        if t == "fixed":
            return np.full(size, float(self.value))
        if t == "exponential":
            return rng.exponential(self.mean, size)
        if t == "normal":  # ponytail: truncated at 0 by clipping, so a wide normal's mean drifts up
            return np.maximum(rng.normal(self.mean, self.sd, size), 0.0)
        if t == "lognormal":
            sigma2 = math.log1p((self.sd / self.mean) ** 2)
            return rng.lognormal(math.log(self.mean) - sigma2 / 2, math.sqrt(sigma2), size)
        if t == "gamma":
            if self.sd == 0:
                return np.full(size, float(self.mean))
            return rng.gamma(self.mean ** 2 / self.sd ** 2, self.sd ** 2 / self.mean, size)
        if t == "triangular":
            return rng.triangular(self.min, self.mode, self.max, size) if self.max > self.min else np.full(size, self.min)
        if t == "uniform":
            return rng.uniform(self.min, self.max, size)
        return rng.choice(np.asarray(self.values, dtype=float), size)


class Arrivals(Strict):
    """How cases arrive (PSS): `peak` = `count` cases at once at `start`; `interval` = one every `every`
    (a time, or a distribution of the gaps: the course's F is a fixed one); `poisson` = `rate` cases per
    unit (exponential gaps); `list` = at the given `times`. Up to `count` cases, or until the horizon."""

    type: Literal["peak", "interval", "poisson", "list"] = "poisson"
    count: int | None = Field(None, ge=0)
    every: Distribution | None = None
    rate: float | None = Field(None, gt=0)
    times: list[float] | None = None
    start: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def _check(self):
        needs = {"peak": "count", "interval": "every", "poisson": "rate", "list": "times"}[self.type]
        if getattr(self, needs) is None:
            raise ValueError(f"{self.type} arrivals need {needs}")
        if self.type == "interval" and self.every.expected() == 0:
            raise ValueError("interval arrivals need a positive gap")
        return self

    def gaps(self):
        """The inter-arrival distribution (None for a peak or a list)."""
        if self.type == "poisson":
            return Distribution(type="exponential", mean=1 / self.rate)
        return self.every if self.type == "interval" else None

    def rate_and_scv(self):
        """(λ, ca²) of a steady stream of cases; None for a peak (no steady state)."""
        if self.type == "list":
            times = sorted(self.times)
            if len(times) < 2 or times[-1] == times[0]:
                return None
            gaps = np.diff(times)
            return (len(times) - 1) / (times[-1] - times[0]), float(np.var(gaps, ddof=1) / np.mean(gaps) ** 2) if len(gaps) > 1 else 0.0
        gaps = self.gaps()
        return None if gaps is None else (1 / gaps.expected(), gaps.scv())

    def arrival_times(self, rng, horizon=None):
        end = math.inf if horizon is None else horizon
        if self.type == "list":
            return sorted(t for t in self.times if t <= end)[:self.count]
        if self.type == "peak":
            return [self.start] * self.count if self.start <= end else []
        if self.count is None and horizon is None:
            raise SpecError(f"{self.type} arrivals need a count or a horizon")
        times, t, gaps = [], self.start, self.gaps()
        while t <= end and (self.count is None or len(times) < self.count):
            times.append(t)
            t += float(gaps.sample(rng, 1)[0])
        return times


class WorkSchedule(Strict):
    """WS: the days (mon … sun) and the hours [start, end) a resource works ("09:00" or 9)."""

    days: list[Literal[DAYS]] = list(DAYS)
    start: float = 0.0
    end: float = 24.0

    @field_validator("start", "end", mode="before")
    @classmethod
    def _hours(cls, value):
        if isinstance(value, str) and (m := re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", value.strip())):
            h, mi, s = int(m[1]), int(m[2]), int(m[3] or 0)
            return 24.0 if (h, mi, s) == (23, 59, 59) else h + mi / 60 + s / 3600  # the course's end of day
        return value

    @model_validator(mode="after")
    def _check(self):
        if not 0 <= self.start < self.end <= 24:
            raise ValueError("a working day needs 0 <= start < end <= 24")
        return self


class Resource(Strict):
    """RES: `capacity` people or servers working in parallel (null: unlimited), their `cost_per_hour`
    (paid for the time they work), the `schedule` they follow (a key of `schedules`)."""

    capacity: int | None = Field(1, ge=1)
    cost_per_hour: float = Field(0.0, ge=0)
    schedule: str | None = None


class Task(Strict):
    """TSK: one step (any node, by its name): how long it takes, what one execution costs, who does it
    (a key of `resources`; "" = nobody: a delay), its priority in the resource's queue (lower first).
    For an event-based gateway the duration is the time until the message comes."""

    duration: Distribution | None = None
    cost: float = Field(0.0, ge=0)
    cost_per_call: float | None = Field(None, ge=0)  # an LLM agent's model cost per call; empty: the provider's prices
    resource: str | None = None
    priority: int = 0


class FunctionalSpec(Strict):
    """E10, what a functional simulation (`simulate(..., functional=True)`) needs besides the workflow. `start`:
    a generator per start-data field: a constant, a list of choices, or a distribution (`{"type": "lognormal",
    "mean": 80, "sd": 120}`); the fields it leaves out are generated from the process contract's types.
    `cases`: recorded start data to draw from instead (calibrate.recorded_cases). `outputs`: what a step adds
    instead of running (a person's answers, a Laya gateway's or an LLM agent's result, a task not to run)."""

    start: dict[str, Any] = {}
    cases: list[dict[str, Any]] = []
    outputs: dict[str, dict[str, Any]] = {}


class SimSpec(Strict):
    unit: Literal[tuple(UNITS)] = "minutes"
    workflow: str | None = None  # relative to the project folder; default workflow.json
    arrivals: Arrivals | None = None  # default: one case an hour, 100 cases
    resources: dict[str, Resource] = {}
    schedules: dict[str, WorkSchedule] = {}
    tasks: dict[str, Task] = {}
    gateways: dict[str, dict[str, float]] = {}  # node -> {output: probability}
    functional: FunctionalSpec | None = None  # E10: start data and stubs of a functional simulation
    replications: int = Field(10, ge=1)
    seed: int = 0
    horizon: float | None = Field(None, gt=0)  # stop the clock here; unfinished cases are not counted
    warmup: float = Field(0.0, ge=0)  # cases that arrive before it are left out of the statistics
    visit_limit: int = Field(100, ge=1)  # per case and node: a loop that never ends stops there

    @model_validator(mode="after")
    def _defaults(self):
        if self.arrivals is None:
            self.arrivals = Arrivals(type="poisson", rate=UNITS[self.unit] / 60, count=100)
        if unknown := {r.schedule for r in self.resources.values() if r.schedule} - set(self.schedules):
            raise ValueError(f"no schedule named {', '.join(sorted(unknown))}")
        return self

    def minutes(self, amount, unit):
        """`amount` of `unit` (seconds … weeks) in the spec's unit."""
        return float(amount) * UNITS[unit] / UNITS[self.unit]


def load(folder):
    """(workflow, spec) of a project folder: simulation.json (optional) and the workflow it names
    (workflow.json by default)."""
    folder = Path(folder)
    path = folder / "simulation.json"
    spec = SimSpec.model_validate_json(path.read_text()) if path.exists() else SimSpec()
    workflow = folder / (spec.workflow or "workflow.json")
    if not workflow.exists():
        raise SpecError(f"{folder}: no {spec.workflow or 'workflow.json'}")
    return json.loads(workflow.read_text()), spec


class SimModel:
    """A SimSpec applied to a workflow: for every step its duration, cost, resource and, for a choice, its
    options [(outputs taken, probability)]. Defaults by element kind fill what the spec leaves out."""

    def __init__(self, workflow, spec=None, llm=None):
        """llm: the default LLM provider's prices, {"cost_per_1k_input", "cost_per_1k_output"} (E9): what an
        LLM agent's call costs when its task gives no cost_per_call."""
        self.graph = workflow if isinstance(workflow, ProcessGraph) else ProcessGraph(workflow)
        self.spec = spec = spec or SimSpec()
        g = self.graph
        if unknown := (set(spec.tasks) | set(spec.gateways) | set(spec.functional.outputs if spec.functional else ())) \
                - set(g.flow_nodes):
            raise SpecError(f"the workflow has no step named {', '.join(sorted(unknown))}")
        self.warnings, self.duration, self.cost, self.resource, self.priority = [], {}, {}, {}, {}
        self.model_cost = {}  # per call of an LLM agent: what the model costs, apart from people and fixed costs
        self.options, self.timeout = {}, {}
        for node in g.flow_nodes:
            element, params, task = g.element(node), g.nodes[node].get("parameters") or {}, spec.tasks.get(node)
            if element.timeout_first:
                self.timeout[node] = self._wait(node, params)
            self.duration[node] = task.duration if task and task.duration else self._default(node, element, params)
            self.cost[node] = task.cost if task else 0.0
            self.model_cost[node] = self._call_cost(node, element, task, llm)
            self.priority[node] = task.priority if task else 0
            lane = (params.get("lane") or "people") if element.human else None
            self.resource[node] = (task.resource if task and task.resource is not None else lane) or None
            if element.chooses:
                self.options[node] = self._options(node, element, spec.gateways.get(node, {}))
            elif node in spec.gateways:
                raise SpecError(f"{node} does not choose: it takes no probabilities")
        used = {r for r in self.resource.values() if r}
        self.capacity = {r: (spec.resources[r].capacity if r in spec.resources else 1) or math.inf
                         for r in used | set(spec.resources)}
        self.cost_per_hour = {r: spec.resources[r].cost_per_hour if r in spec.resources else 0.0 for r in self.capacity}
        day = UNITS["days"] / UNITS[spec.unit]
        self.schedules = {r: Schedule(frozenset(DAYS.index(d) for d in ws.days), ws.start, ws.end, day)
                          for r, res in spec.resources.items() if res.schedule
                          for ws in [spec.schedules[res.schedule]]}

    def _call_cost(self, node, element, task, llm):
        if element.kind != "llmAgent":
            return 0.0
        if task and task.cost_per_call is not None:
            return task.cost_per_call
        if not llm:
            return 0.0
        g = self.graph  # ponytail: the prompt's length over 4 characters a token; the answer is OUTPUT_TOKENS
        prompt = (((g.nodes.get(f"{node}: prompt") or {}).get("parameters") or {}).get("input") or {}).get("value") or {}
        system = ((g.nodes[node].get("parameters") or {}).get("options") or {}).get("systemMessage") or ""
        sent = (len(str(prompt.get("prompt", ""))) + len(system)) / 4
        return (float(llm.get("cost_per_1k_input", 0)) * sent + float(llm.get("cost_per_1k_output", 0)) * OUTPUT_TOKENS) / 1000

    def _wait(self, node, params):
        if params.get("mode", "for") == "until":
            self.warnings.append(f"{node}: waits until a date the simulation cannot know; it takes no time")
            return 0.0
        return self.spec.minutes(params.get("amount", 1), params.get("unit", "hours"))

    def _default(self, node, element, params):
        def minutes(m):
            return self.spec.minutes(m, "minutes")

        if element.human:
            return Distribution(type="exponential", mean=minutes(HUMAN_MINUTES))
        if element.kind == "receiveTask":
            return Distribution(type="exponential", mean=minutes(MESSAGE_MINUTES))
        if element.kind == "timerEvent":
            return Distribution(value=self._wait(node, params))
        if element.timeout_first:  # the message comes at any moment before the timeout
            return Distribution(type="uniform", min=0, max=self.timeout[node])
        return Distribution(value=0)

    def _options(self, node, element, given):
        names = self.graph.outputs(node)
        if unknown := set(given) - set(names):
            raise SpecError(f"{node} has no output {', '.join(sorted(unknown))}, only {', '.join(names)}")
        if any(not 0 <= p <= 1 for p in given.values()):
            raise SpecError(f"{node}: probabilities go from 0 to 1")
        if element.kind == "inclusiveGateway":  # every branch applies or not on its own; none: default
            if "default" in given:
                raise SpecError(f"{node}: give each branch's probability; default is taken when none applies")
            branches = names[:-1]
            p = {b: given.get(b, 0.0 if given else 0.5) for b in branches}
            options = []
            for applies in itertools.product((True, False), repeat=len(branches)):
                taken = tuple(b for b, yes in zip(branches, applies) if yes) or ("default",)
                options.append((taken, math.prod(p[b] if yes else 1 - p[b] for b, yes in zip(branches, applies))))
        else:  # exclusive or event-based: one output; what the others leave goes to default / timeout
            rest = "default" if element.default_last else "timeout"
            if not given:
                named = [n for n in names if n != "default"] or names
                p = {n: 1 / len(named) if n in named else 0.0 for n in names}
            else:
                p = {n: given.get(n, 0.0) for n in names}
                if rest not in given:
                    p[rest] = 1 - sum(v for n, v in p.items() if n != rest)
            if abs(sum(p.values()) - 1) > 1e-9 or p[rest] < -1e-9:
                raise SpecError(f"{node}: the probabilities of its outputs must sum to 1, not {sum(given.values()):g}")
            options = [((n,), p[n]) for n in names]
        return [(taken, probability) for taken, probability in options if probability > 0]

    def mean_time(self, node):
        """The mean time of one visit (an event-based gateway: the timeout or the message, as likely)."""
        if node not in self.timeout:
            return self.duration[node].expected()
        message = min(self.duration[node].expected(), self.timeout[node])
        return sum(p * (self.timeout[node] if taken == ("timeout",) else message) for taken, p in self.options[node])

    @property
    def unit(self):
        return self.spec.unit

    def with_capacity(self, capacity):
        """The same model with other capacities ({resource: servers}): a what-if."""
        other = copy.copy(self)
        other.capacity = {**self.capacity, **capacity}
        return other
