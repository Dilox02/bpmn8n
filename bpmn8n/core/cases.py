"""The case a request belongs to, with its process variables (rules in variables.py), and the search over
cases (GET /api/cases, /cases/)."""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db.models import Case as Switch
from django.db.models import Exists, OuterRef, Q, Subquery, Value, When
from django.utils import timezone

from ..models import Case, Event, MessageSubscription, UserTask
from . import history, shared
from .observability import Trace
from .variables import (
    GLOBALS,
    MISSING,
    Condition,
    Globals,
    bind,
    from_item,
    global_keys,
    lookup,
    render,
)


class CaseData:
    """The variables of the case a request belongs to, including the data of the n8n item that
    reached the node. A direct API call (no case) only has the data it brings. Templates may also
    read global variables ({{vars.<key>}}): fetched when a template names them, never stored in the case."""

    def __init__(self, request, item=None):
        self.trace = Trace.of(request)
        brought = from_item(item or {})
        self.variables = brought
        if self.trace.case_id:
            self.variables, created = Case.upsert(self.trace.case_id, self.trace.process, brought)
            if created:  # which version of the workflow it runs: outside the row lock, cached per workflow
                history.stamp(self.trace.case_id)

    def _merge(self, data):
        return Case.merge(self.trace.case_id, self.trace.process, data)

    def scope(self, value):
        """The variables `value`'s templates read: the case's, plus the global ones it names under `vars`."""
        keys = global_keys(value)
        if keys is None:
            return self.variables
        return {**self.variables, GLOBALS: Globals(shared.store().get_many(k for k in keys if shared.KEY.fullmatch(k)))}

    def render(self, value):
        return render(value, self.scope(value))

    def text(self, value):
        """Like render, for a value that must be text: an unknown lone variable is ""."""
        rendered = self.render(value)
        return "" if rendered is MISSING else rendered

    def bind(self, schema, given, extracted=None):
        return bind(schema, given, self.scope(given), extracted)

    def holds(self, condition):
        return Condition.holds(condition, self.scope(condition))

    def remember(self, data):
        """A step's result becomes process data."""
        if isinstance(data, dict) and data:
            self.variables = self._merge(data) if self.trace.case_id else {**self.variables, **data}


QUIET = timedelta(minutes=1)  # as mining's closed cases: nothing open and no event for this long
STATUSES = ("waiting", "error", "active", "closed")
OPERATORS = ("gt", "gte", "lt", "lte", "contains")
PATH = re.compile(r"[\w-]+(?:\.[\w-]+)*")


class SearchError(ValueError):
    pass


@dataclass
class CaseSearch:
    """Cases by process, workflow version, start time, status and the values of their variables: n8n's
    paid advanced execution filters and custom execution data, on bpmn8n's case table.

    status: waiting (a person or a message is awaited) · error (its last step failed) · active (a step ran
    in the last minute, or runs now) · closed. A variable criterion is (path, operator, value): path in
    dots ("customer.email"), operator "" (equals), gt, gte, lt, lte or contains (text, ignoring case);
    the value is read as JSON when it parses (1000, true, null, "quoted text"), else as text."""

    process: str = ""
    version: str = ""
    since: datetime | None = None
    until: datetime | None = None
    status: str = ""
    variables: list = field(default_factory=list)

    MAX_CRITERIA = 10

    @classmethod
    def criteria(cls, params):
        """The (path, operator, value) of the `var.<path>[__<operator>]=<value>` query parameters."""
        found = []
        for name, values in params.lists():
            if name.startswith("var."):
                path, _, op = name[4:].rpartition("__") if name.rpartition("__")[2] in OPERATORS else (name[4:], "", "")
                found += [cls.criterion(path, op, v) for v in values]
        return found

    @staticmethod
    def criterion(path, op, value):
        if not PATH.fullmatch(path) or op not in ("", *OPERATORS):
            raise SearchError(f"{path!r} {op}: write var.<path>[__gt|__gte|__lt|__lte|__contains]")
        return path, op, value

    def cases(self):
        if self.status and self.status not in STATUSES:
            raise SearchError(f"status: one of {', '.join(STATUSES)}")
        if len(self.variables) > self.MAX_CRITERIA:
            raise SearchError(f"at most {self.MAX_CRITERIA} variable criteria")
        events = Event.objects.filter(case_id=OuterRef("case_id"))
        last = events.exclude(status="running").order_by("-ended", "-id")
        cases = Case.objects.annotate(
            last_event=Subquery(last.values("ended")[:1]), last_status=Subquery(last.values("status")[:1]),
            waits_person=Exists(UserTask.objects.waiting().filter(case_id=OuterRef("case_id"))),
            waits_message=Exists(MessageSubscription.objects.waiting().filter(case_id=OuterRef("case_id"))),
            runs=Exists(events.filter(status="running")),
        ).annotate(status=Switch(
            When(Q(waits_person=True) | Q(waits_message=True), then=Value("waiting")),
            When(last_status="error", then=Value("error")),
            When(Q(runs=True) | Q(last_event__gte=timezone.now() - QUIET), then=Value("active")),
            default=Value("closed")))
        for name, value in (("process", self.process), ("process_version", self.version),
                            ("created__gte", self.since), ("created__lte", self.until), ("status", self.status)):
            if value:
                cases = cases.filter(**{name: value})
        for path, op, raw in self.variables:
            lookup_name = "__".join(("variables", *path.split("."), *(("icontains",) if op == "contains" else (op,) if op else ())))
            cases = cases.filter(**{lookup_name: raw if op == "contains" else as_value(raw)})
        return cases.order_by("-created", "-case_id")

    def matched(self, case):
        """{path: value} of the variables the criteria name, for one case."""
        return {path: (None if (v := lookup(case.variables, path)) is MISSING else v) for path, _, _ in self.variables}


def as_value(raw):
    """A query value as JSON when it parses (numbers, true, false, null, "text"), else as text."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw
