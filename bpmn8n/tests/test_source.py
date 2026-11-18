"""manage.py workflows diff|push|pull: a project folder in git against a fake n8n (no network)."""
import copy
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs

from django.core.management import CommandError, call_command
from django.test import TestCase

from ..clients.n8n import N8nError
from ..models import Project
from ..workflows.elements import node_type

CREDENTIAL = {"bpmn8nApi": {"id": "dev-1", "name": "bpmn8n"}}


def workflow(name="Claims", amount=100, extra=(), credentials=CREDENTIAL):
    nodes = [{"id": "n1", "name": "Claim Submitted", "type": "n8n-nodes-base.webhook", "typeVersion": 2,
              "position": [0, 0], "parameters": {"path": "claims"}, "webhookId": "w1"},
             {"id": "n2", "name": "Check Policy", "type": node_type("businessRuleTask"), "typeVersion": 1,
              "position": [280, 0], "parameters": {"task": "/api/tasks/check_policy", "amount": amount},
              "credentials": credentials},
             *extra]
    return {"name": name, "nodes": nodes, "settings": {"executionOrder": "v1"},
            "connections": {"Claim Submitted": {"main": [[{"node": "Check Policy", "type": "main", "index": 0}]]}}}


class FakeN8n:
    """n8n's public API as bpmn8n.clients.n8n uses it: workflows and credentials, `page` items per page."""

    def __init__(self, workflows=(), credentials=({"id": "dev-1", "name": "bpmn8n", "type": "bpmn8nApi"},), page=100):
        self.workflows = {w["id"]: copy.deepcopy(w) for w in workflows}
        self.credentials, self.page, self.calls = list(credentials), page, []

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        route, _, query = path.partition("?")
        params = {k: v[0] for k, v in parse_qs(query).items()}
        if method == "GET" and route in ("/workflows", "/credentials"):
            items = [w for w in self.workflows.values() if w["name"] == params.get("name", w["name"])] \
                if route == "/workflows" else self.credentials
            start = int(params.get("cursor", 0))
            more = start + self.page < len(items)
            return {"data": copy.deepcopy(items[start:start + self.page]), "nextCursor": str(start + self.page) if more else None}
        if method == "POST" and route == "/workflows":
            created = {**copy.deepcopy(body), "id": f"wf{len(self.workflows) + 1}", "active": False, "versionId": "v1"}
            self.workflows[created["id"]] = created
            return copy.deepcopy(created)
        found = self.workflows.get(route.rsplit("/", 1)[-1])
        if not found:
            raise N8nError("n8n: HTTP 404", 404)
        if method == "PUT":
            found.update(copy.deepcopy(body), versionId="v2")
        return copy.deepcopy(found)

    def stored(self, workflow_id):
        """As n8n hands it back: its own node ids, positions, versions and dates."""
        return self.workflows[workflow_id]


class SourceTests(TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.n8n = FakeN8n()
        patcher = mock.patch("bpmn8n.clients.n8n.request", side_effect=lambda *a, **k: self.n8n.request(*a, **k))
        patcher.start()
        self.addCleanup(patcher.stop)

    def project(self, slug, data):
        folder = self.root / slug
        folder.mkdir(exist_ok=True)
        (folder / "workflow.json").write_text(json.dumps(data, indent=2))
        return folder

    def in_n8n(self, data, workflow_id="wf9", **extra):
        self.n8n.workflows[workflow_id] = {**copy.deepcopy(data), "id": workflow_id, "active": True,
                                           "versionId": "abc", "createdAt": "2026-11-01", "updatedAt": "2026-11-18", **extra}
        return self.n8n.workflows[workflow_id]

    def run_command(self, *args):
        out = StringIO()
        try:
            call_command("workflows", *map(str, args), stdout=out)
        except CommandError as e:
            return out.getvalue(), e.returncode, str(e)
        return out.getvalue(), 0, ""

    def report(self, *args):
        out, code, _ = self.run_command(*args, "--json")
        return json.loads(out), code

    # --- diff -----------------------------------------------------------------------------

    def test_same_workflow_ignores_ids_positions_versions_and_credential_ids(self):
        folder = self.project("claims", workflow())
        remote = self.in_n8n(workflow(credentials={"bpmn8nApi": {"id": "prod-7", "name": "bpmn8n"}}))
        for node in remote["nodes"]:
            node["id"], node["position"], node["webhookId"] = node["id"] + "x", [999, 999], "other"
        (report,), code = self.report("diff", folder)
        self.assertEqual((report["status"], report["workflow"], code), ("same", "wf9", 0))

    def test_drift_lists_nodes_parameters_and_connections_and_exits_1(self):
        folder = self.project("claims", workflow())
        changed = workflow(amount=200, extra=[{"id": "n3", "name": "Notify", "type": "n8n-nodes-base.noOp",
                                               "typeVersion": 1, "position": [560, 0], "parameters": {}}])
        changed["connections"]["Check Policy"] = {"main": [[{"node": "Notify", "type": "main", "index": 0}]]}
        changed["settings"]["errorWorkflow"] = "wf2"
        self.in_n8n(changed)
        (report,), code = self.report("diff", folder)
        self.assertEqual((report["status"], code), ("drift", 1))
        self.assertEqual(report["nodes"]["added"], ["Notify"])
        self.assertEqual(report["nodes"]["changed"], {"Check Policy": [{"path": "parameters.amount", "git": 100, "n8n": 200}]})
        self.assertEqual(report["connections"]["added"], ["Check Policy [main 0] → Notify [main 0]"])
        self.assertEqual(report["properties"], [{"path": "settings.errorWorkflow", "n8n": "wf2"}])
        out, code, error = self.run_command("diff", folder)
        self.assertEqual(code, 1)
        self.assertIn("~ Check Policy: parameters.amount: 100 → 200", out)
        self.assertIn("+ node Notify  (in n8n, not in git)", out)
        self.assertIn("drift between git and n8n", error)

    def test_a_node_removed_in_n8n(self):
        folder = self.project("claims", workflow())
        remote = workflow()
        remote["nodes"].pop()
        remote["connections"] = {}
        self.in_n8n(remote)
        (report,), _ = self.report("diff", folder)
        self.assertEqual(report["nodes"]["removed"], ["Check Policy"])
        self.assertEqual(report["connections"]["removed"], ["Claim Submitted [main 0] → Check Policy [main 0]"])

    def test_missing_in_n8n_is_drift(self):
        (report,), code = self.report("diff", self.project("claims", workflow()))
        self.assertEqual((report["status"], code), ("missing", 1))

    def test_the_studio_projects_id_wins_over_the_name(self):
        folder = self.project("claims", workflow())
        self.in_n8n(workflow(amount=1), "wf1")
        self.in_n8n(workflow(), "wf2")
        _, code, error = self.run_command("diff", folder)
        self.assertEqual(code, 2)  # two workflows with this name: which one?
        self.assertIn("2 workflows named 'Claims'", error)
        Project.objects.create(name="Claims", n8n_id="wf2")  # slug "claims"
        (report,), code = self.report("diff", folder)
        self.assertEqual((report["workflow"], report["status"], code), ("wf2", "same", 0))

    def test_listings_follow_the_pages(self):
        self.n8n.page = 1
        for i in range(3):
            self.in_n8n(workflow(name=f"Other {i}"), f"wf{i}")
        self.in_n8n(workflow(), "wf7")
        self.n8n.credentials += [{"id": f"c{i}", "name": f"c{i}", "type": "x"} for i in range(3)]
        from ..clients import n8n
        self.assertEqual([w["id"] for w in n8n.workflows("Claims")], ["wf7"])
        self.assertEqual(len(n8n.credentials()), 4)

    # --- push -----------------------------------------------------------------------------

    def test_push_creates_never_activates_and_remembers_the_id(self):
        project = Project.objects.create(name="Claims")
        folder = self.project(project.slug, workflow())
        (result,), code = self.report("push", folder)
        self.assertEqual((result["status"], code), ("created", 0))
        self.assertFalse(self.n8n.stored(result["workflow"])["active"])
        self.assertFalse(any("activate" in path for _, path in self.n8n.calls))
        project.refresh_from_db()
        self.assertEqual(project.n8n_id, result["workflow"])
        (report,), code = self.report("diff", folder)
        self.assertEqual((report["status"], code), ("same", 0))

    def test_push_updates_in_place(self):
        folder = self.project("claims", workflow(amount=300))
        self.in_n8n(workflow())
        (result,), _ = self.report("push", folder)
        self.assertEqual((result["workflow"], result["status"]), ("wf9", "updated"))
        self.assertEqual(self.n8n.stored("wf9")["nodes"][1]["parameters"]["amount"], 300)
        self.assertEqual(len(self.n8n.workflows), 1)

    def test_push_maps_credentials_by_name_in_another_instance(self):
        self.n8n.credentials = [{"id": "prod-7", "name": "bpmn8n", "type": "bpmn8nApi"}]
        (result,), _ = self.report("push", self.project("claims", workflow()))
        self.assertEqual(self.n8n.stored(result["workflow"])["nodes"][1]["credentials"],
                         {"bpmn8nApi": {"id": "prod-7", "name": "bpmn8n"}})

    def test_push_refuses_a_missing_credential_before_writing(self):
        self.n8n.credentials = []
        _, code, error = self.run_command("push", self.project("claims", workflow()))
        self.assertEqual(code, 2)
        self.assertIn("'Check Policy' needs the bpmn8nApi credential 'bpmn8n', which this n8n does not have", error)
        self.assertEqual(self.n8n.workflows, {})

    def test_push_deploys_the_sub_process_first_and_points_the_call_at_its_new_id(self):
        call = {"id": "n3", "name": "Assess Damage", "type": node_type("callActivity"), "typeVersion": 1,
                "position": [560, 0], "parameters": {"workflowId": {"__rl": True, "mode": "list", "value": "dev-42",
                                                                     "cachedResultName": "Damage assessment"}}}
        parent = self.project("claims", workflow(extra=[call]))
        child = self.project("damage", workflow(name="Damage assessment"))
        results, code = self.report("push", parent, child)
        self.assertEqual(code, 0)
        self.assertEqual([r["project"] for r in results], [str(child), str(parent)])
        child_id = results[0]["workflow"]
        called = self.n8n.stored(results[1]["workflow"])["nodes"][2]["parameters"]["workflowId"]
        self.assertEqual((called["value"], called["cachedResultName"]), (child_id, "Damage assessment"))
        self.assertIn("dev-42", parent.joinpath("workflow.json").read_text())  # git keeps its own reference

    def test_push_refuses_a_call_to_a_workflow_n8n_does_not_have(self):
        call = {"name": "Assess Damage", "type": node_type("callActivity"), "parameters": {"workflowId": "nowhere"}}
        _, code, error = self.run_command("push", self.project("claims", workflow(extra=[call])))
        self.assertEqual(code, 2)
        self.assertIn("calls workflow 'nowhere', which this n8n does not have", error)
        self.assertEqual(self.n8n.workflows, {})

    # --- pull -----------------------------------------------------------------------------

    def test_pull_writes_the_editors_changes_in_the_files_key_order(self):
        folder = self.project("claims", workflow())
        edited = self.in_n8n(workflow(amount=250))
        edited["nodes"][1] = dict(reversed(list(edited["nodes"][1].items())))  # n8n's own key order
        edited["nodes"][1]["position"] = [300, 40]
        (result,), _ = self.report("pull", folder)
        self.assertEqual(result["status"], "updated")
        text = (folder / "workflow.json").read_text()
        data = json.loads(text)
        self.assertEqual(list(data), list(workflow()))  # the file's order; no id, versionId, dates
        self.assertEqual(list(data["nodes"][1]), list(workflow()["nodes"][1]))
        self.assertEqual((data["nodes"][1]["parameters"]["amount"], data["nodes"][1]["position"]), (250, [300, 40]))
        (result,), _ = self.report("pull", folder)
        self.assertEqual(result["status"], "same")
        self.assertEqual((folder / "workflow.json").read_text(), text)

    def test_pull_of_a_workflow_n8n_does_not_have(self):
        _, code, error = self.run_command("pull", self.project("claims", workflow()))
        self.assertEqual(code, 2)
        self.assertIn("push it first", error)

    def test_no_api_key_is_a_clear_error(self):
        folder = self.project("claims", workflow())
        mock.patch.stopall()
        with self.settings(N8N_API_KEY=""):
            _, code, error = self.run_command("diff", folder)
        self.assertEqual(code, 2)
        self.assertIn("no N8N_API_KEY", error)
