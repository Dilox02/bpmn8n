"""BPMN coverage C2, C4, C5, C8, C9: reminders and escalation, signals, terminate, call-activity data mapping,
compensation (a saga)."""
import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from ninja import Schema

from ..api import TASKS, task
from ..clients.n8n import N8nError
from ..engine import COMPLETED, Stub, TokenEngine
from ..models import AuditEntry, Case, Event, MessageSubscription, Reminder, UserTask
from ..workflows.builder import SpecError, Workflow
from ..workflows.check import WorkflowCheck
from ..workflows.scenarios import ScenarioRunner

RESUME = "bpmn8n.models.waiting.post_json"
JSON = "application/json"


def headers(case, activity="Step"):
    return {"X-Bpmn8n-Case": case, "X-Bpmn8n-Activity": activity}


@override_settings(N8N_URL="http://n8n:5678")
class SignalTest(TestCase):
    def catch(self, case, name="market-closed"):
        r = self.client.post("/api/messages/subscriptions", {
            "name": name, "correlation_key": "", "resume_url": f"http://localhost:5678/webhook-waiting/{case}",
            "broadcast": True}, content_type=JSON, headers=headers(f"wf:{case}", "Wait for close"))
        self.assertEqual(r.status_code, 201, r.content)

    def throw(self, name="market-closed", payload=None):
        return self.client.post(f"/api/signals/{name}", {"payload": payload or {"price": 7}}, content_type=JSON)

    @patch(RESUME)
    def test_one_signal_resumes_every_waiting_execution_once(self, resume):
        self.catch(1), self.catch(2)
        r = self.throw()
        self.assertEqual((r.status_code, r.json()["delivered"]), (200, 2))
        self.assertEqual({c.args[0] for c in resume.call_args_list},
                         {"http://n8n:5678/webhook-waiting/1", "http://n8n:5678/webhook-waiting/2"})
        self.assertEqual(resume.call_args.args[1], {"price": 7, "_signal": "market-closed", "_correlation_key": ""})
        self.assertEqual(self.throw().status_code, 404)  # nobody waits any more: not queued, and nothing resumes twice
        self.assertEqual(resume.call_count, 2)
        event = Event.objects.get(case_id="wf:1")
        self.assertEqual((event.endpoint, event.resource), ("signalCatch", "signal:market-closed"))

    @patch(RESUME)
    def test_a_signal_is_not_a_message_and_goes_by_name(self, resume):
        self.catch(1)
        self.catch(2, name="other")
        r = self.client.post("/api/messages/market-closed", {"correlation_key": ""}, content_type=JSON)
        self.assertEqual(r.status_code, 404)  # a message never reaches a signal catch
        self.assertEqual(self.throw().json()["delivered"], 1)
        self.assertEqual(MessageSubscription.objects.get(name="other").status, "open")

    def test_nobody_waiting_is_404(self):
        self.assertEqual(self.throw().status_code, 404)

    @patch(RESUME)
    def test_a_signal_adds_variables_only(self, resume):
        Case.objects.create(case_id="wf:1", variables={"price": 1})
        self.catch(1)
        self.throw(payload={"price": 7, "volume": 2})
        self.assertEqual(resume.call_args.args[1], {"volume": 2, "_signal": "market-closed", "_correlation_key": ""})

    @patch(RESUME)
    def test_signal_throw_is_a_task_that_resumes_the_catchers(self, resume):
        self.catch(1)
        r = self.client.post("/api/tasks/throw_signal/run", {"fields": {"name": "market-closed", "payload": {"price": 3}}},
                             content_type=JSON, headers=headers("wf:9", "Throw"))
        self.assertEqual((r.status_code, r.json()["result"]["signal_delivered"]), (200, 1))
        self.assertEqual(resume.call_args.args[1]["price"], 3)
        again = self.client.post("/api/tasks/throw_signal/run", {"fields": {"name": "market-closed"}}, content_type=JSON,
                                 headers={**headers("wf:9", "Throw"), "X-Bpmn8n-Run": "1"})
        self.assertEqual(again.json()["result"]["signal_delivered"], 0)  # nobody waits: no error

    def test_the_steps_and_the_engine(self):
        w = (Workflow("Close").start("In", "in").signal_catch("Wait for close", "market-closed")
             .signal_throw("Announce", "closed", payload={"p": "{{price}}"}).flow("In", "Wait for close", "Announce")).as_json()
        catch = next(n for n in w["nodes"] if n["name"] == "Wait for close")
        self.assertEqual(catch["parameters"], {"signal": "market-closed"})
        self.assertEqual(WorkflowCheck(w).graph.element("Wait for close").waits, True)
        report = ScenarioRunner(w).run({"name": "s", "messages": {"Wait for close": {"name": "market-closed", "payload": {"price": 5}}},
                                        "expect": {"path": ["In", "Wait for close", "Announce"], "variables": {"price": 5}}})
        self.assertTrue(report["passed"], report)
        report = ScenarioRunner(w).run({"name": "s", "messages": {"Wait for close": {"name": "wrong"}}})
        self.assertIn("waits for 'market-closed'", report["failure"])
        self.assertIn("waits for a message", ScenarioRunner(w).run({"name": "s"})["failure"])
        with self.assertRaises(SpecError):
            Workflow("x").signal_catch("a", " ")


@override_settings(N8N_URL="http://n8n:5678")
class TerminateTest(TestCase):
    CASE = "wf:99"

    def setUp(self):
        self.client.post("/api/usertasks", {"title": "Approve", "lane": "m", "resume_url": "http://localhost:5678/w/99"},
                         content_type=JSON, headers=headers(self.CASE, "Approve"))
        self.client.post("/api/messages/subscriptions", {"name": "paid", "correlation_key": "1", "resume_url": "http://x/w/98"},
                         content_type=JSON, headers=headers(self.CASE, "Wait"))
        self.client.post("/api/messages/subscriptions", {"name": "paid", "correlation_key": "1", "resume_url": "http://x/w/5"},
                         content_type=JSON, headers=headers("wf:5", "Wait"))

    def terminate(self, case=None):
        return self.client.post(f"/api/cases/{case or self.CASE}/terminate", {"reason": "customer left"}, content_type=JSON,
                                headers=headers(case or self.CASE, "Terminate"))

    @patch("bpmn8n.clients.n8n.request")
    def test_it_cancels_what_the_case_waits_on_and_stops_n8n(self, stop):
        r = self.terminate()
        self.assertEqual(r.json(), {"case_id": self.CASE, "terminated": True, "cancelled": 2, "stopped": True, "detail": ""})
        stop.assert_called_once_with("POST", "/executions/99/stop")
        self.assertEqual(UserTask.objects.get().status, "cancelled")
        self.assertIn("terminated", UserTask.objects.get().reason)
        self.assertEqual(MessageSubscription.objects.filter(case_id=self.CASE).get().status, "cancelled")
        self.assertEqual(MessageSubscription.objects.get(case_id="wf:5").status, "open")  # other cases are untouched
        self.assertIsNotNone(Case.objects.get(case_id=self.CASE).terminated)
        self.assertTrue(AuditEntry.objects.filter(action="case.terminate", case_id=self.CASE).exists())
        self.assertTrue(Event.objects.filter(case_id=self.CASE, endpoint="terminateEndEvent").exists())

    @patch("bpmn8n.clients.n8n.request")
    def test_later_calls_of_the_case_answer_terminated(self, stop):
        self.terminate()
        r = self.client.post("/api/tasks/compose_prompt/run", {"fields": {"prompt": "x"}}, content_type=JSON,
                             headers=headers(self.CASE, "Compose"))
        self.assertEqual(r.status_code, 409)
        self.assertIn("terminated", r.json()["detail"])
        ok = self.client.post("/api/tasks/compose_prompt/run", {"fields": {"prompt": "x"}}, content_type=JSON,
                              headers=headers("wf:100", "Compose"))
        self.assertEqual(ok.status_code, 200)  # another case goes on
        self.assertEqual(self.terminate().status_code, 200)  # terminating again is allowed
        self.assertEqual(AuditEntry.objects.filter(action="case.terminate").count(), 1)

    @patch("bpmn8n.clients.n8n.request", side_effect=N8nError("no N8N_API_KEY"))
    def test_without_n8ns_stop_bpmn8n_still_ends_the_case(self, stop):
        r = self.terminate().json()
        self.assertEqual((r["terminated"], r["stopped"], r["cancelled"]), (True, False, 2))
        self.assertIn("N8N_API_KEY", r["detail"])

    def test_the_engine_kills_the_other_branches(self):
        w = (Workflow("Order").start("In", "in").parallel_gateway("Fork", ["a", "b"])
             .task("Slow", "serviceTask", "t").task("After", "serviceTask", "t").terminate_end("Stop", "cancelled")
             .flow("In", "Fork").flow("Fork", "Slow", "After", output="a").flow("Fork", "Stop", output="b")).as_json()
        engine = TokenEngine(w, Stub(durations={"Slow": 5}))
        case = engine.start()
        engine.run()
        self.assertEqual((case.status, case.ends), (COMPLETED, ["Stop"]))
        self.assertNotIn("After", [v.node for v in case.trace])
        self.assertIsNone(next(v for v in case.trace if v.node == "Slow").ended or None)
        self.assertEqual(case.tokens["cancelled"], 1)


@override_settings(N8N_URL="http://n8n:5678", DEFAULT_FROM_EMAIL="bpmn8n@example.test")
class ReminderTest(TestCase):
    URL = "http://localhost:5678/webhook-waiting/5"

    def setUp(self):
        for name, email in (("anna", "anna@example.test"), ("bob", "bob@example.test"), ("sam", "sam@example.test")):
            user = User.objects.create_user(name, email, "pw")
            user.groups.add(Group.objects.get_or_create(name="supervisors" if name == "sam" else "clerks")[0])
        self.now = timezone.now()

    def create(self, **more):
        r = self.client.post("/api/usertasks", {"title": "Check claim", "lane": "clerks", "resume_url": self.URL,
                                                "remind_at": (self.now + timedelta(hours=2)).isoformat(), **more},
                             content_type=JSON, headers=headers("wf:5", "Check claim"))
        self.assertEqual(r.status_code, 201, r.content)
        return UserTask.objects.get(pk=r.json()[0]["id"])

    def run_at(self, moment):
        out = StringIO()
        with patch("bpmn8n.management.commands.run_deadlines.timezone.now", return_value=moment):
            call_command("run_deadlines", stdout=out, stderr=out)
        return out.getvalue()

    def test_a_task_still_open_at_its_time_reminds_its_lane_once(self):
        task = self.create()
        self.assertEqual(self.run_at(self.now + timedelta(hours=1)), "0 sent, 0 failed\n")
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.run_at(self.now + timedelta(hours=3)), "1 sent, 0 failed\n")
        self.assertEqual(sorted(mail.outbox[0].to), ["anna@example.test", "bob@example.test"])
        self.assertIn("Check claim", mail.outbox[0].subject)
        task.refresh_from_db()
        self.assertEqual(task.status, "open")  # non-interrupting: the task stays open
        self.assertEqual(self.run_at(self.now + timedelta(hours=4)), "0 sent, 0 failed\n")
        self.assertEqual(len(mail.outbox), 1)
        audit = AuditEntry.objects.get(action="usertask.remind")
        self.assertEqual((audit.actor, audit.case_id, audit.details["how"]), ("system", "wf:5", "mail to 2"))

    def test_escalate_to_a_lane_notifies_that_lane(self):
        self.create(escalate_to="supervisors")
        self.run_at(self.now + timedelta(hours=3))
        self.assertEqual(mail.outbox[0].to, ["sam@example.test"])

    @patch(RESUME)
    def test_escalate_to_a_webhook_starts_the_workflow_with_the_case_id(self, post):
        task = self.create(escalate_to="/escalate-claims")
        self.run_at(self.now + timedelta(hours=3))
        url, payload = post.call_args.args
        self.assertEqual(url, "http://n8n:5678/webhook/escalate-claims")
        self.assertEqual((payload["case_id"], payload["task_id"], payload["title"]), ("wf:5", task.pk, "Check claim"))
        self.assertEqual(len(mail.outbox), 0)

    def test_a_completed_task_is_not_reminded(self):
        task = self.create()
        task.cancel("done elsewhere")
        self.run_at(self.now + timedelta(hours=3))
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Reminder.objects.get().sent is not None, True)  # dropped, not retried

    @patch(RESUME, side_effect=RuntimeError("n8n down"))
    def test_a_failed_escalation_is_tried_again(self, post):
        self.create(escalate_to="/escalate")
        self.assertTrue(self.run_at(self.now + timedelta(hours=3)).endswith("0 sent, 1 failed\n"))
        self.assertIsNone(Reminder.objects.get().sent)

    def test_the_builder_writes_it_and_the_check_knows_the_lane(self):
        w = (Workflow("Claims").start("In", "in").user_task("Check claim", "clerks", "clerks", [], remind_after=(2, "hours"),
                                                           escalate_to="nobody")).as_json()
        params = next(n for n in w["nodes"] if n["name"] == "Check claim")["parameters"]
        self.assertEqual((params["remindAmount"], params["remindUnit"], params["escalateTo"]), (2, "hours", "nobody"))
        problems = {(p.rule, p.node) for p in WorkflowCheck(w).problems}
        self.assertIn(("lane", "Check claim"), problems)  # no group "nobody"
        Group.objects.create(name="nobody")
        self.assertFalse([p for p in WorkflowCheck(w).problems if p.rule == "lane" and "nobody" in p.message])
        for bad in ({"remind_after": (0, "hours")}, {"remind_after": (1, "years")}, {"escalate_to": "x"}):
            with self.assertRaises(SpecError):
                Workflow("x").manual_task("m", "t", "l", **bad)


def sub_process():
    return Workflow("Sub").subprocess_start("In").task("Compose", "scriptTask", "compose_prompt").flow("In", "Compose").as_json()


class CallMappingTest(TestCase):
    def call(self, **mapping):
        return (Workflow("Parent").start("In", "in").call_activity("Call", "sub-1", **mapping)
                .task("Use", "scriptTask", "compose_prompt", prompt="{{answer}}").flow("In", "Call", "Use")).as_json()

    def test_the_builder_writes_the_mapping(self):
        node = next(n for n in self.call(inputs={"prompt": "{{text}}"}, outputs=["answer"])["nodes"] if n["name"] == "Call")
        self.assertEqual(json.loads(node["parameters"]["inputs"]), {"prompt": "{{text}}"})
        self.assertEqual(node["parameters"]["outputs"], ["answer"])
        plain = next(n for n in self.call()["nodes"] if n["name"] == "Call")["parameters"]
        self.assertEqual(set(plain), {"workflowId"})  # no mapping: as before
        for bad in ({"inputs": {}}, {"outputs": []}, {"outputs": "answer"}, {"inputs": ["a"]}):
            with self.assertRaises(SpecError):
                Workflow("x").call_activity("c", "1", **bad)

    def test_the_check_takes_outputs_as_what_the_step_produces(self):
        def variable(w):
            return {(p.rule, p.node) for p in WorkflowCheck(w, fetch=lambda _: sub_process(), start_data=["text"]).problems}
        self.assertNotIn(("variable", "Use"), variable(self.call(inputs={"prompt": "{{text}}"}, outputs=["answer"])))
        self.assertIn(("variable", "Use"), variable(self.call(inputs={"prompt": "{{text}}"}, outputs=["other"])))

    def test_it_warns_about_a_required_input_left_unmapped(self):
        def warned(**mapping):
            check = WorkflowCheck(self.call(**mapping), fetch=lambda _: sub_process())
            return [p for p in check.problems if p.rule == "call-input"]
        (problem,) = warned(inputs={"other": "{{text}}"})
        self.assertEqual((problem.level, problem.node), ("warning", "Call"))
        self.assertIn("prompt", problem.message)
        self.assertEqual(warned(inputs={"prompt": "{{text}}"}), [])
        self.assertEqual(warned(), [])  # no mapping: the whole item goes in
        self.assertEqual([p for p in WorkflowCheck(self.call(inputs={"other": "x"}), fetch=lambda _: None).problems
                          if p.rule == "call-input"], [])  # sub-process not readable

    def test_a_scenario_gets_back_only_the_listed_outputs(self):
        w = self.call(inputs={"prompt": "{{text}}"}, outputs=["answer"])
        report = ScenarioRunner(w).run({"name": "s", "start": {"text": "hi"}, "tasks": "stub",
                                        "outputs": {"Call": {"answer": "yes", "secret": "no"}},
                                        "expect": {"variables": {"answer": "yes"}}})
        self.assertTrue(report["passed"], report)
        self.assertNotIn("secret", report["variables"])


# --- the saga: a flight, then a hotel that fails, cancels the flight --------------------------------------

UNDONE = []


class FlightIn(Schema):
    city: str


class FlightOut(Schema):
    booking: str


class CancelFlightIn(Schema):
    booking: str
    city: str  # from the original input; booking from its output


class HotelIn(Schema):
    city: str


class HotelOut(Schema):
    hotel_ok: bool = False


@task("serviceTask", compensate="cancel_flight")
def book_flight(request, data: FlightIn) -> FlightOut:
    """Books a flight."""
    return FlightOut(booking=f"F-{data.city}")


@task("serviceTask")
def cancel_flight(request, data: CancelFlightIn):
    """Cancels a flight."""
    UNDONE.append((data.booking, data.city))
    return {"cancelled": data.booking}


@task("serviceTask", compensate="cancel_hotel")
def book_hotel(request, data: HotelIn) -> HotelOut:
    """Books a hotel: none is free."""
    raise RuntimeError("no room")


@task("serviceTask")
def cancel_hotel(request, data: HotelIn):
    """Cancels a hotel."""
    UNDONE.append(("hotel", data.city))
    return {}


class SagaTest(TestCase):
    def setUp(self):
        UNDONE.clear()

    def run_task(self, name, case, **fields):
        return self.client.post(f"/api/tasks/{name}/run", {"fields": fields}, content_type=JSON, headers=headers(case, name))

    def compensate(self, case):
        r = self.client.post(f"/api/cases/{case}/compensate", content_type=JSON)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_the_hotel_fails_so_the_flight_is_cancelled(self):
        self.assertEqual(self.run_task("book_flight", "trip:1", city="Rome").status_code, 200)
        with self.assertRaises(RuntimeError):
            self.run_task("book_hotel", "trip:1", city="Rome")
        done = self.compensate("trip:1")
        self.assertEqual([c["task"] for c in done["compensated"]], ["book_flight"])  # the failed hotel was never completed
        self.assertEqual(UNDONE, [("F-Rome", "Rome")])  # with the original input (city) and output (booking)
        undo = Event.objects.get(case_id="trip:1", endpoint="cancel_flight")
        self.assertEqual((undo.status, undo.data["compensated"], undo.data["output"]), ("complete", "book_flight", {"cancelled": "F-Rome"}))
        audit = AuditEntry.objects.get(action="case.compensate", case_id="trip:1")
        self.assertEqual((audit.details["task"], audit.details["undo"], audit.details["status"]), ("book_flight", "cancel_flight", "done"))

    def test_once_per_task_run_and_newest_first(self):
        self.run_task("book_flight", "trip:2", city="Rome")
        self.run_task("book_flight", "trip:2", city="Oslo")
        self.assertEqual([c["task"] for c in self.compensate("trip:2")["compensated"]], ["book_flight"] * 2)
        self.assertEqual([c for _, c in UNDONE], ["Oslo", "Rome"])  # reverse order of completion
        again = self.compensate("trip:2")
        self.assertEqual((again["compensated"], again["skipped"]), ([], 2))  # idempotent
        self.assertEqual(len(UNDONE), 2)
        self.run_task("book_flight", "trip:2", city="Bari")  # later work is undone too, once
        self.assertEqual(len(self.compensate("trip:2")["compensated"]), 1)
        self.assertEqual(self.compensate("trip:3"), {"compensated": [], "failed": [], "skipped": 0})  # another case: nothing

    def test_a_failing_undo_does_not_stop_the_others_and_is_retried(self):
        self.run_task("book_flight", "trip:4", city="Rome")
        with patch.dict(TASKS):
            del TASKS["cancel_flight"]
            result = self.compensate("trip:4")
        self.assertEqual((result["compensated"], len(result["failed"])), ([], 1))
        self.assertIn("cancel_flight", result["failed"][0]["error"])
        self.assertEqual(self.compensate("trip:4")["compensated"][0]["task"], "book_flight")  # next time it works

    def test_the_compensate_step_and_a_cancel_end_in_a_scenario(self):
        w = (Workflow("Trip").start("In", "in").task("Flight", "serviceTask", "book_flight", city="{{city}}")
             .task("Hotel", "serviceTask", "book_hotel", city="{{city}}")
             .condition_gateway("Room?", "exclusive", {"yes": "{{hotel_ok}}", "no": "not {{hotel_ok}}"})
             .cancel_end("Give up", "TRIP_FAILED", "no room")
             .flow("In", "Flight", "Hotel", "Room?").flow("Room?", "Give up", output="no")).as_json()
        problems = [p for p in WorkflowCheck(w).problems if p.level == "error"]
        self.assertEqual(problems, [])
        report = ScenarioRunner(w).run({"name": "no room", "start": {"city": "Rome"}, "outputs": {"Hotel": {"hotel_ok": False}},
                                        "expect": {"ends": "error:TRIP_FAILED", "path": [
                                            "In", "Flight", "Hotel", "Room?", "Give up: undo", "Give up"]}})
        self.assertTrue(report["passed"], report)
        self.assertEqual(UNDONE, [("F-Rome", "Rome")])
        report = ScenarioRunner(w).run({"name": "no room", "start": {"city": "Rome"}, "outputs": {"Hotel": {"hotel_ok": True}}})
        self.assertTrue(report["passed"], report)
        self.assertEqual(len(UNDONE), 1)  # a hotel found: nothing more is undone

    def test_compensate_must_name_a_task(self):
        with self.assertRaises(ValueError):
            task("serviceTask", compensate="")

    def test_the_compensate_task_runs_the_saga_of_its_case(self):
        self.run_task("book_flight", "trip:5", city="Rome")
        r = self.run_task("compensate_case", "trip:5")
        self.assertEqual(r.json()["result"], {"compensated": ["book_flight"]})
        self.assertEqual(UNDONE, [("F-Rome", "Rome")])
