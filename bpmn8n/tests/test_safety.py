"""Data safety (PLAN G6, G8): a repeated task run executes once, LLM keys are encrypted at rest, an append-only
audit log of what people and systems did, and erasure of a case's personal data."""
import json
from datetime import timedelta
from importlib import import_module
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from ninja import Schema

from ..api.tasks import RunIn, Task
from ..clients.llm import LLMError
from ..core import crypto, shared
from ..core.erasure import StillWaiting, erase
from ..models import (
    AppendOnly,
    AuditEntry,
    Case,
    Event,
    LLMProvider,
    MessageSubscription,
    Settings,
    TaskRun,
    UserTask,
    WorkflowVersion,
)
from .support import add_events, step
from .test_people import RESUME, People
from . import test_simulation_ui

RUN = {"X-Bpmn8n-Case": "wf:1", "X-Bpmn8n-Activity": "Send Greeting", "X-Bpmn8n-Node": "node-1",
       "X-Bpmn8n-Run": "0", "X-Bpmn8n-Item": "0"}


class Greeting(Schema):
    to: str


@override_settings(API_TOKEN="")
class IdempotentRunTest(TestCase):
    """n8n repeats a run (retryOnFail, a worker restart) with the same case, node, run and item: the function runs once."""

    def setUp(self):
        self.calls, self.fail = [], False

        def send_greeting(request, data: Greeting):
            if self.fail:
                raise RuntimeError("mail server down")
            self.calls.append(data.to)
            return {"sent": len(self.calls)}

        self.task = Task(send_greeting, "sendTask")

    def run_task(self, to="ann", **headers):
        request = RequestFactory().post("/api/tasks/send_greeting/run", headers={**RUN, **headers})
        return self.task.run_in_case(request, RunIn(fields={"to": to}))

    def test_a_repeated_run_executes_once_and_gets_the_same_answer(self):
        first, again = self.run_task(), self.run_task()
        self.assertEqual(self.calls, ["ann"])
        self.assertEqual(first[0], 200)
        self.assertEqual(again, (200, {"result": {"sent": 1}, "extracted": {}}))
        self.assertEqual(TaskRun.objects.get().status, TaskRun.DONE)

    def test_another_run_or_item_executes_again(self):
        self.run_task()
        self.run_task(**{"X-Bpmn8n-Run": "1"})  # a loop came back to the node
        self.run_task(**{"X-Bpmn8n-Item": "1"})  # the node's second item
        self.run_task(**{"X-Bpmn8n-Case": "wf:2"})
        self.assertEqual(len(self.calls), 4)

    def test_without_the_identity_every_call_runs(self):
        request = RequestFactory().post("/api/tasks/send_greeting/run", headers={"X-Bpmn8n-Case": "wf:1"})
        for _ in range(2):
            self.task.run_in_case(request, RunIn(fields={"to": "ann"}))
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(TaskRun.objects.exists())

    def test_a_failure_stores_nothing_so_the_retry_runs(self):
        self.fail = True
        with self.assertRaises(RuntimeError):
            self.run_task()
        self.assertFalse(TaskRun.objects.exists())
        self.fail = False
        self.assertEqual(self.run_task()[0], 200)
        self.assertEqual(self.calls, ["ann"])

    def test_invalid_input_stores_nothing(self):
        request = RequestFactory().post("/api/tasks/send_greeting/run", headers=RUN)
        self.assertEqual(self.task.run_in_case(request, RunIn())[0], 422)
        self.assertFalse(TaskRun.objects.exists())

    def test_a_run_in_progress_is_not_run_twice_and_a_dead_one_is_taken_over(self):
        key = {"task": "send_greeting", "case_id": "wf:1", "node_id": "node-1", "run_index": 0, "item_index": 0}
        held = TaskRun.objects.create(**key, started=timezone.now())  # another worker is running it
        status, body = self.run_task()
        self.assertEqual(status, 409)
        self.assertIn("already running", body["detail"])
        self.assertEqual(self.calls, [])
        TaskRun.objects.filter(pk=held.pk).update(started=timezone.now() - TaskRun.STALE - timedelta(seconds=1))
        self.assertEqual(self.run_task()[0], 200)  # that worker died: this call runs it
        self.assertEqual(self.calls, ["ann"])

    def test_a_send_task_retried_by_n8n_sends_one_email(self):
        """The real route, the ACME project's send task: the second identical call sends nothing."""
        body = {"fields": {"to": "ann@example.org", "subject": "Hi", "body": "Hello"}}
        answers = [self.client.post("/api/tasks/send_notification/run", body, content_type="application/json",
                                    headers=RUN).json() for _ in range(2)]
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(Event.objects.filter(endpoint="send_notification").count(), 1)


class EncryptionTest(TestCase):
    KEY = "fake-provider-key-0123456789abcdef"

    def raw(self, provider):
        with connection.cursor() as cursor:
            cursor.execute("SELECT api_key FROM bpmn8n_llmprovider WHERE id = %s", [provider.pk])
            return cursor.fetchone()[0]

    def provider(self, key=KEY):
        return LLMProvider.objects.create(name="openai", preset="openai", model="gpt", api_key=key)

    def test_the_key_is_stored_encrypted_and_read_in_clear(self):
        provider = self.provider()
        stored = self.raw(provider)
        self.assertTrue(stored.startswith("enc:v1:"))
        self.assertNotIn(self.KEY, stored)
        self.assertEqual(LLMProvider.objects.get().api_key, self.KEY)
        self.assertEqual(LLMProvider.objects.get().client().api_key, self.KEY)
        self.assertEqual(self.raw(LLMProvider.objects.create(name="local", preset="ollama")), "")  # no key: empty

    def test_round_trip_and_keys(self):
        token = crypto.encrypt("secret", "one")
        self.assertEqual(crypto.decrypt(token, "one"), "secret")
        self.assertEqual(crypto.encrypt(token, "one"), token)  # never twice
        self.assertEqual(crypto.decrypt("plain"), "plain")
        with self.assertRaises(crypto.CannotDecrypt):
            crypto.decrypt(token, "two")
        with override_settings(FIELD_ENCRYPTION_KEY="field-key"):  # it wins over SECRET_KEY
            self.assertEqual(crypto.decrypt(crypto.encrypt("secret"), "field-key"), "secret")

    def test_the_migration_encrypts_plain_keys_and_back(self):
        provider = self.provider()
        with connection.cursor() as cursor:  # a key saved before the migration
            cursor.execute("UPDATE bpmn8n_llmprovider SET api_key = %s WHERE id = %s", [self.KEY, provider.pk])
        migration = import_module("bpmn8n.migrations.0014_hardening")
        editor = SimpleNamespace(connection=connection, quote_name=connection.ops.quote_name)
        migration.encrypt_keys(None, editor)
        self.assertTrue(self.raw(provider).startswith("enc:v1:"))
        self.assertEqual(LLMProvider.objects.get().api_key, self.KEY)
        migration.decrypt_keys(None, editor)
        self.assertEqual(self.raw(provider), self.KEY)

    def test_a_changed_key_is_a_clear_error_and_rotate_keys_fixes_it(self):
        provider = self.provider()
        Settings.objects.filter(pk=1).update(llm=provider)
        with override_settings(FIELD_ENCRYPTION_KEY="a-new-key"):
            call_command("check")  # the server starts
            self.assertTrue(LLMProvider.objects.get().api_key.startswith("enc:v1:"))  # loads, never sent
            with self.assertRaisesRegex(LLMError, "rotate_keys"):
                Settings.llm_client()
            with self.assertRaises(CommandError):
                call_command("rotate_keys", from_secret_key="not-the-old-one", stdout=StringIO())
            from django.conf import settings
            call_command("rotate_keys", from_secret_key=settings.SECRET_KEY, stdout=StringIO())
            self.assertEqual(Settings.llm_client().api_key, self.KEY)
        self.assertTrue(AuditEntry.objects.filter(action="llmprovider.rotate_keys").exists())


@override_settings(API_TOKEN="")
class AuditTest(People):
    def entry(self, action):
        return AuditEntry.objects.filter(action=action).latest("id")

    def test_people_and_the_api_are_recorded_in_the_transitions(self):
        task, = self.create()
        self.client.force_login(self.anna)
        self.client.post(f"/inbox/{task.pk}/claim/")  # the page
        claim = self.entry("usertask.claim")
        self.assertEqual((claim.actor, claim.case_id, claim.object_id, claim.ip), ("anna", "wf:99", str(task.pk), "127.0.0.1"))
        self.assertEqual(claim.details["activity"], "Approve Expense")
        task.refresh_from_db()
        task.release(self.anna)
        task.suspend(self.boss)
        task.resume(self.boss)
        task.reassign(self.boss, self.carl)
        task.delegate(self.carl, self.anna)
        self.assertEqual(self.entry("usertask.delegate").details["assignee"], "anna")
        self.client.logout()
        with patch(RESUME):
            self.api(task, "complete", {"ok": True})  # the API
        done = self.entry("usertask.complete")
        self.assertEqual(done.actor, "api")
        self.assertNotIn("ok", json.dumps(done.details))  # never the result
        self.assertEqual(list(AuditEntry.objects.filter(object_id=task.pk).order_by("id").values_list("action", "actor")), [
            ("usertask.claim", "anna"), ("usertask.release", "anna"), ("usertask.suspend", "boss"),
            ("usertask.resume", "boss"), ("usertask.reassign", "boss"), ("usertask.delegate", "carl"),
            ("usertask.complete", "api")])

    def test_expire_and_cancel(self):
        late, other = self.create(due=(timezone.now() - timedelta(hours=1)).isoformat(), deadline=True)[0], \
            self.create(url="http://localhost:5678/webhook-waiting/100?signature=x")[0]
        UserTask.objects.expire_due()
        self.assertEqual((self.entry("usertask.expire").actor, self.entry("usertask.expire").object_id), ("system", str(late.pk)))
        User.objects.filter(pk=self.boss.pk).update(is_superuser=True)
        self.client.force_login(self.boss)
        self.client.post("/admin/bpmn8n/usertask/", {"action": "cancel", "_selected_action": [other.pk]})
        self.assertEqual(self.entry("usertask.cancel").actor, "boss")

    def test_global_variables_through_the_api_and_the_page(self):
        with patch.object(shared, "connection", return_value=None):  # the database store
            self.client.put("/api/variables/limit", {"value": {"secret": "s3"}}, content_type="application/json")
            self.client.post("/api/variables/count/increment", {"by": 2}, content_type="application/json")
            self.client.delete("/api/variables/limit")
            self.client.force_login(self.boss)
            self.client.post("/variables/", {"key": "acme.limit", "value": "3"})
        rows = list(AuditEntry.objects.filter(object_type="variable").order_by("id").values_list("action", "object_id", "actor"))
        self.assertEqual(rows, [("variable.set", "limit", "api"), ("variable.increment", "count", "api"),
                                ("variable.delete", "limit", "api"), ("variable.set", "acme.limit", "boss")])
        self.assertNotIn("s3", json.dumps(list(AuditEntry.objects.values_list("details", flat=True))))

    def test_workflow_restore(self):
        WorkflowVersion.objects.create(workflow_id="wf1", version_id="v1", workflow={"nodes": []}, content_hash="x")
        self.client.force_login(self.boss)
        with patch("bpmn8n.core.history.restore", return_value=SimpleNamespace(version_id="v2")):
            self.client.post("/workflows/wf1/v1/restore/")
        entry = self.entry("workflow.restore")
        self.assertEqual((entry.actor, entry.object_id, entry.details), ("boss", "wf1", {"version": "v1", "saved_as": "v2"}))

    def test_settings_and_providers_in_the_admin(self):
        User.objects.filter(pk=self.boss.pk).update(is_superuser=True)
        self.client.force_login(self.boss)
        Settings.load()
        r = self.client.post("/admin/bpmn8n/settings/1/change/", {
            "llm": "", "providers-TOTAL_FORMS": "1", "providers-INITIAL_FORMS": "0", "providers-0-name": "openai",
            "providers-0-preset": "openai", "providers-0-model": "gpt", "providers-0-base_url": "",
            "providers-0-api_key": "sk-admin-secret", "providers-0-timeout": "120"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        entry = self.entry("llmprovider.add")
        self.assertEqual((entry.actor, entry.details["name"]), ("boss", "openai"))
        self.assertNotIn("sk-admin-secret", json.dumps(list(AuditEntry.objects.values_list("details", flat=True))))

    def test_the_log_is_append_only_and_read_only_in_the_admin(self):
        entry = AuditEntry.write("case.erase", object_type="case", object_id="wf:1")
        self.assertEqual(entry.actor, "system")
        with self.assertRaises(AppendOnly):
            entry.save()
        with self.assertRaises(AppendOnly):
            entry.delete()
        User.objects.filter(pk=self.boss.pk).update(is_superuser=True)
        self.client.force_login(self.boss)
        self.assertContains(self.client.get("/admin/bpmn8n/auditentry/"), "case.erase")
        self.assertEqual(self.client.get("/admin/bpmn8n/auditentry/add/").status_code, 403)
        self.client.post(f"/admin/bpmn8n/auditentry/{entry.pk}/change/", {"actor": "someone else"})
        self.assertEqual(AuditEntry.objects.get(pk=entry.pk).actor, "system")
        self.assertEqual(self.client.post(f"/admin/bpmn8n/auditentry/{entry.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.client.post("/admin/bpmn8n/auditentry/", {"action": "delete_selected", "_selected_action": [entry.pk], "post": "yes"})
        self.assertTrue(AuditEntry.objects.filter(pk=entry.pk).exists())

    def test_the_api_filters_and_the_case_page_shows_it(self):
        task, = self.create()
        task.claim(self.anna)
        AuditEntry.write("variable.set", object_type="variable", object_id="x")
        rows = self.client.get("/api/audit", {"case_id": "wf:99"}).json()
        self.assertEqual([(r["action"], r["actor"]) for r in rows], [("usertask.claim", "anna")])
        self.assertEqual(len(self.client.get("/api/audit", {"action": "variable."}).json()), 1)
        self.assertEqual(len(self.client.get("/api/audit", {"actor": "anna"}).json()), 1)
        self.assertEqual(self.client.get("/api/audit", {"since": "2999-01-01T00:00:00Z"}).json(), [])
        self.client.force_login(self.boss)
        self.assertContains(self.client.get("/cases/wf:99/"), "usertask.claim")


class SpecSaveAuditTest(TestCase):
    setUp, post = test_simulation_ui.SaveSpecTest.setUp, test_simulation_ui.SaveSpecTest.post  # not its tests

    def test_saving_a_spec_is_recorded(self):
        self.post(json.dumps({"resources": {"managers": {"capacity": 3}}}))
        entry = AuditEntry.objects.get(action="simulation.save")
        self.assertEqual((entry.actor, entry.object_id), ("sam", "demo"))


PERSONAL = ("Mario Rossi", "mario.rossi@example.org", "IT60X0542811101000000123456")


@override_settings(API_TOKEN="")
class ErasureTest(TestCase):
    CASE = "wf:42"

    def setUp(self):
        name, email, iban = PERSONAL
        Case.objects.create(case_id=self.CASE, process="Refund", variables={"customer": name, "email": email, "iban": iban})
        add_events((self.CASE, "Refund", [
            step("Register Request", 0, data={"input": {"customer": name}, "output": {"email": email},
                                              "invocation": {"node_id": "n1", "run_index": 0, "item_index": 0}}),
            step("Check Iban", 1, status="error", data={"input": {"iban": iban}}),
            step("Approve?", 2, data={"decider": "conditions", "input": {"state": {"iban": iban}},
                                      "output": {"branches": ["yes"], "scores": {"yes": 1.0}}}),
            step("Pay Refund", 3, resource="anna", data={"output": {"paid_to": iban}}),
        ]))
        Event.objects.filter(activity="Check Iban").update(error=f"ValueError: not an IBAN: {iban}")
        self.task = UserTask.objects.create(case_id=self.CASE, activity="Approve Refund", title=f"Approve {name}",
                                            description=f"Write to {email}", context={"iban": iban},
                                            result={"note": name}, resume_url="http://n8n/w/1", status="completed")
        self.message = MessageSubscription.objects.create(case_id=self.CASE, activity="Wait Bank", name="paid",
                                                          correlation_key=email, payload={"iban": iban},
                                                          resume_url="http://n8n/w/2", status="completed")
        TaskRun.objects.create(task="pay_refund", case_id=self.CASE, node_id="n4", run_index=0, item_index=0,
                               status="done", response={"result": {"iban": iban}}, started=timezone.now())

    def remaining(self):
        """Every field of every row of the case, as text."""
        rows = [*Case.objects.filter(case_id=self.CASE).values(), *Event.objects.filter(case_id=self.CASE).values(),
                *UserTask.objects.filter(case_id=self.CASE).values(), *TaskRun.objects.filter(case_id=self.CASE).values(),
                *MessageSubscription.objects.filter(case_id=self.CASE).values(),
                *AuditEntry.objects.filter(case_id=self.CASE).values()]
        return json.dumps(rows, default=str)

    def test_nothing_personal_remains_and_mining_still_works(self):
        self.assertIn(PERSONAL[0], self.remaining())
        out = StringIO()
        call_command("erase_case", self.CASE, "--yes", stdout=out)
        self.assertIn("erased wf:42", out.getvalue())
        text = self.remaining()
        for value in PERSONAL:
            self.assertNotIn(value, text)
        self.assertEqual(Case.objects.get().variables, {})
        self.assertEqual(Event.objects.filter(case_id=self.CASE).count(), 4)  # activities and times stay
        self.assertEqual(Event.objects.get(activity="Check Iban").error, "ValueError")
        self.assertEqual(Event.objects.get(activity="Pay Refund").resource, "anna")  # staff, not the data subject
        self.assertEqual(UserTask.objects.get().title, "Approve Refund")
        entry = AuditEntry.objects.get(action="case.erase")
        self.assertEqual((entry.actor, entry.case_id, entry.details["events"]), ("system", self.CASE, 4))
        process = self.client.get("/api/processmining").json()["Refund"]
        self.assertEqual(process["cases"], 1)
        self.assertEqual(process["decisions"]["Approve?"]["branches"], {"yes": 1})

    def test_a_waiting_case_is_refused_unless_forced(self):
        waiting = UserTask.objects.create(case_id=self.CASE, activity="Check", title="Check", resume_url="http://n8n/w/3")
        with self.assertRaisesRegex(CommandError, "still waits"):
            call_command("erase_case", self.CASE, "--yes")
        self.assertEqual(self.client.post(f"/api/cases/{self.CASE}/erase").status_code, 409)
        self.assertEqual(Case.objects.get().variables["customer"], PERSONAL[0])  # untouched
        r = self.client.post(f"/api/cases/{self.CASE}/erase?force=true")
        self.assertEqual(r.status_code, 200, r.content)
        waiting.refresh_from_db()
        self.assertEqual(waiting.status, UserTask.CANCELLED)
        self.assertEqual(AuditEntry.objects.get(action="case.erase").actor, "api")
        self.assertTrue(AuditEntry.objects.filter(action="usertask.cancel", object_id=waiting.pk).exists())
        self.assertNotIn(PERSONAL[2], self.remaining())

    def test_an_unknown_case_and_the_confirmation(self):
        self.assertEqual(self.client.post("/api/cases/wf:nope/erase").status_code, 404)
        with patch("builtins.input", return_value="n"), self.assertRaisesRegex(CommandError, "not erased"):
            call_command("erase_case", self.CASE)
        with self.assertRaises(StillWaiting):
            MessageSubscription.objects.filter(pk=self.message.pk).update(status="open")
            erase(self.CASE)
