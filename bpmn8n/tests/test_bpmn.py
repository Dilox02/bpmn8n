"""BPMN 2.0 XML export and import, swimlane layout and the data lens (C12, C13, C14)."""
import json
import tempfile
from io import StringIO
from pathlib import Path
from xml.etree import ElementTree as ET

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from projects.acme.processes import PROCESSES

from ..api.tasks import TASKS, discover
from ..workflows import Workflow
from ..workflows.bpmn_import import bpmn_to_spec, parse_duration
from ..workflows.bpmn_xml import BPMN, BPMNDI, DC, DI, to_bpmn
from ..workflows.builder import BAND
from ..workflows.check import WorkflowCheck
from ..workflows.graph import ProcessGraph

FIXTURE = Path(__file__).parent / "fixtures" / "order_approval.bpmn"


def m(tag):
    return f"{{{BPMN}}}{tag}"


def shape(workflow):
    """What an export and import must keep: the node kinds and the flows with their output names."""
    g = ProcessGraph(workflow)
    return ({n: g.element(n).kind for n in g.flow_nodes},
            {(s, g.outputs(s)[p], t) for s in g.nodes for p, targets in enumerate(g.outs[s]) for t in targets})


def small():
    return (Workflow("Small").start("Go", "go")
            .task("Charge", "serviceTask", "calculate_refund", on_error="branch")
            .user_task("Look", "Look at {{x}}", "reviewers", form=[("ok", "OK", "boolean")])
            .error_end("Fail", "charge-failed", "no charge")
            .flow("Go", "Charge", "Look").flow("Charge", "Fail", output="error"))


class ExportTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        discover()

    def test_valid_document_for_every_acme_process(self):
        for key, build in PROCESSES.items():
            with self.subTest(key):
                root = ET.fromstring(to_bpmn(build().as_json()))
                ids = [e.get("id") for e in root.iter() if e.get("id")]
                self.assertEqual(len(ids), len(set(ids)), "ids are unique")
                process = root.find(m("process"))
                elements = {e.get("id") for e in process.iter()}
                flows = process.findall(m("sequenceFlow"))
                self.assertTrue(flows)
                for f in flows:
                    self.assertIn(f.get("sourceRef"), elements)
                    self.assertIn(f.get("targetRef"), elements)
                shapes = root.findall(f".//{{{BPMNDI}}}BPMNShape")
                edges = root.findall(f".//{{{BPMNDI}}}BPMNEdge")
                for d in (*shapes, *edges):
                    self.assertIn(d.get("bpmnElement"), ids)
                self.assertEqual({e.get("bpmnElement") for e in edges}, {f.get("id") for f in flows})
                self.assertTrue(all(len(e.findall(f"{{{DI}}}waypoint")) >= 2 for e in edges))
                at = {s.get("bpmnElement"): s.find(f"{{{DC}}}Bounds") for s in shapes}
                for lane in process.iter(m("lane")):  # a lane's flow nodes are inside its band
                    band = at[lane.get("id")]
                    for ref in lane.findall(m("flowNodeRef")):
                        self.assertIn(ref.text, elements)
                        box = at[ref.text]
                        self.assertGreaterEqual(int(box.get("y")), int(band.get("y")))
                        self.assertLessEqual(int(box.get("y")) + int(box.get("height")),
                                             int(band.get("y")) + int(band.get("height")))

    def test_elements_of_the_right_type_and_sizes(self):
        root = ET.fromstring(to_bpmn(PROCESSES["library-lending"]().as_json()))
        process = root.find(m("process"))
        tags = {e.tag.rpartition("}")[2] for e in process}
        self.assertLessEqual({"startEvent", "serviceTask", "sendTask", "manualTask", "userTask", "exclusiveGateway",
                              "parallelGateway", "eventBasedGateway", "intermediateCatchEvent", "laneSet"}, tags)
        start = process.find(m("startEvent"))
        self.assertIsNotNone(start.find(m("messageEventDefinition")))  # a webhook
        timers = [e.find(m("timerEventDefinition")) for e in process.findall(m("intermediateCatchEvent"))]
        self.assertEqual(sorted(t.findtext(m("timeDuration")) for t in timers if t is not None),
                         ["P1W", "P2W"])  # the wait, and the event gateway's timeout
        lanes = {lane.get("name") for lane in process.iter(m("lane"))}
        self.assertEqual(lanes, {"Customer", "librarians", "System"})
        sizes = {s.get("bpmnElement"): s.find(f"{{{DC}}}Bounds") for s in root.iter(f"{{{BPMNDI}}}BPMNShape")}
        for e in process:
            if e.tag in (m("serviceTask"), m("userTask")):
                self.assertEqual((sizes[e.get("id")].get("width"), sizes[e.get("id")].get("height")), ("100", "80"))
            if e.tag in (m("exclusiveGateway"), m("parallelGateway")):
                self.assertEqual(sizes[e.get("id")].get("width"), "50")
            if e.tag == m("startEvent"):
                self.assertEqual(sizes[e.get("id")].get("width"), "36")

    def test_flows_named_after_branches_and_default(self):
        root = ET.fromstring(to_bpmn(PROCESSES["library-lending"]().as_json()))
        process = root.find(m("process"))
        gateway = next(g for g in process.findall(m("exclusiveGateway")) if g.get("name") == "Is Book Available?")
        flows = {f.get("name"): f for f in process.findall(m("sequenceFlow")) if f.get("sourceRef") == gateway.get("id")}
        self.assertEqual(set(flows), {"available", "on loan"})  # its unused "default" output has no flow
        self.assertIn("{{available}}", flows["available"].findtext(m("conditionExpression")))
        laya = ET.fromstring(to_bpmn(PROCESSES["ticket-triage"]().as_json())).find(m("process"))
        route = next(g for g in laya.findall(m("exclusiveGateway")))
        self.assertTrue(route.get("default"))
        self.assertIn("Which team", route.get("{https://bpmn8n.dev/schema/1.0}question"))

    def test_implicit_semantics_made_explicit(self):
        wf = (Workflow("Implicit").start("Go", "go").task("A", "serviceTask", "calculate_refund")
              .task("B", "serviceTask", "calculate_refund").task("C", "serviceTask", "calculate_refund").merge("Join")
              .flow("Go", "A").flow("A", "B").flow("A", "C").flow("B", "Join").flow("C", "Join"))
        process = ET.fromstring(to_bpmn(wf.as_json())).find(m("process"))
        split = process.find(m("parallelGateway"))  # A's output leads to two nodes
        self.assertEqual(split.get("{https://bpmn8n.dev/schema/1.0}implicit"), "true")
        self.assertEqual(len([f for f in process.findall(m("sequenceFlow")) if f.get("sourceRef") == split.get("id")]), 2)
        self.assertEqual(len(process.findall(m("inclusiveGateway"))), 1)  # the Merge, converging

    def test_error_end_boundary_and_schedule(self):
        wf = small().as_json()
        wf["nodes"].append({"id": "x", "name": "Daily", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {},
                            "position": [0, 400]})
        wf["connections"]["Daily"] = {"main": [[{"node": "Charge", "type": "main", "index": 0}]]}
        process = ET.fromstring(to_bpmn(wf)).find(m("process"))
        self.assertEqual(sorted(s.find("*").tag.rpartition("}")[2] for s in process.findall(m("startEvent"))),
                         ["messageEventDefinition", "timerEventDefinition"])
        end = process.find(m("endEvent"))
        self.assertIsNotNone(end.find(m("errorEventDefinition")))
        boundary = process.find(m("boundaryEvent"))
        self.assertEqual(boundary.get("attachedToRef"),
                         next(t.get("id") for t in process.findall(m("serviceTask")) if t.get("name") == "Charge"))
        self.assertTrue(any(f.get("sourceRef") == boundary.get("id") for f in process.findall(m("sequenceFlow"))))

    def test_data_lens_off_by_default_and_on_with_data(self):
        wf = PROCESSES["expense-approval"]().as_json()
        self.assertNotIn("dataObject", to_bpmn(wf))
        root = ET.fromstring(to_bpmn(wf, data=True))
        process = root.find(m("process"))
        refs = {r.get("name"): r.get("id") for r in process.findall(m("dataObjectReference"))}
        self.assertLessEqual({"amount", "approved", "employee"}, set(refs))
        ids = {e.get("id") for e in root.iter()}
        for a in (*process.iter(m("dataInputAssociation")), *process.iter(m("dataOutputAssociation"))):
            self.assertTrue(all(r.text in ids for r in a))
        approve = next(t for t in process.findall(m("userTask")) if t.get("name") == "Approve Expense")
        self.assertIn(refs["approved"], [r.text for r in approve.iter(m("targetRef"))])  # a person's answer: written
        self.assertIn(refs["amount"], [r.text for r in approve.iter(m("sourceRef"))])  # its title: read
        shapes = {s.get("bpmnElement") for s in root.iter(f"{{{BPMNDI}}}BPMNShape")}
        self.assertLessEqual(set(refs.values()), shapes)


class ImportTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        discover()

    def test_export_import_build_keeps_kinds_flows_and_branches(self):
        for key, build in PROCESSES.items():
            with self.subTest(key):
                original = build().as_json()
                spec, problems = bpmn_to_spec(to_bpmn(original), TASKS)
                self.assertEqual(problems, [])
                self.assertEqual(shape(Workflow.from_spec(spec).as_json()), shape(original))

    def test_the_tasks_and_their_inputs_survive(self):
        original = PROCESSES["ticket-triage"]().as_json()
        rebuilt = Workflow.from_spec(bpmn_to_spec(to_bpmn(original), TASKS)[0]).as_json()
        for name in ("Notify Sales", "Open Bug Report"):
            was, now = ({n["name"]: n["parameters"] for n in wf["nodes"]}[name] for wf in (original, rebuilt))
            self.assertEqual(now["task"], was["task"])
            self.assertEqual(now["input"]["value"], was["input"]["value"])

    def test_error_boundary_and_lanes_roundtrip(self):
        original = small().as_json()
        spec, problems = bpmn_to_spec(to_bpmn(original), TASKS)
        self.assertEqual(problems, [])
        self.assertEqual(spec["layout"], "lanes")
        self.assertEqual(shape(Workflow.from_spec(spec).as_json()), shape(original))
        self.assertEqual(next(s for s in spec["steps"] if s["name"] == "Look")["lane"], "reviewers")

    def test_a_camunda_modeler_file(self):
        spec, problems = bpmn_to_spec(FIXTURE.read_text(), TASKS)
        steps = {s["name"]: s for s in spec["steps"]}
        self.assertEqual(steps["Order received"]["type"], "start")
        self.assertEqual(steps["Check Expense Policy"]["endpoint"], "check_expense_policy")  # its name in snake_case
        self.assertEqual(steps["Large order?"]["type"], "condition_gateway")
        self.assertEqual(steps["Large order?"]["conditions"], {"large": "{{amount}} > 1000"})
        self.assertEqual((steps["Approve Order"]["type"], steps["Approve Order"]["lane"]), ("user_task", "Managers"))
        self.assertEqual((steps["Wait a day"]["amount"], steps["Wait a day"]["unit"]), (1, "days"))
        self.assertNotIn("Order done", steps)  # a plain end event: the path just ends
        self.assertEqual(len(steps), 7)  # no gateway for the XOR join
        self.assertEqual(problems, ["Ship Goods: unknown activity 'ship_goods': no @task of that name (the task's name "
                                    "in snake_case, or b8n:task, must be a function decorated with @task)"])
        wf = Workflow.from_spec(spec).as_json()
        kinds, flows = shape(wf)
        self.assertIn(("Large order?", "default", "Record Expense"), flows)  # the default flow skips the join
        self.assertIn(("Approve Order", "main", "Record Expense"), flows)
        self.assertTrue(any(n["name"].startswith(BAND) for n in wf["nodes"]))

    def test_other_files_are_refused_and_durations_parsed(self):
        with self.assertRaises(ValueError):
            bpmn_to_spec("<a/>")
        with self.assertRaises(ValueError):
            bpmn_to_spec("not xml")
        self.assertEqual([parse_duration(d) for d in ("P2W", "PT36H", "P1DT12H", "PT90S", "soon")],
                         [(2, "weeks"), (36, "hours"), (36, "hours"), (90, "seconds"), None])

    def test_event_gateway_and_timer_catches(self):
        original = PROCESSES["library-lending"]().as_json()
        spec = bpmn_to_spec(to_bpmn(original), TASKS)[0]
        gateway = next(s for s in spec["steps"] if s["type"] == "event_gateway")
        self.assertEqual((gateway["messages"], gateway["timeout"]), (["hold-book", "decline-hold"], [1, "weeks"]))
        self.assertEqual(next(s for s in spec["steps"] if s["type"] == "timer")["unit"], "weeks")


class SwimlaneTest(TestCase):
    def test_bands_cover_their_nodes_and_lanes_are_rows(self):
        for key, build in PROCESSES.items():
            with self.subTest(key):
                wf = build().layout("lanes")
                graph = ProcessGraph(wf.as_json())
                bands = {n["name"][len(BAND):]: n for n in wf.nodes if n["name"].startswith(BAND)}
                self.assertEqual(list(bands), list(graph.lanes))  # start first, humans in order, System last
                at = {n["name"]: n for n in wf.nodes}
                for lane, names in graph.lanes.items():
                    band = bands[lane]["parameters"]
                    x, y = bands[lane]["position"]
                    self.assertEqual(band["content"], f"## {lane}")
                    for name in names:
                        nx, ny = at[name]["position"]
                        self.assertTrue(x <= nx and nx + 100 <= x + band["width"], (lane, name))
                        self.assertTrue(y <= ny and ny + 100 <= y + band["height"], (lane, name))
                tops = [b["position"][1] for b in bands.values()]
                self.assertEqual(tops, sorted(tops))

    def test_the_default_level_layout_does_not_change(self):
        build = PROCESSES["library-lending"]
        levels = {n["name"]: n["position"] for n in build().layout().nodes}
        lanes = build().layout("lanes")
        self.assertEqual({n["name"]: n["position"][0] for n in lanes.nodes if not n["name"].startswith(BAND)},
                         {k: v[0] for k, v in levels.items()})  # the columns do not move
        again = lanes.layout("levels")
        self.assertEqual({n["name"]: n["position"] for n in again.nodes}, levels)  # and the bands are gone
        self.assertEqual(build().layout().nodes, build().layout("levels").nodes)
        with self.assertRaises(ValueError):
            build().layout("rows")

    def test_a_spec_asks_for_lanes(self):
        spec = {"steps": [{"type": "start", "name": "S", "path": "s"}], "flows": [], "layout": "lanes"}
        self.assertTrue(any(n["name"].startswith(BAND) for n in Workflow.from_spec(spec).nodes))

    def test_handoffs_rule_counts_lane_changes_per_path(self):
        wf = (Workflow("H").start("Go", "go").task("A", "serviceTask", "calculate_refund")
              .user_task("Check", "Check", "managers", form=[]).task("B", "serviceTask", "calculate_refund")
              .flow("Go", "A", "Check", "B"))
        found = [p for p in WorkflowCheck(wf.as_json(), fetch=lambda _: None).problems if p.rule == "handoffs"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].level, "info")
        self.assertIn("changes lane 2 times: System → managers → System", found[0].message)
        one = Workflow("One").start("Go", "go").task("A", "serviceTask", "calculate_refund").flow("Go", "A")
        self.assertFalse([p for p in WorkflowCheck(one.as_json(), fetch=lambda _: None).problems if p.rule == "handoffs"])


class CommandsTest(TestCase):
    def test_export_bpmn_writes_a_file_per_process(self):
        with tempfile.TemporaryDirectory() as out:
            call_command("export_bpmn", "projects/acme", "--out", out, "--data", stdout=StringIO())
            files = sorted(p.name for p in Path(out).iterdir())
            self.assertEqual(files, ["expense-approval.bpmn", "incident-response.bpmn", "library-lending.bpmn",
                                     "ticket-triage.bpmn"])
            root = ET.parse(Path(out) / "ticket-triage.bpmn").getroot()
            self.assertIsNotNone(root.find(f".//{m('dataObjectReference')}"))
        with self.assertRaises(CommandError):
            call_command("export_bpmn", "projects/nothing", stdout=StringIO())

    def test_import_bpmn_writes_the_project_and_runs_the_check(self):
        with tempfile.TemporaryDirectory() as base, override_settings(BASE_DIR=base):
            out = StringIO()
            with self.assertRaises(CommandError):  # the check's errors (Ship Goods has no task) exit with 1
                call_command("import_bpmn", str(FIXTURE), "--project", "orders", stdout=out)
            folder = Path(base) / "projects" / "orders"
            namespace = {}
            exec((folder / "process.py").read_text(), namespace)  # noqa: S102 (a file this command just wrote)
            self.assertEqual(namespace["SPEC"]["steps"][0]["name"], "Order received")
            self.assertEqual(shape(namespace["workflow"]().as_json()), shape(json.loads((folder / "workflow.json").read_text())))
            self.assertIn("unknown activity 'ship_goods'", out.getvalue())
            self.assertIn("workflow.json", out.getvalue())
            with self.assertRaises(CommandError):
                call_command("import_bpmn", str(FIXTURE), "--project", "../x", stdout=out)
            with self.assertRaises(CommandError):
                call_command("import_bpmn", str(FIXTURE.parent / "missing.bpmn"), "--project", "x", stdout=out)

    def test_data_lens_prints_reads_and_writes_per_step(self):
        out = StringIO()
        call_command("data_lens", "projects/acme", stdout=out)
        text = out.getvalue()
        self.assertIn("acme/expense-approval", text)
        line = next(x for x in text.splitlines() if x.strip().startswith("Approve Expense:"))
        self.assertIn("reads amount, employee", line)
        self.assertIn("writes approved, note", line)

    def test_the_api_serves_the_export_with_the_token(self):
        r = self.client.get("/api/workflows/acme/ticket-triage.bpmn")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/xml"))
        self.assertIsNotNone(ET.fromstring(r.content).find(m("process")))
        self.assertNotIn(b"dataObject", r.content)
        self.assertIn(b"dataObjectReference", self.client.get("/api/workflows/acme/ticket-triage.bpmn?data=1").content)
        self.assertEqual(self.client.get("/api/workflows/nobody.bpmn").status_code, 404)
        with override_settings(API_TOKEN="s3cret"):
            self.assertEqual(self.client.get("/api/workflows/acme/ticket-triage.bpmn").status_code, 401)
            ok = self.client.get("/api/workflows/acme/ticket-triage.bpmn", headers={"Authorization": "Bearer s3cret"})
            self.assertEqual(ok.status_code, 200)
