"""What tokens wait for besides time: limited capacity (the people of a lane, the workers of a service)
and the hours it works. The engine asks; this keeps the queues and how busy each resource was.
Pure Python, no Django."""
import heapq
import itertools
import math
from collections import Counter, defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Schedule:
    """Working hours: the days of the week (0 = Monday) and the hours of the day [start, end) a resource
    works. Time 0 of the clock is a Monday at midnight; `day` is a day's length in the clock's unit.
    ponytail: work started before closing goes on after it (no pre-emption); shifts that cross
    midnight need two schedules. Upgrade: a list of intervals per day."""

    days: frozenset = frozenset(range(7))
    start: float = 0.0
    end: float = 24.0
    day: float = 1440.0

    def opens(self, t):
        """The first moment at or after `t` when the resource works (math.inf if never)."""
        hour, today = self.day / 24, math.floor(t / self.day)
        for d in range(today, today + 8):
            if d % 7 in self.days and t < d * self.day + self.end * hour:
                return max(t, d * self.day + self.start * hour)
        return math.inf


class Resources:
    """Each resource has `capacity` servers (math.inf: unlimited); a node of `assignment` waits for a
    free one (and, with a schedule, for working hours), first come first served, a lower `priority`
    first. `timeline[name]` keeps (time, busy servers) after every change: utilisation over time."""

    def __init__(self, capacity, assignment, priority=None, schedules=None):
        self.capacity = dict(capacity)
        self.assignment = {node: name for node, name in assignment.items() if name}
        self.priority = dict(priority or {})
        self.schedules = dict(schedules or {})
        self.busy = Counter()
        self.queues = defaultdict(list)  # name -> heap of (priority, order, visit)
        self.timeline = defaultdict(lambda: [(0.0, 0)])
        self._order = itertools.count()

    def of(self, node):
        return self.assignment.get(node)

    def free(self, name):
        return self.busy[name] < self.capacity.get(name, 1)

    def opens(self, name, now):
        schedule = self.schedules.get(name)
        return schedule.opens(now) if schedule else now

    def wait(self, name, visit):
        heapq.heappush(self.queues[name], (self.priority.get(visit.node, 0), next(self._order), visit))

    def next(self, name):
        return heapq.heappop(self.queues[name])[2]

    def take(self, name, now):
        self.busy[name] += 1
        self.timeline[name].append((now, self.busy[name]))

    def give(self, name, now):
        self.busy[name] -= 1
        self.timeline[name].append((now, self.busy[name]))

    def busy_time(self, name, begin, end):
        """Server-time spent working within [begin, end]: the integral of the busy servers."""
        steps, total = self.timeline[name], 0.0
        for (t, busy), (t_next, _) in zip(steps, [*steps[1:], (math.inf, 0)]):
            total += busy * max(0.0, min(t_next, end) - max(t, begin))
        return total
