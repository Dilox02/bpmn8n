"""The event log as the insights read it: one Trace per case, its steps in start order.

The database narrows the rows (process, workflow, cases started in a window); what needs a case's whole
sequence (variants, waits, handoffs, the filter stack) is computed here, from one streamed pass.
Times are epoch seconds (floats): the arithmetic is simpler and the output converts back to ISO 8601."""
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import groupby

from django.db.models import Min
from django.utils import timezone

from ..models import Event, MessageSubscription, UserTask

SETTLE = 60.0  # seconds: a case with nothing open and no event for this long has ended (as in process mining)
COLUMNS = ("case_id", "process", "activity", "started", "ended", "duration_ms", "status", "resource", "endpoint", "id")
HUMAN = frozenset(("userTask", "manualTask"))  # Event.endpoint of a person's step (UserTask.kind)
WAITS = frozenset(("receiveTask", "signalCatch"))  # Event.endpoint of a wait for an outside party (a message)
NOT_PEOPLE = frozenset(("", "system", "api"))  # resources that are not a person


@dataclass(frozen=True, slots=True)
class Step:
    """One event of a case."""

    activity: str
    started: float
    ended: float
    duration: float  # seconds, as measured (Event.duration_ms)
    status: str
    resource: str
    endpoint: str
    id: int

    @property
    def human(self):
        """A person did it: a User/Manual Task, or (an imported log) a resource that is a person's name."""
        return self.endpoint in HUMAN or not self.delay and self.resource not in NOT_PEOPLE

    @property
    def delay(self):
        """It waited for an outside party (a message): no servers here, nobody of the process works it."""
        return self.endpoint in WAITS or self.resource.startswith(("message:", "signal:"))


@dataclass(slots=True)
class Trace:
    """One case: its steps by start time, and whether it is still open (something waits, or it just moved)."""

    case_id: str
    process: str
    steps: list
    open: bool = False
    _variant: tuple = field(default=None, repr=False)

    @property
    def start(self):
        return self.steps[0].started

    @property
    def end(self):
        return max(s.ended for s in self.steps)

    @property
    def duration(self):
        return self.end - self.start

    @property
    def variant(self):
        if self._variant is None:
            self._variant = tuple(s.activity for s in self.steps)
        return self._variant

    def waits(self):
        """Per step, the time it waited: its start minus the end of the step before it (with parallel
        branches, of the latest step that had ended by then; 0 if none had). None for the first step."""
        out = [None] * len(self.steps)
        for i in range(1, len(self.steps)):
            started = self.steps[i].started
            ended = [p.ended for p in self.steps[:i] if p.ended <= started]
            out[i] = started - max(ended) if ended else 0.0
        return out

    def gap(self, source, target):
        """Seconds from the end of the first `source` to the start of the first `target` after it; None if
        the case does not have both in that order."""
        for i, s in enumerate(self.steps):
            if s.activity == source:
                for t in self.steps[i + 1:]:
                    if t.activity == target:
                        return t.started - s.ended
                return None
        return None


@dataclass(frozen=True)
class Scope:
    """What the database narrows: a process (workflow name), a workflow id (case ids "<id>:<execution>"),
    and the cases that started in [since, until)."""

    process: str | None = None
    workflow: str | None = None
    since: datetime | None = None
    until: datetime | None = None

    def events(self):
        events = Event.objects.cases()  # cases only, no running spans
        if self.process:
            events = events.filter(process=self.process)
        if self.workflow:
            events = events.filter(case_id__startswith=f"{self.workflow}:")
        if self.since or self.until:
            first = events.values("case_id").annotate(first=Min("started"))
            if self.since:
                first = first.filter(first__gte=self.since)
            if self.until:
                first = first.filter(first__lt=self.until)
            events = events.filter(case_id__in=first.values("case_id"))
        return events


@dataclass
class Log:
    """The cases in scope (after any filters) and the observation window they are measured over."""

    traces: list
    scope: Scope = Scope()
    now: float = 0.0

    @classmethod
    def load(cls, scope=Scope(), now=None):
        """ponytail: one pass holds the scope's events in memory (~200 bytes each: about 1 GB per 5 M
        events, as mining); past that, narrow by process and window, or aggregate in SQL."""
        now = (now or timezone.now()).timestamp()
        rows = scope.events().order_by("case_id", "started", "id").values_list(*COLUMNS).iterator(chunk_size=5000)
        traces = []
        for case_id, events in groupby(rows, key=lambda r: r[0]):
            events = list(events)
            steps = [Step(a, s.timestamp(), e.timestamp(), ms / 1000, st, res, ep, pk)
                     for _, _, a, s, e, ms, st, res, ep, pk in events]
            traces.append(Trace(case_id, next((r[1] for r in events if r[1]), ""), steps))
        waiting = {*UserTask.objects.waiting().values_list("case_id", flat=True),
                   *MessageSubscription.objects.waiting().values_list("case_id", flat=True)}
        for t in traces:
            t.open = t.case_id in waiting or t.end > now - SETTLE
        return cls(traces, scope, now)

    def where(self, traces):
        return Log(traces, self.scope, self.now)

    def filter(self, stack):
        """The cases the filter stack keeps, and what each filter kept."""
        traces, steps = stack.apply(self.traces)
        return self.where(traces), steps

    @property
    def start(self):
        """The window: from `since` (or the first event) to `until` (or now while cases are open, else the
        last event)."""
        if self.scope.since:
            return self.scope.since.timestamp()
        return min((t.start for t in self.traces), default=self.now)

    @property
    def end(self):
        if self.scope.until:
            return min(self.scope.until.timestamp(), self.now)
        if any(t.open for t in self.traces):
            return self.now
        return max((t.end for t in self.traces), default=self.now)

    @property
    def span(self):
        return max(self.end - self.start, 0.0)

    @property
    def case_ids(self):
        return {t.case_id for t in self.traces}


def iso(seconds):
    return None if seconds is None else datetime.fromtimestamp(seconds, UTC).isoformat()


def duration_text(seconds):
    """A duration in one or two units: "350 ms", "42 s", "12 min", "3 h 20 min", "4.5 d"."""
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds):  # None, or text from a crafted query
        return "—"
    s = abs(seconds)
    sign = "−" if seconds < 0 else ""
    if s < 1:
        return f"{sign}{s * 1000:.0f} ms"
    if s < 60:
        return f"{sign}{s:.0f} s" if s >= 10 else f"{sign}{s:.1f} s"
    if s < 3600:
        return f"{sign}{s / 60:.0f} min" if s >= 600 else f"{sign}{s / 60:.1f} min"
    if s < 86400:
        h, m = divmod(round(s / 60), 60)
        return f"{sign}{h} h {m} min" if m and h < 10 else f"{sign}{round(s / 3600)} h"
    return f"{sign}{s / 86400:.1f} d"
