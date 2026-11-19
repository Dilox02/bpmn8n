"""E10, functional simulation: cases that carry data. `simulate(model, functional=True)` gives each simulated case
start data (generated from the process contract's types, from the spec's `functional.start` generators, or
drawn from recorded cases), and the steps work on it: a task runs the real @task function on the case's
variables, a conditions gateway evaluates its conditions on them. The branches and the outcome shares then
come out of the rules (a policy, a threshold), not from probabilities written in the spec. What the data
cannot decide keeps the spec's sampled choice: a Laya gateway, an LLM agent, a person (what they add comes
from `functional.outputs` when given), and a conditions gateway that reads a variable nobody produced.
Times and queues stay as sampled.

Django: it runs the project's tasks and the contract. The tasks run for real, as in the scenario runner
(workflows/scenarios.py): what they write is rolled back and their mail stays in memory, so a simulation leaves
no trace. ponytail: capped at MAX_CASES cases per call (each runs real code); beyond, simulate without it or
sample fewer cases."""
import dataclasses
from collections import Counter
from contextlib import contextmanager

from django.db import transaction
from django.test.utils import override_settings

from ..api.tasks import as_data
from ..core.variables import ConditionError
from ..workflows.contract import Contract
from ..workflows.scenarios import Play, ScenarioError
from .des import Sampled, _rng
from .spec import Distribution, FunctionalSpec, SpecError

MAX_CASES = 2000  # cases (over all replications) a functional simulation runs: every one runs real task code
NUMBER_RANGE = (0.0, 100.0)  # a number the contract types but nothing else describes: uniform here


@contextmanager
def sandbox():
    """The tasks run for real: their writes are rolled back and their mail goes nowhere."""
    with transaction.atomic(), override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
        yield
        transaction.set_rollback(True)


def draw(rng, generator):
    """One value of a generator: a list is choices, a dict with a `type` a Distribution (a number), else a constant."""
    if isinstance(generator, list):
        return generator[int(rng.integers(len(generator)))]
    if isinstance(generator, dict) and "type" in generator:
        return float(Distribution.model_validate(generator).sample(rng, 1)[0])
    return generator


def from_schema(rng, schema):
    """A value that fits one property of the contract's start schema."""
    if "default" in schema:  # what the task takes when it is not given (attendees: 1)
        return schema["default"]
    if schema.get("enum"):
        return schema["enum"][int(rng.integers(len(schema["enum"])))]
    kind = schema.get("type")
    if kind == "boolean":
        return bool(rng.random() < 0.5)
    if kind in ("number", "integer"):
        value = float(rng.uniform(*NUMBER_RANGE))
        return round(value) if kind == "integer" else round(value, 2)
    return "sample"


class Functional(Sampled):
    """The sampled resolver, plus what the data decides. `failures` counts the tasks that raised (their
    case goes on without their result) and `fallbacks` the conditions that could not be evaluated."""

    def __init__(self, model, seed, replication):
        super().__init__(model, seed, replication)
        self.settings = model.spec.functional or FunctionalSpec()
        self.schema = Contract(model.graph.workflow).start_schema()["properties"]
        self.plays, self.failures, self.fallbacks, self.errors, self.count = {}, Counter(), Counter(), {}, 0
        self.rng = _rng(seed, replication, "start data")

    def new_case(self):
        """The start data of the next case: a recorded one, or generated field by field."""
        self.count += 1
        if self.settings.cases:
            return dict(self.settings.cases[int(self.rng.integers(len(self.settings.cases)))])
        data = {name: draw(self.rng, self.settings.start[name]) if name in self.settings.start else from_schema(self.rng, schema)
                for name, schema in self.schema.items()}
        return data | {k: draw(self.rng, g) for k, g in self.settings.start.items() if k not in data}

    def play(self, case):
        if case.id not in self.plays:
            self.plays[case.id] = Play(self.model.graph, {"start": case.data, "tasks": "run"})
        return self.plays[case.id]

    def resolve(self, visit):
        out = super().resolve(visit)  # the sampled time and choice
        node, g = visit.node, self.model.graph
        element, params = g.element(node), g.nodes[node].get("parameters") or {}
        play, data = self.play(visit.case), {}
        if (stub := self.settings.outputs.get(node)) is not None:
            data = stub
        elif element.human:  # nobody to ask: answers drawn by the form's field types, unless `outputs` says
            data = {f["name"]: from_schema(self.rng, {"type": {"boolean": "boolean", "number": "number"}.get(f.get("type")),
                                                      "enum": f.get("options") or None})
                    for f in (params.get("form") or {}).get("fields", []) if f.get("name")}
        elif element.task:
            try:
                data = as_data(play.run(node, params)) or {}
            except ScenarioError as e:
                self.failures[node] += 1
                self.errors[node] = str(e)
        elif element.chooses and params.get("decider") == "conditions":
            try:
                taken = play.conditions(node, element, params)
            except (ScenarioError, ConditionError):
                taken = None
            if taken is None:  # an n8n expression, or a variable nobody has: the spec's sampled choice stands
                self.fallbacks[node] += 1
            else:
                out = dataclasses.replace(out, outputs=taken)
        play.variables.update(data if isinstance(data, dict) else {})
        return dataclasses.replace(out, data=data if isinstance(data, dict) else {})

    def report(self):
        return {"cases": self.count, "task_failures": dict(self.failures), "condition_fallbacks": dict(self.fallbacks),
                "errors": self.errors}


def check(model, replications):
    """The cap on a functional run."""
    cases = replications * model.spec.arrivals.count if model.spec.arrivals.count else None
    if cases is None or cases > MAX_CASES:
        raise SpecError(f"a functional simulation runs the real tasks: give the arrivals a count and keep "
                        f"replications × cases at {MAX_CASES} or fewer"
                        + (f" (this is {cases})" if cases else ""))
