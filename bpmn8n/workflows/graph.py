"""An n8n workflow as a graph, parsed once: the shape the workflow check, process mining, the layout,
the simulator and the dry-run engine all read, instead of each re-deriving n8n's connections.

n8n's semantics, kept here:
- a node with several connections on one output runs all of them (an AND split);
- a node reached from several places runs once per arrival (an XOR join), except a Merge, which waits for
  the inputs that can still deliver and then runs once (an OR join, with execution order v1);
- a disabled node passes its items through its first output: it is a step that does nothing, not a gap;
- sticky notes are annotations, not steps; an AI Agent's chat model is a sub-node, not in the flow.
Pure Python, no Django."""
from collections import deque
from functools import cached_property

from .elements import NOOP, STICKY, Element, element

ERROR_OUTPUT = "continueErrorOutput"  # n8n's On Error: "Continue (using error output)", a node setting
CUSTOMER, PEOPLE, SYSTEM = "Customer", "People", "System"  # swimlanes: who starts a case, a lane-less person, software


def outputs(node):
    """A node's outputs in order: its element's (a gateway's branches with "default" or "timeout", a human
    task's with a deadline; else ["main"]), then "error" when n8n's On Error setting gives it one (C3)."""
    labels = element(node).outputs(node.get("parameters"))
    return [*labels, "error"] if node.get("onError") == ERROR_OUTPUT else labels


class ProcessGraph:
    def __init__(self, workflow):
        self.workflow = workflow
        self.name = workflow.get("name", "")
        self.connections = workflow.get("connections", {})
        self.node_names = {n["name"] for n in workflow.get("nodes", [])}  # every node, stickies included
        # n8n executes a disabled node as a pass-through on output 0, not a missing step
        self.nodes = {n["name"]: {**n, "type": NOOP, "parameters": {}} if n.get("disabled") else n
                      for n in workflow.get("nodes", []) if n["type"] != STICKY}
        self.disabled = {n["name"] for n in workflow.get("nodes", []) if n.get("disabled") and n["type"] != STICKY}
        # outs[name][port] = the nodes that output leads to (only port 0 for a disabled node)
        self.outs = {name: [[c["node"] for c in port or [] if c["node"] in self.nodes]
                            for port in self.connections.get(name, {}).get("main", [])[:1 if name in self.disabled else None]]
                     for name in self.nodes}
        self.incoming = {name: [] for name in self.nodes}  # name -> [(source, output port)]
        for source, ports in self.outs.items():
            for port, targets in enumerate(ports):
                for target in targets:
                    self.incoming[target].append((source, port))
        self.into = {name: {source for source, _ in arrivals} for name, arrivals in self.incoming.items()}
        # sub-nodes (an agent's chat model) connect to their node through a non-"main" connection
        self.subnodes = {name for name, outs in self.connections.items() if "main" not in outs and name in self.nodes}
        self.parent = {}  # sub-node -> the node it serves (a chat model -> its agent)
        for name in self.subnodes:
            for ports in self.connections[name].values():
                for links in ports:
                    for link in links or []:
                        self.parent.setdefault(name, link["node"])

    # --- what a node is -------------------------------------------------------------------

    def element(self, name) -> Element:
        return element(self.nodes[name])

    def of(self, **traits):
        """The names of the nodes whose element has all these traits, e.g. of(human=True)."""
        return [name for name in self.nodes if all(getattr(self.element(name), k) == v for k, v in traits.items())]

    def outputs(self, name):
        """Its output labels, in order (a gateway's branches with "default" or "timeout"; else ["main"]; then
        "error" for a node with an error output). A disabled node passes through output 0 only."""
        return ["main"] if name in self.disabled else outputs(self.nodes[name])

    def error_port(self, name):
        """The index of its "error" output, or None."""
        has_one = name not in self.disabled and self.nodes[name].get("onError") == ERROR_OUTPUT
        return len(self.outputs(name)) - 1 if has_one else None

    def successors(self, name):
        return {n for port in self.outs[name] for n in port}

    @cached_property
    def triggers(self):
        return [name for name, node in self.nodes.items() if element(node).trigger
                or node["type"].rsplit(".", 1)[-1].lower() in ("webhook", "start")]

    @cached_property
    def starts(self):
        """Where cases start: the triggers, else the nodes nothing leads to (a fragment)."""
        return self.triggers or [n for n in self.nodes if not self.into[n] and n not in self.subnodes]

    @cached_property
    def flow_nodes(self):
        """The nodes in the flow (sub-nodes left out)."""
        return [n for n in self.nodes if n not in self.subnodes]

    # --- paths ----------------------------------------------------------------------------

    def after(self, start, stop=None):
        """`start` and every step it leads to (not past `stop`)."""
        seen, queue = {start}, deque([start])
        while queue:
            current = queue.popleft()
            if current == stop:
                continue
            for n in self.successors(current) - seen:
                seen.add(n)
                queue.append(n)
        return seen

    def before(self, end):
        """`end` and every step that leads to it."""
        seen, queue = {end}, deque([end])
        while queue:
            for n in self.into[queue.popleft()] - seen:
                seen.add(n)
                queue.append(n)
        return seen

    @cached_property
    def reachable(self):
        """Every node a trigger leads to (or, without triggers, every node a start leads to)."""
        return set().union(*(self.after(s) for s in self.starts)) if self.starts else set()

    @cached_property
    def order(self):
        """A topological order of the flow (depth first from the starts; loops broken at their back edge),
        then the nodes no start reaches."""
        order, seen = [], set()

        def visit(name):
            seen.add(name)
            for n in sorted(self.successors(name), key=self._position):
                if n not in seen:
                    visit(n)
            order.append(name)

        for name in self.starts:
            if name not in seen:
                visit(name)
        order.reverse()
        return order + [n for n in self.flow_nodes if n not in seen]

    @cached_property
    def rank(self):
        return {n: i for i, n in enumerate(self.order)}

    @cached_property
    def back_edges(self):
        """(source, target) flows that go back in the topological order: the loops."""
        return {(s, t) for s in self.nodes for t in self.successors(s) if self.rank.get(t, 0) <= self.rank.get(s, 0)}

    def forward(self, name):
        """Its successors without the loops' back edges, without duplicates, in output order."""
        return [t for port in self.outs[name] for t in dict.fromkeys(port) if (name, t) not in self.back_edges]

    @cached_property
    def dominators(self):
        """name -> the steps every path from a start to it passes (itself included)."""
        starts = {name for name in self.nodes if element(self.nodes[name]).trigger or not self.into[name]}
        reached = set().union(*(self.after(name) for name in starts)) if starts else set()
        dom = {name: {name} if name in starts or name not in reached else set(reached) for name in self.nodes}
        changed = True
        while changed:
            changed = False
            for name in reached - starts:
                parents = self.into[name] & reached
                value = {name} | set.intersection(*(dom[parent] for parent in parents))
                if value != dom[name]:
                    dom[name], changed = value, True
        return dom

    @cached_property
    def cycles(self):
        """The loops: strongly connected components with more than one node, or a node that leads to itself
        (Tarjan's algorithm, iterative)."""
        index, low, on_stack, stack, components, counter = {}, {}, set(), [], [], [0]
        for root in self.nodes:
            if root in index:
                continue
            work = [(root, iter(sorted(self.successors(root))))]
            index[root] = low[root] = counter[0]
            counter[0] += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                node, children = work[-1]
                for child in children:
                    if child not in index:
                        index[child] = low[child] = counter[0]
                        counter[0] += 1
                        stack.append(child)
                        on_stack.add(child)
                        work.append((child, iter(sorted(self.successors(child)))))
                        break
                    if child in on_stack:
                        low[node] = min(low[node], index[child])
                else:
                    work.pop()
                    if work:
                        low[work[-1][0]] = min(low[work[-1][0]], low[node])
                    if low[node] == index[node]:
                        component = set()
                        while True:
                            member = stack.pop()
                            on_stack.discard(member)
                            component.add(member)
                            if member == node:
                                break
                        if len(component) > 1 or node in self.successors(node):
                            components.append(component)
        return components

    def ends(self):
        """The nodes where a path ends (no output leads anywhere)."""
        return [n for n in self.flow_nodes if not self.successors(n)]

    def paths(self, limit=100):
        """The paths from the starts to where they end, loops not followed back (at most `limit`, depth first)."""
        found, stack = [], [[s] for s in reversed(self.starts)]
        while stack and len(found) < limit:
            path = stack.pop()
            nexts = [n for n in self.forward(path[-1]) if n not in path]
            if not nexts:
                found.append(path)
            stack.extend(path + [n] for n in reversed(nexts))
        return found

    # --- swimlanes ([WM] A49–A52) ---------------------------------------------------------

    def lane(self, name):
        """Who holds the step: "Customer" for a trigger (whoever starts the case), a human task's lane, "System"
        for every automated step (gateways and events included)."""
        if name in self.triggers:
            return CUSTOMER
        if self.element(name).human:
            return self.nodes[name]["parameters"].get("lane") or PEOPLE
        return SYSTEM

    @cached_property
    def lanes(self):
        """{lane: its nodes}, in swimlane order ([WM] A51): the customer first, the people's lanes in order of
        first appearance, the systems last; sub-nodes (a chat model) with the systems."""
        lanes = {CUSTOMER: []} | {self.lane(n): [] for n in self.order if self.element(n).human} | {SYSTEM: []}
        for name in [*self.order, *self.subnodes]:
            lanes[self.lane(name)].append(name)
        return {lane: names for lane, names in lanes.items() if names}

    def handoffs(self, path):
        """The lanes a path's work passes, in order (its tasks; gateways and events are no work): each change is
        a handoff, where delay, errors and cost arise ([WM] A63–A64)."""
        lanes = [self.lane(n) for n in path if self.element(n).category == "task"]
        return [lane for i, lane in enumerate(lanes) if not i or lane != lanes[i - 1]]

    def _position(self, name):
        x, y = (self.nodes[name].get("position") or [0, 0])[:2]
        return (y, x, name)


def demo():
    """Self-check: python -c 'import bpmn8n.workflows.graph as g; g.demo()'"""
    from .elements import node_type
    wf = {"name": "t", "nodes": [
        {"name": "Start", "type": "n8n-nodes-base.webhook", "parameters": {}},
        {"name": "Do", "type": node_type("serviceTask"), "parameters": {}},
        {"name": "Off", "type": node_type("serviceTask"), "parameters": {}, "disabled": True},
        {"name": "Again?", "type": node_type("exclusiveGateway"),
         "parameters": {"branches": {"values": [{"name": "yes"}]}}},
        {"name": "Note", "type": STICKY, "parameters": {}},
    ], "connections": {
        "Start": {"main": [[{"node": "Do", "type": "main", "index": 0}]]},
        "Do": {"main": [[{"node": "Off", "type": "main", "index": 0}]]},
        "Off": {"main": [[{"node": "Again?", "type": "main", "index": 0}], [{"node": "Do", "type": "main", "index": 0}]]},
        "Again?": {"main": [[{"node": "Do", "type": "main", "index": 0}], []]},
    }}
    g = ProcessGraph(wf)
    assert "Note" not in g.nodes and g.outs["Off"] == [["Again?"]]  # a disabled node keeps output 0 only
    assert g.triggers == ["Start"] and g.after("Start") == {"Start", "Do", "Off", "Again?"}
    assert g.outputs("Again?") == ["yes", "default"] and g.ends() == []  # "default" leads nowhere, "yes" back
    assert ("Again?", "Do") in g.back_edges and g.cycles == [{"Do", "Off", "Again?"}]
    assert g.dominators["Again?"] == {"Start", "Do", "Off", "Again?"}
    assert g.order[0] == "Start" and g.of(trigger=True) == ["Start"]
    assert outputs({"type": node_type("serviceTask"), "onError": ERROR_OUTPUT}) == ["main", "error"]
    assert g.lanes == {CUSTOMER: ["Start"], SYSTEM: ["Do", "Off", "Again?"]} and g.paths() == [["Start", "Do", "Off", "Again?"]]
