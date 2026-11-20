"""The case page: a synthetic case's steps, decision, waits, variables and canvas; its status; who may see it."""
import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from types import SimpleNamespace as Row
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings

from ..models import Case, Event, MessageSubscription, UserTask
from ..web.cases import case_status, llm_outputs, node_states
from ..workflows.elements import AGENT, SET, WEBHOOK, node_type
from ..workflows.graph import ProcessGraph

T0 = datetime(2026, 11, 1, 9, tzinfo=UTC)
WORKFLOW = "bpmn8n.web.cases.n8n.workflow"


def link(*names):
    return {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in pairwise(names)}


FLOW = {"name": "Support", "nodes": [
    {"name": "Start", "type": WEBHOOK, "parameters": {}, "credentials": {"httpHeaderAuth": {"id": "secret-credential"}}},
    {"name": "Register", "type": node_type("serviceTask"), "parameters": {}},
    {"name": "Triage", "type": node_type("exclusiveGateway"),
     "parameters": {"branches": {"values": [{"name": "refund"}, {"name": "bug"}]}}},
    {"name": "Reply: prompt", "type": node_type("scriptTask"), "parameters": {}},
    {"name": "Reply", "type": AGENT, "parameters": {}},
    {"name": "Reply: answer", "type": SET, "parameters": {"assignments": {"assignments": [{"name": "draft_reply"}]}}},
    {"name": "Review", "type": node_type("userTask"), "parameters": {}},
    {"name": "Fix", "type": node_type("serviceTask"), "parameters": {}},
], "connections": {**link("Start", "Register", "Triage"), **link("Reply: prompt", "Reply", "Reply: answer", "Review"),
                   "Triage": {"main": [[{"node": "Reply: prompt", "type": "main", "index": 0}],
                                       [{"node": "Fix", "type": "main", "index": 0}], []]}}}


def event(activity, minute, endpoint, status="complete", data=None, error=""):
    started = T0 + timedelta(minutes=minute)
    return Event.objects.create(case_id="wf1:42", process="Support", activity=activity, endpoint=endpoint, started=started,
                                ended=started + timedelta(seconds=2), duration_ms=2000, status=status, resource="system",
                                data=data or {}, error=error)


class CasePageTest(TestCase):
    def setUp(self):
        Case.objects.create(case_id="wf1:42", process="Support", variables={
            "ticket": "My invoice is wrong", "customer": {"email": "ann@example.com", "orders": [1, 2]},
            "draft_reply": "We will refund you."})
        event("Register", 0, "register_ticket", data={"input": {"ticket": "My invoice is wrong"}, "output": {"ticket_id": 7}})
        event("Triage", 1, "exclusive_gateway", data={
            "decider": "laya-english", "input": {"question": "What does the customer need?", "state": {},
                                                 "options": {"refund": "money back", "bug": "the app is broken"}},
            "output": {"branches": ["refund"], "scores": {"refund": 0.91}}})
        event("Reply: prompt", 2, "compose_prompt", data={"input": {"prompt": "Draft a reply"}, "output": {"prompt": "Draft a reply"}})
        event("Lookup", 3, "lookup_order", status="error", data={"input": {"draft_reply": "We will refund you."}},
              error="TimeoutError: the shop did not answer")
        self.task = UserTask.objects.create(case_id="wf1:42", process="Support", activity="Review", title="Review the reply",
                                            lane="support", resume_url="http://n8n/webhook-waiting/42")
        MessageSubscription.objects.create(case_id="wf1:42", process="Support", activity="Wait for payment",
                                           name="payment", correlation_key="7", resume_url="http://n8n/webhook-waiting/42b")
        self.staff = User.objects.create_user("sam", password="pw", is_staff=True)
        self.client.force_login(self.staff)

    @patch(WORKFLOW, return_value=None)
    def test_without_n8n_the_page_says_so_and_shows_the_log(self, _):
        page = self.client.get("/workflows/wf1/cases/42/")
        self.assertContains(page, "<h1>Case #42</h1>", html=True)
        self.assertNotContains(page, 'class="notice')  # the subscriptions are not Django's messages
        self.assertContains(page, '<b8-pill tone="accent">waiting for a person</b8-pill>', html=True)
        self.assertContains(page, '<li data-status="error">')  # <b8-timeline> colours the failed step's dot
        self.assertContains(page, "The workflow cannot be drawn")
        self.assertNotContains(page, '<iframe id="preview"')
        for text in ("Register", "TimeoutError: the shop did not answer", "sent to an LLM", "What does the customer need?",
                     "laya-english", "0.91", "the app is broken", "Message <code>payment</code>",
                     "ann@example.com", "Wait for payment"):
            self.assertContains(page, text)
        self.assertContains(page, f'href="/inbox/{self.task.pk}/"')  # the task's own page
        self.assertContains(page, "&quot;ticket_id&quot;: 7")  # a step's output, as JSON
        self.assertContains(page, 'href="http://localhost:5678/workflow/wf1/executions/42" target="_blank"')
        self.assertNotContains(page, "by an LLM")  # without the workflow, nobody knows what an LLM wrote

    @override_settings(N8N_PREVIEW_URL="http://preview.test")
    @patch(WORKFLOW, return_value=FLOW)
    def test_with_the_workflow_the_canvas_gets_the_node_states(self, _):
        page = self.client.get("/workflows/wf1/cases/42/")
        self.assertContains(page, 'data-src="http://preview.test/workflows/demo"')
        self.assertNotIn("credentials", page.context["workflow"]["nodes"][0])  # never sent to the browser
        self.assertNotContains(page, "secret-credential")
        self.assertEqual(json.loads(page.context["states"] and json.dumps(page.context["states"])), {
            "Start": "done", "Register": "done", "Triage": "done", "Reply: prompt": "done", "Reply": "done",
            "Reply: answer": "done", "Review": "active", "Fix": "waiting"})
        self.assertContains(page, '<script id="states" type="application/json">')
        self.assertContains(page, "by an LLM")  # draft_reply, in the variables
        self.assertContains(page, "reads an LLM's answer")  # the step that read it

    @patch(WORKFLOW, return_value=None)
    def test_who_may_see_a_case(self, _):
        self.client.logout()
        self.assertRedirects(self.client.get("/cases/wf1:42/"), "/workflows/wf1/cases/42/", status_code=301,
                             fetch_redirect_response=False)  # the address before cases lived under their workflow
        self.assertEqual(self.client.get("/workflows/wf1/cases/42/").status_code, 302)  # sign in first
        person = User.objects.create_user("ann", password="pw")
        self.client.force_login(person)
        self.assertEqual(self.client.get("/workflows/wf1/cases/42/").status_code, 404)  # not in the support lane
        person.groups.add(Group.objects.create(name="support"))
        self.assertEqual(self.client.get("/workflows/wf1/cases/42/").status_code, 200)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/workflows/wf9/cases/1/").status_code, 404)  # no such case


class StatusTest(TestCase):
    def test_a_case_status_from_its_events_and_waits(self):
        now = T0 + timedelta(hours=1)
        done, failed, running = Row(status="complete"), Row(status="error"), Row(status="running")
        task = Row(status="open")
        message = Row(status="open", expires=None)
        late = Row(status="open", expires=now - timedelta(minutes=5))  # the timeout moved the case on
        self.assertEqual(case_status([done], [task], [], T0, now), "waiting_person")
        self.assertEqual(case_status([done], [], [message], T0, now), "waiting_message")
        self.assertEqual(case_status([done], [], [late], T0, now), "completed")
        self.assertEqual(case_status([done, running], [], [], now, now), "running")
        self.assertEqual(case_status([done, failed], [], [], T0, now), "failed")
        self.assertEqual(case_status([failed, done], [], [], T0, now), "completed")  # an error it recovered from
        self.assertEqual(case_status([done], [], [], now - timedelta(seconds=10), now), "running")  # not settled yet

    def test_nodes_without_events_count_between_passed_steps(self):
        graph = ProcessGraph(FLOW)
        states = node_states(graph, done={"Register", "Triage"}, active=set())
        self.assertEqual((states["Start"], states["Reply: prompt"], states["Reply"], states["Fix"]),
                         ("done", "waiting", "waiting", "waiting"))
        self.assertEqual(llm_outputs(graph), {"draft_reply"})
