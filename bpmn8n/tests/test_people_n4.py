"""N4 people: workload (H7), notifications (H8), start a case from the UI (H9), similar cases (H11)."""
from datetime import UTC, datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.core import mail
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from projects.acme.processes import PROCESSES

from ..clients.http import ServiceUnavailable
from ..models import AuditEntry, Case, CaseEmbedding, Event, Notification, Preference, Reminder, UserTask
from ..web.landscape import Node
from ..web.similar import similar_cases, tfidf
from ..workflows.contract import Contract

NOW = datetime(2026, 5, 1, 12, tzinfo=UTC)
CLOCK = "django.utils.timezone.now"


class Base(TestCase):
    def setUp(self):
        self.managers, self.others = Group.objects.create(name="managers"), Group.objects.create(name="others")
        self.anna = User.objects.create_user("anna", "anna@example.test", "pw")
        self.carl = User.objects.create_user("carl", "carl@example.test", "pw")
        self.dora = User.objects.create_user("dora", "dora@example.test", "pw")
        self.boss = User.objects.create_user("boss", "boss@example.test", "pw", is_staff=True)
        self.anna.groups.add(self.managers)
        self.carl.groups.add(self.managers)
        self.dora.groups.add(self.others)

    def task(self, **fields):
        return UserTask.objects.create(**{"title": "Approve", "lane": "managers", "resume_url": "http://n8n/x", "case_id": "wf:1",
                                          "activity": "Approve", "process": "Expenses", **fields})


class WorkloadTest(Base):
    def test_a_lane_counts_what_waits_and_means_wait_and_work(self):
        with patch(CLOCK, return_value=NOW):
            self.task(due=NOW - timedelta(hours=2))  # open, overdue
            self.task(status="claimed", assignee=self.anna, claimed=NOW, due=NOW + timedelta(hours=5))  # due soon
            self.task(status="suspended")
            self.task(status="completed", assignee=self.carl, claimed=NOW - timedelta(hours=3),
                      completed=NOW - timedelta(hours=1))
            UserTask.objects.filter(status="completed").update(created=NOW - timedelta(hours=4))
            self.client.force_login(self.boss)
            Case.objects.create(case_id="wf:1", process="Expenses")
            page = self.client.get("/workflows/wf/people/")  # a workflow's People tab
        section = page.context["sections"][0]  # one block per workflow
        lane = section["lanes"][0]
        self.assertEqual((lane["lane"], lane["open"], lane["claimed"], lane["suspended"], lane["overdue"], lane["soon"]),
                         ("managers", 1, 1, 1, 1, 1))
        self.assertEqual((lane["wait"], lane["work"]), ("30 min", "2.0 h"))  # waits of 1 h and 0 (claimed on creation); one work of 2 h
        self.assertEqual(dict(section["people"]), {"anna": {"claimed": 1, "completed": 0}, "carl": {"claimed": 0, "completed": 1}})
        self.assertEqual([t["title"] for t in section["late"]], ["Approve"])
        from django.contrib.auth.models import Group
        self.assertEqual(lane["members"], Group.objects.get(name="managers").user_set.count())  # where its people come from

    def test_a_person_sees_only_their_lanes_and_staff_all(self):
        self.task(lane="managers")
        self.task(lane="others")
        self.client.force_login(self.anna)
        Case.objects.create(case_id="wf:1", process="Expenses")
        lanes = lambda: [r["lane"] for s in self.client.get("/workflows/wf/people/").context["sections"] for r in s["lanes"]]
        self.assertEqual(lanes(), ["managers"])
        self.client.force_login(self.boss)
        self.assertEqual(lanes(), ["managers", "others"])

    def test_each_workflow_has_its_own_block(self):
        for wid in ("wfA", "wfB"):
            self.task(case_id=f"{wid}:1", lane="managers")
            Case.objects.create(case_id=f"{wid}:1", process=wid)
        self.client.force_login(self.boss)
        sections = self.client.get("/workflows/wfA/people/").context["sections"]
        self.assertEqual([s["ref"].id for s in sections], ["wfA"])  # only its own lanes
        self.assertEqual(sections[0]["lanes"][0]["open"], 1)

    def test_it_is_in_the_sidebar_and_needs_a_login(self):
        self.client.force_login(self.anna)
        self.assertNotContains(self.client.get("/inbox/"), 'href="/workload/"')  # a workflow's People tab now
        self.assertContains(self.client.get("/inbox/"), 'href="/start/"')
        self.assertEqual(Client().get("/workload/").status_code, 302)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class NotificationTest(Base):
    def test_a_reassignment_tells_the_new_assignee_once(self):
        task = self.task(status="claimed", assignee=self.anna, claimed=NOW)
        task.reassign(self.boss, self.carl)
        self.assertEqual([m.to for m in mail.outbox], [["carl@example.test"]])
        Notification.notify(task, "assigned")  # asking again: nothing goes twice
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(AuditEntry.objects.filter(action="usertask.notify", details__to="carl").exists())
        task.delegate(self.carl, self.anna)
        self.assertEqual(len(mail.outbox), 2)  # another person: told

    def test_an_unassigned_task_tells_its_lane(self):
        Notification.notify(self.task(), "overdue")
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["anna@example.test", "carl@example.test"])

    def test_the_deadline_run_tells_due_soon_then_overdue_each_once(self):
        task = self.task(status="claimed", assignee=self.anna, claimed=NOW, due=NOW + timedelta(hours=3))
        for moment, count in ((NOW, 1), (NOW + timedelta(hours=1), 1), (NOW + timedelta(hours=4), 2), (NOW + timedelta(hours=5), 2)):
            with patch(CLOCK, return_value=moment):
                call_command("run_deadlines", stdout=StringIO())
            self.assertEqual(len(mail.outbox), count, moment)
        self.assertEqual([m.subject for m in mail.outbox], ["Due soon: Approve", "Overdue: Approve"])
        self.assertEqual(Notification.objects.filter(task=task).count(), 2)

    def test_a_task_not_due_within_a_day_is_not_announced(self):
        self.task(due=NOW + timedelta(days=3))
        with patch(CLOCK, return_value=NOW):
            call_command("run_deadlines", stdout=StringIO())
        self.assertEqual(mail.outbox, [])

    def test_an_escalation_tells_the_assignee(self):
        task = self.task(status="claimed", assignee=self.anna, claimed=NOW)
        reminder = Reminder.objects.create(task=task, at=NOW, target="managers")
        reminder.fire()
        self.assertIn("anna@example.test", [m.to[0] for m in mail.outbox if m.subject.startswith("Escalated")])

    def test_a_person_can_opt_out_and_a_failing_mail_is_tried_again(self):
        Preference.objects.create(user=self.anna, email=False)
        task = self.task(status="claimed", assignee=self.anna, claimed=NOW)
        self.assertEqual(Notification.notify(task, "overdue"), 0)
        UserTask.objects.filter(pk=task.pk).update(assignee=self.carl)
        task.refresh_from_db()
        with patch("bpmn8n.models.people.send_mail", side_effect=OSError("smtp down")), self.assertLogs("bpmn8n.models.people"):
            self.assertEqual(Notification.notify(task, "overdue"), 0)
        self.assertEqual(Notification.notify(task, "overdue"), 1)

    def test_the_settings_page_saves_the_choice(self):
        self.client.force_login(self.anna)
        self.client.post("/notifications/", {"digest": "on"})
        pref = Preference.objects.get(user=self.anna)
        self.assertEqual((pref.email, pref.digest), (False, True))
        self.assertTrue(AuditEntry.objects.filter(action="user.preferences").exists())

    def test_the_digest_goes_once_a_day_to_who_opted_in_and_has_something_waiting(self):
        Preference.objects.create(user=self.anna, digest=True)
        Preference.objects.create(user=self.dora, digest=True)  # nothing waits in the others lane
        self.task(due=NOW - timedelta(hours=1))
        for moment, count in ((NOW, 1), (NOW + timedelta(hours=2), 1), (NOW + timedelta(hours=25), 2)):
            with patch(CLOCK, return_value=moment):
                call_command("send_digests", stdout=StringIO())
            self.assertEqual(len(mail.outbox), count)
        self.assertEqual(mail.outbox[0].to, ["anna@example.test"])
        self.assertIn("(overdue)", mail.outbox[0].body)


@override_settings(N8N_URL="http://n8n:5678")
class StartCaseTest(Base):
    def setUp(self):
        super().setUp()
        contract = Contract(PROCESSES["ticket-triage"]().as_json(), workflow_id="42")
        patcher = patch("bpmn8n.web.people.known_workflows", return_value=[Node("Ticket triage", contract, workflow_id="42")])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.anna)

    def test_the_form_is_generated_from_the_typed_start_data(self):
        page = self.client.get("/start/")
        fields = {f["name"]: f for f in page.context["processes"][0]["fields"]}
        self.assertEqual((fields["customer"]["kind"], fields["customer"]["required"]), ("string", True))
        self.assertEqual((fields["channel"]["kind"], fields["channel"]["options"]), ("choice", ["email", "chat", "phone"]))
        self.assertFalse(fields["channel"]["required"])
        self.assertContains(page, 'action="/start/acme-ticket/"')
        self.assertContains(page, "csrfmiddlewaretoken")

    def test_a_valid_form_posts_the_typed_declared_fields_and_links_the_case(self):
        with patch("bpmn8n.web.people.post_json", return_value={"executionId": "77"}) as post:
            page = self.client.post("/start/acme-ticket/", {"customer": "ACME", "message": "Broken", "amount": "12.5",
                                                           "channel": "chat", "is_admin": "true", "evil": "x"})
        self.assertEqual(post.call_args.args[:2], ("http://n8n:5678/webhook/acme-ticket",
                                                     {"customer": "ACME", "message": "Broken", "amount": 12.5,
                                                      "channel": "chat", "duplicate_charge": False}))
        self.assertContains(page, "/workflows/42/cases/77/")
        entry = AuditEntry.objects.get(action="case.start")
        self.assertEqual((entry.actor, entry.case_id), ("anna", "42:77"))
        self.assertEqual(entry.details["fields"], ["amount", "channel", "customer", "duplicate_charge", "message"])

    def test_without_an_execution_id_it_links_the_inbox(self):
        with patch("bpmn8n.web.people.post_json", return_value={"text": "ok"}):
            page = self.client.post("/start/acme-ticket/", {"customer": "ACME", "message": "Broken", "amount": "3"})
        self.assertContains(page, 'href="/inbox/"')

    def test_errors_show_next_to_their_fields_and_nothing_is_sent(self):
        with patch("bpmn8n.web.people.post_json") as post:
            page = self.client.post("/start/acme-ticket/", {"customer": "ACME", "channel": "fax"})
        self.assertEqual(page.status_code, 422)
        self.assertEqual(page.context["errors"], {"amount": "required", "message": "required", "channel": "must be one of: email, chat, phone"})
        self.assertContains(page, '<p class="error">required</p>', status_code=422)
        post.assert_not_called()

    def test_n8n_down_is_said_and_an_unknown_process_is_not_found(self):
        with patch("bpmn8n.web.people.post_json", side_effect=ServiceUnavailable("down")):
            page = self.client.post("/start/acme-ticket/", {"customer": "ACME", "message": "Broken", "amount": "3"})
        self.assertContains(page, "n8n did not take it", status_code=502)
        self.assertEqual(self.client.post("/start/nope/", {}).status_code, 404)

    def test_the_post_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.anna)
        self.assertEqual(client.post("/start/acme-ticket/", {"customer": "x"}).status_code, 403)


class SimilarCasesTest(Base):
    def setUp(self):
        super().setUp()
        self.context = {"customer": "ACME", "message": "printer broken in the office"}
        self.open = self.task(context=self.context, form=[{"name": "ok", "type": "boolean"}])
        self.past("wf:2", {"customer": "ACME", "message": "office printer is broken", "iban": "IT60 SECRET"}, ok=True)
        self.past("wf:3", {"customer": "Globex", "message": "invoice total wrong", "iban": "IT99 SECRET"}, ok=False)

    def past(self, case_id, variables, ok):
        Case.objects.create(case_id=case_id, process="Expenses", variables=variables)
        done = self.task(case_id=case_id, status="completed", result={"ok": ok, "note": "private"}, form=[{"name": "ok"}],
                         assignee=self.anna)
        Event.objects.create(case_id=case_id, activity="Archive", process="Expenses", endpoint="a", started=NOW, ended=NOW,
                             duration_ms=1, status="complete", resource="system")
        return done

    def test_without_the_models_server_words_decide_and_the_outcome_is_shown(self):
        with patch("bpmn8n.web.similar.EmbedClient.embed", side_effect=ServiceUnavailable("down")):
            found, how = similar_cases(self.open, self.boss)
        self.assertEqual(how, "word overlap")
        self.assertEqual([c["case_id"] for c in found][0], "wf:2")
        self.assertEqual((found[0]["end"], found[0]["decision"]), ("Archive", {"ok": True}))

    def test_tfidf_is_deterministic(self):
        self.assertEqual(tfidf(["a b", "b c"]), tfidf(["a b", "b c"]))

    def test_the_models_server_ranks_by_embeddings_and_each_case_is_embedded_once(self):
        vectors = {"customer: ACME\nmessage: printer broken in the office": [1, 0],
                   "customer: ACME\nmessage: office printer is broken": [.6, .8], "customer: Globex\nmessage: invoice total wrong": [.9, .1]}
        embed = patch("bpmn8n.web.similar.EmbedClient.embed", side_effect=lambda texts: [vectors[t] for t in texts])
        with embed as call:
            found, how = similar_cases(self.open, self.boss)
            self.assertEqual(how, "sentence embeddings")
            self.assertEqual([c["case_id"] for c in found], ["wf:3", "wf:2"])  # the mocked vectors say so
            self.assertEqual(CaseEmbedding.objects.count(), 2)
            similar_cases(self.open, self.boss)
            self.assertEqual(len(call.call_args.args[0]), 1)  # the second time only the query goes

    def test_only_what_the_task_shows_is_compared_or_shown(self):
        embedded = []
        with patch("bpmn8n.web.similar.EmbedClient.embed", side_effect=lambda t: embedded.extend(t) or [[1, 0]] * len(t)):
            similar_cases(self.open, self.boss)
        self.assertFalse([t for t in embedded if "IT60" in t or "iban" in t])
        self.client.force_login(self.anna)
        with patch("bpmn8n.web.similar.EmbedClient.embed", side_effect=ServiceUnavailable("down")):
            body = self.client.get(f"/inbox/{self.open.pk}/").content.decode()
        self.assertIn("Similar past cases", body)
        for secret in ("IT60", "IT99", "private"):
            self.assertNotIn(secret, body)

    def test_open_cases_and_other_processes_are_left_out_and_no_context_means_no_panel(self):
        self.task(case_id="wf:4")  # still waiting
        Case.objects.create(case_id="wf:4", process="Expenses", variables=self.context)
        Case.objects.create(case_id="wf:5", process="Other", variables=self.context)
        with patch("bpmn8n.web.similar.EmbedClient.embed", side_effect=ServiceUnavailable("down")):
            self.assertEqual({c["case_id"] for c in similar_cases(self.open, self.boss)[0]}, {"wf:2", "wf:3"})
            self.assertEqual(similar_cases(self.task(case_id="wf:9", context={}), self.boss), ([], ""))

    def test_a_person_gets_a_link_only_to_cases_of_their_lanes(self):
        found, _ = similar_cases(self.open, self.dora)
        self.assertFalse(any(c["link"] for c in found))
        self.assertTrue(all(c["link"] for c in similar_cases(self.open, self.anna)[0]))
        self.assertTrue(timezone.now())
