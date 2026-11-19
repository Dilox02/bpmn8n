"""N4: the learning loop (J6): overrides of automatic decisions and the thresholds they suggest; goals and
alerts (J8, J9); LLM cost (E9); functional simulation (E10)."""
from datetime import UTC, datetime
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase

from ..analytics import Log, Scope, stats
from ..core import overrides
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
