"""N4: the learning loop (J6): overrides of automatic decisions and the thresholds they suggest; goals and
alerts (J8, J9); LLM cost (E9); functional simulation (E10)."""
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from ..analytics import Log, Scope, stats
from ..core import goals, overrides
from ..models import Event, UserTask
from .support import add_events, step

AT = datetime(2026, 1, 1, 10, tzinfo=UTC)


def laya(taken, p, **extra):
    return {"decider": "laya", "input": {"options": {"simple": "", "complex": ""}},
            "output": {"branches": [taken], "scores": {taken: p}}, **extra}


class OverridesTest(TestCase):
    def test_a_form_that_changes_the_branch_marks_the_decision_overridden(self):
        add_events(("w:1", "P", [step("Route Ticket", 1, data=laya("simple", 0.9)),
                                 step("Draft", 2, data={"llm": {"variable": "draft", "draft": "Hello"}})]))
        overrides.capture("w:1", {"route_ticket": "complex", "draft": "Hello there"})
        route, draft = (Event.objects.get(case_id="w:1", activity=a).data for a in ("Route Ticket", "Draft"))
        self.assertEqual((route["overridden"], route["final"]), (True, "complex"))
        self.assertEqual((draft["overridden"], draft["final"]), (True, "Hello there"))
        add_events(("w:2", "P", [step("Route Ticket", 1, data=laya("simple", 0.9)),
                                 step("Draft", 2, data={"llm": {"variable": "draft", "draft": "Hello"}})]))
        overrides.capture("w:2", {"route_ticket": "simple", "draft": " Hello "})  # accepted as it was
        self.assertEqual([Event.objects.get(case_id="w:2", activity=a).data["overridden"] for a in ("Route Ticket", "Draft")],
                         [False, False])
        add_events(("w:3", "P", [step("Route Ticket", 1, data=laya("simple", 0.9))]))
        overrides.capture("w:3", {"route_ticket": "nonsense", "other": 1})  # not one of its branches: not a review
        self.assertNotIn("overridden", Event.objects.get(case_id="w:3").data)

    def test_completing_a_user_task_records_it_and_the_api_exposes_the_rate(self):
        anna = User.objects.create_user("anna")
        anna.groups.add(Group.objects.create(name="managers"))
        add_events(("w:9", "P", [step("Route Ticket", 1, data=laya("simple", 0.8))]))
        task = UserTask.objects.create(case_id="w:9", process="P", activity="Review", lane="managers", title="Review",
                                       resume_url="http://localhost:5678/webhook-waiting/9?signature=a",
                                       form=[{"name": "route_ticket", "type": "string"}])
        with patch("bpmn8n.models.waiting.post_json"):
            task.complete(anna,{"route_ticket": "complex"})
        self.assertTrue(Event.objects.get(case_id="w:9", activity="Route Ticket").data["overridden"])
        add_events(("w:10", "P", [step("Route Ticket", 1, data={**laya("simple", 0.9), "overridden": False})]))
        [row] = stats.decisions(Log.load(Scope(process="P")))
        self.assertEqual(row["override"], {"reviewed": 2, "overridden": 1, "rate": 0.5})
        rows = self.client.get("/api/analytics/decisions", {"process": "P"}).json()["decisions"]
        self.assertEqual(rows[0]["override"]["rate"], 0.5)

    def test_the_threshold_keeps_the_override_rate_under_the_target(self):
        # sure decisions (>= 0.8) are never overridden; unsure ones (< 0.8) often are
        pairs = [(0.5 + i / 100, i % 2 == 0) for i in range(30)] + [(0.8 + i / 100, False) for i in range(20)]
        best = overrides.suggest(pairs, target=0.05, min_decisions=10)
        self.assertGreater(best["min_confidence"], 0.7)  # the unsure, often overridden ones are out
        self.assertLessEqual(best["override_rate"], 0.05)
        self.assertIsNone(overrides.suggest([(0.9, True)] * 30, target=0.05))  # no threshold helps
        self.assertIsNone(overrides.suggest(pairs[:5], target=0.05))  # too few to tell

    def test_the_command_reports_per_gateway(self):
        for i in range(40):
            sure = i >= 20
            add_events((f"c:{i}", "P", [step("Route", 1, data={**laya("simple", 0.9 if sure else 0.6),
                                                                 "overridden": not sure and i % 2 == 0})]))
        out = StringIO()
        call_command("suggest_thresholds", "--process", "P", stdout=out)
        self.assertIn("P · Route: 40 reviewed, 25% overridden. min_confidence 0.9 keeps 20 decisions at 0%", out.getvalue())


class GoalsTest(TestCase):
    GOALS = [{"name": "Fast", "kpi": "p90_cycle_time", "target": 25 * 60},
             {"name": "Reliable", "kpi": "error_rate", "target": 0.1, "window_days": 7},
             {"name": "Trusted", "kpi": "override_rate", "target": 0.5},
             {"name": "Busy", "kpi": "mean_cycle_time", "target": 10, "direction": "higher"},
             {"name": "On time", "kpi": "sla_breaches", "target": 0}]

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        project = Path(self.folder.name) / "projects" / "loans"
        project.mkdir(parents=True)
        (project / "workflow.json").write_text('{"name": "Loans"}')
        (project / "evaluations.json").write_text(json.dumps({"goals": self.GOALS}))
        self.settings = override_settings(BASE_DIR=Path(self.folder.name))
        self.settings.enable()
        self.addCleanup(self.settings.disable)
        # ten cases of 5, 10, ... 50 minutes, two with an error, two decisions overridden of four reviewed
        at = timezone.now() - timedelta(days=2)
        add_events(*((f"w:{i}", "Loans", [
            step("Register", 0), step("Decide", i * 5, status="error" if i < 3 else "complete",
                                      data={**laya("simple", 0.9), "overridden": i % 2 == 0} if i < 5 else {})])
                      for i in range(1, 11)), at=at)

    def run_check(self, *args, command="check_goals"):
        out = StringIO()
        try:
            call_command(command, *args, stdout=out)
            return 0, out.getvalue()
        except CommandError as e:
            return e.returncode, f"{out.getvalue()}{e}"

    def test_the_goals_are_measured_from_the_log(self):
        found = {g["name"]: g for g in goals.evaluate()}
        self.assertEqual(found["Fast"]["value"], 45.5 * 60 + 1)  # p90 of 5..50 minutes (and the 1 s of its last step)
        self.assertFalse(found["Fast"]["ok"])
        self.assertEqual((found["Reliable"]["value"], found["Reliable"]["ok"]), (0.2, False))
        self.assertEqual((found["Trusted"]["value"], found["Trusted"]["ok"]), (0.5, True))  # 2 of 4 overridden
        self.assertTrue(found["Busy"]["ok"])  # higher is better: cases do take longer than 10 s
        self.assertEqual((found["On time"]["value"], found["On time"]["ok"], found["On time"]["cases"]), (0, True, 0))
        self.assertEqual([g["name"] for g in goals.evaluate(process="Nothing")], [])

    def test_the_window_limits_the_log(self):
        old = {**self.GOALS[1], "process": "Loans", "window_days": 1 / 48, "project": "loans"}  # half an hour ago
        self.assertIsNone(goals.value(old)[0])  # no case started so recently

    def test_a_task_past_its_due_time_breaches_the_sla(self):
        now = timezone.now()
        for due, completed in ((-1, None), (-1, 2), (1, None), (5, 2)):
            UserTask.objects.create(process="Loans", activity="Review", lane="x", title="t", due=now + timedelta(hours=due),
                                    completed=None if completed is None else now + timedelta(hours=completed),
                                    resume_url=f"http://localhost:5678/webhook-waiting/{due}{completed}?s=1")
        sla = next(g for g in goals.evaluate() if g["name"] == "On time")
        self.assertEqual((sla["value"], sla["cases"], sla["ok"]), (2, 4, False))  # open past due, and finished late

    def test_check_goals_the_api_the_gate_and_prometheus(self):
        code, text = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("MISSED: loans · Fast · p90_cycle_time", text)
        self.assertIn("ok: loans · Trusted", text)
        rows = self.client.get("/api/goals", {"process": "Loans"}).json()["goals"]
        self.assertEqual({g["name"]: g["ok"] for g in rows}["Reliable"], False)
        self.assertEqual(self.client.get("/api/goals", {"process": "Other"}).json(), {"goals": []})
        metrics = self.client.get("/api/metrics/prometheus").content.decode()
        self.assertIn('bpmn8n_goal_ok{process="Loans",goal="Fast"} 0', metrics)
        self.assertIn('bpmn8n_goal_ok{process="Loans",goal="Trusted"} 1', metrics)
        self.assertEqual(self.run_check("--goals", command="eval_gate")[0], 1)  # no evaluation run, a missed goal
        self.assertEqual(self.run_check("--project", "loans", command="eval_gate")[0], 2)  # without --goals: nothing to judge

    def test_a_goal_that_holds_passes_and_a_bad_one_is_refused(self):
        import json
        from pathlib import Path
        path = Path(self.folder.name) / "projects" / "loans" / "evaluations.json"
        path.write_text(json.dumps({"goals": [self.GOALS[2]]}))
        self.assertEqual(self.run_check()[0], 0)
        self.assertEqual(self.run_check("--goals", command="eval_gate")[0], 0)
        path.write_text(json.dumps({"goals": [{"kpi": "happiness", "target": 1}]}))
        self.assertEqual(self.run_check()[0], 2)
        self.assertEqual(self.client.get("/api/goals").status_code, 422)


class LLMCostTest(TestCase):
    def setUp(self):
        from decimal import Decimal

        from ..models import LLMProvider, Settings
        provider = LLMProvider.objects.create(name="paid", preset="openai", model="m", cost_per_1k_input=Decimal("0.5"),
                                              cost_per_1k_output=Decimal("1.5"))
        Settings.objects.update_or_create(pk=1, defaults={"llm": provider})

    def call(self, case, **body):
        r = self.client.post("/api/tasks/record_llm_call/run", {"fields": {"agent": "Draft", "variable": "draft", **body}},
                             content_type="application/json", headers={"X-Bpmn8n-Case": case, "X-Bpmn8n-Activity": "Draft: usage",
                                                                       "X-Bpmn8n-Process": "P"})
        self.assertEqual(r.status_code, 200, r.content)
        return Event.objects.get(case_id=case, activity="Draft: usage").data["llm"]

    def test_the_usage_is_estimated_from_the_text_or_taken_from_n8n_and_priced(self):
        estimated = self.call("w:1", prompt="x" * 400, answer="y" * 81)
        self.assertEqual((estimated["input_tokens"], estimated["output_tokens"], estimated["estimated"]), (100, 21, True))
        self.assertAlmostEqual(estimated["cost"], 0.5 * 100 / 1000 + 1.5 * 21 / 1000)
        reported = self.call("w:2", prompt="x", answer="y", tokens={"promptTokens": 1000, "completionTokens": 200})
        self.assertEqual((reported["input_tokens"], reported["output_tokens"], reported["estimated"]), (1000, 200, False))
        self.assertAlmostEqual(reported["cost"], 0.8)
        self.assertEqual(reported["draft"], "y")  # what a person's edit is compared with
        junk = self.call("w:3", prompt="abcd", answer="", tokens="={{ unresolved }}")  # not usage: estimated
        self.assertEqual((junk["input_tokens"], junk["output_tokens"], junk["estimated"]), (1, 0, True))

    def test_no_provider_costs_nothing(self):
        from ..models import Settings
        Settings.objects.update(llm=None)
        self.assertEqual(self.call("w:4", prompt="x" * 400, answer="y")["cost"], 0)


class SimulatedCostTest(TestCase):
    def workflow(self):
        from ..workflows import Workflow
        return (Workflow("Reply").start("In", "in").llm_agent("Draft", "Reply to {{message}}", "draft", instructions="Be kind")
                .user_task("Review", "Check", "staff", [("ok", "OK", "boolean")]).flow("In", "Draft", "Review").as_json())

    def spec(self, **task):
        from ..simulation.spec import SimSpec
        return SimSpec.model_validate({"arrivals": {"type": "peak", "count": 4}, "replications": 2,
                                       "resources": {"staff": {"capacity": 4, "cost_per_hour": 60}},
                                       "tasks": {"Review": {"duration": 30}, **task}})

    def test_an_agent_costs_the_providers_prices_and_people_cost_apart(self):
        from ..simulation import des
        from ..simulation.spec import SimModel
        llm = {"cost_per_1k_input": 1.0, "cost_per_1k_output": 2.0}
        model = SimModel(self.workflow(), self.spec(), llm=llm)
        prompt = len("Reply to {{message}}") + len(model.graph.nodes["Draft"]["parameters"]["options"]["systemMessage"])
        self.assertAlmostEqual(model.model_cost["Draft"], (prompt / 4 * 1.0 + 300 * 2.0) / 1000)
        metrics = des.simulate(model)["metrics"]
        self.assertAlmostEqual(metrics["cost_models_per_case"]["mean"], model.model_cost["Draft"])
        self.assertAlmostEqual(metrics["cost_people_per_case"]["mean"], 30.0)  # half an hour of someone at 60 an hour
        self.assertAlmostEqual(metrics["cost_per_case"]["mean"], 30.0 + model.model_cost["Draft"])
        self.assertEqual(SimModel(self.workflow(), self.spec()).model_cost["Draft"], 0.0)  # no provider prices: free
        explicit = SimModel(self.workflow(), self.spec(Draft={"cost_per_call": 0.25}), llm=llm)
        self.assertEqual(explicit.model_cost["Draft"], 0.25)  # the spec wins

    def test_calibration_reads_the_observed_cost_of_calls(self):
        from ..simulation.calibrate import calibrate
        add_events(*((f"w:{i}", "Reply", [step("Draft: usage", i, data={"llm": {"agent": "Draft", "cost": 0.01 * (i + 1)}}),
                                          step("Review", i + 1)]) for i in range(3)))
        spec, report = calibrate("Reply", workflow=self.workflow())
        self.assertAlmostEqual(spec.tasks["Draft"].cost_per_call, 0.02)
        self.assertAlmostEqual(report["llm_cost_per_call"]["Draft"], 0.02)


class FunctionalSimulationTest(TestCase):
    """E10: ACME's expense policy decides the branches, not probabilities in the spec."""

    def setUp(self):
        from ..api import discover
        discover()

    def workflow(self):
        from ..workflows import Workflow
        return (Workflow("Expense").start("Submitted", "expense")
                .task("Check Expense Policy", "businessRuleTask", "check_expense_policy")
                .condition_gateway("Within Policy?", "exclusive", {"within": "{{within_policy}}", "over": "{{excess}} > 0"})
                .task("Record Expense", "serviceTask", "record_expense")
                .manual_task("Approve Excess", "Approve {{excess}} over the limit", "managers")
                .flow("Submitted", "Check Expense Policy", "Within Policy?")
                .flow("Within Policy?", "Record Expense", output="within")
                .flow("Within Policy?", "Approve Excess", output="over")).as_json()

    def spec(self, start, **extra):
        from ..simulation.spec import SimSpec
        return SimSpec.model_validate({
            "arrivals": {"type": "peak", "count": 150}, "replications": 2, "resources": {"managers": {"capacity": 2}},
            "tasks": {"Approve Excess": {"duration": 5}},
            "gateways": {"Within Policy?": {"within": 0.99, "over": 0.01}},  # what a fixed-probability run would use
            "functional": {"start": {"employee": "ann@acme.test", "category": "meal", **start}}, **extra})

    def shares(self, start, **extra):
        from ..simulation import des
        from ..simulation.spec import SimModel
        result = des.simulate(SimModel(self.workflow(), self.spec(start, **extra)), functional=True)
        counts = {label: row["count"] for label, row in result["scenarios"].items()}
        return {label: n / sum(counts.values()) for label, n in counts.items()}, result

    def test_the_branch_shares_follow_the_policy_rule_not_the_spec_probabilities(self):
        # meals cost 0..120 for one person against a limit of 60: half of them are within policy
        shares, result = self.shares({"amount": {"type": "uniform", "min": 0, "max": 120}})
        self.assertAlmostEqual(shares["within"], 0.5, delta=0.1)  # the spec's 0.99 is not used
        self.assertAlmostEqual(shares["over"], 0.5, delta=0.1)
        self.assertEqual(result["functional"]["cases"], 300)
        self.assertEqual((result["functional"]["task_failures"], result["functional"]["condition_fallbacks"]), ({}, {}))
        # the same amounts for two people: the limit doubles, nobody is over
        self.assertEqual(self.shares({"amount": {"type": "uniform", "min": 0, "max": 120}, "attendees": 2})[0],
                         {"within": 1.0})
        # a different rule for the category moves the shares: hotels have a limit of 180
        hotel, _ = self.shares({"category": "hotel", "amount": {"type": "uniform", "min": 0, "max": 120}})
        self.assertEqual(hotel, {"within": 1.0})

    def test_a_condition_that_cannot_be_evaluated_falls_back_to_the_spec_and_is_counted(self):
        from ..simulation import des
        from ..simulation.spec import SimModel
        spec = self.spec({"amount": 10}, tasks={"Approve Excess": {"duration": 5}})
        spec.functional.outputs["Check Expense Policy"] = {}  # the task does not run: nothing says if it is within
        result = des.simulate(SimModel(self.workflow(), spec), functional=True)
        self.assertEqual(result["functional"]["condition_fallbacks"], {"Within Policy?": 300})
        self.assertGreater(result["scenarios"]["within"]["count"], result["scenarios"].get("over", {"count": 0})["count"])

    def test_the_command_runs_it(self):
        out = StringIO()
        call_command("simulate", "projects/acme", "--functional", "--replications", "1", "--json", stdout=out)
        import json
        result = json.loads(out.getvalue())
        self.assertEqual(result["functional"]["cases"], 500)  # ACME's spec: 500 arrivals
        self.assertEqual(result["functional"]["task_failures"], {})  # the person's `approved` is drawn from the form

    def test_it_is_capped_and_a_plain_run_is_unchanged(self):
        from ..simulation import des
        from ..simulation.spec import SimModel, SpecError
        big = SimModel(self.workflow(), self.spec({"amount": 10}, replications=20, arrivals={"type": "peak", "count": 500}))
        with self.assertRaisesRegex(SpecError, "2000"):
            des.simulate(big, functional=True)
        plain = des.simulate(SimModel(self.workflow(), self.spec({"amount": 10})))
        self.assertNotIn("functional", plain)
        self.assertGreater(plain["scenarios"]["within"]["count"], 140)  # the spec's probabilities decide


class MiningOverridesTest(TestCase):
    def test_the_decisions_dataset_and_mining_know_the_overrides(self):
        from ..mining import Mining
        add_events(*((f"wf:{i}", "Refunds", [
            step("Is It Big?", i * 60 + 1, data={**laya("simple", 0.9), "overridden": i % 4 == 0, "final": "complex"}),
            step("Refund", i * 60 + 2)]) for i in range(8)))
        report = Mining().processes["Refunds"].decisions()["Is It Big?"]
        self.assertEqual(report["override_rate"], 0.25)
        rows = list(Mining().training().decisions())
        self.assertEqual(sorted(d["overridden"] for d in rows), [False] * 6 + [True] * 2)
        self.assertEqual({d["final"] for d in rows}, {"complex"})
