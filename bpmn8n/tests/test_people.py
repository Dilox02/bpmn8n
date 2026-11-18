"""People's work on user tasks: the states and who may move them, deadlines, quorum, four-eyes, the
richer forms, the inbox and the task page, the builder and the API."""
from datetime import datetime, timedelta
from datetime import timezone as utc
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from ..models import Cancelled, TransitionError, UserTask
from ..workflows.builder import SpecError, Workflow
from ..workflows.check import WorkflowCheck
from ..workflows.elements import element
from ..workflows.graph import ProcessGraph

RESUME = "bpmn8n.models.waiting.post_json"
NOW = "django.utils.timezone.now"


@override_settings(N8N_URL="http://n8n:5678")
class People(TestCase):
    URL = "http://localhost:5678/webhook-waiting/99?signature=abc"

    def setUp(self):
        managers = Group.objects.create(name="managers")
        self.anna, self.carl, self.boss = (User.objects.create_user(n, password="pw") for n in ("anna", "carl", "boss"))
        for person in (self.anna, self.carl, self.boss):
            person.groups.add(managers)
        User.objects.filter(pk=self.boss.pk).update(is_staff=True)
        self.boss.refresh_from_db()
        self.bob = User.objects.create_user("bob", password="pw")  # in no lane

    def create(self, status=201, activity="Approve Expense", url=None, **body):
        r = self.client.post("/api/usertasks", {
            "title": "Approve #7", "lane": "managers", "resume_url": url or self.URL,
            "form": [{"name": "ok", "type": "boolean"}], **body,
        }, content_type="application/json", headers={"X-Bpmn8n-Case": "wf:99", "X-Bpmn8n-Activity": activity})
        self.assertEqual(r.status_code, status, r.content)
        return [UserTask.objects.get(pk=t["id"]) for t in r.json()] if status == 201 else r

    def api(self, task, action, body=None):
        return self.client.post(f"/api/usertasks/{task.pk}/{action}", body or {}, content_type="application/json")


class TransitionsTest(People):
    ALLOWED = {  # what each state allows: everything else is a forbidden move
        "open": {"claim", "complete", "suspend", "reassign", "cancel", "expire"},
        "claimed": {"release", "complete", "suspend", "reassign", "delegate", "cancel", "expire"},
        "suspended": {"resume", "cancel", "expire"},
        "completed": set(), "cancelled": set(), "expired": set(),
    }

    def test_every_state_allows_only_its_moves(self):
        task, = self.create()
        for status, allowed in self.ALLOWED.items():
            for action in UserTask.TRANSITIONS:
                UserTask.objects.filter(pk=task.pk).update(status=status)
                task.refresh_from_db()
                with self.subTest(status=status, action=action):
                    if action in allowed:
                        task.transition(action)
                        self.assertEqual(task.status, UserTask.TRANSITIONS[action][1])
                    else:
                        self.assertRaises(TransitionError, task.transition, action)

    def test_two_people_claim_at_once_and_one_wins(self):
        task, = self.create()
        mine, theirs = UserTask.objects.get(pk=task.pk), UserTask.objects.get(pk=task.pk)  # both read it open
        mine.claim(self.anna)
        with self.assertRaisesRegex(TransitionError, "it is claimed"):
            theirs.claim(self.carl)
        self.assertEqual(UserTask.objects.get(pk=task.pk).assignee, self.anna)

    def test_suspended_waits_for_nobody(self):
        task, = self.create()
        task.suspend(self.anna)  # a lane member, on an open task
        self.assertEqual(task.status, UserTask.SUSPENDED)
        self.assertTrue(UserTask.objects.waiting().filter(pk=task.pk).exists())  # n8n still waits
        self.assertFalse(UserTask.objects.workable().exists())
        with self.assertRaisesRegex(TransitionError, "it is suspended"):
            task.claim(self.carl)
        self.assertEqual(self.api(task, "complete", {"ok": True}).status_code, 409)
        self.client.force_login(self.anna)
        self.assertEqual(self.client.get("/inbox/").context["waiting_count"], 0)
        task.resume(self.carl)
        self.assertEqual((task.status, task.assignee, task.claimed), (UserTask.OPEN, None, None))

    def test_only_the_assignee_or_staff_suspend_a_claimed_task(self):
        task, = self.create()
        task.claim(self.anna)
        with self.assertRaisesRegex(TransitionError, "claimed by anna"):
            task.suspend(self.carl)
        task.suspend(self.boss)
        self.assertEqual(task.status, UserTask.SUSPENDED)

    def test_staff_reassign_to_someone_who_may_work_it(self):
        task, = self.create()
        task.claim(self.anna)
        with self.assertRaisesRegex(TransitionError, "only staff"):
            task.reassign(self.carl, self.carl)
        with self.assertRaisesRegex(TransitionError, "managers lane"):
            task.reassign(self.boss, self.bob)
        task.reassign(self.boss, self.carl)
        self.assertEqual((task.status, task.assignee), (UserTask.CLAIMED, self.carl))
        self.assertIsNotNone(task.claimed)
        with self.assertRaisesRegex(TransitionError, "already carl"):
            task.reassign(self.boss, self.carl)

    def test_the_assignee_delegates(self):
        task, = self.create()
        with self.assertRaisesRegex(TransitionError, "cannot delegate"):
            task.delegate(self.anna, self.carl)  # nobody works on it yet
        task.claim(self.anna)
        with self.assertRaisesRegex(TransitionError, "only anna"):
            task.delegate(self.carl, self.carl)
        with self.assertRaisesRegex(TransitionError, "managers lane"):
            task.delegate(self.anna, self.bob)
        task.delegate(self.anna, self.carl)
        self.assertEqual(task.assignee, self.carl)

    def test_the_api_actions_and_their_codes(self):
        task, = self.create()
        self.assertEqual(self.api(task, "suspend").json()["status"], "suspended")
        self.assertEqual(self.api(task, "suspend").status_code, 409)
        self.assertEqual(self.api(task, "resume").json()["status"], "open")
        self.assertEqual(self.api(task, "delegate", {"username": "carl"}).status_code, 409)  # nobody to delegate from
        self.assertEqual(self.api(task, "reassign", {"username": "nobody"}).status_code, 422)
        self.assertEqual(self.api(task, "reassign", {"username": "bob"}).status_code, 409)  # not in the lane
        self.assertEqual(self.api(task, "reassign", {"username": "carl"}).json()["assignee"], "carl")
        self.assertEqual(self.api(task, "delegate", {"username": "anna"}).json()["assignee"], "anna")


class DeadlineTest(People):
    def test_an_interrupting_deadline_expires_the_task(self):
        due = timezone.now() + timedelta(hours=1)
        task, = self.create(due=due.isoformat(), deadline=True, priority="urgent")
        self.assertEqual((task.due, task.deadline, task.priority), (due, True, "urgent"))
        with patch(RESUME) as resume, patch(NOW, return_value=due + timedelta(minutes=1)):
            self.assertFalse(UserTask.objects.waiting().exists())  # n8n went on through "timeout" by itself
            r = self.api(task, "complete", {"ok": True})
            self.assertEqual(r.status_code, 410)
            self.assertIn("deadline passed", r.json()["detail"])
            task.refresh_from_db()
            self.assertEqual((task.status, task.reason, task.completed), (UserTask.EXPIRED, "deadline passed", due))
            self.assertRaises(Cancelled, task.claim, self.anna)
        self.assertFalse(resume.called)

    def test_reading_or_the_command_marks_expired_tasks(self):
        due = timezone.now() + timedelta(hours=1)
        first, = self.create(due=due.isoformat(), deadline=True, url=f"{self.URL}&1")
        self.create(due=due.isoformat(), deadline=True, url=f"{self.URL}&2")
        with patch(NOW, return_value=due + timedelta(seconds=1)):
            self.client.force_login(self.anna)
            self.assertContains(self.client.get(f"/inbox/{first.pk}/"), "Expired: its deadline passed")
            out = StringIO()
            call_command("expire_tasks", stdout=out)
        self.assertEqual(out.getvalue().strip(), "1 expired")  # the page had marked the first one already

    @patch(RESUME)
    def test_a_due_time_without_deadline_only_marks_it_overdue(self, resume):
        task, = self.create(due=(timezone.now() - timedelta(hours=2)).isoformat())
        self.assertTrue(task.overdue)
        self.assertTrue(UserTask.objects.workable().exists())
        self.assertEqual(self.api(task, "complete", {"ok": True}).json()["status"], "completed")

    def test_due_from_a_variable_and_one_deadline_per_batch(self):
        items = [{"due_date": "2026-12-31"}, {"due_date": "2026-12-20T09:30:00+01:00"}]
        own = self.create(items=items, due="{{due_date}}", url=f"{self.URL}&own")
        self.assertEqual([t.due for t in own], [datetime(2026, 12, 31, 23, 59, 59, tzinfo=utc.utc),  # the end of the day
                                                datetime(2026, 12, 20, 8, 30, tzinfo=utc.utc)])
        shared = self.create(items=items, due="{{due_date}}", deadline=True, url=f"{self.URL}&shared")
        self.assertEqual({t.due for t in shared}, {datetime(2026, 12, 20, 8, 30, tzinfo=utc.utc)})  # n8n waits once
        self.assertIn("not an ISO date", self.create(422, due="{{nothing}}x").json()["detail"])
        self.assertIn("needs a due time", self.create(422, deadline=True).json()["detail"])


@patch(RESUME)
class QuorumTest(People):
    def test_any_resumes_with_the_first_and_cancels_the_rest(self, resume):
        first, second, third = self.create(items=[{}, {}, {}], completion="any")
        self.api(second, "complete", {"ok": True})
        self.assertEqual([i["_user_task_id"] for i in resume.call_args.args[1]["_items"]], [second.pk])
        for loser in (first, third):
            loser.refresh_from_db()
            self.assertEqual((loser.status, loser.reason), (UserTask.CANCELLED, "the quorum was reached"))
        self.assertEqual(self.api(first, "complete", {"ok": True}).status_code, 410)
        self.assertEqual(resume.call_count, 1)  # n8n resumed once

    def test_at_least_n_of_m(self, resume):
        first, second, third = self.create(items=[{}, {}, {}], completion=2)
        self.api(third, "complete", {"ok": False})
        self.assertFalse(resume.called)
        self.api(first, "complete", {"ok": True})
        items = resume.call_args.args[1]["_items"]
        self.assertEqual([(i["_user_task_id"], i["ok"]) for i in items], [(first.pk, True), (third.pk, False)])
        second.refresh_from_db()
        self.assertEqual(second.status, UserTask.CANCELLED)

    def test_a_quorum_above_the_batch_needs_them_all(self, resume):
        first, second = self.create(items=[{}, {}], completion=5)
        self.api(first, "complete", {"ok": True})
        self.assertFalse(resume.called)
        self.api(second, "complete", {"ok": True})
        self.assertEqual(len(resume.call_args.args[1]["_items"]), 2)


@patch(RESUME)
class ActorsTest(People):
    def submitted_by(self, person):
        submit, = self.create(activity="Submit Expense", url=f"{self.URL}&submit")
        submit.complete(person, {"ok": True})

    def test_four_eyes(self, resume):
        self.submitted_by(self.anna)
        approve, = self.create(not_same_as=["Submit Expense"])
        with self.assertRaisesRegex(TransitionError, "four-eyes"):
            approve.claim(self.anna)
        with self.assertRaisesRegex(TransitionError, "four-eyes"):
            approve.complete(self.anna, {"ok": True})
        self.client.force_login(self.anna)
        page = self.client.get(f"/inbox/{approve.pk}/")
        self.assertContains(page, "You cannot work on it")
        self.assertNotContains(page, '<form class="fields"')
        self.assertNotIn(self.anna, approve.candidates())
        approve.claim(self.carl)
        self.assertEqual(approve.assignee, self.carl)

    def test_the_assignee_comes_from_a_variable(self, resume):
        task, = self.create(assignee="{{approver}}", items=[{"approver": "carl"}], url=f"{self.URL}&1")
        self.assertEqual((task.status, task.assignee), (UserTask.CLAIMED, self.carl))
        self.assertIsNotNone(task.claimed)
        for who in ("nobody", "bob"):  # unknown, not in the lane: open to the lane
            task, = self.create(assignee=who, url=f"{self.URL}&{who}")
            self.assertEqual((task.status, task.assignee), (UserTask.OPEN, None))
        self.submitted_by(self.anna)
        task, = self.create(assignee="anna", not_same_as=["Submit Expense"], url=f"{self.URL}&four-eyes")
        self.assertEqual(task.status, UserTask.OPEN)


class FormsTest(People):
    FORM = [{"name": "note", "type": "text", "help": "What you saw"},
            {"name": "decision", "type": "choice", "options": "approve, reject"},
            {"name": "on", "type": "date"}, {"name": "mail", "type": "email"},
            {"name": "extra", "type": "string", "required": False},
            {"name": "n", "type": "number"}, {"name": "ok", "type": "boolean", "required": True}]

    def test_each_field_type_is_validated(self):
        task, = self.create(form=self.FORM)
        self.assertEqual(task.form[1]["options"], ["approve", "reject"])  # the node's comma-separated text
        good = {"note": "line 1\nline 2", "decision": "reject", "on": "2026-12-31", "mail": " a@b.it ", "n": "3"}
        self.assertEqual(task.clean_result(good), {"note": "line 1\nline 2", "decision": "reject", "on": "2026-12-31",
                                                   "mail": "a@b.it", "extra": None, "n": 3.0, "ok": False})
        with self.assertRaises(ValidationError) as e:
            task.clean_result({"note": " ", "decision": "maybe", "on": "31/12/2026", "mail": "nope", "n": "x"})
        self.assertEqual(e.exception.message_dict, {
            "note": ["required"], "decision": ["must be one of: approve, reject"], "on": ["must be a date, YYYY-MM-DD"],
            "mail": ["must be an email address"], "n": ["must be a number"]})

    def test_a_choice_needs_its_options(self):
        self.create(422, form=[{"name": "decision", "type": "choice"}])

    def test_the_task_page_draws_each_type_and_its_errors(self):
        task, = self.create(form=self.FORM)
        self.client.force_login(self.anna)
        page = self.client.get(f"/inbox/{task.pk}/")
        for html in ('<textarea class="input" id="field-1" name="note" rows="4" required aria-describedby="help-1">',
                     '<input type="radio" name="decision" value="approve" required>', 'type="date"', 'type="email"',
                     '<p class="help" id="help-1">What you saw</p>', '<span class="meta">optional</span>'):
            self.assertContains(page, html)
        r = self.client.post(f"/inbox/{task.pk}/complete/", {"note": "seen", "decision": "maybe", "on": "2026-12-31",
                                                            "mail": "a@b.it", "n": "1"})
        self.assertContains(r, '<p class="error" id="error-2">must be one of: approve, reject</p>', status_code=422)
        self.assertContains(r, ">seen</textarea>", status_code=422)  # what was typed stays
        self.assertContains(r, 'value="2026-12-31"', status_code=422)
        self.assertEqual(UserTask.objects.get(pk=task.pk).status, UserTask.OPEN)


class InboxTest(People):
    def make(self, title, **body):
        task, = self.create(title=title, activity=title, url=f"{self.URL}&{title}", **body)
        return task

    def test_most_urgent_first(self):
        now = timezone.now()
        for title, body in (("normal old", {}), ("late low", {"due": (now - timedelta(hours=2)).isoformat(), "priority": "low"}),
                            ("high later", {"due": (now + timedelta(days=5)).isoformat(), "priority": "high"}),
                            ("urgent", {"priority": "urgent"}), ("normal new", {}),
                            ("high sooner", {"due": (now + timedelta(days=1)).isoformat(), "priority": "high"}),
                            ("late normal", {"due": (now - timedelta(hours=1)).isoformat()})):
            self.make(title, **body)
        self.client.force_login(self.anna)
        page = self.client.get("/inbox/")
        self.assertEqual([t.title for t in page.context["tasks"]], [
            "late normal", "late low", "urgent", "high sooner", "high later", "normal old", "normal new"])
        self.assertContains(page, "overdue by 1 hour")
        self.assertContains(page, '<span class="pill priority p-urgent">urgent</span>', html=True)

    def test_filters(self):
        mine, suspended, urgent = self.make("mine"), self.make("suspended"), self.make("urgent", priority="urgent")
        late = self.make("late", due=(timezone.now() - timedelta(minutes=5)).isoformat())
        anyone = self.make("anyone", lane="")
        mine.claim(self.anna)
        suspended.suspend(self.boss)
        self.client.force_login(self.anna)

        def listed(**query):
            return {t.title for t in self.client.get("/inbox/", query).context["tasks"]}

        self.assertEqual(listed(), {"mine", "urgent", "late", "anyone"})  # suspended waits for nobody
        self.assertEqual(listed(show="mine"), {"mine"})
        self.assertEqual(listed(show="lanes"), {"mine", "urgent", "late"})
        self.assertEqual(listed(show="overdue"), {late.title})
        self.assertEqual(listed(show="suspended"), {"suspended"})
        self.assertEqual(listed(priority="urgent"), {urgent.title})
        self.assertEqual(listed(show="mine", priority="urgent"), set())
        self.assertEqual(self.client.get("/inbox/").context["waiting_count"], 4)
        self.assertEqual(anyone.lane, "")

    def test_the_task_page_offers_what_the_state_allows(self):
        task = self.make("approve")
        self.client.force_login(self.anna)
        page = self.client.get(f"/inbox/{task.pk}/")
        self.assertEqual(page.context["allowed"], ["claim", "complete", "suspend"])
        self.assertContains(page, 'href="/cases/wf%3A99/"')
        self.assertNotContains(page, "Reassign to")
        self.client.force_login(self.boss)
        page = self.client.get(f"/inbox/{task.pk}/")
        self.assertContains(page, "Reassign to")
        self.assertEqual([u.username for u in page.context["candidates"]], ["anna", "boss", "carl"])
        self.client.post(f"/inbox/{task.pk}/reassign/", {"username": "carl"})
        self.client.force_login(self.carl)
        self.assertEqual(self.client.get(f"/inbox/{task.pk}/").context["allowed"],
                         ["release", "complete", "suspend", "delegate"])


class BuilderTest(TestCase):
    def test_a_deadline_gives_the_task_timeout_and_done(self):
        w = (Workflow("Expenses").start("In", "in")
             .user_task("Approve", "Approve", "managers", form=[["ok", "OK", "boolean"]], due_in=(2, "days"), deadline=True,
                        priority="high", assignee="{{approver}}", not_same_as=["Submit"], completion=2)
             .manual_task("Escalate", "Escalate", "managers").manual_task("Pay", "Pay", "managers", priority="urgent"))
        params = w.node("Approve")["parameters"]
        self.assertEqual({k: params[k] for k in ("due", "dueAmount", "dueUnit", "deadline", "priority", "assignee",
                                                 "notSameAs", "completion", "quorum")},
                         {"due": "for", "dueAmount": 2, "dueUnit": "days", "deadline": True, "priority": "high",
                          "assignee": "{{approver}}", "notSameAs": ["Submit"], "completion": "atLeast", "quorum": 2})
        self.assertEqual(element(w.node("Approve")).outputs(params), ["timeout", "done"])
        w.flow("In", "Approve").flow("Approve", "Escalate", output="timeout").flow("Approve", "Pay", output="done")
        self.assertEqual([[c["node"] for c in port] for port in w.connections["Approve"]["main"]], [["Escalate"], ["Pay"]])
        workflow = w.as_json()
        self.assertEqual(ProcessGraph(workflow).outputs("Approve"), ["timeout", "done"])
        self.assertEqual(WorkflowCheck(workflow, fetch=lambda _: None).errors, [])

    def test_without_a_deadline_the_task_keeps_one_output(self):
        w = Workflow("Lending").user_task("Check Out", "Check out", "librarians", form=[], due_at="{{due_date}}")
        params = w.node("Check Out")["parameters"]
        self.assertEqual((params["due"], params["dueUntil"], "deadline" in params), ("until", "{{due_date}}", False))
        self.assertEqual(element(w.node("Check Out")).outputs(params), ["main"])
        with self.assertRaisesRegex(ValueError, "no output 'timeout'"):
            w.flow("Check Out", "Check Out", output="timeout")
        plain = Workflow("Old").user_task("U", "t", "l", form=[["ok", "OK", "boolean"]]).node("U")["parameters"]
        self.assertEqual(plain["form"], {"fields": [{"name": "ok", "label": "OK", "type": "boolean"}]})  # as before
        self.assertFalse({"due", "priority", "completion"} & plain.keys())

    def test_richer_form_fields(self):
        fields = Workflow("p").user_task("U", "t", "l", form=[
            {"name": "decision", "label": "Decision", "type": "choice", "options": ["approve", "reject"], "help": "h"},
            {"name": "note", "type": "text", "required": False}]).node("U")["parameters"]["form"]["fields"]
        self.assertEqual(fields, [{"name": "decision", "label": "Decision", "type": "choice", "options": "approve, reject",
                                   "help": "h"}, {"name": "note", "label": "", "type": "text", "required": False}])

    def test_wrong_people_arguments_are_spec_errors(self):
        start = {"type": "start", "name": "In", "path": "in"}
        task = {"type": "user_task", "name": "U", "title": "t", "lane": "l", "form": []}
        for extra, error in (({"deadline": True}, "needs its time"), ({"priority": "asap"}, "priority"),
                             ({"due_in": [2, "fortnights"]}, "due_in"), ({"due_at": "tomorrow"}, "due_at"),
                             ({"completion": 0}, "completion"), ({"not_same_as": "Submit"}, "not_same_as"),
                             ({"form": [{"name": "d", "type": "choice"}]}, "options"),
                             ({"form": [{"name": "d", "type": "file"}]}, "type 'file'")):
            with self.subTest(extra), self.assertRaisesRegex(SpecError, error):
                Workflow.from_spec({"steps": [start, {**task, **extra}]})
        spec = {"steps": [start, {**task, "due_in": [4, "hours"], "deadline": True}, {**task, "name": "Late"}],
                "flows": [{"from": "In", "to": ["U"]}, {"from": "U", "branch": "timeout", "to": ["Late"]}]}
        self.assertEqual(Workflow.from_spec(spec).connections["U"]["main"][0][0]["node"], "Late")
