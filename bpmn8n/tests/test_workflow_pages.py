"""Every page keyed by n8n's workflows: the directory (n8n's truth plus what bpmn8n saw), one workflow's pages under
/workflows/<id>/, the framework pages narrowed to one workflow, the reconciliation of waits with n8n, the pager."""
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings

from ..core import directory
from ..models import Case, Event, MessageSubscription, UserTask
from ..templatetags.bpmn8n import PROCESS_HUES, process_chip
from ..web.paging import paginate
from .support import add_events, step

N8N_WORKFLOWS = [{"id": "wf1", "name": "Refunds", "active": True}, {"id": "wf2", "name": "Onboarding", "active": False},
                 {"id": "wf3", "name": "Old intake", "active": False, "isArchived": True}]


def n8n_has(workflows=N8N_WORKFLOWS):
    """n8n's API answering with `workflows` (None: unreadable)."""
    def pages(path, **query):
        if workflows is None:
            raise directory.n8n.N8nError("down")
        return workflows
    return patch("bpmn8n.core.directory.n8n.pages", side_effect=pages)


@override_settings(N8N_API_KEY="key", REDIS_URL="", EVENT_WEBHOOKS=[])
class WorkflowPagesTest(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user("ops", password="pw", is_staff=True, is_superuser=True)
        add_events(("wf1:1", "Refunds", [step("Register", 0)]), ("wf1:2", "Refunds", [step("Register", 60, status="error")]),
                   ("wf2:7", "Onboarding", [step("Welcome", 0)]), ("gone:5", "e2e copy", [step("Register", 0)]))
        for case_id, process in (("wf1:1", "Refunds"), ("wf1:2", "Refunds"), ("wf2:7", "Onboarding"), ("gone:5", "e2e copy")):
            Case.objects.get_or_create(case_id=case_id, defaults={"process": process})
        self.refund = UserTask.objects.create(case_id="wf1:1", process="Refunds", activity="Approve", title="Approve the refund", resume_url="x")
        self.welcome = UserTask.objects.create(case_id="wf2:7", process="Onboarding", activity="Greet", title="Greet Ada", resume_url="x")
        self.orphan = UserTask.objects.create(case_id="gone:5", process="e2e copy", activity="Review", title="Review it", resume_url="x")
        self.client.force_login(self.staff)

    def test_the_directory_is_n8n_plus_what_bpmn8n_saw(self):
        with n8n_has():
            states = {r.id: (r.name, r.state) for r in directory.workflows()}
        self.assertEqual(states, {"wf1": ("Refunds", "published"), "wf2": ("Onboarding", "draft"),
                                  "wf3": ("Old intake", "archived"), "gone": ("e2e copy", "deleted")})
        cache.clear()
        with n8n_has(None):  # n8n unreadable: nothing is called deleted
            self.assertEqual({r.state for r in directory.workflows()}, {"unknown"})

    def test_a_workflows_pages_share_its_header_and_tabs(self):
        with n8n_has():
            page = self.client.get("/workflows/wf1/")
            self.assertContains(page, '<span class="proc-name">Refunds</span>', count=1)  # its mark in the header
            self.assertContains(page, '<a href="/workflows/wf1/">Refunds</a>')  # and its name in the crumbs
            for tab in ("/workflows/wf1/cases/", "/workflows/wf1/insights/", "/workflows/wf1/versions/"):
                self.assertContains(page, f'href="{tab}"')
            cases = self.client.get("/workflows/wf1/cases/")
            self.assertContains(cases, "2 cases of this workflow")
            self.assertNotContains(cases, "#7")  # wf2's
            self.assertEqual(self.client.get("/workflows/nope/").status_code, 404)
        self.assertRedirects(self.client.get("/cases/wf1:1/"), "/workflows/wf1/cases/1/", status_code=301,
                             fetch_redirect_response=False)

    def test_framework_pages_narrow_to_one_workflow(self):
        with n8n_has():
            inbox = self.client.get("/inbox/")
            self.assertContains(inbox, "?workflow=wf1")  # the row of workflows, with what each has here
            self.assertNotContains(inbox, "?workflow=gone")  # not in n8n
            self.assertNotContains(inbox, "Review it")  # a deleted workflow's task: nothing can resume it
            self.assertContains(inbox, 'aria-label="2 waiting"')  # nor does the sidebar count it
            one = self.client.get("/inbox/", {"workflow": "wf1"})
            self.assertContains(one, "Approve the refund")
            self.assertNotContains(one, "Greet Ada")
            self.assertNotContains(one, '<th scope="col">Process</th>')  # one workflow: no column repeating it
            for moved in ("/cases/", "/insights/", "/workload/"):  # each now a workflow's tab
                self.assertRedirects(self.client.get(moved), "/workflows/", fetch_redirect_response=False)
            self.assertContains(self.client.get("/workflows/wf2/cases/"), "1 case")

    def test_reconcile_cancels_only_the_waits_n8n_can_no_longer_resume(self):
        MessageSubscription.objects.create(case_id="gone:5", name="ping", correlation_key="k", resume_url="x")
        with n8n_has(None):
            call_command("reconcile", stdout=StringIO())
        self.assertEqual(UserTask.objects.waiting().count(), 3)  # an unreadable n8n says nothing
        cache.clear()
        out = StringIO()
        with n8n_has():
            call_command("reconcile", stdout=out)
        self.assertIn("cancelled 1 task(s) and 1 message wait(s)", out.getvalue())
        self.assertEqual(set(UserTask.objects.waiting().values_list("pk", flat=True)), {self.refund.pk, self.welcome.pk})
        self.orphan.refresh_from_db()
        self.assertEqual((self.orphan.status, self.orphan.reason), (UserTask.CANCELLED, "its workflow was deleted in n8n"))

    def test_only_published_workflows_can_be_started(self):
        """A production webhook answers only for a published workflow: a draft or a project never imported is not offered."""
        from types import SimpleNamespace
        from ..web import people
        node = lambda name, wid, path: SimpleNamespace(name=name, workflow_id=wid, contract=SimpleNamespace(
            starts=[{"kind": "webhook", "path": path, "method": "POST"}]))
        nodes = [node("Refunds", "wf1", "refund"), node("Onboarding", "wf2", "hello"), node("Never imported", "", "draft")]
        with n8n_has(), patch.object(people, "known_workflows", return_value=nodes), \
                patch.object(people, "start_fields", return_value=[]):
            self.assertEqual(set(people.read_starts()), {"refund"})
        cache.clear()
        with n8n_has(None), patch.object(people, "known_workflows", return_value=nodes), \
                patch.object(people, "start_fields", return_value=[]):
            self.assertEqual(set(people.read_starts()), {"refund", "hello", "draft"})  # n8n unreadable: nothing says otherwise


class PagerAndMarksTest(TestCase):
    def test_the_pager_says_the_range_and_keeps_the_query(self):
        request = RequestFactory().get("/cases/", {"status": "closed", "page": "3"})
        items, pager = paginate(request, list(range(120)), 50)
        self.assertEqual((list(items)[:2], pager["range"]), ([100, 101], "101–120 of 120"))
        self.assertIn("status=closed", pager["previous"])
        self.assertEqual(pager["next"], "")

    @override_settings(N8N_API_KEY="key")
    def test_the_workflows_in_n8n_never_share_a_hue(self):
        cache.clear()
        names = [{"id": f"w{i}", "name": n, "active": True} for i, n in enumerate(
            ["Lotto di generazione", "Dataset italiano multi-fase", "PAISÀ · qualità", "Dataset contrastivo"])]
        with n8n_has(names):
            hues = {process_chip(w["name"]).split("--h: ")[1].split('"')[0] for w in names}
        self.assertEqual(len(hues), 4)
        self.assertTrue(all(int(h) in PROCESS_HUES for h in hues))
        cache.clear()

    def test_steps_of_a_case(self):
        from ..web.cases import steps_of
        add_events(("w:1", "P", [step("A", 0), step("B", 1, status="error")]))
        self.assertEqual(steps_of(["w:1"]), {"w:1": ["ok", "error"]})
        self.assertFalse(Event.objects.filter(case_id="w:2").exists())
