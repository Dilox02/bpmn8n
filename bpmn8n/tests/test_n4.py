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
