import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from ..api import TASKS, task
from ..api.tasks import Task, is_mushy
from .support import fake_laya


class ApiTest(TestCase):
    def post(self, url, data, **headers):
        return self.client.post(url, data, content_type="application/json", headers=headers)

    def test_tasks_are_discoverable_in_openapi(self):
        spec = self.client.get("/api/openapi.json").json()
        tasks = {op["operationId"]: op["x-bpmn-task"] for p in spec["paths"].values()
                 for op in p.values() if "x-bpmn-task" in op}
        self.assertEqual(tasks["register_ticket"], "serviceTask")
        self.assertEqual(tasks["check_expense_policy"], "businessRuleTask")
        self.assertEqual(tasks["normalize_alert"], "scriptTask")
        self.assertEqual(tasks["send_notification"], "sendTask")

    def test_task_runs_and_validates(self):
        r = self.post("/api/tasks/check_expense_policy", {"employee": "ann", "category": "meal", "amount": 150,
                                                           "attendees": 2})
        self.assertEqual(r.json(), {"within_policy": False, "limit": 120.0, "excess": 30.0})
        bad = self.post("/api/tasks/check_expense_policy", {"employee": "ann", "category": "yacht", "amount": 1})
        self.assertEqual(bad.status_code, 422)

    def test_metrics_and_profile(self):
        self.post("/api/tasks/normalize_alert", {"source": "edr", "raw": " host-7  beacon to 10.0.0.9 "},
                  X_Bpmn8n_Profile="1")
        m = self.client.get("/api/metrics").json()["normalize_alert"]
        self.assertGreaterEqual(m["calls"], 1)
        self.assertIn("function calls", m["last_profile"]["cprofile"])
        self.assertIn("peak_kb", m["last_profile"])

    @patch("bpmn8n.clients.laya.LayaClient.ask", fake_laya)
    def test_exclusive_gateway(self):
        body = {"question": "Which department?", "state": {"body": "charged twice"},
                "branches": {"billing": "payments", "technical": "bugs"}}
        self.assertEqual(self.post("/api/gateways/exclusive", body).json(), {"branches": ["billing"], "scores": {"billing": 0.8}})
        low = self.post("/api/gateways/exclusive", {**body, "min_confidence": 0.95}).json()
        self.assertEqual(low["branches"], [])  # -> default flow

    @patch("bpmn8n.clients.laya.LayaClient.ask", fake_laya)
    def test_inclusive_gateway(self):
        body = {"question": "Does the customer need", "state": "charged twice",
                "branches": {"refund": "a refund?", "apology": "an apology?"}}
        r = self.post("/api/gateways/inclusive", body).json()
        self.assertEqual(r["branches"], ["refund"])

    def test_laya_down_is_502(self):
        with self.settings(LAYA_URL="http://127.0.0.1:9/v1/systemone"):
            body = {"question": "q", "state": "s", "branches": {"a": "x", "b": "y"}}
            self.assertEqual(self.post("/api/gateways/exclusive", body).status_code, 502)

    def test_event_log_records_cases(self):
        alert = {"source": "edr", "raw": "a"}
        self.post("/api/tasks/normalize_alert", alert, X_Bpmn8n_Case="42", X_Bpmn8n_Activity="Normalize, alert")
        self.post("/api/tasks/normalize_alert", alert)  # no case: a metric, not a case step
        rows = b"".join(self.client.get("/api/eventlog.csv").streaming_content).decode().splitlines()
        self.assertEqual(rows[0], "case_id,activity,endpoint,start_timestamp,end_timestamp,status,resource")
        self.assertEqual(len(rows), 2)
        self.assertTrue(rows[1].startswith('42,"Normalize, alert",normalize_alert,'))  # quoted CSV cell
        self.assertTrue(rows[1].endswith(",complete,system"))
        self.assertEqual(self.client.get("/api/metrics").json()["normalize_alert"]["calls"], 2)

    def test_mushy_task_names(self):
        self.assertTrue(is_mushy("manage_orders"))
        self.assertFalse(is_mushy("issue_refund"))

    @override_settings(API_TOKEN="s3cret")
    def test_token_required(self):
        self.assertEqual(self.post("/api/tasks/calculate_refund", {"amount": 1}).status_code, 401)
        r = self.post("/api/tasks/calculate_refund", {"amount": 1}, Authorization="Bearer s3cret")
        self.assertEqual(r.status_code, 200)


class TaskRegistryTest(TestCase):
    def test_a_declared_output_is_documented_and_known(self):
        spec = self.client.get("/api/openapi.json").json()
        ok = spec["paths"]["/api/tasks/register_ticket"]["post"]["responses"]["200"]["content"]["application/json"]
        self.assertIn("TicketRecord", ok["schema"]["$ref"])
        self.assertEqual([f.name for f in TASKS["register_ticket"].outputs], ["ticket_id", "customer", "message"])
        r = self.client.post("/api/tasks/register_ticket/run", {"fields": {"customer": "Contoso", "message": "hi"}},
                             content_type="application/json")
        self.assertTrue(r.json()["result"]["ticket_id"].startswith("TCK-"))

    def test_a_task_needs_one_schema_argument(self):
        with self.assertRaisesRegex(TypeError, "one Schema argument"):
            Task(lambda request, text: None, "serviceTask")

    def test_examples_and_results_are_checked(self):
        """A result that breaks the declared Schema fails where it is made; an example says what is wrong."""
        from ninja import Schema

        class Number(Schema):
            n: int

        class Doubled(Schema):
            doubled: int

        def double_number(request, data: Number) -> Doubled:
            return {"doubled": data.n * 2} if data.n < 10 else {"tripled": data.n * 3}

        double = Task(double_number, "scriptTask")
        self.assertIsNone(double.check({"input": {"n": 2}, "output": {"doubled": 4}}))
        self.assertIn("returned {'doubled': 4}, expected {'doubled': 5}", double.check({"input": {"n": 2}, "output": {"doubled": 5}}))
        self.assertIn("returned {'tripled': 60}, not a Doubled", double.check({"input": {"n": 20}, "output": {}}))
        self.assertIn("ValidationError", double.check({"input": {}, "output": {}}))  # n is required

    def test_every_project_passes_its_checks(self):
        """Each project's activities do what its examples.json says, and its workflow passes the check."""
        from django.conf import settings
        from ..workflows.check import ProjectCheck
        for app in settings.PROJECTS:
            check = ProjectCheck(settings.BASE_DIR / app.replace(".", "/"), fetch=lambda _: None)
            self.assertTrue(check.ok, (app, check.as_dict()))
        self.assertIn("check_expense_policy", json.loads((settings.BASE_DIR / "projects/acme/examples.json").read_text()))

    def test_a_module_that_fails_to_import_is_left_out(self):
        """A broken project does not take the server down: it is logged, the others load."""
        from ..api import discover
        with patch("bpmn8n.api.tasks.task_modules", return_value=["projects.acme.tasks", "projects.no_such.tasks"]), \
                self.assertLogs("bpmn8n", "ERROR"):
            self.assertEqual(list(discover()), ["projects.no_such.tasks"])

    def test_a_name_is_registered_once(self):
        def register_ticket(request, data: TASKS["register_ticket"].schema):
            pass
        with self.assertRaisesRegex(ValueError, "already registered by projects.acme.tasks"):
            task()(register_ticket)


class OcrTest(TestCase):
    def test_a_document_is_read_and_its_image_not_logged(self):
        from ..models import Event
        from .support import LocalServer
        with LocalServer(b'{"text": "Codice NRE: 1200A4567890123", "confidence": 0.98, "lines": []}') as ocr, \
                override_settings(OCR_URL=ocr.url):
            r = self.client.post("/api/tasks/read_document_text/run", {"item": {"image": "data:image/jpeg;base64,/9j/4AAQ"}},
                                 content_type="application/json", headers={"X-Bpmn8n-Case": "wf:1"})
            self.assertEqual(r.json()["result"], {"document_text": "Codice NRE: 1200A4567890123", "ocr_confidence": 0.98})
            self.assertEqual(ocr.requests[0][2], b'{"image": "data:image/jpeg;base64,/9j/4AAQ"}')
        logged = Event.objects.get(case_id="wf:1", endpoint="read_document_text").data
        self.assertNotIn("/9j/4AAQ", str(logged))  # the photo stays out of the event log


class PagesTest(TestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        self.assertEqual(self.client.get("/docs/").status_code, 302)  # people only
        self.client.force_login(User.objects.create_user("ann", password="pw"))

    def test_the_guide_with_its_index(self):
        page = self.client.get("/docs/")
        self.assertContains(page, '<a href="/docs/getting-started/">Getting started</a>', html=True)  # the index of its sections
        page = self.client.get("/docs/getting-started/")  # one section per page
        self.assertContains(page, '<h2 id="getting-started">Getting started</h2>', html=True)
        self.assertContains(page, 'rel="next"')
        self.assertEqual(self.client.get("/docs/no-such-section/").status_code, 404)
        self.assertNotContains(page, "register_ticket</strong>")  # the activities have their own page

    def test_the_activities_with_their_decorators(self):
        page = self.client.get("/activities/", {"q": "register_ticket"})
        self.assertContains(page, '@task(&quot;serviceTask&quot;)\ndef register_ticket(request, data: Ticket) -&gt; TicketRecord:')
        self.assertContains(page, "<td>ticket_id</td>", html=True)  # its declared result
        sends = self.client.get("/activities/", {"kind": "sendTask"})
        self.assertContains(sends, "send_notification")
        self.assertNotContains(sends, "register_ticket")
