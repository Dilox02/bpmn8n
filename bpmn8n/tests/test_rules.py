"""The workflow check's rules as classes (bpmn8n/workflows/rules.py): one small workflow per new rule that
makes it fire and one that does not; the registry and the guide's table; the builder ↔ n8n node drift."""
import json
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.management import call_command
from django.test import TestCase, override_settings

from ..management.commands.elements import FIXTURE, SAMPLES, builder_params
from ..workflows import Workflow
from ..workflows.check import WorkflowCheck
from ..workflows.elements import ELEMENTS, manifest
from ..workflows.rules import RULES, table

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "GUIDE.md"


def found(workflow, rule, **kwargs):
    """(node, level) of the problems one rule finds."""
    return {(p.node, p.level) for p in WorkflowCheck(workflow, fetch=lambda _: None, **kwargs).problems if p.rule == rule}


def steps(name, *names):
    """A webhook "In" and short timers (steps that are not tasks, so nothing else is reported) called `names`."""
    w = Workflow(name).start("In", "in")
    for n in names:
        w.timer(n, 30, "seconds")
    return w


class ImplicitGatewaysTest(TestCase):
    def test_one_output_to_several_steps_is_a_split_nobody_drew(self):
        w = steps("Split", "A", "B", "C").flow("In", "A").flow("A", "B").flow("A", "C")
        self.assertEqual(found(w.as_json(), "implicit-split"), {("A", "info")})
        drawn = steps("Drawn", "B", "C").parallel_gateway("Both", ["b", "c"])
        drawn.flow("In", "Both").flow("Both", "B", output="b").flow("Both", "C", output="c")
        self.assertEqual(found(drawn.as_json(), "implicit-split"), set())

    def test_branches_that_run_at_once_meeting_at_a_step_run_it_twice(self):
        implicit = steps("Twice", "A", "B", "C", "D").flow("In", "A").flow("A", "B", "D").flow("A", "C", "D")
        self.assertEqual(found(implicit.as_json(), "implicit-join"), {("D", "warning")})
        parallel = steps("Twice", "B", "C", "D").parallel_gateway("Both", ["b", "c"])
        parallel.flow("In", "Both").flow("Both", "B", "D", output="b").flow("Both", "C", "D", output="c")
        self.assertEqual(found(parallel.as_json(), "implicit-join"), {("D", "warning")})
        inclusive = steps("Twice", "B", "C", "D").condition_gateway("Which", "inclusive", {"b": "true", "c": "true"})
        inclusive.flow("In", "Which").flow("Which", "B", "D", output="b").flow("Which", "C", "D", output="c")
        self.assertEqual(found(inclusive.as_json(), "implicit-join"), {("D", "warning")})
        straight = steps("Gateway to step", "B", "D").parallel_gateway("Both", ["b", "d"])  # D also right after it
        straight.flow("In", "Both").flow("Both", "B", "D", output="b").flow("Both", "D", output="d")
        self.assertEqual(found(straight.as_json(), "implicit-join"), {("D", "warning")})

    def test_a_merge_or_the_branches_of_a_choice_are_fine(self):
        merged = steps("Joined", "B", "C").parallel_gateway("Both", ["b", "c"]).merge("Join")
        merged.flow("In", "Both").flow("Both", "B", "Join", output="b").flow("Both", "C", "Join", output="c")
        self.assertEqual(found(merged.as_json(), "implicit-join"), set())
        choice = steps("Choice", "B", "C", "D").condition_gateway("Which", "exclusive", {"b": "true"})
        choice.flow("In", "Which").flow("Which", "B", "D", output="b").flow("Which", "C", "D", output="default")
        self.assertEqual(found(choice.as_json(), "implicit-join"), set())
        race = steps("Race", "B", "C", "D").event_gateway("First", ["m"], "{{k}}", (1, "days"))
        race.flow("In", "First").flow("First", "B", "D", output="timeout").flow("First", "C", "D", output="m")
        self.assertEqual(found(race.as_json(), "implicit-join"), set())
        loop = steps("Again", "A", "N").condition_gateway("Again?", "exclusive", {"yes": "true"})
        loop.flow("In", "A", "N", "Again?").flow("Again?", "N", output="yes")  # the way back is not a second branch
        self.assertEqual(found(loop.as_json(), "implicit-join"), set())


class LoopsTest(TestCase):
    def test_a_loop_nothing_leaves_never_ends(self):
        w = steps("Forever", "A", "B").flow("In", "A", "B", "A")
        self.assertEqual(found(w.as_json(), "endless-loop"), {("A", "error")})
        self.assertEqual(found(w.as_json(), "loop"), set())  # one problem: the error says it all

    def test_a_loop_ends_through_an_output_out_of_it_or_left_open(self):
        out = steps("Out", "A", "Done").condition_gateway("Again?", "exclusive", {"yes": "true"})
        out.flow("In", "A", "Again?").flow("Again?", "A", output="yes").flow("Again?", "Done", output="default")
        left_open = steps("Open", "A").condition_gateway("Again?", "exclusive", {"yes": "true"})
        left_open.flow("In", "A", "Again?").flow("Again?", "A", output="yes")  # default: the case ends
        for w in (out, left_open):
            self.assertEqual(found(w.as_json(), "endless-loop") | found(w.as_json(), "loop"), set(), w.name)

    def test_a_loop_nothing_decides_in_may_spin(self):
        w = (steps("Spin", "Out").task("A", "sendTask", "send_notification").task("B", "sendTask", "send_notification")
             .flow("In", "A", "B", "A").flow("B", "Out"))  # B goes on AND back
        self.assertEqual(found(w.as_json(), "loop"), {("A", "warning")})
        self.assertEqual(found(w.as_json(), "endless-loop"), set())
        timed = steps("Paced", "A", "B", "Out").flow("In", "A", "B", "A").flow("B", "Out")  # timers keep time
        self.assertEqual(found(timed.as_json(), "loop"), set())

    def test_an_n8n_node_the_registry_does_not_know_may_leave(self):
        w = steps("Unknown", "A").flow("In", "A").as_json()
        w["nodes"].append({"name": "If", "type": "n8n-nodes-base.if", "parameters": {}, "position": [0, 0]})
        w["connections"]["A"] = {"main": [[{"node": "If", "type": "main", "index": 0}]]}
        w["connections"]["If"] = {"main": [[{"node": "A", "type": "main", "index": 0}]]}  # its false output not shown
        self.assertEqual(found(w, "endless-loop") | found(w, "loop"), set())


class NamingTest(TestCase):
    def test_a_step_is_named_by_its_action(self):
        w = (Workflow("Handle claims").start("Claim Received", "c")
             .task("Process Claim", "serviceTask", "register_ticket").task("Claim Registered", "serviceTask", "register_ticket")
             .user_task("Request is approved", "t", "", form=[]).manual_task("Payment Done", "t", "")
             .task("Approve Expense", "serviceTask", "register_ticket").task("Check Speed", "serviceTask", "register_ticket")
             .timer("Two Weeks Passed", 2, "weeks")
             .flow("Claim Received", "Process Claim", "Claim Registered", "Request is approved", "Payment Done",
                   "Approve Expense", "Check Speed", "Two Weeks Passed"))
        problems = {p.node: p.message for p in WorkflowCheck(w.as_json(), fetch=lambda _: None).problems
                    if p.rule == "naming"}
        self.assertEqual(set(problems), {"", "Process Claim", "Claim Registered", "Request is approved", "Payment Done"})
        self.assertIn("'handle' is a mushy verb", problems[""])  # the process too; its trigger may be past tense
        self.assertIn("verb + noun", problems["Claim Registered"])


class SeparationOfDutiesTest(TestCase):
    """D7: who asks for something should not be who approves it."""

    def setUp(self):
        Group.objects.create(name="staff")
        Group.objects.create(name="managers")

    @staticmethod
    def purchase(lane="staff", name="Approve Purchase", title="Purchase {{item}}", form=(("ok", "OK", "string"),),
                 **approval):
        return (Workflow("Buy Item").start("In", "in")
                .user_task("Submit Purchase", "Submit a purchase", "staff", [("item", "Item", "string")])
                .user_task(name, title, lane, list(form), **approval)
                .flow("In", "Submit Purchase", name)).as_json()

    def duties(self, workflow):
        return {(p.node, p.level, p.message) for p in WorkflowCheck(workflow, fetch=lambda _: None).problems
                if p.rule == "separation-of-duties"}

    def test_an_approval_after_a_task_the_same_person_may_work_is_noted(self):
        message = 'separation of duties: add not_same_as=["Submit Purchase"] so one person cannot request and approve'
        self.assertEqual(self.duties(self.purchase()), {("Approve Purchase", "info", message)})
        for decides in ({"name": "Sign-off Purchase"}, {"name": "Accept Order"}, {"name": "Settle Bill", "title": "Decide on {{item}}"},
                        {"name": "Check Purchase", "form": [("is_approved", "Approved", "boolean")]}):
            self.assertEqual({node for node, _, _ in self.duties(self.purchase(**decides))}, {decides["name"]}, decides)
        self.assertTrue(self.duties(self.purchase(lane="")))  # no lane: superusers do both

    def test_lanes_are_one_person_only_with_a_member_in_common(self):
        self.assertEqual(self.duties(self.purchase(lane="managers")), set())
        both = User.objects.create(username="both")
        both.groups.add(*Group.objects.all())
        self.assertEqual({node for node, _, _ in self.duties(self.purchase(lane="managers"))}, {"Approve Purchase"})

    def test_four_eyes_declared_or_nothing_to_approve_is_quiet(self):
        self.assertEqual(self.duties(self.purchase(not_same_as=["Submit Purchase"])), set())
        for plain in ({"name": "Ship Purchase"}, {"name": "Assign Owner", "title": "Design the {{item}} form"},
                      {"name": "Ship Purchase", "form": [("approved", "Approved", "string")]}):  # a yes/no only
            self.assertEqual(self.duties(self.purchase(**plain)), set(), plain)
        first = (Workflow("Buy Item").start("In", "in").user_task("Approve Purchase", "t", "staff", [("ok", "OK", "boolean")])
                 .manual_task("Order Item", "Order it", "staff").flow("In", "Approve Purchase", "Order Item")).as_json()
        self.assertEqual(self.duties(first), set())  # nobody asked before it


class VariablesTest(TestCase):
    def test_global_variables_always_exist(self):
        w = (Workflow("Notify").start("In", "in")
             .task("Tell", "sendTask", "send_notification", to="{{vars.office}}", subject="Limit {{vars.limit}}")
             .flow("In", "Tell")).as_json()
        self.assertEqual(found(w, "variable", start_data=["message"]), set())
        w["nodes"][1]["parameters"]["input"]["value"]["subject"] = "{{limit}}"
        self.assertEqual(found(w, "variable", start_data=["message"]), {("Tell", "error")})


class RegistryTest(TestCase):
    def test_the_rules_keep_the_report_order_and_the_guide_shows_them(self):
        ids = [r.id for r in RULES]
        self.assertEqual(ids[:15], ["empty", "trigger", "connection", "reachable", "task", "output", "condition", "lane",
                                    "subprocess", "input", "variable", "start-data", "human", "wait", "metrics"])
        out = StringIO()
        call_command("rules", "--md", stdout=out)
        self.assertEqual(out.getvalue().strip(), table())
        self.assertIn(table(), GUIDE.read_text(), "the guide's rules table is stale: paste `manage.py rules --md`")


class DriftTest(TestCase):
    """F6: the parameters the Python builder writes on bpmn8n's nodes, against the TypeScript nodes
    (n8n-nodes-bpmn8n/test/drift.test.js reads the same fixture against the built nodes)."""

    def test_every_builder_step_has_a_sample(self):
        self.assertEqual(set(SAMPLES), set(Workflow.STEPS))

    def test_the_fixture_is_what_the_builder_writes(self):
        self.assertEqual(json.loads(FIXTURE.read_text()), builder_params(),
                         "stale: run `python manage.py elements --builder-params`, then `npm test` in n8n-nodes-bpmn8n")
        self.assertEqual(set(builder_params()), {t for t, e in ELEMENTS.items() if e.bpmn8n})  # every node has one
        self.assertIn("decisionScope", builder_params()["CUSTOM.bpmn8nExclusiveGateway"])  # set only conditionally

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://localhost:5678")
    def test_the_elements_are_public_for_the_editor(self):
        r = self.client.get("/api/elements", headers={"Origin": "http://localhost:5678"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), json.loads(json.dumps(manifest())))
        self.assertEqual(r["Access-Control-Allow-Origin"], "http://localhost:5678")
        out = StringIO()
        call_command("elements", stdout=out)
        self.assertEqual(json.loads(out.getvalue()), json.loads(json.dumps(manifest())))
