"""The simulation page, the canvas's Capacity mode and Studio's Capacity card: queueing theory and the event
simulation in front of people, on projects/acme (expense approval: 2 managers, ρ = 0.54)."""
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from ..analytics.seed import generate
from ..models import Project
from ..simulation.analytic import analyse
from ..simulation.spec import SimModel, SimSpec, load
from ..web.simulation import effective

ACME = Path(settings.BASE_DIR) / "projects" / "acme"
EXPENSES = {  # a log of the expense approval as it would run in n8n: same steps, two managers
    "arrivals_per_hour": 6, "start": "Check Expense Policy", "lanes": {"managers": ["mara", "nico"]},
    "steps": {"Check Expense Policy": {"kind": "businessRuleTask", "seconds": 0.2, "next": "Assess Justification"},
              "Assess Justification": {"kind": "conditions", "branches": {"business": ("Approve Expense", 0.6),
                                                                          "personal": ("Notify Rejection", 0.1)},
                                       "default": "Approve Expense"},
              "Approve Expense": {"kind": "userTask", "lane": "managers", "minutes": 12, "next": "Record Expense"},
              "Record Expense": {"kind": "serviceTask", "seconds": 0.5, "next": "Notify Employee"},
              "Notify Employee": {"kind": "sendTask", "seconds": 0.3},
              "Notify Rejection": {"kind": "sendTask", "seconds": 0.3}}}
PROCESS = "ACME · Expense approval"  # the name of acme's workflow: the log finds its project by it


class PageTest(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("sam", is_staff=True)
        self.client.force_login(self.staff)

    def get(self, **query):
        return self.client.get("/simulation/", query)

    def test_staff_only_and_in_the_sidebar(self):
        self.assertContains(self.client.get("/docs/"), 'href="/simulation/"')
        self.client.force_login(User.objects.create_user("ann"))
        self.assertEqual(self.get().status_code, 302)
        self.assertNotContains(self.client.get("/docs/"), 'href="/simulation/"')

    def test_empty_state_lists_the_projects_and_the_acme_examples(self):
        r = self.get()
        self.assertContains(r, "Choose a workflow to read its capacity")
        self.assertContains(r, "?project=acme")
        self.assertContains(r, "?project=example%3Alibrary-lending")
        self.assertNotContains(r, "Capacity now")
        self.assertContains(self.get(project="nowhere"), "Cannot read nowhere")

    def test_a_project_shows_capacity_scenarios_and_its_spec(self):
        r = self.get(project="acme")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "The bottleneck is managers: its 2 people are busy 54 %")
        self.assertContains(r, 'class="meter"')  # ρ < 0.85: not red
        self.assertNotContains(r, 'class="meter hot"')
        rows = {row["scenario"]: row["tokens"] for row in r.context["scenarios"]["rows"]}
        self.assertEqual({k: round(v) for k, v in rows.items()}, {"business": 60, "default": 30, "personal": 10})
        self.assertContains(r, "= 100 start tokens")
        self.assertContains(r, "Save to projects/acme/simulation.json")
        steps = {s["node"]: s for s in r.context["now"]["steps"]}
        self.assertAlmostEqual(steps["Approve Expense"]["visits"], 0.9)
        self.assertGreater(steps["Approve Expense"]["waiting_time"], 0)
        self.assertEqual(steps["Record Expense"]["waiting_time"], 0)
        self.assertNotContains(r, "Model and reality")
        self.assertEqual(self.get(project="acme", tokens=1000).context["scenarios"]["total"], 1000)

    def test_an_example_runs_on_the_defaults_and_cannot_be_saved(self):
        r = self.get(project="example:library-lending")
        self.assertContains(r, "ACME · Library lending")
        self.assertContains(r, "An example has no project folder")
        self.assertTrue(r.context["scenarios"]["balanced"])

    def test_what_if_moves_the_numbers_the_right_way(self):
        now = self.get(project="acme").context["now"]["stations"][0]
        more = self.get(project="acme", **{"c:managers": 3}).context["delta"]
        [row] = more["rows"]
        self.assertEqual(row["servers"], (2, 3))
        self.assertLess(row["wait"][1], now["waiting_time"])
        self.assertLess(row["delta"], 0)
        self.assertLess(more["cycle_delta"], 0)
        busier = self.get(project="acme", load=2)
        [row] = busier.context["delta"]["rows"]
        self.assertAlmostEqual(row["utilisation"][1], 2 * now["utilisation"])
        self.assertFalse(row["stable"])  # 1.08: work comes faster than two managers do it
        self.assertContains(busier, "cannot keep up")
        self.assertContains(busier, 'class="meter hot"')
        self.assertContains(busier, "Back to now")
        self.assertEqual(self.get(project="acme", **{"c:managers": "x"}).context["changed"], False)

    def test_staffing_finds_the_fewest_managers(self):
        r = self.get(project="acme", target=20, metric="mean")
        s = r.context["staffing"]
        self.assertTrue(s["feasible"])
        self.assertEqual(s["capacity"], {"managers": 2})
        self.assertFalse(s["sensitivity"][0]["meets_target"])  # one manager cannot
        self.assertIn("c%3Amanagers=2", s["try"])
        self.assertContains(r, "needs 2 people (managers 2)")
        self.assertIn("give a positive target", self.get(project="acme", target="0").context["staffing_error"])

    def test_simulate_with_intervals_charts_and_the_closed_form_check(self):
        r = self.get(project="acme", run=1, replications=4, level=99)
        run = r.context["run"]
        self.assertEqual((run["replications"], run["level"]), (4, "99"))
        self.assertContains(r, "± 99 %")
        self.assertContains(r, 'class="plot"', count=2)  # the cycle-time histogram and the managers over time
        self.assertIn("agree", run["check"])
        self.assertTrue(any(n["node"] == "Approve Expense" and n["queue_time"] > 0 for n in run["nodes"]))
        self.assertIn("exceed", self.get(project="acme", run=1, replications=1000).context["run_error"])

    def test_the_closed_form_says_why_when_it_disagrees(self):
        r = self.get(project="acme", run=1, replications=3, **{"c:managers": 1})
        self.assertTrue(r.context["run"]["check"]["unstable"])
        self.assertContains(r, "managers cannot keep up")

    def test_download_and_the_effective_spec_read_back_to_the_same_model(self):
        r = self.get(project="acme", download=1, **{"c:managers": 3})
        self.assertIn("attachment", r["Content-Disposition"])
        spec = SimSpec.model_validate_json(r.content)
        self.assertEqual(spec.resources["managers"].capacity, 3)
        self.assertEqual(spec.workflow, "../../examples/workflows/expense-approval.json")
        workflow, file_spec = load(ACME)
        model = SimModel(workflow, file_spec)
        again = SimModel(workflow, SimSpec.model_validate(effective(model)))
        self.assertAlmostEqual(analyse(again)["cycle_time"], analyse(model)["cycle_time"])
        self.assertEqual(again.options, model.options)


class SaveSpecTest(TestCase):
    """Saving writes projects/<p>/simulation.json, checked first; never anywhere else."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("sam", is_staff=True))
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base)
        (self.base / "projects" / "demo").mkdir(parents=True)
        shutil.copy(Path(settings.BASE_DIR) / "examples" / "workflows" / "expense-approval.json",
                    self.base / "projects" / "demo" / "workflow.json")
        self.target = self.base / "projects" / "demo" / "simulation.json"

    def post(self, spec, project="demo"):
        with override_settings(BASE_DIR=self.base):
            return self.client.post(f"/simulation/?project={project}", {"spec": spec})

    def test_a_valid_spec_is_written_and_read_back(self):
        spec = {"resources": {"managers": {"capacity": 3}}, "tasks": {"Approve Expense": {"duration": 20}}}
        r = self.post(json.dumps(spec))
        self.assertRedirects(r, "/simulation/?project=demo", fetch_redirect_response=False)
        saved = json.loads(self.target.read_text())
        self.assertEqual(saved["resources"]["managers"]["capacity"], 3)
        with override_settings(BASE_DIR=self.base):
            self.assertEqual(self.client.get("/simulation/", {"project": "demo"}).context["now"]["stations"][0]["servers"], 3)

    def test_what_does_not_parse_or_fit_is_refused(self):
        for spec in ("{not json", json.dumps({"tasks": {"Nobody": {"duration": 1}}}), json.dumps({"seed": "x"}),
                     json.dumps({"workflow": "../../../../etc/hosts"}), json.dumps({"workflow": "/etc/hosts"})):
            with self.subTest(spec=spec):
                r = self.post(spec)
                self.assertEqual(r.status_code, 422)
                self.assertContains(r, "not saved", status_code=422)
                self.assertFalse(self.target.exists())

    def test_nothing_is_written_outside_a_project_folder(self):
        for project in ("../demo", "demo/../..", "example:library-lending", ".."):
            with self.subTest(project=project):
                r = self.post(json.dumps({}), project=project)
                self.assertNotEqual(r.status_code, 302)
        self.assertEqual(list(self.base.rglob("simulation.json")), [])

    def test_only_staff_save(self):
        self.client.force_login(User.objects.create_user("ann"))
        self.assertEqual(self.post(json.dumps({})).status_code, 302)  # to the sign-in, not a write
        self.assertFalse(self.target.exists())


class FromTheLogTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        generate(PROCESS, 160, seed=4, spec=EXPENSES)

    def setUp(self):
        self.client.force_login(User.objects.create_user("sam", is_staff=True))

    def test_calibration_finds_the_workflow_by_name_and_shows_the_fitted_spec(self):
        r = self.client.get("/simulation/", {"process": PROCESS})
        self.assertEqual(r.context["state"]["project"], "acme")
        log = r.context["log"]
        self.assertEqual(log["report"]["cases"], 160)
        approve = next(a for a in log["activities"] if a["name"] == "Approve Expense")
        self.assertEqual(approve["resource"], "managers")
        self.assertIn(approve["fitted"], ("exponential", "lognormal", "gamma"))
        self.assertEqual(log["people"], [("managers", ["mara", "nico"], 2)])
        shares = {b: share for b, _, share in log["gateways"][0]["branches"]}
        self.assertAlmostEqual(shares["business"], 0.6, delta=0.12)
        self.assertContains(r, "Simulate with these parameters")
        self.assertContains(r, "Simulate to compare the model with these cases")

    def test_model_and_reality_side_by_side(self):
        r = self.client.get("/simulation/", {"process": PROCESS, "source": "log", "run": 1, "replications": 2})
        self.assertTrue(r.context["calibrated"])
        reality = r.context["reality"]
        rows = {row["activity"]: row for row in reality["rows"]}
        self.assertIn("Approve Expense", rows)
        record = rows["Record Expense"]  # an automated step: calibrated from its own times, so close
        self.assertLess(abs(record["error"]), 0.25)
        self.assertGreater(rows["Approve Expense"]["error"], 0)  # the log's open time holds the queue: added again
        self.assertIsNotNone(reality["fit"])
        self.assertContains(r, "Model and reality")
        self.assertContains(r, "Back to the spec")
        what_if = self.client.get("/simulation/", {"process": PROCESS, "run": 1, "replications": 2, "c:managers": 3})
        self.assertNotIn("reality", what_if.context)  # a what-if is not what happened

    def test_a_process_without_a_workflow_is_calibrated_but_not_simulated(self):
        generate("Claims", 40, seed=1)
        r = self.client.get("/simulation/", {"process": "Claims"})
        self.assertNotIn("now", r.context)
        self.assertContains(r, "Choose the workflow that runs Claims")
        self.assertContains(self.client.get("/simulation/", {"process": "Nobody"}), "no events of the process")


class OverlayTest(TestCase):
    def setUp(self):
        Project.objects.create(name="ACME", slug="acme", n8n_id="wf1")  # Studio's project imported as wf1

    def test_per_node_utilisation_of_the_project_behind_a_canvas(self):
        body = self.client.get("/api/simulation/overlay", {"workflow_id": "wf1"}).json()
        self.assertEqual((body["source"], body["bottleneck"], body["page"]), ("acme", "managers", "/simulation/?project=acme"))
        approve = body["nodes"]["Approve Expense"]
        self.assertEqual((approve["resource"], approve["servers"]), ("managers", 2))
        self.assertAlmostEqual(approve["utilisation"], 0.54)
        self.assertGreater(approve["wait"], 60)  # seconds
        self.assertIsNone(body["nodes"]["Record Expense"]["utilisation"])  # nobody waits for it

    def test_a_workflow_read_from_n8n_takes_the_spec_of_its_namesake(self):
        library = json.loads((Path(settings.BASE_DIR) / "examples" / "workflows" / "library-lending.json").read_text())
        with patch("bpmn8n.api.simulation.n8n.workflow", return_value=library):
            body = self.client.get("/api/simulation/overlay", {"workflow_id": "lib"}).json()
        self.assertEqual(body["source"], "example:library-lending")
        self.assertEqual(body["page"], "/simulation/?project=example%3Alibrary-lending")
        with patch("bpmn8n.api.simulation.n8n.workflow", return_value=None):
            self.assertEqual(self.client.get("/api/simulation/overlay", {"workflow_id": "gone"}).status_code, 404)
        Project.objects.create(name="Draft")  # not imported into n8n: no id to match
        self.assertEqual(self.client.get("/api/simulation/overlay", {"workflow_id": ""}).status_code, 404)

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://localhost:5678")
    def test_the_token_or_an_n8n_session_that_may_open_the_workflow(self):
        url = "/api/simulation/overlay"
        self.assertEqual(self.client.get(url, {"workflow_id": "wf1"}).status_code, 401)
        self.assertEqual(self.client.get(url, {"workflow_id": "wf1"}, HTTP_AUTHORIZATION="Bearer secret").status_code, 200)
        self.client.cookies["n8n-auth"] = "session"
        with patch("bpmn8n.api.evaluations.request_json") as n8n:
            r = self.client.get(url, {"workflow_id": "wf1"}, HTTP_ORIGIN="http://localhost:5678")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/rest/workflows/wf1", n8n.call_args.args[1])
        self.assertEqual((r["Access-Control-Allow-Origin"], r["Access-Control-Allow-Credentials"]), ("http://localhost:5678", "true"))
        preflight = self.client.options(url, HTTP_ORIGIN="http://localhost:5678")
        self.assertEqual((preflight.status_code, preflight["Access-Control-Allow-Origin"]), (204, "http://localhost:5678"))


class StudioCardTest(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_user("sam", is_staff=True))

    def test_a_project_with_a_workflow_shows_its_bottleneck(self):
        Project.objects.create(name="ACME", slug="acme")  # a folder Studio did not build: given, not derived
        r = self.client.get("/studio/acme/")
        self.assertContains(r, 'href="/simulation/?project=acme"')
        self.assertContains(r, "The bottleneck is <b>managers</b>, busy 54 % of the time")

    def test_a_project_still_without_a_workflow_has_no_card(self):
        Project.objects.create(name="Draft idea")
        self.assertNotContains(self.client.get("/studio/draft_idea/"), "Open the simulation")
