"""Queueing theory in closed form: how long work waits at a step, how busy its people are, how many
people a step needs. The answers come at once, without a simulation, and they are the oracle the
event simulator is tested against.

A step of a process is a station: work arrives at rate λ (cases per unit of time), each case takes
E[S] of work, `c` servers (the people of a lane, the workers of a service) work in parallel.
- M/M/c (Erlang C) is exact for Poisson arrivals and exponential work;
- G/G/c (Allen–Cunneen) corrects it with the squared coefficients of variation of inter-arrival
  times (ca²) and of work times (cs²): exact for M/M/c, and for M/G/1 it is Pollaczek–Khinchine;
- M/M/1/K has finite room (K in the system): the rest is turned away (blocking);
- M/M/∞ is a delay: nobody waits (a timer, a customer answering).
A process is a network of stations: work leaves a step for the next with the branch probabilities
(feedback loops included). Jackson's traffic equations λ = γ + λP give each station's arrival rate.

Notation and results follow G. Stea, *Notes on Queueing Theory* (University of Pisa): §3.3 M/M/1,
§3.4 M/M/c and delay centres, §3.6 M/M/1/K, §3.9 M/G/1, §4.2–4.3 open networks with feedback.
Units are the caller's: rates per hour with times in hours, or per minute with minutes."""
import math
from dataclasses import asdict, dataclass

INF = math.inf


@dataclass(frozen=True)
class Station:
    """One step as a queue. Times are in the unit of 1/arrival_rate."""

    model: str
    arrival_rate: float  # λ offered
    service_time: float  # E[S]
    servers: float  # c (math.inf for a delay)
    utilisation: float  # ρ = λ_eff E[S] / c: the share of time each server works
    p_wait: float  # probability that a case waits before it is served
    queue_length: float  # Lq: cases waiting
    in_system: float  # L = Lq + λ_eff E[S]: cases waiting or in service
    waiting_time: float  # Wq
    response_time: float  # W = Wq + E[S]
    throughput: float  # λ_eff: what gets in (λ minus the blocked)
    blocking: float = 0.0  # share turned away (finite room)

    @property
    def stable(self):
        return self.utilisation < 1 and math.isfinite(self.waiting_time)

    def as_dict(self):
        return {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                for k, v in {**asdict(self), "stable": self.stable}.items()}


def _check(arrival_rate, service_time, servers=1):
    if arrival_rate < 0 or service_time < 0:
        raise ValueError("arrival rate and service time must not be negative")
    if servers < 1:
        raise ValueError("a station needs at least one server")


def _unstable(model, lam, s, c, rho):
    return Station(model, lam, s, c, rho, 1.0, INF, INF, INF, INF, lam)


def erlang_b(c, offered):
    """Blocking probability of M/M/c/c with `offered` = λE[S] erlangs (the stable recursion)."""
    b = 1.0
    for k in range(1, int(c) + 1):
        b = offered * b / (k + offered * b)
    return b


def erlang_c(c, offered):
    """Probability that a case waits in M/M/c with `offered` = λE[S] erlangs (needs offered < c)."""
    rho = offered / c
    if rho >= 1:
        return 1.0
    b = erlang_b(c, offered)
    return b / (1 - rho * (1 - b))


def mmc(arrival_rate, service_time, servers=1):
    """M/M/c: Poisson arrivals, exponential work, c servers, room for everyone (Erlang C)."""
    _check(arrival_rate, service_time, servers)
    lam, s, c = arrival_rate, service_time, int(servers)
    offered, rho = lam * s, lam * s / c
    if rho >= 1:
        return _unstable(f"M/M/{c}", lam, s, c, rho)
    if lam == 0 or s == 0:
        return Station(f"M/M/{c}", lam, s, c, rho, 0.0, 0.0, offered, 0.0, s, lam)
    p_wait = erlang_c(c, offered)
    wq = p_wait * s / (c - offered)  # = C / (cμ − λ)
    return Station(f"M/M/{c}", lam, s, c, rho, p_wait, lam * wq, lam * (wq + s), wq, wq + s, lam)


def mm1(arrival_rate, service_time):
    """M/M/1: L = ρ/(1−ρ), W = E[S]/(1−ρ)."""
    return mmc(arrival_rate, service_time, 1)


def ggc(arrival_rate, service_time, servers=1, ca2=1.0, cs2=1.0):
    """G/G/c by Allen–Cunneen: Wq ≈ Wq(M/M/c) · (ca² + cs²)/2. Exact for M/M/c (ca² = cs² = 1) and,
    with c = 1 and ca² = 1, it is Pollaczek–Khinchine's M/G/1. cs² = 0 is deterministic work."""
    if ca2 < 0 or cs2 < 0:
        raise ValueError("squared coefficients of variation must not be negative")
    base = mmc(arrival_rate, service_time, servers)
    if not base.stable or base.waiting_time == 0:
        return Station(_name(ca2, cs2, base.servers), *list(asdict(base).values())[1:])
    factor = (ca2 + cs2) / 2
    wq = base.waiting_time * factor
    lam, s = base.arrival_rate, base.service_time
    return Station(_name(ca2, cs2, base.servers), lam, s, base.servers, base.utilisation, base.p_wait,
                   lam * wq, lam * (wq + s), wq, wq + s, lam)


def _name(ca2, cs2, c):
    letter = lambda v: "M" if v == 1 else "D" if v == 0 else "G"  # noqa: E731
    return f"{letter(ca2)}/{letter(cs2)}/{int(c)}"


def mg1(arrival_rate, service_time, cs2=1.0):
    """M/G/1, Pollaczek–Khinchine: Wq = λE[S²] / (2(1−ρ)), E[S²] = (1 + cs²)E[S]²."""
    return ggc(arrival_rate, service_time, 1, 1.0, cs2)


def mm1k(arrival_rate, service_time, capacity):
    """M/M/1/K: at most K cases in the system (waiting + in service); an arrival that finds it full
    is turned away. Stable at any load."""
    _check(arrival_rate, service_time)
    if capacity < 1:
        raise ValueError("capacity must be at least 1")
    lam, s, k = arrival_rate, service_time, int(capacity)
    rho = lam * s
    if rho == 1:
        probabilities = [1 / (k + 1)] * (k + 1)
    else:
        p0 = (1 - rho) / (1 - rho ** (k + 1))
        probabilities = [p0 * rho ** n for n in range(k + 1)]
    blocking = probabilities[k]
    throughput = lam * (1 - blocking)
    in_system = sum(n * p for n, p in enumerate(probabilities))
    busy = 1 - probabilities[0]
    response = in_system / throughput if throughput else 0.0
    wq = max(response - s, 0.0)
    p_wait = (1 - probabilities[0] - blocking) / (1 - blocking) if blocking < 1 else 1.0  # admitted, not alone
    return Station(f"M/M/1/{k}", lam, s, 1, busy, p_wait, throughput * wq, in_system, wq, response, throughput,
                   blocking)


def mminf(arrival_rate, service_time):
    """M/M/∞, a delay centre: nobody waits, L = λE[S] cases are in it on average."""
    _check(arrival_rate, service_time)
    return Station("M/M/inf", arrival_rate, service_time, INF, 0.0, 0.0, 0.0, arrival_rate * service_time,
                   0.0, service_time, arrival_rate)


def staff(arrival_rate, service_time, max_wait=None, max_p_wait=None, ca2=1.0, cs2=1.0, max_servers=10_000):
    """The fewest servers that keep the station stable and, when given, the mean wait ≤ max_wait and the
    probability of waiting ≤ max_p_wait. None when even max_servers are not enough."""
    start = max(1, math.floor(arrival_rate * service_time) + 1)
    for c in range(start, max_servers + 1):
        st = ggc(arrival_rate, service_time, c, ca2, cs2)
        if st.stable and (max_wait is None or st.waiting_time <= max_wait) and \
                (max_p_wait is None or st.p_wait <= max_p_wait):
            return c
    return None


def scv(values):
    """Squared coefficient of variation var/mean² of samples (1 for exponential, 0 for constant)."""
    values = [float(v) for v in values]
    if len(values) < 2:
        return 1.0
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return variance / mean ** 2


def traffic(external, routing):
    """Jackson's traffic equations λ = γ + λP: the arrival rate at every station, loops included.
    external: γ per station; routing: P[i][j] = probability that work leaving i goes to j (rows may sum
    to less than 1: the rest leaves the process). Raises ValueError when work never leaves (a loop with
    probability 1)."""
    import numpy as np
    gamma = np.asarray(external, dtype=float)
    p = np.asarray(routing, dtype=float)
    n = len(gamma)
    if p.shape != (n, n):
        raise ValueError(f"routing must be {n}×{n}")
    if (p < 0).any() or (p.sum(axis=1) > 1 + 1e-9).any():
        raise ValueError("routing rows must be probabilities summing to at most 1")
    try:
        rates = np.linalg.solve(np.eye(n) - p.T, gamma)
    except np.linalg.LinAlgError:
        raise ValueError("work never leaves the network: a loop is taken with probability 1") from None
    if (rates < -1e-9).any():
        raise ValueError("work never leaves the network: a loop is taken with probability 1")
    return [max(float(r), 0.0) for r in rates]


@dataclass(frozen=True)
class Network:
    stations: dict  # name -> Station
    throughput: float  # Σγ: cases per unit of time entering (and, when stable, leaving)

    @property
    def stable(self):
        return all(s.stable for s in self.stations.values())

    @property
    def in_system(self):
        return sum(s.in_system for s in self.stations.values())

    @property
    def response_time(self):
        """Mean time a case spends in the process (Little's law over the network: L / Σγ)."""
        return self.in_system / self.throughput if self.throughput else 0.0

    @property
    def bottleneck(self):
        """The station with the highest utilisation."""
        return max(self.stations, key=lambda n: self.stations[n].utilisation, default=None)

    def as_dict(self):
        finite = lambda v: v if math.isfinite(v) else None  # noqa: E731
        return {"stable": self.stable, "throughput": self.throughput, "in_system": finite(self.in_system),
                "response_time": finite(self.response_time), "bottleneck": self.bottleneck,
                "stations": {n: s.as_dict() for n, s in self.stations.items()}}


def network(names, external, routing, service_time, servers, ca2=None, cs2=None):
    """An open network of G/G/c stations (exactly Jackson's when every ca² = cs² = 1): per station
    name, external arrival rate, routing row, mean work, servers (math.inf: a delay, nobody waits)."""
    rates = traffic(external, routing)
    stations = {}
    for i, name in enumerate(names):
        c = servers[i]
        if math.isinf(c):
            stations[name] = mminf(rates[i], service_time[i])
        else:
            stations[name] = ggc(rates[i], service_time[i], c, (ca2 or [1.0] * len(names))[i],
                                 (cs2 or [1.0] * len(names))[i])
    return Network(stations, float(sum(external)))


def demo():
    """Self-check: python -c 'import bpmn8n.simulation.queueing as q; q.demo()'"""
    close = lambda a, b: abs(a - b) < 1e-9  # noqa: E731
    s = mm1(0.5, 1)  # ρ = .5: L = 1, W = 2, Wq = 1
    assert close(s.in_system, 1) and close(s.response_time, 2) and close(s.waiting_time, 1)
    s = mmc(1, 1, 2)  # a = 1 erlang on 2 servers: C = 1/3, Wq = C/(cμ − λ) = 1/3
    assert close(s.p_wait, 1 / 3) and close(s.waiting_time, 1 / 3)
    assert close(mg1(0.5, 1, 0).waiting_time, mm1(0.5, 1).waiting_time / 2)  # M/D/1 waits half of M/M/1
    assert not mm1(1, 1).stable and math.isinf(mm1(2, 1).waiting_time)
    assert abs(mm1k(0.5, 1, 200).in_system - 1) < 1e-6 and mm1k(2, 1, 3).blocking > 0.5
    assert staff(10, 0.5, max_wait=0.1) == 7
    assert traffic([1, 0], [[0, 1], [0.5, 0]]) == [2.0, 2.0]  # half the work loops back: twice the load
    net = network(["a", "b"], [0.5, 0], [[0, 1], [0, 0]], [1, 1], [1, 1])  # two M/M/1 in tandem
    assert close(net.response_time, 4) and net.bottleneck in ("a", "b")
