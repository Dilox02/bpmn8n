"""A bpmn8n workflow as a BPMN 2.0 XML document with diagram interchange (opens in bpmn.io, Camunda Modeler,
Signavio, BIMP), and the document back as a builder spec (bpmn_import.py). Standard library only (xml.etree).

n8n's implicit semantics are made explicit: an output that leads to several nodes becomes a parallel split
gateway (b8n:implicit, so the import dissolves it again), a Merge an inclusive converging gateway, a task's
error output an error boundary event, an event-based gateway's outputs intermediate catch events. What BPMN has
no place for (a task's endpoint and inputs, a form, a Laya question) goes in attributes of the b8n namespace,
which every BPMN tool keeps and ignores. Pure Python, no Django (the data lens imports the check)."""
import json
import re
from copy import deepcopy
from xml.etree import ElementTree as ET

from .builder import BAND, TASK_KINDS, Workflow
from .graph import ProcessGraph

BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DI = "http://www.omg.org/spec/DD/20100524/DI"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
B8N = "https://bpmn8n.dev/schema/1.0"
for prefix, uri in (("bpmn", BPMN), ("bpmndi", BPMNDI), ("dc", DC), ("di", DI), ("xsi", XSI), ("b8n", B8N)):
    ET.register_namespace(prefix, uri)

SIZE = {"task": (100, 80), "gateway": (50, 50), "event": (36, 36)}
SX, SY = 0.8, 0.85  # n8n positions (a 280 x 200 grid) to BPMN's: tasks 100 x 80 fit, lanes of one row are 170 high
GAP = 112  # half a column: where the gateway, catch events and so on an output needs go
UNIT_ISO = {"seconds": "T{}S", "minutes": "T{}M", "hours": "T{}H", "days": "{}D", "weeks": "{}W"}
HUMAN_OR_TASK = (*TASK_KINDS, "receiveTask", "userTask", "manualTask", "callActivity")
AS_SCRIPT = ("set", "globalVariable")  # n8n nodes that transform data: script tasks


def q(namespace, tag):
    return f"{{{namespace}}}{tag}"


def duration(amount, unit):
    """ISO 8601: (2, "weeks") -> "P2W"."""
    return "P" + UNIT_ISO[unit].format(f"{amount:g}" if isinstance(amount, float) else amount)


def to_bpmn(workflow, data=False):
    """The BPMN 2.0 document (a str) of an n8n workflow (as_json()). Its shapes come from the lane layout
    (Workflow.layout("lanes")): the workflow's own positions when its lane bands are there, else those of a
    lane layout made here. data=True adds the data lens: a data object per variable and the data associations."""
    if not any(n["name"].startswith(BAND) for n in workflow.get("nodes", [])):
        lanes = Workflow(workflow.get("name") or "Process")
        lanes.nodes, lanes.connections = deepcopy(workflow["nodes"]), deepcopy(workflow.get("connections", {}))
        workflow = {**workflow, **lanes.layout("lanes").as_json()}
    return Export(workflow, data).xml()


class Export:
    def __init__(self, workflow, data):
        self.workflow, self.data, self.graph = workflow, data, ProcessGraph(workflow)
        self.counter, self.ids = {}, {}  # a node's name -> its id
        self.shape, self.lane_of, self.taken = {}, {}, set()  # id -> (x, y, w, h); id -> lane
        self.kind, self.associations = {}, []  # id -> BPMN tag; (id, source, target) of the data associations
        self.messages, self.errors, self.flows, self.boundaries = {}, {}, [], {}
        self.root = ET.Element(q(BPMN, "definitions"), {"id": "Definitions_1", "targetNamespace": B8N + "/process",
                                                        "exporter": "bpmn8n"})
        self.process = ET.Element(q(BPMN, "process"), {"id": "Process_1", "name": workflow.get("name", ""),
                                                       "isExecutable": "false"})

    def new(self, prefix):
        self.counter[prefix] = self.counter.get(prefix, 0) + 1
        return f"{prefix}_{self.counter[prefix]}"

    def put(self, parent, tag, **attributes):
        return ET.SubElement(parent, q(BPMN, tag), {k: v for k, v in attributes.items() if v not in (None, "")})

    def extension(self, element, **values):
        """b8n:<key>: what BPMN has no place for, strings or JSON."""
        for key, value in values.items():
            if value not in (None, "", [], {}):
                element.set(q(B8N, key), value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))

    def reference(self, registry, tag, name, **attributes):
        if name not in registry:
            registry[name] = self.new(tag.capitalize())
            self.put(self.root, tag, id=registry[name], name=name, **attributes)
        return registry[name]

    # --- nodes ----------------------------------------------------------------------------

    def xml(self):
        g = self.graph
        lanes = g.lanes
        self.process.append(laneset := ET.Element(q(BPMN, "laneSet"), {"id": "LaneSet_1"}))
        lane_ids = {lane: self.new("Lane") for lane in lanes}
        for lane, lane_id in lane_ids.items():
            self.put(laneset, "lane", id=lane_id, name=lane)
        for name in g.flow_nodes:
            self.node(name)
        for name in g.flow_nodes:
            self.connect_outputs(name)
        if self.data:
            self.data_objects()
        for flow in self.flows:  # the document order the schema wants: flow nodes, then their flows
            self.process.append(flow["element"])
        for lane, element in zip(lanes, laneset):
            for element_id, owner in self.lane_of.items():
                if owner == lane:
                    self.put(element, "flowNodeRef").text = element_id
        self.root.append(collaboration := ET.Element(q(BPMN, "collaboration"), {"id": "Collaboration_1"}))
        self.put(collaboration, "participant", id="Participant_1", name=g.name or "Process", processRef="Process_1")
        self.root.append(self.process)
        self.diagram(lane_ids)
        ET.indent(self.root)
        return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(self.root, encoding="unicode")

    def node(self, name):
        g, node = self.graph, self.graph.nodes[name]
        e, p = g.element(name), node.get("parameters") or {}
        node_id = self.ids[name] = self.new(f"{e.category.capitalize()}")
        tag, size, definition = self.classify(e, p, name)
        element = self.put(self.process, tag, id=node_id, name=name)
        match e.kind:
            case kind if kind in TASK_KINDS:
                inputs = (p.get("input") or {}).get("value") or {}
                self.extension(element, task=str(p.get("task", "")).rsplit("/", 1)[-1], input=inputs)
            case "userTask" | "manualTask":
                self.extension(element, title=p.get("title"), form=(p.get("form") or {}).get("fields"),
                               description=p.get("description"), context=p.get("context"))
            case "receiveTask":
                element.set("messageRef", self.reference(self.messages, "message", p.get("message", name)))
                self.extension(element, correlationKey=p.get("correlationKey"))
            case "callActivity":
                element.set("calledElement", str((p.get("workflowId") or {}).get("value", "")))
            case "exclusiveGateway" | "inclusiveGateway":
                self.extension(element, **({"question": p.get("question"), "state": p.get("state"),
                                           "minConfidence": p.get("minConfidence")} if p.get("decider") == "laya" else {}))
            case "eventBasedGateway":
                self.extension(element, correlationKey=p.get("correlationKey"))
        if definition == "message":
            self.put(element, "messageEventDefinition", messageRef=self.reference(
                self.messages, "message", p.get("path") or name))
        elif definition == "timer":
            self.put(element, "timerEventDefinition")
        elif definition == "timerEvent":
            timer = self.put(element, "timerEventDefinition")
            if p.get("mode") == "until":
                self.put(timer, "timeDate").text = re.sub(r"=\{\{\s*\$json\.([\w.]+)\s*\}\}", r"{{\1}}", p.get("until", ""))
            else:
                self.put(timer, "timeDuration").text = duration(p.get("amount", 1), p.get("unit", "hours"))
        elif definition == "error":
            self.put(element, "errorEventDefinition", errorRef=self.reference(
                self.errors, "error", p.get("code", "error"), errorCode=p.get("code", "error")))
            self.extension(element, message=p.get("message"))
        pos = node.get("position") or [0, 0]
        self.place(node_id, (pos[0] + 50) * SX, (pos[1] + 50) * SY, size, g.lane(name))
        self.kind[node_id] = tag

    def classify(self, e, p, name):
        """(BPMN tag, shape size, event definition)."""
        if e.trigger:
            return "startEvent", SIZE["event"], "timer" if e.kind == "timerStart" else "message" if e.kind in (
                "start", "formStart") else None
        if e.kind == "timerEvent":
            return "intermediateCatchEvent", SIZE["event"], "timerEvent"
        if e.kind == "errorEndEvent":
            return "endEvent", SIZE["event"], "error"
        if e.kind in HUMAN_OR_TASK:
            return e.kind, SIZE["task"], None
        if e.kind in AS_SCRIPT:
            return "scriptTask", SIZE["task"], None
        if e.kind == "merge":
            return "inclusiveGateway", SIZE["gateway"], None
        if e.category == "gateway":
            return e.kind, SIZE["gateway"], None
        return "task", SIZE["task"], None  # an LLM agent, no-op, metrics, any other n8n node

    def place(self, element_id, cx, cy, size, lane):
        w, h = size
        while (round(cx), round(cy)) in self.taken:  # a free place: the gap between two columns holds a few
            cy += 60
        self.taken.add((round(cx), round(cy)))
        self.shape[element_id], self.lane_of[element_id] = (cx - w / 2, cy - h / 2, w, h), lane

    # --- flows ----------------------------------------------------------------------------

    def connect_outputs(self, name):
        g, node_id = self.graph, self.ids[name]
        e, p = g.element(name), self.graph.nodes[name].get("parameters") or {}
        host, labels, boundary_count, catches = self.shape[node_id], g.outputs(name), 0, 0
        lane, gateway = g.lane(name), self.process.find(f"*[@id='{node_id}']")
        for port, label in enumerate(labels):
            targets = list(dict.fromkeys(g.outs[name][port])) if port < len(g.outs[name]) else []
            source, extra = node_id, {}
            if e.kind == "eventBasedGateway":
                if targets:  # a catch event per output, one under the other in the gateway's row, then on
                    catch = self.catch(label, p, host, lane, catches, len(labels))
                    catches += 1
                    self.flow(node_id, catch)
                    self.connect(catch, targets, lane)
                continue
            if label == "error" or (label == "timeout" and e.human):
                boundary = self.boundary(node_id, host, boundary_count, "error" if label == "error" else "timer", lane)
                boundary_count, source = boundary_count + 1, boundary
            elif e.branching:
                extra = {"name": label}
                node = self.graph.nodes[name]
                value = next((b for b in (node.get("parameters") or {}).get("branches", {}).get("values", [])
                              if b["name"] == label), {})
                if label != "default":
                    extra |= {"condition": value.get("condition"), "doc": value.get("description")}
            first = self.connect(source, targets, lane, **extra)
            if first and e.default_last and label == "default":
                gateway.set("default", first)

    def catch(self, label, p, host, lane, k, n):
        element_id = self.new("Event")
        element = self.put(self.process, "intermediateCatchEvent", id=element_id, name=label)
        if label == "timeout":
            timer = self.put(element, "timerEventDefinition")
            self.put(timer, "timeDuration").text = duration(p.get("amount", 1), p.get("unit", "weeks"))
        else:
            self.put(element, "messageEventDefinition", messageRef=self.reference(self.messages, "message", label))
        self.place(element_id, host[0] + host[2] / 2 + GAP, host[1] + host[3] / 2 + (k - (n - 1) / 2) * 45,
                   SIZE["event"], lane)
        return element_id

    def boundary(self, host_id, host, index, kind, lane):
        boundary_id = self.new("Event")
        element = self.put(self.process, "boundaryEvent", id=boundary_id, attachedToRef=host_id,
                           name=kind if kind == "error" else "timeout")
        self.put(element, "errorEventDefinition" if kind == "error" else "timerEventDefinition")
        w, h = SIZE["event"]
        self.shape[boundary_id], self.lane_of[boundary_id] = (host[0] + host[2] / 2 - w / 2 + 40 * index - 20,
                                                              host[1] + host[3] - h / 2, w, h), lane
        self.boundaries[boundary_id] = host_id
        return boundary_id

    def connect(self, source, targets, lane, name=None, condition=None, doc=None):
        """The flows of one output; several targets: through a parallel split gateway. -> the first flow's id."""
        ids = [self.ids[t] if t in self.ids else t for t in targets]
        if not ids:
            return None
        if len(ids) == 1:
            return self.flow(source, ids[0], name, condition, doc)
        s = self.shape[source]
        split = self.new("Gateway")
        element = self.put(self.process, "parallelGateway", id=split)
        self.extension(element, implicit="true")
        self.place(split, s[0] + s[2] / 2 + GAP, s[1] + s[3] / 2, SIZE["gateway"], lane)
        first = self.flow(source, split, name, condition, doc)
        for target in ids:
            self.flow(split, target)
        return first

    def flow(self, source, target, name=None, condition=None, doc=None):
        flow_id = self.new("Flow")
        element = ET.Element(q(BPMN, "sequenceFlow"), {"id": flow_id, "sourceRef": source, "targetRef": target,
                                                       **({"name": name} if name else {})})
        if doc:
            self.put(element, "documentation").text = doc
        if condition:
            ET.SubElement(element, q(BPMN, "conditionExpression"), {q(XSI, "type"): "bpmn:tFormalExpression"}).text = condition
        self.flows.append({"id": flow_id, "source": source, "target": target, "element": element})
        return flow_id

    # --- data lens (C14) ------------------------------------------------------------------

    def data_objects(self):
        from .check import WorkflowCheck  # needs Django (the tasks): only the data lens does
        lens = WorkflowCheck(self.workflow, fetch=lambda _: None).data_lens()
        tasks = {n: self.ids[n] for n in lens if self.kind[self.ids[n]].endswith(("Task", "Activity"))}
        variables = sorted({v for n in tasks for side in lens[n] for v in side})
        under, refs = {}, {}
        for variable in variables:
            owner = next((n for n in tasks if variable in lens[n][1]), None) or next(n for n in tasks if variable in lens[n][0])
            do, ref = self.new("DataObject"), self.new("DataObjectReference")
            refs[variable] = ref
            self.put(self.process, "dataObject", id=do)
            self.put(self.process, "dataObjectReference", id=ref, name=variable, dataObjectRef=do)
            host = self.shape[tasks[owner]]
            k = under[owner] = under.get(owner, -1) + 1  # side by side under the task that writes it
            self.shape[ref] = (host[0] + 35 * k, host[1] + host[3] + 2, 30, 40)
        for name, node_id in tasks.items():
            reads, writes = lens[name]
            element = self.process.find(f"*[@id='{node_id}']")
            if reads:
                prop = self.new("Property")
                self.put(element, "property", id=prop, name="__targetRef_placeholder")
                for variable in reads:
                    association = self.new("DataInputAssociation")
                    inner = self.put(element, "dataInputAssociation", id=association)
                    self.put(inner, "sourceRef").text = refs[variable]
                    self.put(inner, "targetRef").text = prop
                    self.associations.append((association, refs[variable], node_id))
            for variable in writes:
                association = self.new("DataOutputAssociation")
                inner = self.put(element, "dataOutputAssociation", id=association)
                self.put(inner, "targetRef").text = refs[variable]
                self.associations.append((association, node_id, refs[variable]))

    # --- diagram interchange --------------------------------------------------------------

    def diagram(self, lane_ids):
        diagram = ET.SubElement(self.root, q(BPMNDI, "BPMNDiagram"), {"id": "Diagram_1"})
        plane = ET.SubElement(diagram, q(BPMNDI, "BPMNPlane"), {"id": "Plane_1", "bpmnElement": "Collaboration_1"})
        xs = [s[0] for s in self.shape.values()] + [s[0] + s[2] for s in self.shape.values()]
        left, right = min(xs) - 40, max(xs) + 40
        bands = {}  # a lane is its members' rows: the lane layout gives each lane its own, one after the other
        for lane in lane_ids:
            centres = [s[1] + s[3] / 2 for i, s in self.shape.items() if self.lane_of.get(i) == lane and i not in self.boundaries]
            bands[lane] = (min(centres) - 85, max(centres) + 85)
        top, bottom = min(b[0] for b in bands.values()), max(b[1] for b in bands.values())
        self.bounds(plane, "Participant_1", left - 30, top, right - left + 30, bottom - top, True)
        for lane, lane_id in lane_ids.items():
            self.bounds(plane, lane_id, left, bands[lane][0], right - left, bands[lane][1] - bands[lane][0], True)
        for element_id, shape in self.shape.items():
            self.bounds(plane, element_id, *shape, marker=self.kind.get(element_id) == "exclusiveGateway")
        for flow in self.flows:
            self.edge(plane, flow["id"], self.route(flow["source"], flow["target"]))
        for association, source, target in self.associations:
            a, b = self.shape[source], self.shape[target]
            self.edge(plane, association, [(a[0] + a[2] / 2, a[1] + a[3] / 2), (b[0] + b[2] / 2, b[1] + b[3] / 2)])

    def bounds(self, plane, element_id, x, y, w, h, horizontal=False, marker=False):
        attributes = {"id": f"{element_id}_di", "bpmnElement": element_id}
        if horizontal:
            attributes["isHorizontal"] = "true"
        if marker:
            attributes["isMarkerVisible"] = "true"
        shape = ET.SubElement(plane, q(BPMNDI, "BPMNShape"), attributes)
        ET.SubElement(shape, q(DC, "Bounds"), {k: f"{v:g}" for k, v in zip(("x", "y", "width", "height"), (round(x), round(y), w, h))})

    def edge(self, plane, element_id, points):
        edge = ET.SubElement(plane, q(BPMNDI, "BPMNEdge"), {"id": f"{element_id}_di", "bpmnElement": element_id})
        for x, y in points:
            ET.SubElement(edge, q(DI, "waypoint"), {"x": f"{round(x)}", "y": f"{round(y)}"})

    def route(self, source, target):
        """Right of the source, then straight or in an elbow to the left of the target; a flow that goes back
        runs under both. A boundary event's flow leaves from its bottom."""
        (sx, sy, sw, sh), (tx, ty, tw, th) = self.shape[source], self.shape[target]
        out = (sx + sw / 2, sy + sh) if source in self.boundaries else (sx + sw, sy + sh / 2)
        into = (tx, ty + th / 2)
        if into[0] > out[0] + 1:
            if source in self.boundaries:
                return [out, (out[0], into[1]), into]
            mid = (out[0] + into[0]) / 2
            return [out, into] if abs(out[1] - into[1]) < 1 else [out, (mid, out[1]), (mid, into[1]), into]
        below = max(sy + sh, ty + th) + 25
        start = (sx + sw / 2, sy + sh)
        return [start, (start[0], below), (tx + tw / 2, below), (tx + tw / 2, ty + th)]
