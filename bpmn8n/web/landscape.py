"""The process landscape, /landscape/: every workflow bpmn8n knows (its projects, n8n's workflows when its API answers,
the processes of the event log) as a node; an edge where one calls another (a Call Activity) or sends a message
another waits for (from their contracts, workflows/contract.py; and the subscriptions seen, with their process).
A derived conversation diagram ([AW] §11–12), not an executable one. Findings: a message some process waits for
that no known process sends (it must come from outside), a sub-process that is not published or not known, a
workflow nothing calls and nothing starts. Drawn on the server as SVG in layers, with a table for screen readers."""
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cached_property

from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Max, Q
from django.shortcuts import render
from django.utils.html import format_html, format_html_join
from django.utils.text import slugify

from ..clients import n8n
from ..clients.http import ServiceUnavailable
from ..models import Event, MessageSubscription
from ..workflows.contract import Contract, local_workflows

OUTSIDE = "Outside"  # where a message nobody here sends comes from: another system, through /api/messages/<name>
WIDTH, HEIGHT, PITCH_X, PITCH_Y, MARGIN = 216, 52, 380, 76, 16  # a workflow's box and the grid it sits on (px)
NAME_CHARS = 28


@dataclass
class Node:
    """A workflow of the landscape, with what bpmn8n knows of it."""
    name: str
    contract: Contract | None = None  # None: only its name is known (the event log, a subscription)
    project: str = ""  # its key under projects/
    workflow_id: str = ""  # its n8n id
    active: bool | None = None  # published in n8n; None: unknown
    cases: int = 0  # cases in the event log
    missing: bool = False  # a sub-process some workflow calls that neither n8n nor a project has

    @property
    def anchor(self):
        return "w-" + (slugify(self.name) or "workflow")

    @property
    def starts(self):
        return [s["kind"] for s in self.contract.starts] if self.contract else []


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    kind: str  # call | message
    label: str  # the Call Activity, or the message's name
    unsent: bool = False  # a message no known process sends: it comes from outside


@dataclass(frozen=True)
class Finding:
    level: str  # warning | info
    kind: str  # unsent | unpublished | unknown | orphan
    subject: str
    text: str


def listing(names):
    names = sorted(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


@dataclass
class Landscape:
    """nodes: the workflows; subscriptions: [(message, waiting workflow's name, seen, delivered)]."""
    nodes: list
    subscriptions: list = field(default_factory=list)

    def __post_init__(self):
        self.by_name = {n.name: n for n in self.nodes}
        self.waiting, self.sending, self.delivered = defaultdict(set), defaultdict(set), defaultdict(int)
        for node in self.nodes:
            for wait in node.contract.receives if node.contract else ():
                self.waiting[wait["message"]].add(node.name)
            for send in node.contract.sends if node.contract else ():
                if send["message"]:
                    self.sending[send["message"]].add(node.name)
        for message, process, _seen, delivered in self.subscriptions:
            if process:
                self.waiting[message].add(process)
                self.add(Node(process))
            self.delivered[message] += delivered

    def add(self, node):
        if node.name not in self.by_name:
            self.by_name[node.name] = node
            self.nodes.append(node)
        return self.by_name[node.name]

    @cached_property
    def edges(self):
        ids = {n.workflow_id: n.name for n in self.nodes if n.workflow_id}
        edges = []
        for node in list(self.nodes):
            for call in node.contract.calls if node.contract else ():
                name = ids.get(call["workflow_id"]) or (call["name"] if call["name"] in self.by_name else "")
                target = self.by_name[name] if name else self.add(Node(
                    call["name"] or f"Workflow {call['workflow_id'] or '?'}", workflow_id=call["workflow_id"], missing=True))
                edges.append(Edge(node.name, target.name, "call", call["activity"]))
        for message, waiters in sorted(self.waiting.items()):
            senders = self.sending.get(message)
            if not senders:
                self.add(Node(OUTSIDE))
            for source in sorted(senders or [OUTSIDE]):
                edges += [Edge(source, waiter, "message", message, unsent=not senders) for waiter in sorted(waiters)]
        return edges

    @cached_property
    def findings(self):
        found = []
        for message, waiters in sorted(self.waiting.items()):
            if not self.sending.get(message):
                times = self.delivered[message]
                found.append(Finding("warning", "unsent", message, (
                    f"“{message}”: {listing(waiters)} {'waits' if len(waiters) == 1 else 'wait'} for it and no known "
                    f"process sends it, so it must come from outside, through POST /api/messages/{message}") +
                    (f"; delivered {times} time{'s' if times != 1 else ''} so far" if times else "")))
        for edge in self.edges:
            target = self.by_name[edge.target]
            if edge.kind == "call" and target.missing:
                found.append(Finding("warning", "unknown", target.name,
                                     f"{edge.source} calls {target.name} ({edge.label}), which neither n8n nor a project has"))
            elif edge.kind == "call" and target.active is False:
                found.append(Finding("warning", "unpublished", target.name, f"{edge.source} calls {target.name} "
                                     f"({edge.label}), which is not published: n8n refuses to run it until it is"))
        called = {e.target for e in self.edges if e.kind == "call"}
        for node in self.nodes:
            if node.contract and node.name not in called and set(node.starts) <= {"call"}:
                found.append(Finding("info", "orphan", node.name, f"Nothing calls {node.name} and nothing starts it" +
                                     ("" if node.starts else ": it has no trigger")))
        return found

    # --- the drawing ------------------------------------------------------------------------

    @cached_property
    def layers(self):
        """[[names]]: a workflow one layer right of every one that calls it or messages it (longest path, a loop
        broken where the walk meets it), ordered by where those sit; workflows with no edge in a last group."""
        linked = {e.source for e in self.edges} | {e.target for e in self.edges}
        names = sorted(linked, key=lambda n: (n != OUTSIDE, n.lower()))
        after = {n: sorted({e.target for e in self.edges if e.source == n and e.target != n}, key=str.lower) for n in names}
        state, order, back = {}, [], set()

        def visit(name):  # ponytail: recursive; a landscape is tens of workflows, not thousands
            state[name] = "open"
            for n in after[name]:
                if state.get(n) == "open":
                    back.add((name, n))
                elif n not in state:
                    visit(n)
            state[name] = "done"
            order.append(name)

        for name in names:
            if name not in state:
                visit(name)
        depth = {}
        for name in reversed(order):
            depth.setdefault(name, 0)
            for n in after[name]:
                if (name, n) not in back:
                    depth[n] = max(depth.get(n, 0), depth[name] + 1)
        layers = [[] for _ in range(max(depth.values(), default=-1) + 1)]
        row = {}
        for name in sorted(names, key=lambda n: depth[n]):
            before = [row[e.source] for e in self.edges if e.target == name and e.source in row]
            layers[depth[name]].append((sum(before) / len(before) if before else len(layers[depth[name]]), name))
            layers[depth[name]].sort()
            row.update({n: i for i, (_, n) in enumerate(layers[depth[name]])})
        return [[n for _, n in layer] for layer in layers]

    @cached_property
    def alone(self):
        linked = {e.source for e in self.edges} | {e.target for e in self.edges}
        return sorted((n.name for n in self.nodes if n.name not in linked), key=str.lower)

    @cached_property
    def positions(self):
        """name -> (x, y) of its box's top left."""
        spots = {name: (MARGIN + i * PITCH_X, MARGIN + j * PITCH_Y) for i, layer in enumerate(self.layers)
                 for j, name in enumerate(layer)}
        top = MARGIN + max((len(layer) for layer in self.layers), default=0) * PITCH_Y + (PITCH_Y // 2 if self.layers else 0)
        columns = max(len(self.layers), 3)
        spots |= {name: (MARGIN + (k % columns) * PITCH_X, top + (k // columns) * PITCH_Y) for k, name in enumerate(self.alone)}
        return spots

    def svg(self):
        if not self.nodes:
            return ""
        at = self.positions
        width = max(x for x, _ in at.values()) + WIDTH + MARGIN
        height = max(y for _, y in at.values()) + HEIGHT + MARGIN
        flagged = {f.subject for f in self.findings if f.level == "warning"} | {e.target for e in self.edges if e.unsent}
        return format_html(
            '<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="group" aria-label="{label}">'
            '<defs><marker id="b8-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
            'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker></defs>'
            '<g class="edges">{edges}</g><g class="nodes">{nodes}</g></svg>',
            w=width, h=height, label=f"{len(self.nodes)} workflows, {len(self.edges)} links; the table below lists them",
            edges=format_html_join("", "{}", ((self.edge_svg(e, at),) for e in self.drawn_edges())),
            nodes=format_html_join("", "{}", ((self.node_svg(self.by_name[n], *at[n], n in flagged),) for n in at)))

    def drawn_edges(self):
        """The edges as drawn: one line per pair of workflows and kind, its labels joined."""
        lines = {}
        for e in self.edges:
            key = (e.source, e.target, e.kind, e.unsent)
            lines[key] = Edge(*key[:3], f"{lines[key].label}, {e.label}", e.unsent) if key in lines else e
        return list(lines.values())

    @staticmethod
    def edge_svg(edge, at):
        (x1, y1), (x2, y2) = at[edge.source], at[edge.target]
        y1, y2 = y1 + HEIGHT / 2, y2 + HEIGHT / 2
        if x2 > x1:  # forward: out of the right side, into the left
            x1 += WIDTH
            # the label sits over the arrow into the target: lines fanning out of one workflow keep theirs apart
            path, label = f"M{x1},{y1} C{x1 + 60},{y1} {x2 - 60},{y2} {x2},{y2}", (x2 - 10, y2 - 7, "end")
        else:  # back, or within a layer: out of the bottom, around below, into the bottom
            x1, x2, bend = x1 + WIDTH / 2, x2 + WIDTH / 2, max(y1, y2) + HEIGHT
            path = f"M{x1},{y1 + HEIGHT / 2} C{x1},{bend} {x2},{bend} {x2},{y2 + HEIGHT / 2}"
            label = ((x1 + x2) / 2, bend - 4, "middle")
        kind = f"edge {edge.kind}{' unsent' if edge.unsent else ''}"
        what = f"{edge.source} calls {edge.target} ({edge.label})" if edge.kind == "call" else \
            f"{edge.source} → {edge.target}: message “{edge.label}”"
        return format_html('<g class="{}" data-source="{}" data-target="{}"><title>{}</title>'
                           '<path d="{}" marker-end="url(#b8-arrow)"/><text x="{}" y="{}" text-anchor="{}">{}</text></g>',
                           kind, edge.source, edge.target, what, path, *label, edge.label if edge.kind == "message" else "calls")

    @staticmethod
    def node_svg(node, x, y, flagged):
        meta = " · ".join(filter(None, (", ".join(node.starts) if node.contract else "only seen in the log" if node.cases else "",
                                        f"{node.cases} case{'s' if node.cases != 1 else ''}" if node.cases else "")))
        if node.name == OUTSIDE:
            meta = "other systems, POST /api/messages"
        elif node.missing:
            meta = "not in n8n nor in projects/"
        kind = " ".join(filter(None, ("node", "outside" if node.name == OUTSIDE else "", "missing" if node.missing else "",
                                      "warn" if flagged and node.name != OUTSIDE else "")))
        name = node.name if len(node.name) <= NAME_CHARS else node.name[:NAME_CHARS - 1] + "…"
        return format_html('<a class="{}" href="#{}" data-id="{}"><title>{}</title><rect x="{}" y="{}" width="{}" height="{}" rx="6"/>'
                           '<text class="name" x="{}" y="{}">{}</text><text class="meta" x="{}" y="{}">{}</text></a>',
                           kind, node.anchor, node.name, node.name, x, y, WIDTH, HEIGHT, x + 12, y + 22, name, x + 12, y + 40, meta)

    def rows(self):
        """The table: one row per workflow, with what links it."""
        out = []
        for node in sorted(self.nodes, key=lambda n: (n.name == OUTSIDE, n.name.lower())):
            edges_in = [e for e in self.edges if e.target == node.name]
            edges_out = [e for e in self.edges if e.source == node.name]
            out.append({"node": node, "starts": node.starts,
                        "waits": sorted({e.label for e in edges_in if e.kind == "message"}),
                        "unsent": sorted({e.label for e in edges_in if e.unsent}),
                        "sends": sorted({e.label for e in edges_out if e.kind == "message"}),
                        "calls": sorted({e.target for e in edges_out if e.kind == "call"}),
                        "called_by": sorted({e.source for e in edges_in if e.kind == "call"})})
        return out


def known_workflows():
    """Every workflow bpmn8n knows, one Node per name: the projects, then n8n's workflows (their contract read from
    what runs, the project's declared start data kept), then the processes of the event log. ponytail: one name is
    one workflow; a project published under another name shows twice (upgrade: match Project.n8n_id)."""
    found = {}
    for key, (workflow, declared) in local_workflows(django_settings.BASE_DIR).items():
        name = workflow.get("name") or key
        found[name] = Node(name, Contract(workflow, declared, key), project=key)
    try:
        listed = n8n.pages("/workflows")
    except ServiceUnavailable:  # no API key, or n8n down: what bpmn8n has on its own
        listed = []
    for workflow in listed:
        if workflow.get("isArchived"):
            continue
        node = found.setdefault(workflow.get("name") or str(workflow["id"]), Node(workflow.get("name") or str(workflow["id"])))
        node.workflow_id, node.active = str(workflow["id"]), workflow.get("active")
        if isinstance(workflow.get("nodes"), list):
            node.contract = Contract(workflow, node.contract.declared if node.contract else None, node.workflow_id)
    for row in Event.objects.exclude(process="").exclude(case_id="").values("process").annotate(
            cases=Count("case_id", distinct=True), last=Max("case_id")):
        node = found.setdefault(row["process"], Node(row["process"]))
        node.cases, node.workflow_id = row["cases"], node.workflow_id or row["last"].rpartition(":")[0]
    return list(found.values())


def subscriptions():
    return [(r["name"], r["process"], r["seen"], r["delivered"]) for r in MessageSubscription.objects.values("name", "process")
            .annotate(seen=Count("id"), delivered=Count("id", filter=Q(status=MessageSubscription.COMPLETED)))]


@login_required
def landscape(request):
    view = Landscape(known_workflows(), subscriptions())
    edges = view.edges  # adds the outside and the unknown sub-processes before the page counts the workflows
    return render(request, "bpmn8n/landscape.html", {
        "map": view.svg(), "rows": view.rows(), "findings": view.findings, "outside": OUTSIDE,
        "workflows": sum(n.name != OUTSIDE for n in view.nodes), "calls": sum(e.kind == "call" for e in edges),
        "message_links": sum(e.kind == "message" for e in edges)})  # not "messages": the base template shows those
