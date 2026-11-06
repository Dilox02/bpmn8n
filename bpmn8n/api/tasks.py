"""@task: a Python function becomes a BPMN task. POST /api/tasks/<name> takes its input as is
(any API client); POST /api/tasks/<name>/run is what the n8n nodes call: the input comes from the
case's process variables (by name, or {{templates}}), optionally extracted from text, and the
result is remembered as process data."""
import inspect
import json
import logging
import math
import pkgutil
import traceback
import warnings
from functools import wraps
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pydantic
from django.apps import apps
from django.conf import settings
from django.http import HttpRequest
from django.utils.module_loading import module_has_submodule
from ninja import Router, Schema

from ..clients.extraction import Extractor, field_specs
from ..core.cases import CaseData
from ..core.observability import measure, profiled, record
from ..workflows.builder import TASK_KINDS
from .errors import upstream

# Sharp, "Workflow Modeling": these verbs name no countable result ("no result, no process").
MUSHY_VERBS = {"manage", "handle", "process", "maintain", "administer", "improve", "facilitate", "track",
               "analyze", "coordinate", "do", "support", "drive", "review", "monitor"}

log = logging.getLogger("bpmn8n")
TASKS = {}  # name -> Task: read by the n8n nodes (via OpenAPI), /docs/, the workflow check and Studio
router = Router()


def is_mushy(name):
    return name.split("_")[0] in MUSHY_VERBS


class RunIn(Schema):
    fields: dict[str, Any] = {}  # set on the node, {{variable}} templates allowed; empty ones are skipped
    item: dict[str, Any] = {}  # the n8n item that reached the node: it becomes process data
    text: str | None = None  # extract the input from this text (GLiNER2.5 + Laya)
    min_confidence: float = 0.5  # confidence below which an extracted field counts as not found
    verify: bool = False  # Laya double-checks GLiNER's values; off: no gain measured (H11, README)


class RunOut(Schema):
    result: Any
    extracted: dict[str, dict] = {}  # field -> {"value", "confidence", "verified"?}


class TaskError(TypeError):
    """A @task returned something its declared output Schema does not allow."""


class Task:
    """A @task function: its BPMN kind, its input Schema, its output Schema when the function
    declares one (`-> MyResult`: documented in OpenAPI, checked on every call, and the workflow
    check knows what it produces), and its two endpoints."""

    def __init__(self, fn, kind):
        self.fn, self.name, self.kind = fn, fn.__name__, kind
        signature = inspect.signature(fn)
        schemas = [p.annotation for p in signature.parameters.values() if is_schema(p.annotation)]
        if len(schemas) != 1:
            raise TypeError(f"@task {self.name}: take the input as one Schema argument, "
                            f"e.g. def {self.name}(request, data: MyInput)")
        self.schema = schemas[0]
        self.output = signature.return_annotation if is_schema(signature.return_annotation) else None
        self.run = profiled(self.recorded(fn))

    @property
    def description(self):
        return inspect.getdoc(self.fn) or ""

    @property
    def module(self):
        return self.fn.__module__

    @property
    def location(self):
        """Where the function is written, path:line from the repository root (n8n's node panel shows it)."""
        try:
            path, line = Path(inspect.getsourcefile(self.fn)), inspect.getsourcelines(self.fn)[1]
        except (OSError, TypeError):  # no source file
            return self.module
        root = Path(settings.BASE_DIR)
        return f"{path.relative_to(root) if path.is_relative_to(root) else path}:{line}"

    @property
    def examples(self):
        """Its examples from its project's examples.json (next to tasks.py or to the tasks/ package)."""
        try:
            here = Path(inspect.getsourcefile(self.fn)).parent
        except TypeError:
            return []
        for folder in (here, here.parent):
            if (path := folder / "examples.json").exists():
                try:
                    return json.loads(path.read_text()).get(self.name, [])
                except ValueError:
                    return []
        return []

    @property
    def inputs(self):
        return field_specs(self.schema)

    @property
    def outputs(self):
        return field_specs(self.output) if self.output else ()

    def recorded(self, fn):
        """fn, with its result checked and, with its input, kept in the event log."""
        @wraps(fn)
        def call(request, data):
            result = self.validated(fn(request, data))
            record(input=data.model_dump(mode="json"), output=as_data(result))
            return result
        return call

    def validated(self, result):
        """The result, checked against the output Schema when one is declared."""
        if self.output is None or isinstance(result, self.output):
            return result
        try:
            return self.output.model_validate(as_data(result))
        except pydantic.ValidationError as e:
            raise TaskError(f"@task {self.name} returned {as_data(result)!r}, not a {self.output.__name__}: {e}") from e

    def check(self, example):
        """None if, given example["input"], the task returns at least example["output"]'s fields with
        their values (numbers to the cent); else what went wrong. Examples are a project's tests
        (examples.json): they run the function itself, outside any case."""
        given, expected = example.get("input", {}), example.get("output", {})
        try:
            result = as_data(self.validated(self.fn(HttpRequest(), self.schema(**given))))
        except Exception:  # whatever the code does wrong is the finding
            return f"input {given}: {traceback.format_exc(limit=-3).strip()}"
        result = result if isinstance(result, dict) else {"result": result}
        if wrong := {k: result.get(k) for k, v in expected.items() if not same(result.get(k), v)}:
            return f"input {given}: returned {wrong}, expected {({k: expected[k] for k in wrong})}"
        return None

    def register(self, router, **options):
        router.post(f"/{self.name}", operation_id=self.name, tags=[self.kind], openapi_extra={"x-bpmn-task": self.kind, "x-bpmn-source": self.location,
                                                                       "x-bpmn-examples": self.examples},
                    **({"response": self.output} if self.output else {}), **options)(self.run)

        def run(request, data: RunIn):  # ninja tags the view function: no bound methods
            return self.run_in_case(request, data)

        router.post(f"/{self.name}/run", operation_id=f"{self.name}_run", tags=[self.kind],
                    response={200: RunOut, 422: dict})(run)

    def run_in_case(self, request, data: RunIn):
        case = CaseData(request, data.item)
        extracted = {}
        if data.text is not None:
            with upstream(), measure(f"extract:{self.name}"):
                extracted = Extractor().extract(self.schema, case.text(data.text), data.min_confidence, data.verify)
        values = case.bind(self.schema, data.fields, {name: hit["value"] for name, hit in extracted.items()})
        try:
            payload = self.schema(**values)
        except pydantic.ValidationError as e:
            problems = {".".join(map(str, err["loc"])): err["msg"] for err in e.errors()}
            return 422, {"detail": f"cannot run {self.name}: {problems}", "problems": problems, "extracted": extracted}
        result = self.run(request, payload)
        case.remember(as_data(result))
        return 200, {"result": result, "extracted": extracted}


def is_schema(annotation):
    return isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel)


def as_data(result):
    return result.model_dump(mode="json") if isinstance(result, pydantic.BaseModel) else result


def same(actual, expected):
    numbers = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (actual, expected))
    return math.isclose(actual, expected, abs_tol=0.005) if numbers else actual == expected


def task(kind="serviceTask", **options):
    """Registers fn as a BPMN task of `kind` (see Task). Name it verb_noun in the singular
    (issue_refund, not handle_refunds); its argument is a Schema; `options` go to ninja."""
    if kind not in TASK_KINDS:
        raise ValueError(f"kind must be one of {TASK_KINDS}")

    def register(fn):
        if fn.__name__ in TASKS:
            raise ValueError(f"task {fn.__name__!r} is already registered by {TASKS[fn.__name__].module}")
        if is_mushy(fn.__name__):
            warnings.warn(f"task {fn.__name__!r}: mushy verb, name it after a countable result", stacklevel=2)
        TASKS[fn.__name__] = Task(fn, kind)
        TASKS[fn.__name__].register(router, **options)
        return fn

    return register


def task_modules():
    """The modules that register @tasks: each app's tasks.py, or its tasks/ package and every module
    in it (one activity per module, as bpmn8n Studio writes them)."""
    for app in apps.get_app_configs():
        if module_has_submodule(app.module, "tasks"):
            spec = find_spec(f"{app.name}.tasks")
            yield spec.name
            yield from (f"{spec.name}.{m.name}" for m in pkgutil.iter_modules(spec.submodule_search_locations or ()))


def discover():
    """Imports every module that registers @tasks (task_modules). One that fails is logged and left out,
    so that a broken project does not take the others down; returns {module: its traceback's end}."""
    failed = {}
    for name in task_modules():
        try:
            import_module(name)
        except Exception:  # whatever the code does wrong
            failed[name] = traceback.format_exc(limit=-3).strip()
            log.exception("the tasks of %s are not loaded", name)
    return failed
