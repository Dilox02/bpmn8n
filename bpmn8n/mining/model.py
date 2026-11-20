"""The process as modeled: an n8n workflow of bpmn8n nodes, turned into BPMN (PM4Py's object,
exportable as .bpmn) and into the Petri net PM4Py checks the event log against. The nodes that
write to the event log (the elements that are `logged`) become visible transitions; webhooks, timers,
parallel gateways, Merge, disabled nodes and every other n8n node are silent."""
from collections import Counter
from itertools import combinations

import pm4py
from pm4py.objects.bpmn.obj import BPMN
from pm4py.objects.petri_net.obj import Marking, PetriNet
from pm4py.objects.petri_net.utils.petri_utils import add_arc_from_to

from ..workflows.elements import element
from ..workflows.graph import ProcessGraph

DIVERGING, CONVERGING = BPMN.Gateway.Direction.DIVERGING, BPMN.Gateway.Direction.CONVERGING


def bpmn_xml(bpmn):
    """BPMN 2.0 XML, for bpmn.io, Camunda Modeler and the other BPMN tools."""
    from pm4py.objects.bpmn.exporter.variants.etree import get_xml_string
    return get_xml_string(bpmn).decode()


class WorkflowModel:
    """n8n semantics in BPMN: a node with several outgoing connections runs them all (AND split),
    a node reached from several places runs once per arrival (XOR join) except Merge, which waits
    for its active inputs (OR join); a gateway's outputs are its branches (an inclusive gateway takes
    any non-empty subset) and it may take none (default flow); an event-based gateway that times out
    leaves on output 0 without writing an event. A disabled node is a silent step, an agent's chat
    model and sticky notes are not steps (ProcessGraph)."""

    def __init__(self, workflow, activity=None):
        """`activity(node)`: the activity a node writes to the event log; by default its name,
        which is what bpmn8n's n8n nodes send (X-Bpmn8n-Activity)."""
        self.name = workflow["name"]
        self.activity = activity or (lambda node: node["name"])
        graph = ProcessGraph(workflow)
        self.nodes = {name: graph.nodes[name] for name in graph.flow_nodes}
        self.outputs = {name: graph.outs[name] for name in self.nodes}
        self.bpmn = BPMN()
        self._build()

    def petri_net(self):
        net, initial, final = pm4py.convert_to_petri_net(self.bpmn)
        for transition in net.transitions:
            node = self.nodes.get(transition.label) if transition.label is not None else None
            transition.label = self.activity(node) if node and element(node).logged else None
        # n8n ends a case once every branch has ended (parallel branches need no join): a silent
        # transition drains the end place, so the final marking is empty
        drain = PetriNet.Transition("end", None)
        net.transitions.add(drain)
        for place in final:
            add_arc_from_to(place, drain, net)
        return net, initial, Marking()

    # --- building -------------------------------------------------------------------------

    def _add(self, node):
        self.bpmn.add_node(node)
        return node

    def _flow(self, source, target):
        self.bpmn.add_flow(BPMN.SequenceFlow(source, target))

    def _gateway(self, kind, direction):
        return self._add(kind(gateway_direction=direction))

    def _build(self):
        incoming = Counter(target for outs in self.outputs.values() for out in outs for target in out)
        self.task = {name: self._add(BPMN.Task(name=name)) for name in self.nodes}
        self.entry, self.timeout = {}, {}
        for name, node in self.nodes.items():
            entry = self.task[name]
            if element(node).timeout_first:
                entry = self.timeout[name] = self._gateway(BPMN.ExclusiveGateway, DIVERGING)
                self._flow(entry, self.task[name])
            if incoming[name] > 1:
                join = self._gateway(BPMN.InclusiveGateway if element(node).joins else BPMN.ExclusiveGateway, CONVERGING)
                self._flow(join, entry)
                entry = join
            self.entry[name] = entry
        start, end = self._add(BPMN.NormalStartEvent()), self._add(BPMN.NormalEndEvent())
        self.end = self._gateway(BPMN.ExclusiveGateway, CONVERGING)
        self._flow(self.end, end)
        triggers = [[name] for name in self.nodes if not incoming[name]]
        self._choose(start, triggers, default=False) if len(triggers) > 1 else self._branch(start, triggers[0])
        for name, node in self.nodes.items():
            outs, e = self.outputs[name], element(node)
            if e.timeout_first:
                self._branch(self.timeout[name], outs[0] if outs else [])
                outs = outs[1:]
            if e.chooses:
                # ponytail: an inclusive gateway is expanded into its 2^n - 1 branch subsets (fine up to ~10)
                options = ([[t for out in subset for t in out] for k in range(1, len(outs) + 1)
                            for subset in combinations(outs, k)] if e.kind == "inclusiveGateway" else outs)
                self._choose(self.task[name], options)
            else:
                self._branch(self.task[name], [target for out in outs for target in out])

    def _branch(self, source, targets):
        """source runs all targets (none: the case ends there)."""
        if not targets:
            return self._flow(source, self.end)
        if len(targets) > 1:
            split = self._gateway(BPMN.ParallelGateway, DIVERGING)
            self._flow(source, split)
            source = split
        for target in targets:
            self._flow(source, self.entry[target])

    def _choose(self, source, options, default=True):
        """source takes exactly one of the options (lists of targets), or, by default, none."""
        choice = self._gateway(BPMN.ExclusiveGateway, DIVERGING)
        self._flow(source, choice)
        for targets in options:
            self._branch(choice, targets)
        if default:
            self._flow(choice, self.end)
