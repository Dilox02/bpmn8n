from unittest.mock import patch

from django.test import TestCase, override_settings

from ..core.variables import demo as variables_demo
from ..models import Case, Event


@override_settings(N8N_URL="http://n8n:5678")
class ProcessVariablesTest(TestCase):
    """Nodes refer to process data as {{name}}, or not at all: fields bind by name."""

    CASE = {"X-Bpmn8n-Case": "wf1:7", "X-Bpmn8n-Process": "Library", "X-Bpmn8n-Activity": "Step"}

    def post(self, url, body):
        return self.client.post(url, body, content_type="application/json", headers=self.CASE)

    def test_template_rules(self):
        variables_demo()

    def test_fields_bind_by_name_across_steps(self):
        webhook = {"headers": {"x": "y"}, "body": {"request_id": "R-1", "book": "Dune"}}
        first = self.post("/api/tasks/get_book_status/run", {"item": webhook})  # nothing mapped on the node
        self.assertEqual(first.json()["result"], {"request_id": "R-1", "book": "Dune", "available": False})
        # a later step with no item at all still finds the case's variables, results included
        later = self.post("/api/tasks/send_notification/run", {"fields": {
            "to": "{{request_id}}@acme.test", "subject": "{{book}} available: {{available}}"}})
        self.assertEqual(later.json()["result"]["subject"], "Dune available: False")
        self.assertEqual(Case.objects.get(pk="wf1:7").variables["sent"], True)

    def test_a_lone_template_keeps_the_type(self):
        self.post("/api/tasks/normalize_alert/run", {"item": {"total": 1500, "source": "s", "raw": "r"}})
        r = self.post("/api/tasks/calculate_refund/run", {"fields": {"amount": "{{total}}"}})
        self.assertEqual(r.json()["result"], {"refund": 1500.0, "automatic": False})

    def test_conditions_gateway(self):
        url, item = "/api/gateways/conditions", {"available": False, "amount": 1500, "vip": True}
        r = self.post(url, {"kind": "exclusive", "item": item,
                            "conditions": {"available": "{{available}}", "big": "{{amount}} > 1000 and {{vip}}",
                                           "other": "true"}})
        self.assertEqual(r.json()["branches"], ["big"])  # exclusive: the first that holds
        r = self.post(url, {"kind": "inclusive", "conditions": {"a": "not {{available}}", "b": "{{vip}} == true"}})
        self.assertEqual(r.json()["branches"], ["a", "b"])
        bad = self.post(url, {"kind": "exclusive", "conditions": {"x": "__import__('os').system('id')"}})
        self.assertEqual(bad.status_code, 422)
        decision = Event.objects.filter(case_id="wf1:7", endpoint="conditions_gateway").first().data
        self.assertEqual((decision["decider"], decision["output"]["branches"]), ("conditions", ["big"]))
        self.assertEqual(decision["input"]["state"]["amount"], 1500)  # what was decided on: for decision mining

    def test_what_a_sub_process_returns_next_to_the_webhook_body_decides(self):
        """A Call Activity's results sit next to the webhook's body in the item; the case of the calling
        execution never saw them (the sub-process's steps wrote to its own case)."""
        self.post("/api/gateways/conditions", {"kind": "exclusive", "item": {"done": False},
                                               "conditions": {"done": "{{done}} == true"}})  # the case: not done
        item = {"headers": {"host": "x"}, "params": {}, "query": {}, "webhookUrl": "u", "executionMode": "production",
                "body": {"run_dir": "r", "done": False}, "done": True}
        r = self.post("/api/gateways/conditions", {"kind": "exclusive", "item": item,
                                                   "conditions": {"done": "{{done}} == true"}})
        self.assertEqual(r.json()["branches"], ["done"])

    def test_tasks_keep_input_and_output_in_the_event_log(self):
        self.post("/api/tasks/calculate_refund/run", {"fields": {"amount": "120"}})
        data = Event.objects.get(case_id="wf1:7", endpoint="calculate_refund").data
        self.assertEqual(data, {"input": {"amount": 120.0, "duplicate_charge": False},
                                "output": {"refund": 120.0, "automatic": False}})

    @patch("bpmn8n.models.waiting.post_json")
    def test_user_task_shows_the_case_and_its_form_becomes_data(self, resume):
        self.post("/api/tasks/normalize_alert/run", {"item": {"employee": "ann", "amount": 180, "source": "s", "raw": "r"}})
        r = self.post("/api/usertasks", {"title": "Approve {{amount}} EUR for {{employee}}", "lane": "managers",
                                         "form": [{"name": "approved", "type": "boolean"}],
                                         "resume_url": "http://localhost:5678/webhook-waiting/7"})
        task, = r.json()
        self.assertEqual(task["title"], "Approve 180 EUR for ann")
        self.assertEqual(task["context"]["employee"], "ann")  # context defaults to every variable
        self.client.post(f"/api/usertasks/{task['id']}/complete", {"approved": True}, content_type="application/json")
        self.assertIs(Case.objects.get(pk="wf1:7").variables["approved"], True)

    def test_error_message_is_filled_in(self):
        self.post("/api/tasks/normalize_alert/run", {"item": {"partner": "Contoso", "source": "s", "raw": "r"}})
        r = self.post("/api/events", {"status": "error", "message": "No ack from {{partner}}"})
        self.assertEqual(r.json()["message"], "No ack from Contoso")
