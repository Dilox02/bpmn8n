"""A BPMN 2.0 XML document (bpmn.io, Camunda Modeler, Signavio, or bpmn_xml.to_bpmn) as a builder spec
(Workflow.from_spec): its steps and flows, and the problems of what bpmn8n cannot build as it is. Standard
library only. Diagram positions are ignored: the workflow is laid out again.

Mapping: tasks to task steps (the @task is b8n:task, else the task's name in snake_case), user and manual tasks
with their lane, an exclusive or inclusive gateway to a condition_gateway when its flows have {{variable}}
conditions, else to a Laya gateway whose branches are the flow names, parallel gateways, converging inclusive
and parallel gateways to a Merge (a converging exclusive one is the implicit XOR join of n8n: it dissolves),
event-based gateways with their message and timer catch events, timers, start events, an error end event. A
plain end event only ends the path."""
import json
import re
from xml.etree import ElementTree as ET

from .bpmn_xml import B8N, BPMN
from .builder import TASK_KINDS

UNITS = (("W", "weeks", 604800), ("D", "days", 86400), ("H", "hours", 3600), ("M", "minutes", 60), ("S", "seconds", 1))
DURATION = re.compile(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")
GATEWAYS = ("exclusiveGateway", "inclusiveGateway", "parallelGateway", "eventBasedGateway")
NODES = ("startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent", "task", "serviceTask",
         "scriptTask", "businessRuleTask", "sendTask", "receiveTask", "userTask", "manualTask", "callActivity",
         *GATEWAYS, "subProcess", "transaction", "adHocSubProcess")


def snake(name):
    return re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()


def slug(name):
    return snake(name).replace("_", "-")


def parse_duration(text):
    """"P2W" -> (2, "weeks"); "PT36H" -> (36, "hours"); None when it is not a duration."""
    match = DURATION.match((text or "").strip())
    if not match or not any(match.groups()):
        return None
    seconds = sum(int(n) * size for n, (_, _, size) in zip(match.groups(), UNITS) if n)
    return next((seconds // size, unit) for _, unit, size in UNITS if seconds % size == 0)


def tag(element):
    return element.tag.rpartition("}")[2]


def bpmn_to_spec(text, tasks=()):
    """(spec, problems): `tasks` the names of the registered @tasks. Raises ValueError for a document that is no BPMN."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not an XML document: {e}") from e
    process = root.find(f"{{{BPMN}}}process")
    if process is None:
        raise ValueError("no <bpmn:process> in the document")
    return Import(root, process, set(tasks)).spec()


class Import:
    def __init__(self, root, process, tasks):
        self.root, self.process, self.tasks, self.problems = root, process, tasks, []
        self.catches, self.branch_names, self.errors_of = {}, {}, set()  # gateway -> its catch events; flow -> branch
        self.messages = {m.get("id"): m.get("name") for m in root.iter(f"{{{BPMN}}}message")}
        self.errors = {m.get("id"): m.get("errorCode") or m.get("name") for m in root.iter(f"{{{BPMN}}}error")}
        self.lanes = {ref.text.strip(): lane.get("name") or lane.get("id") for lane in process.iter(f"{{{BPMN}}}lane")
                      for ref in lane.findall(f"{{{BPMN}}}flowNodeRef") if ref.text}
        self.nodes = {n.get("id"): n for n in process if tag(n) in NODES}
        self.boundary = {n.get("id"): n for n in process if tag(n) == "boundaryEvent"}
        self.flows = [{"source": f.get("sourceRef"), "target": f.get("targetRef"), "id": f.get("id"),
                       "name": (f.get("name") or "").strip(), "default": False,
                       "condition": next(((c.text or "").strip() for c in f.findall(f"{{{BPMN}}}conditionExpression")), ""),
                       "doc": next(((d.text or "").strip() for d in f.findall(f"{{{BPMN}}}documentation")), "")}
                      for f in process.iter(f"{{{BPMN}}}sequenceFlow")]
        for f in self.flows:
            f["default"] = self.nodes.get(f["source"]) is not None and self.nodes[f["source"]].get("default") == f["id"]

    # --- helpers --------------------------------------------------------------------------

    def ext(self, element, key, default=None, load=False):
        value = element.get(f"{{{B8N}}}{key}")
        if value is None:
            return default
        return json.loads(value) if load else value

    def definition(self, element):
        for child in element:
            if tag(child).endswith("EventDefinition"):
                return tag(child)[: -len("EventDefinition")], child
        return None, None

    def message(self, element, child):
        return self.messages.get(child.get("messageRef") if child is not None else None) or element.get(
            "messageRef") and self.messages.get(element.get("messageRef")) or element.get("name") or element.get("id")

    def incoming(self, node_id):
        return [f for f in self.flows if f["target"] == node_id]

    def outgoing(self, node_id):
        return [f for f in self.flows if f["source"] == node_id]

    def dissolve(self, node_id):
        """Remove a node that only passes the work on: each flow into it goes on to each flow out of it,
        keeping the name, condition and default mark of the flow in. A node nothing leaves ends its paths."""
        into, out = self.incoming(node_id), self.outgoing(node_id)
        self.flows = [f for f in self.flows if node_id not in (f["source"], f["target"])]
        self.flows += [{**f, "id": f"{f['id']}>{o['id']}", "target": o["target"]} for f in into for o in out]
        self.nodes.pop(node_id, None)

    def unique(self, names, name):
        """The step name: unique in the process."""
        base, n = name, 1
        while name in names:
            n += 1
            name = f"{base} {n}"
        return name

    # --- the spec -------------------------------------------------------------------------

    def spec(self):
        for node_id, event in self.boundary.items():  # a boundary event: an error output, or not imported
            host, (kind, child) = event.get("attachedToRef"), self.definition(event)
            if kind == "error" and host in self.nodes:
                for f in self.outgoing(node_id):
                    f.update(source=host, name="error", port="error")
                self.errors_of.add(host)
            else:
                self.problems.append(f"{event.get('name') or node_id}: a boundary {kind or 'event'} is not imported "
                                     "(only error boundary events are); its flows are dropped")
                self.flows = [f for f in self.flows if f["source"] != node_id]
        for node_id, node in list(self.nodes.items()):  # what only passes the work on, or ends a path
            kind = tag(node)
            if kind == "endEvent" and self.definition(node)[0] != "error":
                self.dissolve(node_id)
            elif kind == "exclusiveGateway" and len(self.incoming(node_id)) > 1 and len(self.outgoing(node_id)) == 1:
                self.dissolve(node_id)  # the implicit XOR join of n8n
            elif kind == "parallelGateway" and self.ext(node, "implicit"):
                self.dissolve(node_id)
            elif kind == "intermediateThrowEvent" or kind in ("subProcess", "transaction", "adHocSubProcess"):
                self.problems.append(f"{node.get('name') or node_id}: a {kind} is not imported (n8n has no such "
                                     "node; use a call activity); the flow goes past it")
                self.dissolve(node_id)
        for node_id, node in list(self.nodes.items()):  # an event-based gateway's catch events name its outputs
            if tag(node) != "eventBasedGateway":
                continue
            for f in self.outgoing(node_id):
                catch = self.nodes.get(f["target"])
                if catch is None or tag(catch) != "intermediateCatchEvent":
                    self.problems.append(f"{node.get('name') or node_id}: an event-based gateway leads to catch events only")
                    continue
                kind, child = self.definition(catch)
                label = "timeout" if kind == "timer" else self.message(catch, child)
                self.catches.setdefault(node_id, []).append((label, child if kind == "timer" else None))
                for out in self.outgoing(f["target"]):
                    self.flows.append({**out, "source": node_id, "name": label, "default": False})
                self.flows = [x for x in self.flows if f["target"] not in (x["source"], x["target"])]
                self.nodes.pop(f["target"])
        names, steps, step_of = set(), [], {}
        for node_id, node in self.nodes.items():
            if (step := self.step(node_id, node, names)) is not None:
                step_of[node_id] = step["name"]
                names.add(step["name"])
                steps.append(step)
        flows = [{"from": step_of[f["source"]], "to": [step_of[f["target"]]], **self.port(f)}
                 for f in self.flows if f["source"] in step_of and f["target"] in step_of]
        spec = {"name": self.process.get("name") or self.process.get("id") or "Imported process",
                "steps": steps, "flows": flows}
        if self.lanes:
            spec["layout"] = "lanes"
        return spec, self.problems

    def port(self, f):
        """The branch a flow leaves by, in the flow entry: a gateway's flow name (default: "default"), an error output."""
        node = self.nodes[f["source"]]
        if f.get("port"):
            return {"branch": f["port"]}
        if tag(node) in ("exclusiveGateway", "inclusiveGateway", "parallelGateway", "eventBasedGateway"):
            return {"branch": "default" if f["default"] else self.branch_names[f["id"]]}
        return {}

    # --- one step -------------------------------------------------------------------------

    def step(self, node_id, node, names):
        kind, name = tag(node), self.unique(names, node.get("name") or node_id)
        event_kind, child = self.definition(node)
        if kind == "startEvent":
            path = slug(self.message(node, child)) if event_kind == "message" else slug(name)
            return {"type": "start", "name": name, "path": path or "start"}
        if kind == "endEvent":  # an error end
            code = self.errors.get(child.get("errorRef")) if child is not None else None
            return {"type": "error_end", "name": name, "code": code or "error",
                    **({"message": self.ext(node, "message")} if self.ext(node, "message") else {})}
        if kind == "intermediateCatchEvent":
            if event_kind == "timer":
                return self.timer(name, child)
            return {"type": "receive_task", "name": name, "message": self.message(node, child),
                    "correlation_key": self.ext(node, "correlationKey", "{{case_id}}")}
        if kind == "receiveTask":
            return {"type": "receive_task", "name": name, "message": self.message(node, None),
                    "correlation_key": self.ext(node, "correlationKey", "{{case_id}}")}
        if kind in ("userTask", "manualTask"):
            return self.human(node_id, node, name, kind)
        if kind == "callActivity":
            if not node.get("calledElement"):
                self.problems.append(f"{name}: a call activity without calledElement")
            return {"type": "call_activity", "name": name, "workflow_id": node.get("calledElement") or ""}
        if kind in GATEWAYS:
            return self.gateway(node_id, node, name, kind)
        return self.task(node, name, kind if kind in TASK_KINDS else "serviceTask")

    def timer(self, name, child):
        date = child.find(f"{{{BPMN}}}timeDate")
        if date is not None and re.fullmatch(r"\{\{\s*[\w.]+\s*\}\}", (date.text or "").strip()):
            return {"type": "timer", "name": name, "until": date.text.strip()}
        duration = parse_duration((child.findtext(f"{{{BPMN}}}timeDuration") or ""))
        if duration is None:
            self.problems.append(f"{name}: a timer without a duration (P2W, PT3H...) or a {{{{variable}}}} date: "
                                 "waits 1 hour")
            duration = (1, "hours")
        return {"type": "timer", "name": name, "amount": duration[0], "unit": duration[1]}

    def task(self, node, name, kind):
        endpoint = self.ext(node, "task") or snake(name)
        if endpoint not in self.tasks:
            self.problems.append(f"{name}: unknown activity '{endpoint}': no @task of that name (the task's name "
                                 "in snake_case, or b8n:task, must be a function decorated with @task)")
        fields = self.ext(node, "input", {}, load=True)
        return {"type": "task", "name": name, "kind": kind, "endpoint": endpoint, **({"fields": fields} if fields else {}),
                **({"on_error": "branch"} if node.get("id") in self.errors_of else {})}

    def human(self, node_id, node, name, kind):
        lane = self.lanes.get(node_id)
        if lane is None:
            self.problems.append(f"{name}: a {kind} outside any lane: lane 'People'")
        step = {"type": "manual_task" if kind == "manualTask" else "user_task", "name": name,
                "title": self.ext(node, "title") or name, "lane": lane or "People"}
        if kind == "userTask":
            step["form"] = self.ext(node, "form", [], load=True)
        for key in ("description", "context"):
            if value := self.ext(node, key):
                step[key] = value
        return step

    def gateway(self, node_id, node, name, kind):
        into, out = self.incoming(node_id), self.outgoing(node_id)
        flavour = kind.removesuffix("Gateway")
        if flavour in ("inclusive", "parallel") and len(into) > 1 and len(out) == 1:
            return {"type": "merge", "name": name, "inputs": len(into)}
        taken = set()
        for i, f in enumerate(out, 1):  # a branch is its flow's name: unique, "default" for the default flow
            label = "default" if f["default"] else f["name"] or self.unique(taken, f"branch {i}")
            taken.add(label)
            self.branch_names[f["id"]] = label
        branches = list(dict.fromkeys(self.branch_names[f["id"]] for f in out if not f["default"]))
        if flavour == "parallel":
            return {"type": "parallel_gateway", "name": name, "branches": branches}
        if flavour == "eventBased":
            timer = next((child for label, child in self.catches.get(node_id, []) if label == "timeout"), None)
            duration = parse_duration(timer.findtext(f"{{{BPMN}}}timeDuration")) if timer is not None else None
            if duration is None:
                self.problems.append(f"{name}: an event-based gateway needs a timer catch event for its timeout: 1 week")
            return {"type": "event_gateway", "name": name,
                    "messages": [label for label, child in self.catches.get(node_id, []) if label != "timeout"],
                    "correlation_key": self.ext(node, "correlationKey", "{{case_id}}"), "timeout": list(duration or (1, "weeks"))}
        if any("{{" in f["condition"] for f in out if not f["default"]):
            for f in out:
                if not f["default"] and not f["condition"]:
                    self.problems.append(f"{name}: the flow '{f['name']}' has no condition")
            return {"type": "condition_gateway", "name": name, "kind": flavour,
                    "conditions": {self.branch_names[f["id"]]: f["condition"] or "{{true}}" for f in out if not f["default"]}}
        return {"type": "gateway", "name": name, "kind": flavour,
                "question": self.ext(node, "question") or f"{name.rstrip('?')}?",
                "branches": {self.branch_names[f["id"]]: f["doc"] or self.branch_names[f["id"]] for f in out if not f["default"]},
                **({"state": self.ext(node, "state")} if self.ext(node, "state") else {}),
                **({"min_confidence": float(self.ext(node, "minConfidence"))} if self.ext(node, "minConfidence") else {})}
