import json
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from projects.acme.processes import PROCESSES
from ..workflows import SpecError, Workflow
from ..workflows.check import WorkflowCheck

EXAMPLES = Path(__file__).parents[2] / "examples" / "workflows"
LANES = ("managers", "librarians", "support-leads", "soc-analysts")


def rules(workflow, **kwargs):
    return {(p.node, p.rule, p.level) for p in WorkflowCheck(workflow, fetch=lambda _: None, **kwargs).problems}


class BuilderTest(SimpleTestCase):
    SPEC = {"name": "Purchases", "steps": [
        {"type": "start", "name": "Request", "path": "purchase"},
        {"type": "parallel_gateway", "name": "Both", "branches": ["quote", "budget"]},
        {"type": "receive_task", "name": "Quote", "message": "quote-received", "correlation_key": "{{request_id}}"},
        {"type": "task", "name": "Budget", "kind": "businessRuleTask", "endpoint": "check_expense_policy"},
        {"type": "merge", "name": "Join"},
        {"type": "gateway", "name": "Decide", "kind": "exclusive", "question": "Buy it?",
         "branches": {"buy": "worth it"}},
        {"type": "user_task", "name": "Review", "title": "Review {{item}}", "lane": "managers",
         "form": [["ok", "OK", "boolean"]]}],
        "flows": [{"from": "Request", "to": ["Both"]},
                  {"from": "Both", "branch": "quote", "to": ["Quote", "Join"]},
                  {"from": "Both", "branch": "budget", "to": ["Budget", "Join"]},
                  {"from": "Join", "to": ["Decide"]},
                  {"from": "Decide", "branch": "default", "to": ["Review"]}]}

    def test_a_timer_waits_for_a_span_or_until_a_date_of_the_case(self):
        w = Workflow("Deadline").start("In", "in").timer("Two Days", 2, "days").timer("Before Departure", until="{{alert_date}}")
        params = {n["name"]: n["parameters"] for n in w.nodes}
        self.assertEqual(params["Two Days"], {"mode": "for", "amount": 2, "unit": "days"})
        self.assertEqual(params["Before Departure"], {"mode": "until", "until": "={{ $json.alert_date }}"})
        with self.assertRaises(SpecError):
            Workflow("Bad").timer("When", until="15 days before departure")

    def test_no_flow_passes_behind_a_step(self):
        """A detour (a branch that joins its sibling later) goes below the direct flow, and a branch keeps
        its row up to the step it joins: a flow along a row never crosses another step."""
        def behind(workflow):
            at = {n["name"]: tuple(n["position"]) for n in workflow["nodes"]}
            return [(a, b, c) for a, outs in workflow["connections"].items() for port in outs.get("main", [])
                    for b in (link["node"] for link in port) if at[a][1] == at[b][1] and at[b][0] > at[a][0]
                    for c, (x, y) in at.items() if y == at[a][1] and at[a][0] < x < at[b][0]]
        detour = (Workflow("Detour").start("In", "in")
                  .condition_gateway("Personal Data?", "exclusive", {"yes": "{{personal}}", "no": "not {{personal}}"})
                  .user_task("Confirm", "Confirm", "dpo", form=[["personal", "Personal", "boolean"]])
                  .condition_gateway("Overruled?", "exclusive", {"yes": "{{personal}}", "no": "not {{personal}}"})
                  .user_task("Assess", "Assess", "dpo", form=[["risk", "Risk", "boolean"]])
                  .manual_task("Record", "Record", "dpo")
                  .flow("In", "Personal Data?").flow("Personal Data?", "Assess", "Record", output=0)
                  .flow("Personal Data?", "Confirm", "Overruled?", output=1).flow("Overruled?", "Assess", output=0)
                  .flow("Overruled?", "Record", output=1).layout().as_json())
        rows = {n["name"]: n["position"][1] for n in detour["nodes"]}
        self.assertEqual((rows["Assess"], rows["Record"]), (0, 0))  # the direct flow stays on the top row
        self.assertGreater(rows["Confirm"], 0)  # the detour goes below it
        for workflow in [detour, *(build().as_json() for build in PROCESSES.values())]:
            self.assertEqual(behind(workflow), [], workflow["name"])

    def test_a_spec_laid_out_in_levels(self):
        """Time runs right; the longest branch stays on the gateway's row, the other goes below, the join after both."""
        at = {n["name"]: n["position"] for n in Workflow.from_spec(self.SPEC).as_json()["nodes"]}
        self.assertEqual(at["Request"], [0, 0])
        self.assertEqual(sorted([at["Quote"][1], at["Budget"][1]]), [at["Both"][1], at["Both"][1] + 200])
        self.assertEqual(at["Quote"][0], at["Budget"][0])
        self.assertTrue(at["Request"][0] < at["Both"][0] < at["Quote"][0] < at["Join"][0] < at["Review"][0])

    def test_a_spec_builds_a_laid_out_workflow(self):
        workflow = Workflow.from_spec(self.SPEC).as_json()
        nodes = {n["name"]: n for n in workflow["nodes"]}
        self.assertEqual(nodes["Quote"]["parameters"], {"message": "quote-received", "correlationKey": "{{request_id}}"})
        self.assertEqual([c["index"] for port in (workflow["connections"]["Quote"]["main"],
                                                  workflow["connections"]["Budget"]["main"]) for c in port[0]], [0, 1])
        self.assertEqual(workflow["connections"]["Decide"]["main"][1], [{"node": "Review", "type": "main", "index": 0}])
        self.assertEqual([nodes[n]["position"][0] for n in ("Request", "Both", "Quote", "Join", "Decide")],
                         [0, 280, 560, 840, 1120])  # columns by distance from the trigger

    def test_a_join_comes_after_its_longest_branch_and_a_loop_moves_nothing(self):
        w = (Workflow("Join").start("Start", "j").parallel_gateway("Split", ["a", "b"])
             .timer("Short", 1, "minutes").timer("Long 1", 1, "minutes").timer("Long 2", 1, "minutes").merge("Join")
             .manual_task("Check", "Check", "staff"))
        w.flow("Start", "Split").flow("Split", "Short", "Join", output="a")
        w.flow("Split", "Long 1", "Long 2", "Join", output="b").flow("Join", "Check", "Split")  # Check loops back
        columns = {n["name"]: n["position"][0] // 280 for n in w.layout().nodes}
        self.assertEqual([columns[n] for n in ("Start", "Split", "Short", "Long 1", "Long 2", "Join", "Check")],
                         [0, 1, 2, 2, 3, 4, 5])

    def test_two_flows_through_the_same_steps_add_them_once(self):
        workflow = (Workflow("Twice").start("Start", "t").condition_gateway("Small?", "exclusive", {"yes": "true"})
                    .task("Order", "sendTask", "send_notification").task("Confirm", "sendTask", "send_notification")
                    .flow("Start", "Small?").flow("Small?", "Order", "Confirm", output="yes")
                    .flow("Small?", "Order", "Confirm", output="default"))
        self.assertEqual(workflow.connections["Order"]["main"], [[{"node": "Confirm", "type": "main", "index": 0}]])

    def test_spec_mistakes_are_named(self):
        for steps, flows, error in (
                ([{"type": "delete_everything", "name": "X"}], [], "unknown type"),
                ([{"type": "timer", "name": "T"}], [], "missing"),
                ([{"type": "timer", "name": "T", "amount": 1, "unit": "days"}] * 2, [], "two steps"),
                ([{"type": "timer", "name": "T", "amount": 1, "unit": "days"}], [{"from": "T", "to": ["Nope"]}], "no step"),
                (self.SPEC["steps"], [{"from": "Decide", "branch": "maybe", "to": ["Review"]}], "no output 'maybe'")):
            with self.subTest(error), self.assertRaisesRegex(SpecError, error):
                Workflow.from_spec({"steps": steps, "flows": flows})

    def test_an_llms_spec_of_the_wrong_shape_goes_back_to_it(self):
        """Whatever shape the LLM gets wrong is a SpecError (Studio sends those back), never a crash."""
        start = {"type": "start", "name": "In", "path": "in"}
        for steps, error in (
                ({"In": start}, "lists of objects"),
                ([start, {"type": "gateway", "name": "G", "kind": "exclusive", "question": "q", "branches": ["a"]}],
                 "AttributeError"),
                ([start, {"type": "user_task", "name": "U", "title": "t", "lane": "l",
                          "form": [{"name": "ok", "label": "OK", "type": "boolean"}]}], r"\[name, label, type\]"),
                ([start, {"type": "event_gateway", "name": "E", "messages": ["m"], "correlation_key": "k",
                          "timeout": {"amount": 1}}], "KeyError")):
            with self.subTest(error), self.assertRaisesRegex(SpecError, error):
                Workflow.from_spec({"steps": steps})
        timer = {"type": "timer", "name": "Wait", "amount": 1, "unit": "days"}
        workflow = Workflow.from_spec({"steps": [start, timer], "flows": [{"from": "In", "branch": None, "to": "Wait"}]})
        self.assertEqual(workflow.connections["In"]["main"][0][0]["node"], "Wait")  # null branch, a single "to"


    def test_an_llm_agent_is_four_nodes_with_its_chat_model(self):
        model = {"type": "@n8n/n8n-nodes-langchain.lmChatGoogleGemini", "typeVersion": 1.2,
                 "parameters": {"modelName": "models/gemini-3.5-flash"}, "credentials": {"googlePalmApi": {"id": "7"}}}
        workflow = Workflow.from_spec({"steps": [
            {"type": "start", "name": "In", "path": "in"},
            {"type": "llm_agent", "name": "Draft", "prompt": "Reply to {{message}}", "output": "draft"},
            {"type": "user_task", "name": "Review", "title": "Review", "lane": "managers", "form": []}],
            "flows": [{"from": "In", "to": ["Draft", "Review"]}]}, chat_model=model).as_json()
        nodes, links = {n["name"]: n for n in workflow["nodes"]}, workflow["connections"]
        self.assertEqual([links[n]["main"][0][0]["node"] for n in ("In", "Draft: prompt", "Draft", "Draft: answer")],
                         ["Draft: prompt", "Draft", "Draft: answer", "Review"])
        self.assertEqual(links["Draft: model"], {"ai_languageModel": [[{"node": "Draft", "type": "ai_languageModel", "index": 0}]]})
        self.assertEqual(nodes["Draft: model"]["credentials"], {"googlePalmApi": {"id": "7"}})
        self.assertEqual(nodes["Draft: prompt"]["parameters"]["input"]["value"], {"prompt": "Reply to {{message}}"})
        self.assertIn("never present your answer as a final decision", nodes["Draft"]["parameters"]["options"]["systemMessage"])
        # the provider down (after 3 tries): the error goes on as its answer, for the person after it
        self.assertEqual((nodes["Draft"]["retryOnFail"], nodes["Draft"]["onError"]), (True, "continueRegularOutput"))
        self.assertIn("The LLM did not answer", nodes["Draft: answer"]["parameters"]["assignments"]["assignments"][0]["value"])
        self.assertEqual(nodes["Draft: model"]["position"][0], nodes["Draft"]["position"][0])  # under its agent
        self.assertGreater(nodes["Draft: model"]["position"][1], nodes["Draft"]["position"][1])


class CheckTest(TestCase):
    def setUp(self):
        Group.objects.bulk_create(Group(name=lane) for lane in LANES)

    def test_the_examples_pass(self):
        for path in EXAMPLES.glob("*.json"):
            problems = WorkflowCheck(json.loads(path.read_text()), fetch=lambda _: None).problems
            self.assertEqual([p for p in problems if p.level == "error"], [], path.name)
        self.assertEqual(rules(PROCESSES["ticket-triage"]().as_json()),  # it refunds and closes tickets on its own
                         {("", "start-data", "info"), ("Route Ticket", "human", "warning")})

    def test_people_take_the_final_decisions(self):
        def agent(*after):
            workflow = (Workflow("Drafts").start("In", "in").llm_agent("Draft", "Reply to {{message}}", "draft"))
            for step in after:
                step(workflow)
            return workflow.flow("In", "Draft", *[n["name"] for n in workflow.nodes[4:]]).as_json()
        review = lambda w: w.user_task("Review", "Check {{draft}}", "managers", form=[["approved", "OK", "boolean"]])  # noqa: E731
        send = lambda w: w.task("Send", "sendTask", "send_notification")  # noqa: E731
        self.assertIn(("Draft", "human", "error"), rules(agent(send)))  # the LLM's answer goes out unchecked
        reviewed = rules(agent(review, send), start_data=["message"])
        self.assertFalse({p for p in reviewed if p[1] in ("human", "reachable", "variable")}, reviewed)
        approved = (Workflow("Approval").start("In", "in")
                    .user_task("Review", "Approve?", "managers", form=[["approved", "OK", "boolean"]])
                    .condition_gateway("Approved?", "exclusive", {"yes": "{{approved}}"})
                    .task("Send", "sendTask", "send_notification")
                    .flow("In", "Review", "Approved?", "Send")).as_json()
        self.assertNotIn("human", {p[1] for p in rules(approved)})  # it applies the person's decision

    def test_a_workflow_that_does_nothing_is_an_error(self):
        self.assertIn(("", "empty", "error"), rules(Workflow("Empty").as_json()))
        self.assertIn(("", "empty", "error"), rules(Workflow("Only the trigger").start("In", "in").as_json()))

    def test_what_would_fail_at_run_time_is_an_error(self):
        workflow = (Workflow("Broken").start("Start", "broken")
                    .task("Ghost", "serviceTask", "summon_ghost")
                    .task("Wrong Node", "sendTask", "check_expense_policy")  # a businessRuleTask
                    .condition_gateway("Bad Condition", "exclusive", {"evil": "__import__('os')", "empty": ""})
                    .timer("Orphan", 1, "days")
                    .flow("Start", "Ghost", "Wrong Node", "Bad Condition")).as_json()
        self.assertTrue({("Ghost", "task", "error"), ("Wrong Node", "task", "error"),
                         ("Bad Condition", "condition", "error"), ("Bad Condition", "condition", "warning"),
                         ("Bad Condition", "output", "warning"), ("Orphan", "reachable", "error")} <= rules(workflow))

    def test_branches_at_once_may_not_both_wait(self):
        """n8n parks an execution on one wait: a person's answer would wait for the other branch's timer."""
        def both(timer):
            return rules(timer(Workflow("Deadline").start("In", "in").parallel_gateway("Both", ["review", "deadline"])
                               .user_task("Review", "Review", "managers", form=[["ok", "OK", "boolean"]]))
                         .flow("In", "Both").flow("Both", "Review", output=0).flow("Both", "Wait", output=1).as_json())
        self.assertIn(("Both", "wait", "error"), both(lambda w: w.timer("Wait", until="{{alert_date}}")))
        self.assertIn(("Both", "wait", "error"), both(lambda w: w.timer("Wait", 2, "days")))
        self.assertNotIn(("Both", "wait", "error"), both(lambda w: w.timer("Wait", 30, "seconds")))  # sleeps in place

    def test_probable_mistakes_are_warnings(self):
        workflow = (Workflow("Odd").start("Start", "odd")
                    .gateway("Route", "exclusive", "Which?", {"a": "x"})
                    .user_task("Approve", "Approve", "nobody-has-this-role", form=[])
                    .task("Refund", "businessRuleTask", "calculate_refund")
                    .flow("Start", "Route", "Approve", "Refund")).as_json()
        self.assertEqual(rules(workflow), {("Route", "output", "warning"),  # "default" leads nowhere
                                           ("Approve", "lane", "warning"), ("", "start-data", "info")})
        self.assertIn(("", "trigger", "warning"), rules({"nodes": [workflow["nodes"][1]], "connections": {}}))

    def test_the_start_data_a_workflow_needs_is_listed(self):
        def start_data(workflow):
            return [p.message for p in WorkflowCheck(workflow).problems if p.rule == "start-data"]
        known = (Workflow("Known").start("Start", "k").task("Register", "serviceTask", "register_ticket")
                 .task("Refund", "businessRuleTask", "calculate_refund")
                 .flow("Start", "Register", "Refund")).as_json()  # register_ticket's result has no amount
        self.assertEqual(start_data(known), ["the data that starts a case must carry: amount, customer, message"])
        known["nodes"][2]["parameters"]["input"]["value"] = {"amount": "{{total}}"}
        self.assertEqual(start_data(known), ["the data that starts a case must carry: customer, message"])

    def test_with_the_start_data_declared_what_nothing_provides_is_an_error(self):
        def problems(workflow, start_data):
            return {(p.node, p.rule) for p in WorkflowCheck(workflow, fetch=lambda _: None, start_data=start_data).problems}
        workflow = (Workflow("Mileage").start("Start", "m").task("Book", "serviceTask", "record_expense")
                    .task("Tell", "sendTask", "send_notification", to="{{employee}}", subject="Booked {{registration_id}}")
                    .flow("Start", "Book", "Tell")).as_json()
        # record_expense produces expense_id, not registration_id; amount and approved come from nowhere
        self.assertEqual(problems(workflow, ["employee"]), {("Book", "input"), ("Tell", "variable")})
        workflow["nodes"][2]["parameters"]["input"]["value"]["subject"] = "Booked {{expense_id}}"
        self.assertEqual(problems(workflow, ["employee", "amount", "approved"]), set())
        self.assertIn(("", "start-data"), problems(workflow, None))  # not declared: only listed

    def test_a_call_activity_must_not_wait(self):
        workflow = Workflow("Parent").start("Start", "p").call_activity("Sub", "42").flow("Start", "Sub").as_json()
        waits = {"nodes": [{"name": "Ask", "type": "CUSTOM.bpmn8nUserTask", "parameters": {}}]}
        problems = WorkflowCheck(workflow, fetch=lambda wid: waits if wid == "42" else None).problems
        self.assertEqual([(p.node, p.level) for p in problems if p.rule == "subprocess"], [("Sub", "error")])

    def test_endpoint_and_command(self):
        r = self.client.post("/api/workflows/check", PROCESSES["expense-approval"]().as_json(),
                             content_type="application/json")
        self.assertTrue(r.json()["ok"], r.json())
        out = StringIO()
        call_command("check_workflow", str(EXAMPLES / "library-lending.json"), "--json", stdout=out)
        problems = json.loads(out.getvalue())[str(EXAMPLES / "library-lending.json")]
        self.assertEqual([(p["rule"], p["level"]) for p in problems], [("human", "warning")])  # "on loan" ends it by rule
        broken = Path(self.id().replace(".", "_") + ".json")
        broken.write_text(json.dumps(Workflow("x").start("S", "s").task("T", "serviceTask", "nope").flow("S", "T").as_json()))
        try:
            with self.assertRaises(CommandError):
                call_command("check_workflow", str(broken), stdout=StringIO())
        finally:
            broken.unlink()
