from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings

from ..clients.http import ServiceUnavailable
from ..models import Case, Event, MessageSubscription, UserTask

RESUME = "bpmn8n.models.waiting.post_json"


GONE = ServiceUnavailable("http://n8n:5678/...: HTTP 409 b'The execution \"99\" has finished with error.'", 409)
BUSY = ServiceUnavailable("http://n8n:5678/...: HTTP 409 b'The execution \"99\" is running already.'", 409)


@override_settings(N8N_URL="http://n8n:5678")
class UserTaskTest(TestCase):
    FORM = [{"name": "approved", "type": "boolean"}, {"name": "amount", "type": "number"}]
    URL = "http://localhost:5678/webhook-waiting/99?signature=abc"

    def create(self, items=None, activity="Approve Expense"):
        r = self.client.post("/api/usertasks", {
            "title": "Approve expense #7", "lane": "managers", "form": self.FORM, "context": {"amount": 300},
            "resume_url": self.URL, **({"items": items} if items else {}),
        }, content_type="application/json", headers={"X-Bpmn8n-Case": "wf:99", "X-Bpmn8n-Activity": activity})
        self.assertEqual(r.status_code, 201, r.content)
        return [UserTask.objects.get(pk=t["id"]) for t in r.json()]

    def setUp(self):
        self.task, = self.create()
        self.manager = User.objects.create_user("anna", password="pw")
        self.manager.groups.add(managers := Group.objects.create(name="managers"))
        self.colleague = User.objects.create_user("carl", password="pw")
        self.colleague.groups.add(managers)
        self.clerk = User.objects.create_user("bob", password="pw")

    def complete(self, result, task=None):
        return self.client.post(f"/api/usertasks/{(task or self.task).pk}/complete", result,
                                content_type="application/json")

    def act(self, user, action, data=None, task=None):
        self.client.force_login(user)
        return self.client.post(f"/inbox/{(task or self.task).pk}/{action}/", data or {}, follow=True)

    @patch(RESUME)
    def test_complete_resumes_n8n_at_internal_host(self, resume):
        r = self.complete({"approved": True, "amount": "250"})
        self.assertEqual(r.json()["status"], "completed")
        url, payload = resume.call_args.args
        self.assertEqual(url, "http://n8n:5678/webhook-waiting/99?signature=abc")
        self.assertEqual(payload["_items"], [{"approved": True, "amount": 250.0, "_user_task_id": self.task.pk,
                                              "_completed_by": None}])
        self.assertTrue(Event.objects.filter(case_id="wf:99", activity="Approve Expense", endpoint="userTask").exists())
        self.assertEqual(self.complete({"approved": True, "amount": 1}).status_code, 409)

    @patch(RESUME, side_effect=ServiceUnavailable("down"))
    def test_n8n_down_keeps_task_open(self, _):
        self.assertEqual(self.complete({"approved": True, "amount": 1}).status_code, 502)
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.result), (UserTask.OPEN, None))
        self.assertFalse(Event.objects.filter(endpoint="userTask").exists())  # rolled back with the claim

    @patch(RESUME, side_effect=BUSY)
    def test_an_execution_still_running_keeps_it_open(self, _):
        self.assertEqual(self.complete({"approved": True, "amount": 1}).status_code, 502)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, UserTask.OPEN)

    @patch(RESUME, side_effect=GONE)
    def test_a_stopped_execution_cancels_the_task(self, _):
        self.assertEqual(self.complete({"approved": True, "amount": 1}).status_code, 410)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, UserTask.CANCELLED)
        self.assertIn("has finished", self.task.reason)
        self.assertContains(self.act(self.manager, "complete", {"amount": 1}), "cannot complete")

    def test_invalid_form_is_422(self):
        self.assertEqual(self.complete({"approved": True, "amount": "lots"}).status_code, 422)
        self.assertEqual(self.complete({"approved": True, "amount": "nan"}).status_code, 422)  # not JSON

    def test_inbox_shows_only_own_lanes(self):
        self.client.force_login(self.clerk)
        self.assertNotContains(self.client.get("/inbox/"), "Approve expense #7")
        self.assertEqual(self.client.post(f"/inbox/{self.task.pk}/complete/", {}).status_code, 404)
        self.assertEqual(self.client.get(f"/inbox/{self.task.pk}/").status_code, 404)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get("/inbox/"), "Approve expense #7")

    @patch(RESUME)
    def test_task_page_completion(self, resume):
        page = self.act(self.manager, "complete", {"approved": "on", "amount": "120"})
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.assignee), (UserTask.COMPLETED, self.manager))
        self.assertEqual(resume.call_args.args[1]["_items"][0]["_completed_by"], "anna")
        self.assertContains(page, "Completed by anna")

    @patch(RESUME)
    def test_claimed_task_is_the_claimers(self, resume):
        self.assertContains(self.act(self.manager, "claim"), "Claimed by you")
        self.assertContains(self.act(self.colleague, "claim"), "cannot claim")
        page = self.act(self.colleague, "complete", {"approved": "on", "amount": "1"})
        self.assertContains(page, "claimed by anna")
        self.assertNotContains(page, "<form class=\"fields\"")  # read-only for the others
        self.assertFalse(resume.called)
        self.act(self.manager, "release")
        self.act(self.colleague, "complete", {"approved": "on", "amount": "1"})
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.assignee), (UserTask.COMPLETED, self.colleague))

    def test_the_task_page_shows_how_the_case_got_here(self):
        from datetime import timedelta
        Event.objects.create(case_id="wf:99", activity="Check Amount", endpoint="conditions_gateway", resource="system",
                             started=self.task.created - timedelta(minutes=1), ended=self.task.created, duration_ms=3,
                             status="complete")
        self.client.force_login(self.manager)
        page = self.client.get(f"/inbox/{self.task.pk}/")
        self.assertContains(page, "How the case got here")
        self.assertContains(page, "Check Amount")
        self.assertContains(page, '<span class="count num" aria-label="1 waiting">1</span>', html=True)  # the nav count

    def test_the_link_in_n8n_opens_the_task_of_that_execution(self):
        self.client.force_login(self.manager)
        r = self.client.get("/inbox/", {"workflow": "wf", "execution": "99"})
        self.assertRedirects(r, f"/inbox/{self.task.pk}/")
        self.assertContains(self.client.get("/inbox/", {"workflow": "wf", "execution": "5"}), "Nothing waits")
        self.assertRedirects(self.client.get("/inbox/", {"workflow": "wf", "execution": "undefined"}),
                             f"/inbox/{self.task.pk}/")  # no execution known: the workflow's tasks

    def test_registering_twice_gives_the_same_tasks(self):
        """n8n running the node again (a retry, a restart) must not create a second task."""
        self.assertEqual(self.create(), [self.task])

    @patch(RESUME)
    def test_one_task_per_item_resumes_once_all_are_done(self, resume):
        first, second = self.create(items=[{"employee": "ann"}, {"employee": "bob"}], activity="Approve Each")
        self.assertEqual((first.instance, second.instance, first.batch), (0, 1, second.batch))
        self.complete({"approved": True, "amount": 2}, task=second)
        self.assertFalse(resume.called)  # the first item is still waiting
        self.complete({"approved": False, "amount": 1}, task=first)
        items = resume.call_args.args[1]["_items"]
        self.assertEqual([(i["_user_task_id"], i["amount"]) for i in items], [(first.pk, 1.0), (second.pk, 2.0)])

    def test_admin_can_cancel_a_stuck_task(self):
        self.client.force_login(User.objects.create_superuser("root", password="pw"))
        self.client.post("/admin/bpmn8n/usertask/", {"action": "cancel", "_selected_action": [self.task.pk]})
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.reason), (UserTask.CANCELLED, "cancelled by root"))


@override_settings(N8N_URL="http://n8n:5678")
class MessageTest(TestCase):
    def setUp(self):
        r = self.client.post("/api/messages/subscriptions",
                             {"name": "payment-confirmed", "correlation_key": "ORD-42",
                              "resume_url": "http://localhost:5678/webhook-waiting/7"},
                             content_type="application/json",
                             headers={"X-Bpmn8n-Case": "7", "X-Bpmn8n-Activity": "Receive Payment"})
        self.assertEqual(r.status_code, 201, r.content)

    def send(self, key):
        return self.client.post("/api/messages/payment-confirmed", {"correlation_key": key, "payload": {"amount": 99}},
                                content_type="application/json")

    @patch(RESUME)
    def test_message_resumes_the_waiting_execution(self, resume):
        r = self.send("ORD-42")
        self.assertEqual(r.json()["status"], "completed")
        url, payload = resume.call_args.args
        self.assertEqual(url, "http://n8n:5678/webhook-waiting/7")
        self.assertEqual(payload, {"amount": 99, "_message": "payment-confirmed", "_correlation_key": "ORD-42"})
        self.assertEqual(self.send("ORD-42").status_code, 404)  # already consumed

    def test_unknown_key_is_404(self):
        self.assertEqual(self.send("ORD-1").status_code, 404)

    @patch(RESUME)
    def test_a_message_never_changes_what_the_case_has(self, resume):
        """A reply from outside cannot redirect a payout: it adds variables, the case's own stay."""
        case_id = MessageSubscription.objects.get().case_id
        Case.objects.update_or_create(case_id=case_id, defaults={"variables": {"iban": "IT-REAL", "amount": 10}})
        r = self.client.post("/api/messages/payment-confirmed", {"correlation_key": "ORD-42", "payload": {
            "amount": 1, "iban": "IT-ATTACKER", "receipt": "R-1"}}, content_type="application/json")
        self.assertEqual(r.json()["reason"], "left out, the case already has them: amount, iban")
        self.assertEqual(resume.call_args.args[1], {"receipt": "R-1", "_message": "payment-confirmed", "_correlation_key": "ORD-42"})
        self.assertEqual(Case.objects.get(case_id=case_id).variables, {"iban": "IT-REAL", "amount": 10, "receipt": "R-1"})

    @patch(RESUME, side_effect=ServiceUnavailable("down"))
    def test_n8n_down_keeps_waiting(self, _):
        self.assertEqual(self.send("ORD-42").status_code, 502)
        self.assertEqual(MessageSubscription.objects.get().status, MessageSubscription.OPEN)


@override_settings(N8N_URL="http://n8n:5678")
class EventBasedGatewayTest(TestCase):
    """One execution waits for hold-book OR decline-hold, until a timeout."""

    def subscribe(self, name, expires=None):
        body = {"name": name, "correlation_key": "REQ-1", "resume_url": "http://localhost:5678/webhook-waiting/9"}
        if expires:
            body["expires"] = expires
        return self.client.post("/api/messages/subscriptions", body, content_type="application/json",
                                headers={"X-Bpmn8n-Case": "9"})

    def send(self, name):
        return self.client.post(f"/api/messages/{name}", {"correlation_key": "REQ-1"}, content_type="application/json")

    @patch(RESUME)
    def test_first_message_wins_and_closes_the_alternatives(self, resume):
        self.subscribe("hold-book")
        self.subscribe("decline-hold")
        self.assertEqual(self.send("decline-hold").status_code, 200)
        self.assertEqual(self.send("hold-book").status_code, 404)  # the race is over
        self.assertEqual(resume.call_count, 1)

    @patch(RESUME, side_effect=ServiceUnavailable("down"))
    def test_failed_resume_reopens_the_alternatives(self, _):
        self.subscribe("hold-book")
        self.subscribe("decline-hold")
        self.assertEqual(self.send("hold-book").status_code, 502)
        self.assertEqual(MessageSubscription.objects.filter(status="open").count(), 2)

    def test_message_after_the_timeout_is_404(self):
        self.subscribe("hold-book", expires="2000-01-01T00:00:00Z")
        self.assertEqual(self.send("hold-book").status_code, 404)

    def test_error_end_event_is_logged(self):
        r = self.client.post("/api/events", {"status": "error"}, content_type="application/json",
                             headers={"X-Bpmn8n-Case": "9", "X-Bpmn8n-Activity": "Partner Failure"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(Event.objects.filter(case_id="9", activity="Partner Failure", status="error").exists())


@override_settings(N8N_URL="http://n8n:5678", N8N_API_KEY="")
class ManyWorkflowsTest(TestCase):
    """Hundreds of active workflows must not get mixed up: process, case and messages."""

    def call(self, workflow, execution, activity="Register Ticket"):
        from urllib.parse import quote
        return self.client.post("/api/tasks/normalize_alert", {"source": "x", "raw": "y"}, content_type="application/json",
                                headers={"X-Bpmn8n-Case": quote(f"{workflow}:{execution}"),
                                         "X-Bpmn8n-Process": quote(f"Triage · {workflow}"),
                                         "X-Bpmn8n-Activity": quote(activity)})

    def test_same_first_activity_in_two_workflows_stays_two_processes(self):
        self.call("wfA", "1")
        self.call("wfB", "1")  # same execution number in another workflow: another case
        processes = self.client.get("/api/processmining").json()
        self.assertEqual(set(processes), {"Triage · wfA", "Triage · wfB"})  # decoded, non-ASCII kept
        self.assertEqual(Event.objects.exclude(case_id="").values("case_id").distinct().count(), 2)

    def subscribe(self, process, resume_id):
        from urllib.parse import quote
        self.client.post("/api/messages/subscriptions",
                         {"name": "payment-confirmed", "correlation_key": "ORD-1",
                          "resume_url": f"http://localhost:5678/webhook-waiting/{resume_id}"},
                         content_type="application/json", headers={"X-Bpmn8n-Process": quote(process)})

    @patch(RESUME)
    def test_a_message_two_workflows_wait_for_is_never_delivered_by_guess(self, resume):
        self.subscribe("Orders", 1)
        self.subscribe("Invoicing", 2)
        url, body = "/api/messages/payment-confirmed", {"correlation_key": "ORD-1"}
        ambiguous = self.client.post(url, body, content_type="application/json")
        self.assertEqual(ambiguous.status_code, 409)
        self.assertFalse(resume.called)
        targeted = self.client.post(url, {**body, "process": "Invoicing"}, content_type="application/json")
        self.assertEqual(targeted.json()["process"], "Invoicing")
        self.assertEqual(resume.call_args.args[0], "http://n8n:5678/webhook-waiting/2")
