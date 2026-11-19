"""The process contract (workflows/contract.py: what the outside world sees of a workflow) and the landscape built
from the contracts (web/landscape.py)."""
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from projects.acme.processes import PROCESSES

from ..models import MessageSubscription, Project
from ..web.landscape import OUTSIDE, Landscape, Node
from ..workflows.builder import Workflow
from ..workflows.contract import Contract, local_workflows
from .support import add_events, step

ROOT = Path(__file__).resolve().parents[2]


def acme(process):
    return Contract(PROCESSES[process]().as_json())


def flow(name, build):
    """A small workflow: build(Workflow) adds its steps and flows."""
    return build(Workflow(name)).as_json()


class AcmeContractTest(TestCase):
    def test_ticket_triage_types_its_start_data_from_the_tasks(self):
        c = acme("ticket-triage")
        self.assertEqual(c.starts, [{"activity": "Ticket Received", "kind": "webhook", "path": "acme-ticket", "method": "POST"}])
        schema = c.start_schema()
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertLessEqual({"customer", "message"}, set(schema["required"]))
        self.assertEqual(schema["properties"]["customer"]["type"], "string")
        self.assertEqual(schema["properties"]["channel"]["enum"], ["email", "chat", "phone"])  # a Literal
        self.assertNotIn("channel", schema["required"])  # it has a default
        self.assertNotIn("team", schema["properties"])  # a person answers it: not start data
        self.assertEqual(c.lanes, {"support-leads": ["Triage Ticket"]})
        self.assertEqual({s["task"] for s in c.sends}, {"open_bug_report", "send_notification"})
        self.assertIn({"activity": "Triage Ticket", "fields": ["team"], "error": ""}, c.results)

    def test_a_variable_no_task_types_is_marked_unknown(self):
        c = acme("expense-approval")
        justification = c.start_schema()["properties"]["justification"]  # only Laya's state reads it
        self.assertEqual(justification["x-bpmn8n-type"], "unknown")
        self.assertIn("read by Assess Justification", justification["description"])
        self.assertEqual(c.start_schema()["properties"]["amount"]["type"], "number")
        self.assertEqual(c.lanes, {"managers": ["Approve Expense"]})
        self.assertIn("unknown", acme("incident-response").start_schema()["properties"]["hosts"]["x-bpmn8n-type"])  # {{hosts.0}}

    def test_library_lending_waits_for_two_messages_and_says_what_may_come_from_a_step(self):
        c = acme("library-lending")
        self.assertEqual([(m["message"], m["correlation_key"], m["timeout"], m["reads"]) for m in c.receives],
                         [("hold-book", "{{request_id}}", "1 weeks", []), ("decline-hold", "{{request_id}}", "1 weeks", [])])
        self.assertEqual(c.deadlines, [{"activity": "Wait for Reader", "due": "1 weeks", "interrupting": True}])
        self.assertEqual(c.fields["available"]["unless"], ["Get Book Status"])  # its task declares no output
        self.assertFalse(c.fields["available"]["required"])
        self.assertTrue(c.fields["request_id"]["required"])  # Get Book Status needs it before anything runs
        paths = c.openapi("http://n8n.test", "http://bpmn8n.test")["paths"]
        self.assertEqual(paths["/webhook/acme-library"]["post"]["servers"], [{"url": "http://n8n.test"}])
        deliver = paths["/api/messages/hold-book"]["post"]
        body = deliver["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(body["required"], ["correlation_key"])
        self.assertEqual(body["properties"]["process"]["const"], "ACME · Library lending")
        self.assertEqual(deliver["security"], [{"token": []}])
        self.assertIn("Waits for “hold-book” at Wait for Reader", c.summary())

    def test_every_acme_process_has_a_contract_from_the_command(self):
        out = StringIO()
        call_command("process_contract", "projects/acme", "--openapi", stdout=out)
        fragments = json.loads(out.getvalue())
        self.assertEqual(sorted(fragments), sorted(f"acme/{p}" for p in PROCESSES))
        self.assertTrue(all(any(path.startswith("/webhook/") for path in f["paths"]) for f in fragments.values()))
        out = StringIO()
        call_command("process_contract", "projects/acme", stdout=out)
        self.assertEqual(out.getvalue().count("Starts: POST /webhook/acme-"), 4)


class ProjectContractTest(TestCase):
    def test_every_tracked_project_declares_what_its_contract_carries(self):
        found = local_workflows(ROOT)
        projects = {key for key in found if "/" not in key}
        self.assertLessEqual({"motor_insurance_claim", "personal_data_breach_response", "hospital"}, projects)
        for key in projects:
            workflow, declared = found[key]
            c = Contract(workflow, declared, key)
            with self.subTest(key):
                self.assertTrue(c.starts, "a project's workflow starts somewhere")
                self.assertLessEqual(set(declared or ()), set(c.start_schema()["properties"]))
                self.assertTrue(c.openapi()["paths"])
                json.dumps(c.as_dict())  # all JSON

    def test_the_motor_claim_waits_for_the_police_report(self):
        workflow, declared = local_workflows(ROOT)["motor_insurance_claim"]
        c = Contract(workflow, declared)
        self.assertEqual([(m["message"], m["correlation_key"]) for m in c.receives],
                         [("Police Report Uploaded", "{{policy_number}}")])
        self.assertIn("/api/messages/Police%20Report%20Uploaded", c.openapi()["paths"])
        self.assertTrue(c.fields["text_description"]["declared"])  # declared, read by no step: still in the contract
        self.assertEqual(c.fields["amount_claimed"]["schema"]["type"], "number")  # Issue Payout's amount: {{amount_claimed}}
        self.assertEqual(set(c.lanes), {"anti-fraud", "claims-handlers", "senior-adjusters"})

    def test_a_data_breach_has_its_legal_deadline(self):
        workflow, declared = local_workflows(ROOT)["personal_data_breach_response"]
        self.assertIn({"activity": "Review and Send to Garante", "due": "until {{authority_deadline}}", "interrupting": False},
                      Contract(workflow, declared).deadlines)


class MessagesTest(TestCase):
    def test_a_message_payload_is_what_the_steps_after_it_read_and_nothing_provides(self):
        c = Contract(flow("Shop", lambda w: w.start("Order Placed", "shop")
                          .receive_task("Wait for Payment", "payment-confirmed", "{{order_id}}")
                          .task("Confirm", "sendTask", "send_notification", to="{{email}}", subject="Paid {{amount_paid}}")
                          .flow("Order Placed", "Wait for Payment", "Confirm")))
        self.assertIn("order_id", c.fields)  # the correlation key is read before the message
        self.assertEqual(c.receives[0]["reads"], ["amount_paid", "body", "email"])  # read only after it (body by name)
        self.assertEqual(c.receives[0]["kind"], "receiveTask")

    def test_what_a_workflow_sends(self):
        c = Contract(flow("Bank", lambda w: w.start("Paid", "bank")
                          .add("Tell the Shop", "n8n-nodes-base.httpRequest",
                               {"method": "POST", "url": "http://web:8000/api/messages/payment-confirmed"}, bpmn=False)
                          .task("Notify", "sendTask", "send_notification", fields={"message": "receipt-sent"}, to="a@b.c",
                                subject="x")
                          .flow("Paid", "Tell the Shop", "Notify")))
        self.assertEqual([(s["activity"], s["message"]) for s in c.sends],
                         [("Tell the Shop", "payment-confirmed"), ("Notify", "receipt-sent")])


@override_settings(API_TOKEN="s3cret")
class ContractApiTest(TestCase):
    def get(self, path):
        return self.client.get(path, HTTP_AUTHORIZATION="Bearer s3cret")

    def test_a_project_process_by_its_key(self):
        r = self.get("/api/contracts/acme/ticket-triage")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["name"], "ACME · Ticket triage")
        self.assertIn("customer", body["start_schema"]["required"])
        self.assertIn("/webhook/acme-ticket", body["openapi"]["paths"])
        self.assertTrue(body["summary"].startswith("ACME · Ticket triage"))
        self.assertEqual(self.client.get("/api/contracts/acme/ticket-triage").status_code, 401)

    @patch("bpmn8n.api.contracts.n8n.workflow")
    def test_an_n8n_workflow_keeps_its_projects_declared_start_data(self, workflow):
        workflow.return_value = local_workflows(ROOT)["motor_insurance_claim"][0] | {"id": "Wf7"}
        Project.objects.create(name="Motor insurance claim", slug="motor_insurance_claim", n8n_id="Wf7")
        body = self.get("/api/contracts/Wf7").json()
        workflow.assert_called_with("Wf7")
        self.assertEqual(body["workflow_id"], "Wf7")
        self.assertTrue(body["start_data"]["text_description"]["declared"])

    @patch("bpmn8n.api.contracts.n8n.workflow", return_value=None)
    def test_a_workflow_nobody_has_is_a_404(self, _):
        self.assertEqual(self.get("/api/contracts/nope").status_code, 404)


def contract(name, build, workflow_id="", active=None, cases=0):
    return Node(name, Contract(flow(name, build)), workflow_id=workflow_id, active=active, cases=cases)


class LandscapeTest(TestCase):
    def landscape(self):
        return Landscape([
            contract("Bank", lambda w: w.start("Paid", "bank").add(
                "Tell the Shop", "n8n-nodes-base.httpRequest", {"url": "http://web/api/messages/order-paid"}, bpmn=False)
                .flow("Paid", "Tell the Shop")),
            contract("Shop", lambda w: w.start("Order", "shop").receive_task("Wait", "order-paid", "{{order}}")
                     .call_activity("Ship", "d1").call_activity("Audit", "zz").flow("Order", "Wait", "Ship", "Audit")),
            contract("Shipping", lambda w: w.subprocess_start("Called").merge("Done", 1).flow("Called", "Done"),
                     workflow_id="d1", active=False),
            contract("Leftover", lambda w: w.subprocess_start("Called").merge("Done", 1).flow("Called", "Done")),
            contract("Fax desk", lambda w: w.start("In", "fax").receive_task("Wait for fax", "fax", "{{ref}}")
                     .flow("In", "Wait for fax")),
        ], [("fax", "Fax desk", 3, 2), ("ping", "Legacy", 1, 0)])

    def test_edges_are_calls_and_messages_a_known_process_sends(self):
        view = self.landscape()
        edges = {(e.source, e.target, e.kind, e.label, e.unsent) for e in view.edges}
        self.assertIn(("Bank", "Shop", "message", "order-paid", False), edges)
        self.assertIn(("Shop", "Shipping", "call", "Ship", False), edges)
        self.assertIn((OUTSIDE, "Fax desk", "message", "fax", True), edges)
        self.assertIn((OUTSIDE, "Legacy", "message", "ping", True), edges)  # seen only as a subscription
        self.assertIn(("Shop", "Workflow zz", "call", "Audit", False), edges)
        self.assertTrue(view.by_name["Workflow zz"].missing)

    def test_findings(self):
        found = {(f.kind, f.subject): f for f in self.landscape().findings}
        self.assertEqual(set(found), {("unsent", "fax"), ("unsent", "ping"), ("unpublished", "Shipping"),
                                      ("unknown", "Workflow zz"), ("orphan", "Leftover")})
        self.assertIn("must come from outside", found["unsent", "fax"].text)
        self.assertIn("delivered 2 times so far", found["unsent", "fax"].text)
        self.assertEqual(found["orphan", "Leftover"].level, "info")
        self.assertNotIn(("unsent", "order-paid"), found)  # the bank sends it

    def test_layers_put_a_workflow_after_what_calls_or_messages_it(self):
        view = self.landscape()
        depth = {name: i for i, layer in enumerate(view.layers) for name in layer}
        self.assertLess(depth["Bank"], depth["Shop"])
        self.assertLess(depth["Shop"], depth["Shipping"])
        self.assertEqual(view.alone, ["Leftover"])
        self.assertEqual(len(view.positions), len(view.nodes))

    def test_the_drawing_escapes_names_and_marks_the_edges(self):
        view = Landscape([contract("<script>x</script>", lambda w: w.start("In", "x").receive_task("W", "m", "{{k}}")
                                   .flow("In", "W"))])
        svg = view.svg()
        self.assertNotIn("<script>", svg)
        self.assertIn('data-source="Outside" data-target="&lt;script&gt;x&lt;/script&gt;"', svg)
        self.assertIn('class="edge message unsent"', svg)


class LandscapePageTest(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("ann", password="pw"))

    @patch("bpmn8n.web.landscape.n8n.pages")
    def test_the_page_draws_projects_n8n_and_the_log(self, pages):
        pages.return_value = [{"id": "n1", "name": "Dispatch", "active": True, "nodes": Workflow("Dispatch").start(
            "In", "dispatch").call_activity("Run", "gone").flow("In", "Run").as_json()["nodes"],
            "connections": {"In": {"main": [[{"node": "Run", "type": "main", "index": 0}]]}}}]
        add_events(("w9:1", "Old intake", [step("Read", 0)]))
        MessageSubscription.objects.create(name="Police Report Uploaded", correlation_key="P1", resume_url="http://x/1",
                                           activity="Wait", process="Motor insurance claim")
        page = self.client.get("/landscape/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '<a class="nav-item" href="/landscape/" aria-current="page">')
        self.assertContains(page, "<b8-process-map")
        self.assertContains(page, "Motor insurance claim")  # a project
        self.assertContains(page, "Old intake")  # the event log
        self.assertContains(page, "Dispatch calls Workflow gone (Run), which neither n8n nor a project has")
        self.assertContains(page, "“Police Report Uploaded”: Motor insurance claim waits for it and no known process sends it")
        self.assertContains(page, '<table class="landscape">')

    def test_it_needs_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/landscape/").status_code, 302)
