"""Scenario tests (scenarios.json): the process played on the token engine with given answers, recorded
from a real case, and the deadlock search (D6) over every combination of choices."""
import io
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from ..models import Case, Event
from ..workflows.builder import Workflow
from ..workflows.check import ProjectCheck, WorkflowCheck
from ..workflows.rules import SEARCH_CASES
from ..workflows.scenarios import ScenarioRunner, record

AMOUNT_OK = {"employee": "ann@acme.test", "category": "meal", "amount": 50, "attendees": 1}
AMOUNT_HIGH = {**AMOUNT_OK, "amount": 500}


def expense():
    """Start → Check → Within? → Book, or → Approve → Approved? → Book | Refuse (error) | Clarify → Approve."""
    return (Workflow("Expense").start("Start", "expense")
            .task("Check", "businessRuleTask", "check_expense_policy")
            .condition_gateway("Within?", "exclusive", {"yes": "{{within_policy}}", "no": "not {{within_policy}}"})
            .user_task("Approve", "Approve it", "finance", form=[("approved", "Approved", "boolean"),
                                                                ("more", "Ask for more", "boolean")])
            .condition_gateway("Approved?", "exclusive", {"more": "{{more}}", "yes": "{{approved}}",
                                                          "no": "not {{approved}}"})
            .user_task("Clarify", "Explain it", "staff", form=[("answer", "Answer", "string")])
            .task("Book", "serviceTask", "record_expense", approved=True)
            .error_end("Refuse", "rejected")
            .flow("Start", "Check", "Within?").flow("Within?", "Book", output="yes")
            .flow("Within?", "Approve", output="no").flow("Approve", "Approved?")
            .flow("Approved?", "Clarify", output="more").flow("Clarify", "Approve")
            .flow("Approved?", "Book", output="yes").flow("Approved?", "Refuse", output="no")).as_json()


def play(workflow, **scenario):
    return ScenarioRunner(workflow).run({"name": "t", **scenario})


class ScenarioRunnerTest(TestCase):
    def test_a_case_takes_the_expected_path_with_the_real_tasks(self):
        report = play(expense(), start=AMOUNT_OK, expect={
            "path": ["Start", "Check", "Within?", "Book"], "visits": ["Book"], "not": ["Approve"], "ends": "Book",
            "variables": {"within_policy": True, "status": "queued for payment"}, "max_steps": 4})
        self.assertTrue(report["passed"], report["failure"])
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["variables"]["limit"], 60)  # check_expense_policy ran on the start data

    def test_each_expectation_reports_its_first_difference(self):
        for expect, failure in (
                ({"path": ["Start", "Check", "Within?", "Approve"]}, "path: step 4 is 'Book', expected 'Approve'"),
                ({"path": ["Start", "Check", "Within?"]}, "path: goes on to 'Book' after the expected end"),
                ({"path": ["Start", "Check", "Within?", "Book", "Pay"]}, "path: ends after 4 steps, expected 'Pay' next"),
                ({"visits": ["Approve"]}, "never visits 'Approve'"),
                ({"not": ["Book"]}, "visits 'Book', which it must not"),
                ({"ends": "Approve"}, "the case ends at 'Book', expected Approve"),
                ({"ends": "error:rejected"}, "the case ends at 'Book', expected error:rejected"),
                ({"variables": {"status": "rejected"}}, "variable 'status' is 'queued for payment', expected 'rejected'"),
                ({"variables": {"nope": 1}}, "variable 'nope' is missing, expected 1"),
                ({"max_steps": 3}, "the case takes 4 steps, more than 3")):
            report = play(expense(), start=AMOUNT_OK, expect=expect)
            self.assertFalse(report["passed"])
            self.assertEqual(report["failure"], failure)

    def test_an_error_end_is_expected_by_its_code(self):
        refused = {"start": AMOUNT_HIGH, "people": {"Approve": {"approved": False, "more": False}}}
        self.assertTrue(play(expense(), **refused, expect={"ends": "error:rejected"})["passed"])
        self.assertEqual(play(expense(), **refused, expect={"ends": "error:other"})["failure"],
                         "the case ends with error rejected, expected error:other")
        self.assertIn("the case ends with an error at 'Refuse'", play(expense(), **refused)["failure"])

    def test_a_loop_takes_one_answer_per_visit_and_running_out_fails(self):
        answers = [{"approved": False, "more": True}, {"approved": True, "more": False}]
        report = play(expense(), start=AMOUNT_HIGH, people={"Approve": answers, "Clarify": {"answer": "a client dinner"}},
                      expect={"path": ["Start", "Check", "Within?", "Approve", "Approved?", "Clarify", "Approve",
                                       "Approved?", "Book"], "variables": {"answer": "a client dinner"}})
        self.assertTrue(report["passed"], report["failure"])
        report = play(expense(), start=AMOUNT_HIGH, people={"Approve": answers[:1], "Clarify": {"answer": "x"}})
        self.assertEqual(report["failure"], "'Approve' is visited 2 times, people gives it 1")
        self.assertEqual(report["path"][-1], "Approve")

    def test_a_missing_or_wrong_answer_names_the_node(self):
        self.assertEqual(play(expense(), start=AMOUNT_HIGH)["failure"],
                         "'Approve' waits for a person: give their answers in people (approved, more)")
        self.assertEqual(play(expense(), start=AMOUNT_HIGH, people={"Approve": {"aproved": True}})["failure"],
                         "'Approve': its form has no aproved")

    def test_a_decision_on_a_conditions_gateway_is_checked_against_its_conditions(self):
        self.assertEqual(play(expense(), start=AMOUNT_HIGH, decisions={"Within?": "yes"})["failure"],
                         "'Within?': its conditions take ['no'], the scenario says yes")

    def test_tasks_run_or_are_stubbed(self):
        stubbed = play(expense(), start=AMOUNT_HIGH, tasks="stub", outputs={"Check": {"within_policy": True}},
                       expect={"path": ["Start", "Check", "Within?", "Book"]})
        self.assertTrue(stubbed["passed"], stubbed["failure"])
        self.assertNotIn("expense_id", stubbed["variables"])  # Book did not run
        given = play(expense(), start=AMOUNT_OK, outputs={"Check": {"within_policy": False}},
                     people={"Approve": {"approved": True, "more": False}}, expect={"visits": ["Approve"]})
        self.assertTrue(given["passed"], given["failure"])
        self.assertIn("expense_id", given["variables"])  # Book ran
        broken = play(expense(), start={"employee": "ann@acme.test", "amount": 5})
        self.assertIn("'Check' (check_expense_policy) fails", broken["failure"])
        self.assertIn("category", broken["failure"])

    def test_laya_and_llm_agents_never_run_their_answers_are_given(self):
        w = (Workflow("Draft").start("Start", "draft")
             .gateway("Route", "inclusive", "Which teams?", {"legal": "a contract", "sales": "an offer"})
             .llm_agent("Draft", "Write about {{topic}}", "draft")
             .user_task("Review", "Review the draft", "legal", form=[("ok", "OK", "boolean")])
             .manual_task("Archive", "Archive it", "sales")
             .flow("Start", "Route").flow("Route", "Draft", output="legal").flow("Draft", "Review")
             .flow("Route", "Archive", output="sales")).as_json()
        start = {"topic": "a supply contract"}
        self.assertEqual(play(w, start=start)["failure"], "'Route' is decided by Laya: give its branch in decisions")
        self.assertEqual(play(w, start=start, decisions={"Route": ["legal"]})["failure"],
                         "'Draft' is an LLM agent: give its answer in outputs")
        report = play(w, start=start, decisions={"Route": ["legal", "sales"]}, outputs={"Draft": "The draft."},
                      people={"Review": {"ok": True}}, expect={"visits": ["Review", "Archive"],
                                                              "variables": {"draft": "The draft.",
                                                                            "prompt": "Write about a supply contract"}})
        self.assertTrue(report["passed"], report["failure"])

    def test_messages_add_new_variables_and_a_race_can_time_out(self):
        w = (Workflow("Order").start("Start", "order").receive_task("Pay", "paid", "{{order}}")
             .event_gateway("Race", ["confirm", "cancel"], "{{order}}", (2, "days"))
             .manual_task("Ship", "Ship it", "store").manual_task("Late", "Call the customer", "store")
             .flow("Start", "Pay", "Race").flow("Race", "Ship", output="confirm")
             .flow("Race", "Late", output="timeout")).as_json()
        start, paid = {"order": "O-1", "amount": 10}, {"name": "paid", "payload": {"amount": 999, "ref": "TX-7"}}
        report = play(w, start=start, messages={"Pay": paid, "Race": {"name": "confirm"}},
                      expect={"ends": "Ship", "variables": {"amount": 10, "ref": "TX-7"}})
        self.assertTrue(report["passed"], report["failure"])
        self.assertTrue(play(w, start=start, messages={"Pay": paid}, decisions={"Race": "timeout"},
                             expect={"ends": "Late"})["passed"])
        self.assertEqual(play(w, start=start, messages={"Pay": paid})["failure"],
                         "'Race' waits for a message: give it in messages (or \"timeout\" in decisions)")
        self.assertEqual(play(w, start=start, messages={"Pay": {"name": "refund"}})["failure"],
                         "'Pay' waits for 'paid', not 'refund'")


class ProjectScenariosTest(TestCase):
    def test_a_failing_scenario_fails_the_project_check_and_the_command(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "workflow.json").write_text(json.dumps(expense()))
            (Path(folder) / "scenarios.json").write_text(json.dumps([
                {"name": "within policy", "start": AMOUNT_OK, "expect": {"ends": "Book"}},
                {"name": "wrong", "start": AMOUNT_OK, "expect": {"ends": "Refuse"}},
                {"name": "elsewhere", "workflow": "missing.json"}]))
            check = ProjectCheck(folder, fetch=lambda _: None)
            self.assertFalse(check.ok)
            reports = check.as_dict()["scenarios"]
            self.assertEqual([r["passed"] for r in reports], [True, False, False])
            self.assertEqual(reports[1]["failure"], "the case ends at 'Book', expected Refuse")
            self.assertIn("cannot play it: FileNotFoundError", reports[2]["failure"])
            out = io.StringIO()
            with self.assertRaises(CommandError):
                call_command("test_scenarios", folder, "--json", stdout=out)
            self.assertEqual([r["name"] for r in json.loads(out.getvalue())], ["within policy", "wrong", "elsewhere"])

    def test_studio_treats_a_failing_scenario_as_a_workflow_error(self):
        """Its verify step sends it to the LLM with the workflow's errors; the MCP refuses to publish."""
        from ..models import Project
        from ..studio.build import ProjectBuilder
        report = {"imports": {}, "examples": {}, "problems": [], "scenarios": [
            {"name": "ok", "passed": True, "failure": None}, {"name": "s", "passed": False, "failure": "never visits 'X'"}]}
        builder = ProjectBuilder(Project.objects.create(name="Scenario check"))
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(ProjectBuilder, "write_spec"), patch.object(ProjectBuilder, "workflow", return_value={}), \
                patch("bpmn8n.studio.build.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(report))):
            builder.draft = Path(folder)
            problems = builder.check()["problems"]
        self.assertEqual(problems, [{"node": "", "rule": "scenario", "level": "error",
                                     "message": "scenario 's': never visits 'X'"}])

    def test_a_real_case_becomes_a_scenario(self):
        """record_scenario: start data, branches, answers and task outputs from the event log; the replay
        passes and gives the exact path."""
        case_id, now = "wf:42", timezone.now()
        steps = [("Check", "check_expense_policy", "system", {"input": AMOUNT_HIGH, "output": {
                     "within_policy": False, "limit": 60, "excess": 440}}),
                 ("Within?", "conditions_gateway", "system", {"decider": "conditions", "input": {
                     "state": {**AMOUNT_HIGH, "within_policy": False, "limit": 60, "excess": 440}},
                     "output": {"branches": ["no"], "scores": {}}}),
                 ("Approve", "userTask", "carla", {"output": {"approved": False, "more": True}}),
                 ("Approved?", "conditions_gateway", "system", {"output": {"branches": ["more"], "scores": {}}}),
                 ("Clarify", "userTask", "ann", {"output": {"answer": "a client dinner"}}),
                 ("Approve", "userTask", "carla", {"output": {"approved": True, "more": False}}),
                 ("Approved?", "conditions_gateway", "system", {"output": {"branches": ["yes"], "scores": {}}}),
                 ("Book", "record_expense", "system", {"output": {"expense_id": "EXP-1", "status": "queued for payment"}})]
        for i, (activity, endpoint, resource, data) in enumerate(steps):
            at = now + timedelta(seconds=i)
            Event.objects.create(case_id=case_id, activity=activity, process="Expense", endpoint=endpoint, started=at,
                                 ended=at, duration_ms=1, status="complete", resource=resource, data=data)
        Case.objects.create(case_id=case_id, process="Expense", variables={
            **AMOUNT_HIGH, "within_policy": False, "limit": 60, "excess": 440, "approved": True, "more": False,
            "answer": "a client dinner", "expense_id": "EXP-1", "status": "queued for payment"})
        scenario, report = record(case_id, expense())
        self.assertTrue(report["passed"], report["failure"])
        self.assertEqual(scenario["start"], AMOUNT_HIGH)
        self.assertEqual(scenario["decisions"], {"Within?": "no", "Approved?": ["more", "yes"]})
        self.assertEqual(scenario["people"]["Approve"], [{"approved": False, "more": True}, {"approved": True, "more": False}])
        self.assertEqual(scenario["outputs"]["Book"], {"expense_id": "EXP-1", "status": "queued for payment"})
        self.assertEqual(scenario["expect"], {"path": ["Start", "Check", "Within?", "Approve", "Approved?", "Clarify",
                                                       "Approve", "Approved?", "Book"]})
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "workflow.json").write_text(json.dumps(expense()))
            call_command("record_scenario", case_id, folder, "--name", "case 42", stdout=io.StringIO())
            call_command("record_scenario", case_id, folder, "--name", "case 42", stdout=io.StringIO())  # replaced
            saved = json.loads((Path(folder) / "scenarios.json").read_text())
            self.assertEqual([s["name"] for s in saved], ["case 42"])
            call_command("test_scenarios", folder, stdout=io.StringIO())  # passes: no CommandError
        with self.assertRaisesRegex(ValueError, "no event of case 'wf:0'"):
            record("wf:0", expense())


def deadlocks(workflow):
    return [p for p in WorkflowCheck(workflow.as_json(), fetch=lambda _: None).problems if p.rule == "deadlock"]


class DeadlockSearchTest(TestCase):
    def test_merges_that_wait_for_each_other_get_stuck_and_never_fire(self):
        """Each Merge waits for a branch that comes out of the other one: a vicious circle ([AW] §3.3)."""
        w = (Workflow("Circle").start("Start", "circle").parallel_gateway("Fork", ["a", "b"])
             .task("A", "serviceTask", "x").task("C", "serviceTask", "x").task("D", "serviceTask", "x")
             .task("B", "serviceTask", "x").merge("M1").merge("M2")
             .flow("Start", "Fork").flow("Fork", "A", output="a").flow("A", "M1").flow("Fork", "C", output="b")
             .flow("C", "M2").flow("M1", "D", "M2").flow("M2", "B", "M1"))
        found = {(p.node, p.message.split(":")[0], p.level) for p in deadlocks(w)}
        self.assertEqual(found, {("M1", "a case can get stuck", "error"), ("M1", "this Merge never fires", "error"),
                                 ("M2", "this Merge never fires", "error")})
        self.assertEqual(deadlocks(w)[0].message, "a case can get stuck: tokens wait at M1, M2 for what never comes; "
                                                  "path: Start → Fork → A → C")  # only where tokens are

    def test_a_loop_no_case_leaves_is_found(self):
        """A parallel gateway in a loop sends a token back every round: the case never completes."""
        w = (Workflow("Spin").start("Start", "spin").task("A", "serviceTask", "x")
             .parallel_gateway("P", ["again", "out"]).task("B", "serviceTask", "x")
             .flow("Start", "A", "P").flow("P", "A", output="again").flow("P", "B", output="out"))
        [problem] = deadlocks(w)
        self.assertEqual((problem.node, problem.level), ("A", "error"))
        self.assertIn("no case leaves this loop (A, P)", problem.message)

    def test_sound_workflows_have_no_finding(self):
        joined = (Workflow("Join").start("Start", "join").parallel_gateway("G", ["a", "b"])
                  .task("A", "serviceTask", "x").task("B", "serviceTask", "x").merge("Join")
                  .condition_gateway("Again?", "exclusive", {"yes": "{{again}}"})
                  .flow("Start", "G").flow("G", "A", output="a").flow("G", "B", output="b")
                  .flow("A", "Join").flow("B", "Join").flow("Join", "Again?").flow("Again?", "G", output="yes"))
        self.assertEqual(deadlocks(joined), [])
        # an n8n If is a choice too: the Merge behind its second output fires
        behind_if = (Workflow("If").start("Start", "if").add("If", "n8n-nodes-base.if", {}, bpmn=False)
                     .task("A", "serviceTask", "x").parallel_gateway("P", ["b", "c"]).task("B", "serviceTask", "x")
                     .task("C", "serviceTask", "x").merge("M")
                     .flow("Start", "If", "A").flow("If", "P", output=1).flow("P", "B", output="b")
                     .flow("P", "C", output="c").flow("B", "M").flow("C", "M"))
        self.assertEqual(deadlocks(behind_if), [])
        for path in (settings.BASE_DIR / "examples/workflows").glob("*.json"):
            problems = WorkflowCheck(json.loads(path.read_text()), fetch=lambda _: None).problems
            self.assertEqual([p for p in problems if p.rule == "deadlock"], [], path)

    def test_too_many_combinations_are_cut_and_said(self):
        w, previous = Workflow("Many").start("Start", "many"), ["Start"]
        for i in range(7):  # 3 ** 7 cases
            w.gateway(f"G{i}", "exclusive", "?", {"a": "", "b": ""})
            for output in (["a", "b", "default"] if i else [0]):
                w.flow(previous[0], f"G{i}", output=output)
            previous = [f"G{i}"]
        [problem] = deadlocks(w)
        self.assertEqual(problem.level, "info")
        self.assertIn(f"could not play every case (more than {SEARCH_CASES}", problem.message)
        # a step four parallel branches reach stops its case early: the search cannot be sure either
        wide = (Workflow("Wide").start("Start", "wide").parallel_gateway("P", ["a", "b", "c", "d"])
                .task("Last", "serviceTask", "x").merge("M"))
        for branch in "abcd":
            wide.flow("P", "Last", output=branch)
        [problem] = deadlocks(wide.flow("Start", "P").flow("Last", "M"))  # not "M never fires"
        self.assertEqual(problem.level, "info")
