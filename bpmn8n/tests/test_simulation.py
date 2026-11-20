"""The token engine and simulation: n8n's semantics, the course's worked examples ([BS]), the closed forms
of queueing theory ([QT]) as the simulator's oracle, calibration from a log and staffing."""
import io
import json
import math
import unittest
from datetime import UTC, datetime, timedelta

import numpy as np
from django.conf import settings
from django.core.management import call_command
from django.test import TestCase
from hypothesis import given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st

from projects.acme import processes

from ..engine import (
    COMPLETED,
    ERROR,
    LOOP_LIMIT,
    STUCK,
    Resources,
    Schedule,
    Stub,
    TokenEngine,
)
from ..models import Event
from ..simulation import optimise, queueing
from ..simulation.analytic import analyse
from ..simulation.calibrate import calibrate
from ..simulation.des import Run, compare, simulate
from ..simulation.scenarios import expected_visits, scenarios
from ..simulation.spec import SimModel, SimSpec, SpecError
from ..workflows.builder import Workflow


def play(workflow, stub=None, resources=None, cases=1, visit_limit=100):
    engine = TokenEngine(workflow.as_json(), stub or Stub(), resources, visit_limit)
    started = [engine.start() for _ in range(cases)]
    engine.run()
    return started if cases > 1 else started[0]


def nodes(case):
    return [v.node for v in case.trace]


def conserved(case):
    t = case.tokens
    return t["created"] == t["ended"] + t["joined"] + t["cancelled"]


def fork_and_join(gateway):
    """Start → gateway (a, b) → A | B → Merge → C."""
    w = Workflow("join").start("Start", "join")
    w = w.parallel_gateway("G", ["a", "b"]) if gateway == "parallel" else \
        w.condition_gateway("G", "exclusive", {"a": "{{a}}", "b": "{{b}}"})
    return (w.task("A", "serviceTask", "a").task("B", "serviceTask", "b").merge("Join").task("C", "serviceTask", "c")
            .flow("Start", "G").flow("G", "A", output="a").flow("G", "B", output="b")
            .flow("A", "Join").flow("B", "Join").flow("Join", "C"))


class EngineTest(unittest.TestCase):
    def test_an_output_to_several_nodes_forks_the_token(self):
        w = (Workflow("and").start("Start", "and").task("A", "serviceTask", "a").task("B", "serviceTask", "b")
             .task("C", "serviceTask", "c").flow("Start", "A", "B").flow("A", "C"))
        case = play(w, Stub(durations={"B": 3, "C": 8}))
        self.assertEqual(case.status, COMPLETED)
        self.assertEqual(sorted(nodes(case)), ["A", "B", "C", "Start"])
        self.assertEqual((case.ended, sorted(case.ends), case.tokens["created"]), (8, ["B", "C"], 2))

    def test_a_node_reached_twice_runs_twice(self):
        w = (Workflow("xor").start("Start", "xor").parallel_gateway("P", ["a", "b"]).task("A", "serviceTask", "a")
             .task("B", "serviceTask", "b").task("Z", "serviceTask", "z")
             .flow("Start", "P").flow("P", "A", "Z", output="a").flow("P", "B", "Z", output="b"))
        case = play(w)
        self.assertEqual(nodes(case).count("Z"), 2)
        self.assertEqual(case.ends, ["Z", "Z"])

    def test_a_merge_waits_only_for_the_branches_that_can_still_arrive(self):
        exclusive = play(fork_and_join("exclusive"), Stub({"G": "a"}, {"A": 5, "B": 10}))
        self.assertEqual([(v.node, v.started) for v in exclusive.trace if v.node in ("Join", "C")], [("Join", 5), ("C", 5)])
        self.assertNotIn("B", nodes(exclusive))
        parallel = play(fork_and_join("parallel"), Stub(durations={"A": 5, "B": 10}))
        join = next(v for v in parallel.trace if v.node == "Join")
        self.assertEqual((join.arrived, join.started), (5, 10))  # A's token waited 5 for B's
        # n8n's Merge in append mode (the default) passes on every item it joined: C runs once per branch
        self.assertEqual(nodes(parallel).count("C"), 2)

    def test_a_merge_in_combine_mode_passes_one_item(self):
        w = fork_and_join("parallel")
        w.node("Join")["parameters"]["mode"] = "combine"
        case = play(w, Stub(durations={"A": 5, "B": 10}, data={"A": {"x": 1}, "B": {"y": 2}}))
        c = [v for v in case.trace if v.node == "C"]
        self.assertEqual(len(c), 1)
        self.assertEqual((c[0].data["x"], c[0].data["y"], case.tokens["joined"]), (1, 2, 1))
        self.assertTrue(conserved(case))

    def test_an_inclusive_gateway_with_no_branch_takes_default(self):
        w = (Workflow("or").start("Start", "or").condition_gateway("G", "inclusive", {"a": "{{a}}", "b": "{{b}}"})
             .task("A", "serviceTask", "a").task("D", "serviceTask", "d")
             .flow("Start", "G").flow("G", "A", output="a").flow("G", "D", output="default"))
        case = play(w, Stub({"G": []}))
        self.assertEqual(case.decisions, [("G", ("default",))])
        self.assertIn("D", nodes(case))
        self.assertEqual(play(w, Stub({"G": ["a", "b"]})).decisions, [("G", ("a", "b"))])

    def test_an_event_based_gateway_times_out_on_output_0(self):
        w = (Workflow("race").start("Start", "race").event_gateway("Wait", ["reply"], "{{id}}", (1, "weeks"))
             .task("Remind", "sendTask", "remind").task("Go", "serviceTask", "go")
             .flow("Start", "Wait").flow("Wait", "Remind", output="timeout").flow("Wait", "Go", output="reply"))
        case = play(w, Stub(durations={"Wait": 7}))
        self.assertEqual((nodes(case)[-1], case.ended, case.decisions), ("Remind", 7, [("Wait", ("timeout",))]))
        with self.assertRaises(ValueError):  # one output only
            play(w, Stub({"Wait": ["timeout", "reply"]}))

    def test_a_loop_stops_at_the_visit_limit(self):
        w = (Workflow("loop").start("Start", "loop").task("Try", "serviceTask", "try")
             .condition_gateway("Again?", "exclusive", {"yes": "{{again}}"})
             .flow("Start", "Try", "Again?").flow("Again?", "Try", output="yes"))
        case = play(w, Stub({"Again?": "yes"}), visit_limit=5)
        self.assertEqual((case.status, case.visits["Try"]), (LOOP_LIMIT, 6))
        self.assertTrue(conserved(case))

    def test_an_error_end_stops_the_case_and_frees_its_people(self):
        w = (Workflow("error").start("Start", "error").parallel_gateway("P", ["work", "fail"])
             .user_task("Review", "Review", "staff", form=[]).error_end("Failed", "E1")
             .flow("Start", "P").flow("P", "Review", output="work").flow("P", "Failed", output="fail"))
        resources = Resources({"staff": 1}, {"Review": "staff"})
        case = play(w, Stub(durations={"Review": 100, "Failed": 2}), resources)
        self.assertEqual((case.status, case.ended, resources.busy["staff"]), (ERROR, 2, 0))
        self.assertTrue(conserved(case))

    def test_people_are_served_first_come_first_served(self):
        w = Workflow("fifo").start("Start", "fifo").user_task("Approve", "Approve", "managers", form=[]).flow("Start", "Approve")
        first, second = play(w, Stub(durations={"Approve": 5}), Resources({"managers": 1}, {"Approve": "managers"}), cases=2)
        self.assertEqual((first.ended, second.ended, second.waiting_time), (5, 10, 5))

    def test_people_wait_for_their_working_hours(self):
        w = Workflow("hours").start("Start", "hours").manual_task("Fetch", "Fetch", "staff").flow("Start", "Fetch")
        office = Resources({"staff": 1}, {"Fetch": "staff"}, schedules={"staff": Schedule(frozenset(range(5)), 9, 17)})
        self.assertEqual(play(w, Stub(durations={"Fetch": 30}), office).ended, 9 * 60 + 30)  # Monday 09:30
        closed = Resources({"staff": 1}, {"Fetch": "staff"}, schedules={"staff": Schedule(frozenset())})
        self.assertEqual(play(w, Stub(), closed).status, STUCK)

    @hypothesis_settings(max_examples=8, deadline=None)
    @given(st.integers(0, 10 ** 6))
    def test_tokens_are_conserved(self, seed):
        for build in (processes.library_lending, processes.incident_response, processes.expense_approval):
            spec = SimSpec(arrivals={"type": "poisson", "rate": 0.05, "count": 25}, seed=seed)
            run = Run(SimModel(build().as_json(), spec))
            self.assertTrue(all(c.done and conserved(c) for c in run.engine.cases))


LIBRARY = {"Is Book Available?": {"available": .6, "on loan": .4},
           "Wait for Reader": {"hold-book": .25, "decline-hold": .25, "timeout": .5}}


def claims(no):
    """[BS] p.83–85: 10 claims, one every 10 min, one claims handler; receive 5 min, enter data 5 min."""
    w = (Workflow("Claims").start("Claim Received", "claims").task("Receive Claim", "serviceTask", "receive_claim")
         .condition_gateway("Is Policy Valid?", "exclusive", {"yes": "{{valid}}", "no": "not {{valid}}"})
         .task("Enter Data into the System", "serviceTask", "enter_data")
         .flow("Claim Received", "Receive Claim", "Is Policy Valid?")
         .flow("Is Policy Valid?", "Enter Data into the System", output="yes"))
    return SimModel(w.as_json(), SimSpec.model_validate({
        "arrivals": {"type": "interval", "every": 10, "count": 10},
        "resources": {"claims handler": {"capacity": 1}},
        "tasks": {"Receive Claim": {"duration": 5, "resource": "claims handler", "cost": 10},
                  "Enter Data into the System": {"duration": 5, "resource": "claims handler", "cost": 5}},
        "gateways": {"Is Policy Valid?": {"no": no, "yes": 1 - no}}, "replications": 1}))


class CourseExamplesTest(unittest.TestCase):
    def test_library_scenarios_with_100_tokens(self):
        """[BS] p.18–21: S1 60, S2 20, S3 10, S4 6, S5 2, S6 1, S7 0.6 (drawn as 1); Σ = 100."""
        table = scenarios(SimModel(processes.library_lending().as_json(), SimSpec(gateways=LIBRARY)), 100, threshold=1e-3)
        rows = [(r["scenario"], round(r["tokens"], 9)) for r in table["rows"]][:7]
        self.assertEqual(rows, [("available", 60), ("on loan & timeout", 20), ("on loan & decline-hold", 10),
                                ("on loan & hold-book & available", 6), ("on loan & hold-book & on loan & timeout", 2),
                                ("on loan & hold-book & on loan & decline-hold", 1),
                                ("on loan & hold-book & on loan & hold-book & available", 0.6)])
        self.assertTrue(table["balanced"])
        self.assertAlmostEqual(table["total"], 100)
        self.assertEqual(sorted(table["rows"][0]["ends"]), ["Send Checkout Reply", "Update Catalogue"])  # ends once, both ends

    def test_library_expected_visits_solve_the_loop(self):
        visits = expected_visits(SimModel(processes.library_lending().as_json(), SimSpec(gateways=LIBRARY)))
        self.assertAlmostEqual(visits["Get Book Status"], 1 / 0.9)  # 1 + 0.1 + 0.01 + …
        self.assertAlmostEqual(visits["Cancel Request"], 0.3 / 0.9)
        self.assertAlmostEqual(visits["Send Checkout Reply"], 0.6 / 0.9)

    def test_claims_policy_never_valid(self):
        """[BS] p.84: 90 + 5 = 95 min; the handler worked 50 of 95 min, 52.63 %."""
        run = Run(claims(no=1.0))
        h = run.headline
        self.assertEqual((h["completed"], h["simulated_time"], h["cycle_time"], h["waiting_time"]), (10, 95, 5, 0))
        self.assertAlmostEqual(h["utilisation claims handler"], 50 / 95)
        self.assertEqual(h["total_cost"], 100)  # [BS] p.28: 10 tokens × 10 €

    def test_claims_policy_always_valid(self):
        """[BS] p.85: 90 + 10 = 100 min; the handler is busy 100 %."""
        h = Run(claims(no=0.0)).headline
        self.assertEqual((h["completed"], h["simulated_time"], h["cycle_time"]), (10, 100, 10))
        self.assertAlmostEqual(h["utilisation claims handler"], 1.0)

    def test_the_closed_forms_see_the_same_load(self):
        a = analyse(claims(no=0.0))  # 0.1 claims a minute × 10 min of work: ρ = 1, nothing to spare
        self.assertAlmostEqual(a["stations"]["claims handler"]["utilisation"], 1.0)
        self.assertFalse(a["stable"])


def one_step(rate, mean, servers, count=3000, replications=8):
    w = Workflow("One").start("S", "one").task("Serve", "serviceTask", "serve").flow("S", "Serve")
    return SimModel(w.as_json(), SimSpec.model_validate({
        "arrivals": {"type": "poisson", "rate": rate, "count": count}, "resources": {"server": {"capacity": servers}},
        "tasks": {"Serve": {"duration": {"type": "exponential", "mean": mean}, "resource": "server"}},
        "replications": replications, "warmup": 100, "seed": 7}))


class ClosedFormsTest(unittest.TestCase):
    def assertInside(self, metric, expected):
        self.assertLessEqual(abs(metric["mean"] - expected), metric["ci"]["99"], f"{metric} vs {expected}")

    def test_mm1_wait_is_inside_the_interval(self):
        result = simulate(one_step(0.5, 1, 1))
        self.assertInside(result["metrics"]["waiting_time"], queueing.mm1(0.5, 1).waiting_time)
        self.assertInside(result["metrics"]["utilisation server"], 0.5)

    def test_mmc_wait_is_inside_the_interval(self):
        result = simulate(one_step(1.5, 1, 2))
        self.assertInside(result["metrics"]["waiting_time"], queueing.mmc(1.5, 1, 2).waiting_time)

    def test_littles_law(self):
        h = Run(one_step(0.8, 1, 1, count=2000)).headline
        self.assertLess(abs(h["in_system"] - h["throughput"] * h["cycle_time"]) / h["in_system"], 1e-6)

    def test_analytic_cycle_time(self):
        a = analyse(one_step(0.5, 1, 1))
        self.assertAlmostEqual(a["cycle_time"], queueing.mm1(0.5, 1).response_time)
        self.assertEqual(a["bottleneck"], "server")
        w = (Workflow("par").start("S", "par").parallel_gateway("P", ["a", "b"]).task("A", "serviceTask", "a")
             .task("B", "serviceTask", "b").flow("S", "P").flow("P", "A", output="a").flow("P", "B", output="b"))
        model = SimModel(w.as_json(), SimSpec(tasks={"A": {"duration": 5}, "B": {"duration": 10}}))
        self.assertEqual(analyse(model)["cycle_time"], 10)  # parallel branches: the longest, not the sum

    def test_common_random_numbers(self):
        same = compare(one_step(0.5, 1, 1, 500, 4), one_step(0.5, 1, 1, 500, 4))
        self.assertEqual(same["difference"]["cycle_time"]["mean"], 0)  # the very same draws
        more = compare(one_step(0.9, 1, 1, 500, 4), one_step(0.9, 1, 1, 500, 4).with_capacity({"server": 2}))
        self.assertLess(more["difference"]["waiting_time"]["mean"], 0)
        self.assertTrue(more["difference"]["waiting_time"]["significant"])


class SpecTest(unittest.TestCase):
    def test_what_does_not_fit_the_workflow_is_refused(self):
        workflow = processes.library_lending().as_json()
        for gateways in ({"Nope": {"x": 1}}, {"Is Book Available?": {"maybe": 1}},
                         {"Is Book Available?": {"available": .7, "on loan": .5}}, {"Get Book Status": {"main": 1}}):
            with self.subTest(gateways), self.assertRaises(SpecError):
                SimModel(workflow, SimSpec(gateways=gateways))

    def test_a_loop_taken_always_never_ends(self):
        with self.assertRaises(SpecError):
            expected_visits(SimModel(processes.library_lending().as_json(), SimSpec(gateways={
                "Is Book Available?": {"on loan": 1}, "Wait for Reader": {"hold-book": 1}})))

    def test_defaults_come_from_the_elements(self):
        model = SimModel(processes.library_lending().as_json())
        self.assertEqual((model.resource["Check Out Book"], model.resource["Get Book Status"]), ("librarians", None))
        self.assertEqual(model.duration["Wait Two Weeks"].expected(), 2 * 10080)  # its parameters, in minutes
        self.assertEqual(model.timeout["Wait for Reader"], 10080)
        self.assertEqual(model.options["Is Book Available?"], [(("available",), .5), (("on loan",), .5)])

    def test_the_course_schedule_notation(self):
        spec = SimSpec.model_validate({"resources": {"ch": {"schedule": "De"}},
                                       "schedules": {"De": {"start": "00:00:00", "end": "23:59:59"}}})  # Mon–Sun
        self.assertEqual((spec.schedules["De"].end, len(spec.schedules["De"].days)), (24, 7))


class StaffingTest(unittest.TestCase):
    def test_fewest_people_for_a_total_time(self):
        """20 cases at once; A takes 10 min (lane a), then B 5 min (lane b); everything done within 60 min."""
        w = (Workflow("Staff").start("S", "staff").task("A", "serviceTask", "a").task("B", "serviceTask", "b")
             .flow("S", "A", "B"))
        model = SimModel(w.as_json(), SimSpec.model_validate({
            "arrivals": {"type": "peak", "count": 20}, "resources": {"a": {}, "b": {}},
            "tasks": {"A": {"duration": 10, "resource": "a"}, "B": {"duration": 5, "resource": "b"}}}))
        result = optimise.staffing(model, 60, "total", max_per_resource=10, replications=1)
        self.assertEqual((result["feasible"], result["capacity"], result["value"]), (True, {"a": 4, "b": 2}, 60))
        sensitivity = {r["resource"]: (r["value"], r["meets_target"]) for r in result["sensitivity"]}
        self.assertEqual(sensitivity, {"a": (75, False), "b": (110, False)})  # one B person works 20 × 5 from minute 10
        self.assertFalse(optimise.staffing(model, 60, "total", max_per_resource=10, total=5, replications=1)["feasible"])


class CalibrationTest(TestCase):
    def test_a_spec_from_the_log_recovers_rate_branches_and_people(self):
        w = (Workflow("Synthetic").start("Start", "synthetic").task("Check", "serviceTask", "check")
             .condition_gateway("Decide", "exclusive", {"yes": "{{ok}}", "no": "not {{ok}}"})
             .user_task("Approve", "Approve", "managers", form=[])
             .flow("Start", "Check", "Decide").flow("Decide", "Approve", output="yes"))
        rng, t0, rows, t = np.random.default_rng(3), datetime(2026, 1, 5, 9, tzinfo=UTC), [], 0.0
        for i in range(400):
            t += rng.exponential(5)  # 0.2 cases a minute
            at, check = t0 + timedelta(minutes=t), rng.exponential(2)
            ok = rng.random() < 0.7
            event = lambda activity, start, minutes, endpoint, resource="system", data=None, i=i: Event(
                case_id=f"wf:{i}", process="Synthetic", activity=activity, endpoint=endpoint, started=start,
                ended=start + timedelta(minutes=minutes), duration_ms=minutes * 60_000, status="complete",
                resource=resource, data=data or {})
            rows += [event("Check", at, check, "check"),
                     event("Decide", at + timedelta(minutes=check), 0.001, "conditions_gateway",
                           data={"output": {"branches": ["yes" if ok else "no"], "scores": {}}})]
            if ok:
                rows.append(event("Approve", at + timedelta(minutes=check), 10, "userTask", ["alice", "bob", "carol"][i % 3]))
        Event.objects.bulk_create(rows)
        spec, report = calibrate("Synthetic", workflow=w.as_json())
        self.assertAlmostEqual(report["rate"], 0.2, delta=0.02)
        self.assertAlmostEqual(spec.gateways["Decide"]["yes"], 0.7, delta=0.05)
        self.assertAlmostEqual(spec.tasks["Check"].duration.expected(), 2, delta=0.25)
        self.assertEqual((spec.resources["managers"].capacity, spec.tasks["Approve"].resource), (3, "managers"))
        self.assertEqual(spec.tasks["Approve"].duration.type, "fixed")
        self.assertTrue(analyse(SimModel(w.as_json(), spec))["stable"])  # it runs as it is
        with self.assertRaises(SpecError):
            calibrate("Nobody")


class InterfacesTest(TestCase):
    def post(self, url, data):
        return self.client.post(f"/api/simulation/{url}", data, content_type="application/json")

    def test_analytic_with_an_inline_workflow(self):
        r = self.post("analytic", {"workflow": processes.library_lending().as_json(), "spec": {"gateways": LIBRARY}})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["scenarios"]["rows"][0]["tokens"], 60)
        self.assertIn("Get Book Status", r.json()["analytic"]["visits"])

    def test_run_staffing_and_errors(self):
        r = self.post("run", {"project": "acme", "replications": 2})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["metrics"]["completed"]["mean"], 500)
        self.assertEqual(self.post("run", {"project": "acme", "replications": 1000}).status_code, 422)  # too big
        self.assertEqual(self.post("run", {"project": "../etc"}).status_code, 422)
        self.assertEqual(self.post("analytic", {"project": "acme", "spec": {"gateways": {"Nope": {}}}}).status_code, 422)
        r = self.post("run", {"project": "acme", "replications": 2,
                              "compare": {**json.loads((settings.BASE_DIR / "projects/acme/simulation.json").read_text()),
                                          "resources": {"managers": {"capacity": 3}}}})
        self.assertLess(r.json()["difference"]["waiting_time"]["mean"], 0)
        r = self.post("staffing", {"project": "acme", "target": 20, "max_per_resource": 4, "replications": 2})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertTrue(r.json()["feasible"])
        self.assertEqual(self.client.get("/api/simulation/calibrate?process=Nobody").status_code, 422)

    def test_the_command(self):
        folder = str(settings.BASE_DIR / "projects" / "acme")
        out = io.StringIO()
        call_command("simulate", folder, "--analytic", stdout=out)
        self.assertIn("scenarios of 100 start tokens", out.getvalue())
        out = io.StringIO()
        call_command("simulate", folder, "--replications", "2", "--json", stdout=out)
        result = json.loads(out.getvalue())
        self.assertTrue(math.isfinite(result["metrics"]["cycle_time"]["mean"]))
