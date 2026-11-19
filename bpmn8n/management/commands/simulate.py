"""Simulates a project's process (projects/<p>/: its workflow and simulation.json). By default the event
simulation, replicated, with confidence intervals; --analytic the closed forms and the scenario table at
once; --calibrate --process NAME a spec read from the event log first; --staffing TARGET the fewest
people per resource that meet it."""
import json
import math
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from bpmn8n.simulation import des, optimise, scenarios
from bpmn8n.simulation.analytic import analyse
from bpmn8n.simulation.calibrate import sim_model
from bpmn8n.simulation.spec import SpecError, load


def number(value, digits=2):
    if value is None:
        return "-"
    if isinstance(value, float) and not math.isfinite(value):
        return "inf"
    return f"{value:,.{digits}f}" if isinstance(value, float) else str(value)


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("folder", type=Path, help="e.g. projects/acme")
        parser.add_argument("--analytic", action="store_true", help="closed forms and scenario table, no simulation")
        parser.add_argument("--replications", type=int)
        parser.add_argument("--seed", type=int)
        parser.add_argument("--tokens", type=int, default=100, help="start tokens of the scenario table")
        parser.add_argument("--calibrate", action="store_true", help="read the spec from the event log first")
        parser.add_argument("--process", help="with --calibrate: the process (n8n workflow name) in the event log")
        parser.add_argument("--since", help="with --calibrate: cases started from this ISO date")
        parser.add_argument("--staffing", type=float, metavar="TARGET", help="find the fewest people that meet it")
        parser.add_argument("--metric", choices=sorted(optimise.METRICS), default="mean", help="what --staffing bounds")
        parser.add_argument("--max-per-resource", type=int, default=20)
        parser.add_argument("--functional", action="store_true",
                            help="cases carry data and tasks run for real, so conditions take real branches (E10; capped)")
        parser.add_argument("--json", dest="as_json", action="store_true", help="the result as JSON on stdout")

    def handle(self, folder, analytic, replications, seed, tokens, calibrate, process, since, staffing, metric,
               max_per_resource, as_json, functional=False, **options):
        try:
            workflow, spec = load(folder)
            report = None
            if calibrate:
                if not process:
                    raise CommandError("--calibrate needs --process NAME")
                from bpmn8n.simulation.calibrate import calibrate as read_log
                calibrated, report = read_log(process, since, workflow=workflow, unit=spec.unit)
                spec = calibrated.model_copy(update={"replications": spec.replications, "seed": spec.seed})
            model = sim_model(workflow, spec)
            if analytic:
                result = {"analytic": analyse(model), "scenarios": scenarios.scenarios(model, tokens)}
            elif staffing is not None:
                result = optimise.staffing(model, staffing, metric, max_per_resource, replications=replications or 5,
                                           seed=seed)
            else:
                result = des.simulate(model, replications, seed, functional=functional)
        except SpecError as e:
            raise CommandError(str(e)) from e
        if report is not None:
            result = {"calibration": report, "spec": spec.model_dump(exclude_none=True), **result}
        if as_json:
            self.stdout.write(json.dumps(result, default=str))
            return
        self.stdout.write(f"{model.graph.name} · {model.unit}")
        if report is not None:
            self.calibration(report)
        if analytic:
            self.analytic(result)
        elif staffing is not None:
            self.staffing(result)
        else:
            self.simulation(result)

    def table(self, headers, rows):
        rows = [[number(v) if not isinstance(v, str) else v for v in row] for row in rows]
        widths = [max(len(str(x)) for x in column) for column in zip(headers, *rows)]
        for row in (headers, *rows):
            self.stdout.write("  ".join(str(x).ljust(w) if i == 0 else str(x).rjust(w)
                                        for i, (x, w) in enumerate(zip(row, widths))))
        self.stdout.write("")

    def calibration(self, report):
        self.stdout.write(f"\ncalibrated from {report['cases']} cases, {report['events']} events; "
                          f"rate {number(report.get('rate'), 4)}, ca² {number(report.get('ca2'))}")
        self.table(["activity", "n", "mean", "fitted"],
                   [[a, r["n"], r["mean"], r.get("fitted", "-")] for a, r in report["activities"].items()])

    def analytic(self, result):
        a, s = result["analytic"], result["scenarios"]
        self.stdout.write(f"\nscenarios of {s['tokens']} start tokens (Σ {number(s['total'])}"
                          f"{'' if s['balanced'] else ' NOT BALANCED'})")
        self.table(["scenario", "tokens", "probability", "ends"],
                   [[r["scenario"], r["tokens"], r["probability"], ", ".join(dict.fromkeys(r["ends"]))] for r in s["rows"]]
                   + ([["unrolled further", s["unrolled"], s["unrolled"] / s["tokens"], ""]] if s["unrolled"] else []))
        for warning in a["warnings"]:
            self.stdout.write(f"warning: {warning}")
        if not a["steady"]:
            self.stdout.write(f"{a['note']}; work per case {a['work']}, total time at least {number(a.get('busy_bound'))}")
            return
        self.stdout.write(f"arrival rate {number(a['rate'], 4)} per {a['unit'][:-1]}, ca² {number(a['ca2'])}; "
                          f"mean cycle time {number(a['cycle_time'])} ({a['cycle_time_method']}); "
                          f"bottleneck {a['bottleneck']}{'' if a['stable'] else '; UNSTABLE: work arrives faster than it is done'}")
        self.table(["resource", "servers", "λ", "work", "ρ", "Wq", "W", "stable"],
                   [[r, s["servers"], s["arrival_rate"], s["service_time"], s["utilisation"], s["waiting_time"],
                     s["response_time"], "yes" if s["stable"] else "NO"] for r, s in a["stations"].items()])
        self.table(["step", "visits", "λ", "time", "wait", "W"],
                   [[n, r["visits"], r["arrival_rate"], r["service_time"], r["waiting_time"], r["response_time"]]
                    for n, r in a["nodes"].items()])

    def simulation(self, result):
        self.stdout.write(f"{result['replications']} replications, seed {result['seed']}\n")
        for warning in result["warnings"]:
            self.stdout.write(f"warning: {warning}")
        if f := result.get("functional"):
            self.stdout.write(f"functional: {f['cases']} cases with data; tasks failed {f['task_failures'] or 'never'}, "
                              f"conditions not evaluable {f['condition_fallbacks'] or 'never'}\n")
        self.table(["metric", "mean", "±90%", "±95%", "±99%"],
                   [[k, m["mean"], *m["ci"].values()] for k, m in result["metrics"].items()])
        self.table(["step", "visits", "queue", "work", "cost"],
                   [[n, r["visits"], r["queue_time"], r["work_time"], r["cost"]] for n, r in result["nodes"].items()])
        self.table(["resource", "capacity", "utilisation"],
                   [[n, r["capacity"], r["utilisation"]] for n, r in result["resources"].items()])
        self.table(["scenario", "cases", "first out", "last out", "cycle time"],
                   [[n, r["count"], r["first_out"], r["last_out"], r["cycle_time"]] for n, r in result["scenarios"].items()])

    def staffing(self, result):
        self.stdout.write(f"\n{result['metric']} ≤ {number(result['target'])}: "
                          f"{'feasible' if result['feasible'] else 'NOT feasible'} with {result['capacity']} "
                          f"({result.get('headcount', '-')} people), {number(result['value'])}; "
                          f"{result['simulations']} configurations simulated. {result.get('note', '')}")
        self.table(["one less", "capacity", result["metric"], "Δ", "meets target"],
                   [[r["resource"], r["capacity"], r["value"], r["delta"], "yes" if r["meets_target"] else "no"]
                    for r in result.get("sensitivity", [])])
