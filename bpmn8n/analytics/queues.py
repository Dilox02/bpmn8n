"""Queueing theory on what really happened (I10, [QT] §3.4): each lane of people is a station, measured
from the log and compared with the closed form that bpmn8n.simulation.queueing gives for it.

Per station: the arrival rate λ and the squared coefficient of variation of the inter-arrival times
(ca²), the mean work E[S] and its cs², the servers c (the lane's Django group: its active members; else
the people seen working it), the utilisation ρ = λE[S]/c, the observed mean wait and response against
the G/G/c prediction (Allen–Cunneen; the lane's tasks pooled first come, first served), Little's law
(the time-average work in progress from the events against λW), the wait with one person more or less,
and the fewest people for a target wait. Per activity: the same figures, its share of the cycle time,
and the lane's prediction (in a pooled FIFO lane every task waits alike); ranked, they are the
bottlenecks.

What the log can and cannot say:
- a User Task's event spans its creation to its completion: waiting and work together. With a claim
  timestamp (`UserTask.claimed`) they separate exactly; without one, the work is estimated from each
  person's previous completion (start = max(created, that person's last completion)): exact when people
  take the next task as soon as they finish one, else an upper bound on work and a lower bound on waiting;
  a task completed through the API names no person, so only its response time is known;
- system steps are delays, not queues: software scales with its workers, so their wait is the time the
  log shows before they started;
- rates are per calendar hour: there are no working hours (no work calendar yet).
Rates in the output are per hour; times are seconds."""
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import pairwise

from django.contrib.auth.models import Group
from django.db.models import Count, Q

from ..models import UserTask
from ..simulation import queueing
from .log import NOT_PEOPLE
from .stats import mean, r

HOUR = 3600.0
NO_LANE = "(no lane)"


@dataclass
class Visit:
    """One piece of work at a station: when it arrived, began and ended (None: still open)."""

    activity: str
    case_id: str
    arrival: float
    end: float | None
    resource: str
    kind: str  # human | automated | delay
    lane: str = ""
    claimed: float | None = None
    start: float | None = None
    source: str = ""  # how `start` is known: claimed | estimated | event
    gap: float | None = None  # what the case waited before the step began (the log's waiting time)


def visits(log):
    """The visits of the cases in scope: one per event, plus the people's tasks still open."""
    keep, completed, out = log.case_ids, {}, []
    tasks = UserTask.objects.defer("form", "context", "result", "description").select_related("assignee")
    if log.scope.process:
        tasks = tasks.filter(process=log.scope.process)
    if log.scope.workflow:
        tasks = tasks.filter(case_id__startswith=f"{log.scope.workflow}:")
    for task in tasks.filter(status__in=(*UserTask.WAITING, UserTask.COMPLETED)).iterator(chunk_size=2000):
        if task.case_id not in keep:
            continue
        claimed = getattr(task, "claimed", None)  # a claim timestamp, where the model records one
        claimed = claimed.timestamp() if claimed else None
        if task.status == UserTask.COMPLETED:
            completed[(task.case_id, task.activity, round(task.created.timestamp(), 3))] = (task.lane, claimed)
        else:
            out.append(Visit(task.activity, task.case_id, task.created.timestamp(), None,
                             getattr(task.assignee, "username", ""), "human", task.lane, claimed))
    for t in log.traces:
        for s, gap in zip(t.steps, t.waits()):
            kind = "human" if s.human else "delay" if s.delay else "automated"
            lane, claimed = completed.get((t.case_id, s.activity, round(s.started, 3)), ("", None))
            out.append(Visit(s.activity, t.case_id, s.started, s.ended, s.resource, kind, lane, claimed, gap=gap))
    starts(out)
    return out


def starts(visits):
    """When the work began: the claim; else the person's availability (see the module's docstring); for a
    system step or an outside wait, the event's own start."""
    people = defaultdict(list)
    for v in visits:
        if v.kind != "human":
            v.start, v.source = v.arrival, "event"
            continue
        if v.claimed is not None:
            v.start, v.source = v.claimed, "claimed"
        if v.end is not None and v.resource not in NOT_PEOPLE:
            people[v.resource].append(v)
    for done in people.values():
        previous = -math.inf
        for v in sorted(done, key=lambda v: v.end):
            if v.start is None:
                v.start, v.source = max(v.arrival, previous), "estimated"
            previous = v.end


def rate(times):
    """Arrivals per second from their times, (n − 1) / (last − first), and ca² of the gaps between them."""
    times = sorted(times)
    if len(times) < 2 or times[-1] <= times[0]:
        return None, None
    gaps = [b - a for a, b in pairwise(times)]
    return (len(times) - 1) / (times[-1] - times[0]), queueing.scv(gaps)


def in_progress(intervals, start, end):
    """Time-average number of intervals open over [start, end] (an open one runs to `end`)."""
    if end <= start:
        return None
    return sum(max(0.0, min(b if b is not None else end, end) - max(a, start)) for a, b in intervals) / (end - start)


def little(intervals, lam, response, start, end):
    """Little's law on the log: the time-average work in progress L against λW."""
    measured = in_progress(intervals, start, end)
    if measured is None or lam is None or response is None:
        return None
    expected = lam * response
    return {"in_progress": r(measured), "expected": r(expected),
            "residual": r((measured - expected) / measured) if measured else None}


def waiting(lam, work, c, ca2, cs2):
    """The predicted mean wait with c servers; None when the station cannot keep up."""
    if c < 1:
        return None
    st = queueing.ggc(lam, work, c, ca2, cs2)
    return r(st.waiting_time) if st.stable else None


def station(name, kind, items, start, end, servers=None, servers_from=None, target_wait=None):
    """The figures of one station from its visits."""
    lam, ca2 = rate([v.arrival for v in items])
    done = [v for v in items if v.end is not None]
    worked = [v for v in done if v.start is not None]
    work = [v.end - v.start for v in worked]
    response = mean([v.end - v.arrival for v in done])
    waits = [v.start - v.arrival for v in worked] if kind == "human" else [v.gap for v in done if v.gap is not None]
    sources = Counter(v.source for v in worked)
    row = {"name": name, "kind": kind, "arrivals": len(items), "completed": len(done), "open": len(items) - len(done),
           "arrival_rate": r(lam * HOUR) if lam else None, "ca2": r(ca2), "work": mean(work),
           "cs2": r(queueing.scv(work)) if len(work) > 1 else None, "wait": mean(waits), "response": response,
           "measured": ("claimed" if set(sources) == {"claimed"} else "estimated" if "estimated" in sources
                        else "event" if sources else "response only"),
           "servers": servers, "servers_from": servers_from, "utilisation": None, "work_includes_waiting": False,
           "predicted": None, "what_if": None, "staff": None,
           "little": little([(v.arrival, v.end) for v in items], lam, response, start, end)}
    if kind != "human" or not servers or not lam:
        return row
    s = row["work"]
    if s is None:  # nothing separates waiting from work here: the response bounds the work from above
        row.update(work=response, work_includes_waiting=True, utilisation=r(lam * response / servers) if response else None)
        return row
    ca2, cs2 = ca2 if ca2 is not None else 1.0, row["cs2"] if row["cs2"] is not None else 1.0
    st = queueing.ggc(lam, s, servers, ca2, cs2)
    row["utilisation"] = r(st.utilisation)
    row["predicted"] = {"model": st.model, "stable": st.stable, "p_wait": r(st.p_wait) if st.stable else None,
                        "wait": r(st.waiting_time) if st.stable else None,
                        "response": r(st.response_time) if st.stable else None}
    row["what_if"] = {"minus_one": waiting(lam, s, servers - 1, ca2, cs2), "plus_one": waiting(lam, s, servers + 1, ca2, cs2)}
    row["staff"] = queueing.staff(lam, s, max_wait=target_wait, ca2=ca2, cs2=cs2)
    return row


def lane_servers(lanes, items):
    """lane -> (servers, where the number comes from): the active members of the lane's group, else the
    people seen working its tasks."""
    groups = dict(Group.objects.filter(name__in=lanes).annotate(
        n=Count("user", filter=Q(user__is_active=True))).values_list("name", "n"))
    out = {}
    for lane in lanes:
        if groups.get(lane):
            out[lane] = (groups[lane], "group")
        else:
            seen = {v.resource for v in items[lane] if v.resource not in NOT_PEOPLE}
            out[lane] = (max(len(seen), 1), "people seen")
    return out


def queues(log, target_wait=None):
    """Stations per lane and per activity, the bottleneck ranking and what the log cannot tell."""
    start, end = log.start, log.end
    every = visits(log)
    by_lane, by_activity = defaultdict(list), defaultdict(list)
    for v in every:
        by_activity[v.activity].append(v)
        if v.kind == "human":
            by_lane[v.lane or NO_LANE].append(v)
    servers = lane_servers([lane for lane in by_lane if lane != NO_LANE], by_lane)
    if NO_LANE in by_lane:  # anyone may work these: the people seen are the servers
        servers[NO_LANE] = (max(len({v.resource for v in by_lane[NO_LANE] if v.resource not in NOT_PEOPLE}), 1),
                            "people seen")
    lanes = {lane: station(lane, "human", items, start, end, *servers[lane], target_wait=target_wait)
             for lane, items in sorted(by_lane.items())}

    total = sum(t.duration for t in log.traces)
    activities = []
    for name, items in by_activity.items():
        kinds = Counter(v.kind for v in items)
        kind = kinds.most_common(1)[0][0]
        lane = Counter(v.lane or NO_LANE for v in items if v.kind == "human").most_common(1)
        row = station(name, kind, items, start, end)
        row["lane"] = lane[0][0] if lane else None
        if row["lane"]:  # people of a pooled lane: every task there waits alike
            shared = lanes[row["lane"]]
            row.update({k: shared[k] for k in ("servers", "servers_from", "utilisation", "work_includes_waiting",
                                               "predicted", "what_if", "staff")})
        row["share"] = r(sum((v.gap or 0) + (v.end - v.arrival) for v in items if v.end is not None) / total) if total else None
        activities.append(row)
    activities.sort(key=lambda a: (-(a["utilisation"] if a["utilisation"] is not None else -1), -(a["share"] or 0), a["name"]))

    cases_lam, _ = rate([t.start for t in log.traces])
    cycle = mean([t.duration for t in log.traces if not t.open])
    process = {"cases": len(log.traces), "arrival_rate": r(cases_lam * HOUR) if cases_lam else None, "cycle_time": cycle,
               "little": little([(t.start, None if t.open else t.end) for t in log.traces], cases_lam, cycle, start, end)}
    return {"process": process, "lanes": list(lanes.values()), "activities": activities,
            "bottlenecks": [a["name"] for a in activities if a["share"]][:5], "target_wait": target_wait,
            "notes": notes(lanes.values(), activities)}


def notes(lanes, activities):
    """In plain words, where the figures are estimates and why."""
    out = []
    if estimated := [lane["name"] for lane in lanes if lane["measured"] == "estimated"]:
        out.append(f"No claim time in the log for {', '.join(estimated)}: work is estimated from each person's "
                   "previous completion, exact when people take the next task as soon as they finish one, "
                   "otherwise an upper bound on work and a lower bound on waiting.")
    if blind := [a["name"] for a in activities if a["kind"] == "human" and a["measured"] == "response only"]:
        out.append(f"{', '.join(blind)}: completed with no person named (through the API), so the log cannot "
                   "separate waiting from work: an event spans the task's creation to its completion. "
                   "Utilisation there is an upper bound.")
    if any(lane["predicted"] for lane in lanes):
        out.append("Predicted waits use G/G/c (Allen–Cunneen, exact for M/M/c), the lane's tasks pooled first come, "
                   "first served. Rates are per calendar hour: there are no working hours.")
    if any(a["kind"] == "automated" for a in activities):
        out.append("System steps are delays, not queues: software scales with its workers, so their wait is the "
                   "time the log shows before they started.")
    return out
