"""Filters over cases, as Disco's (User Guide §5): each one turns a set of cases into a narrower one, and
a stack applies them in order, saying how many cases each step kept. A stack travels as JSON, in a query
string (`filters=[{"type": "timeframe", "mode": "started", "start": "2026-01-01"}, ...]`), so the API,
the Insights page and a saved link all speak the same language.

Times are seconds. Every filter keeps whole cases: none cuts events out of a case."""
import json
from collections import Counter
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from .log import duration_text

Names = Annotated[list[str], BeforeValidator(lambda v: [v] if isinstance(v, str) else v), Field(min_length=1)]
OptionalNames = Annotated[list[str], BeforeValidator(lambda v: [v] if isinstance(v, str) else v)]
Seconds = Annotated[float, Field(ge=0)]


class FilterError(ValueError):
    """A filter stack that cannot be read: the message says which filter and why."""


class LogFilter(BaseModel):
    """One filter: `keeps(trace)` decides for one case; `apply` for the whole set (variants need it)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)

    def apply(self, traces):
        return [t for t in traces if self.keeps(t)]

    def keeps(self, trace):
        raise NotImplementedError

    def as_json(self):
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def _bounds(low, high, unit=duration_text):
    if low is not None and high is not None:
        return f"{unit(low)} – {unit(high)}"
    return f"≥ {unit(low)}" if low is not None else f"≤ {unit(high)}"


class Timeframe(LogFilter):
    """Cases by when they ran: contained in the window, intersecting it, started or ended in it."""

    type: Literal["timeframe"]
    mode: Literal["contained", "intersecting", "started", "ended"] = "intersecting"
    start: datetime | None = None
    end: datetime | None = None

    @model_validator(mode="after")
    def check_window(self):
        if self.start is None and self.end is None:
            raise ValueError("a timeframe needs a start or an end")
        if self.start and self.end and self.start >= self.end:
            raise ValueError("the timeframe's start must come before its end")
        return self

    def keeps(self, t):
        lo = self.start.timestamp() if self.start else float("-inf")
        hi = self.end.timestamp() if self.end else float("inf")
        if self.mode == "contained":
            return lo <= t.start and t.end <= hi
        if self.mode == "intersecting":
            return t.start <= hi and t.end >= lo
        if self.mode == "started":
            return lo <= t.start < hi
        return not t.open and lo <= t.end < hi  # an open case has not ended

    @property
    def label(self):
        when = _bounds(self.start, self.end, lambda d: d.strftime("%Y-%m-%d %H:%M").removesuffix(" 00:00"))
        return f"{self.mode.capitalize()} {when}"


class Variants(LogFilter):
    """The cases of the most frequent variants: the top N, or as many as cover X % of the cases."""

    type: Literal["variants"]
    top: int | None = Field(None, ge=1)
    coverage: float | None = Field(None, gt=0, le=100)

    @model_validator(mode="after")
    def check_one(self):
        if (self.top is None) == (self.coverage is None):
            raise ValueError("give either top or coverage")
        return self

    def apply(self, traces):
        ranked = sorted(Counter(t.variant for t in traces).items(), key=lambda v: (-v[1], v[0]))
        if self.top:
            keep = {v for v, _ in ranked[:self.top]}
        else:
            keep, covered = set(), 0
            for variant, n in ranked:
                if covered * 100 >= self.coverage * len(traces):
                    break
                keep.add(variant)
                covered += n
        return [t for t in traces if t.variant in keep]

    @property
    def label(self):
        return f"Top {self.top} variant{'s' * (self.top > 1)}" if self.top else f"Variants covering {self.coverage:g} %"


class Performance(LogFilter):
    """Cases by how long they took (`duration`), or by the time from one activity's end to the next
    activity's start (`between`)."""

    type: Literal["performance"]
    measure: Literal["duration", "between"] = "duration"
    source: str | None = Field(None, alias="from")
    target: str | None = Field(None, alias="to")
    min: Seconds | None = None
    max: Seconds | None = None

    @model_validator(mode="after")
    def check_bounds(self):
        if self.min is None and self.max is None:
            raise ValueError("a performance filter needs min or max (seconds)")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        if self.measure == "between" and not (self.source and self.target):
            raise ValueError("'between' needs the activities 'from' and 'to'")
        return self

    def keeps(self, t):
        value = t.duration if self.measure == "duration" else t.gap(self.source, self.target)
        return value is not None and (self.min is None or value >= self.min) and (self.max is None or value <= self.max)

    @property
    def label(self):
        what = "Case duration" if self.measure == "duration" else f"{self.source} → {self.target}"
        return f"{what} {_bounds(self.min, self.max)}"


class Endpoints(LogFilter):
    """Cases that start with one of `start` and/or end with one of `end`."""

    type: Literal["endpoints"]
    start: OptionalNames = []
    end: OptionalNames = []

    @model_validator(mode="after")
    def check_some(self):
        if not self.start and not self.end:
            raise ValueError("give the start or the end activities")
        return self

    def keeps(self, t):
        return (not self.start or t.steps[0].activity in self.start) and \
               (not self.end or t.steps[-1].activity in self.end)

    @property
    def label(self):
        parts = [f"starts at {' or '.join(self.start)}"] * bool(self.start) + [f"ends at {' or '.join(self.end)}"] * bool(self.end)
        text = " and ".join(parts)
        return text[:1].upper() + text[1:]


class Attribute(LogFilter):
    """Cases that have (include) or do not have (exclude) an event with this activity, resource, status
    or process."""

    type: Literal["attribute"]
    key: Literal["activity", "resource", "status", "process"]
    values: Names
    mode: Literal["include", "exclude"] = "include"

    def keeps(self, t):
        if self.key == "process":
            has = t.process in self.values
        else:
            has = any(getattr(s, self.key) in self.values for s in t.steps)
        return has == (self.mode == "include")

    @property
    def label(self):
        return f"{'With' if self.mode == 'include' else 'Without'} {self.key} {' or '.join(self.values)}"


class Follower(LogFilter):
    """Cases where `from` is followed by `to`, directly (the next step) or eventually, optionally within
    a time (from the end of `from` to the start of `to`)."""

    type: Literal["follower"]
    source: str = Field(alias="from")
    target: str = Field(alias="to")
    relation: Literal["eventually", "directly"] = "eventually"
    within: Seconds | None = None
    mode: Literal["include", "exclude"] = "include"

    def keeps(self, t):
        steps = t.steps
        found = any(a.activity == self.source and b.activity == self.target
                    and (self.within is None or b.started - a.ended <= self.within)
                    for i, a in enumerate(steps)
                    for b in (steps[i + 1:i + 2] if self.relation == "directly" else steps[i + 1:]))
        return found == (self.mode == "include")

    @property
    def label(self):
        arrow = "→" if self.relation == "directly" else "⇢"
        within = f" within {duration_text(self.within)}" if self.within is not None else ""
        return f"{'' if self.mode == 'include' else 'Not '}{self.source} {arrow} {self.target}{within}"


FILTERS = (Timeframe, Variants, Performance, Endpoints, Attribute, Follower)
_stack = TypeAdapter(list[Annotated[Timeframe | Variants | Performance | Endpoints | Attribute | Follower,
                                    Field(discriminator="type")]])


class FilterStack:
    """Filters applied in order: each narrows what the one before kept."""

    def __init__(self, filters=()):
        self.filters = list(filters)

    @classmethod
    def parse(cls, value):
        """From JSON text (a list, or one filter as an object), or the parsed list; empty: no filter."""
        if value in (None, "", []):
            return cls()
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as e:
                raise FilterError(f"filters must be JSON: {e}") from None
        if isinstance(value, dict):
            value = [value]
        try:
            return cls(_stack.validate_python(value))
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())
            raise FilterError(f"invalid filters: {problems}") from None

    def apply(self, traces):
        steps = []
        for f in self.filters:
            before = len(traces)
            traces = f.apply(traces)
            steps.append({"label": f.label, "filter": f.as_json(), "before": before, "kept": len(traces)})
        return traces, steps

    def __len__(self):
        return len(self.filters)

    def __iter__(self):
        return iter(self.filters)

    def plus(self, f):
        return FilterStack([*self.filters, f])

    def without(self, index):
        return FilterStack(self.filters[:index] + self.filters[index + 1:])

    def as_json(self):
        return [f.as_json() for f in self.filters]

    def query(self):
        """The stack as a query-string value ("" when empty)."""
        return json.dumps(self.as_json(), separators=(",", ":")) if self.filters else ""
