"""One case's page, /cases/<workflow id>:<execution id>/: where it is and how long it has taken, every step
with what it read and produced, what was decided and on what, who and what it waits for, its variables, and
its workflow drawn by the n8n preview with the steps passed, the ones it is at and the ones not reached."""
import json
from collections import deque
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from ..api.executions import preview
from ..clients import n8n
from ..conf import settings
from ..models import AuditEntry, Case, Event, MessageSubscription, UserTask
from ..workflows.elements import SET, element
from ..workflows.graph import ProcessGraph

# mining/log.py's SETTLE (not imported: that module loads PM4Py): nothing open and no new event for this long,
# the case has ended
SETTLE = timedelta(minutes=1)
AUDIT = 50  # the newest audit entries the page shows (GET /api/audit?case_id= has them all)
STEPS = 300  # ponytail: a case's first 300 steps; a longer one says how many more (upgrade: page the timeline)
JSON_CHARS = 6000  # what a step's input or output shows, at most


def case_status(events, tasks, messages, last, now=None):
    """What the case is doing, from its events and waits (a status of bpmn8n/web/vocab.py): waiting for a person
    or a message, running, failed (its last step failed) or completed (nothing open, quiet for SETTLE)."""
    now = now or timezone.now()
    if any(t.status in UserTask.WAITING for t in tasks):
        return "waiting_person"
    if any(m.status in MessageSubscription.WAITING and (m.expires is None or m.expires > now) for m in messages):
        return "waiting_message"
    if any(e.status == "running" for e in events):
        return "running"
    if events and events[-1].status == "error":
        return "failed"
    return "completed" if last and last < now - SETTLE else "running"


def node_states(graph, done, active):
    """Every node of the workflow: "active" (the case is there), "done" (it passed) or "waiting" (not reached).
    A node that writes no event (a trigger, a parallel gateway, a timer, a join) has passed when it lies between
    passed steps. ponytail: such a node on a branch not taken that rejoins before a passed step shows as passed
    too; exact once the TokenEngine (PLAN A3) replays the case."""
    reached = done | active

    def silent(name):  # passing it leaves no event
        return name not in reached and not graph.element(name).logged

    def finds(start, step, goal):
        seen, queue = {start}, deque([start])
        while queue:
            for n in step(queue.popleft()) - seen:
                if goal(n):
                    return True
                if silent(n):
                    seen.add(n)
                    queue.append(n)
        return False

    states = {}
    for name in graph.flow_nodes:
        if name in active or name in done:
            states[name] = "active" if name in active else "done"
        elif silent(name) and finds(name, graph.successors, reached.__contains__) and (
                name in graph.starts or finds(name, lambda n: graph.into[n], lambda n: n in done or n in graph.starts)):
            states[name] = "done"
        else:
            states[name] = "waiting"
    for sub, parent in graph.parent.items():  # an agent's chat model shares its agent's state
        states[sub] = states.get(parent, "waiting")
    return states


def llm_outputs(graph):
    """The variables an LLM agent's answer becomes: what the Set node after the agent assigns (builder.llm_agent)."""
    names = set()
    for agent in graph.of(kind="llmAgent"):
        for after in graph.successors(agent):
            if graph.nodes[after]["type"] == SET:
                names |= {a.get("name") for a in graph.nodes[after].get("parameters", {}).get("assignments", {})
                          .get("assignments", [])}
    return names - {None, ""}


def as_json(value):
    text = json.dumps(preview(value), indent=2, ensure_ascii=False, default=str)
    return text if len(text) <= JSON_CHARS else text[:JSON_CHARS] + "\n…"


def tree(variables, llm=frozenset()):
    """The variables as a tree to fold (bounded like a step's data); the top-level ones an LLM wrote are marked."""
    def node(key, value, top=False):
        item = {"key": key, "llm": top and key in llm}
        if isinstance(value, dict):
            return {**item, "children": [node(k, v) for k, v in value.items()], "size": f"{len(value)} keys"}
        if isinstance(value, list):
            return {**item, "children": [node(str(i), v) for i, v in enumerate(value)], "size": f"{len(value)} items"}
        return {**item, "value": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)}
    return [node(k, v, top=True) for k, v in preview(variables or {}).items()]


def decision(event):
    """A gateway's decision from its event (api/gateways.py `decided`): the question, every option with whether
    it was taken and its score, and who decided (Laya's model, or conditions)."""
    seen, out = event.data.get("input") or {}, event.data.get("output") or {}
    taken, scores = out.get("branches") or [], out.get("scores") or {}
    options = seen.get("options") or {}
    return {"event": event, "decider": event.data["decider"], "question": seen.get("question", ""),
            "conditions": event.data["decider"] == "conditions",
            "options": [{"name": name, "text": text if isinstance(text, str) else json.dumps(text),
                         "taken": name in taken, "score": scores.get(name)} for name, text in options.items()],
            "default": not taken}


def step(event, graph, llm):
    """A step of the timeline: its event, its bounded input and output, what an LLM was sent or wrote."""
    data = event.data if isinstance(event.data, dict) else {}
    given = data.get("input")
    return {"event": event, "input": "" if given is None else as_json(given),
            "output": "" if data.get("output") is None else as_json(data["output"]),
            "prompt": event.endpoint == "compose_prompt",  # the prompt an LLM agent was sent (builder.llm_agent)
            "by_llm": sorted(llm & set(given)) if isinstance(given, dict) else [],  # what it reads that an LLM wrote
            "kind": element(graph.nodes[event.activity]).label if graph and event.activity in graph.nodes else ""}


def workflow_of(workflow_id):
    """The workflow from n8n (None when n8n, its API key or the workflow is missing), without credentials."""
    found = n8n.workflow(workflow_id)
    if not found or not isinstance(found.get("nodes"), list):
        return None
    return {"name": found.get("name", ""), "connections": found.get("connections", {}), "settings": found.get("settings", {}),
            "nodes": [{k: v for k, v in node.items() if k != "credentials"} for node in found["nodes"]]}


@login_required
def case(request, case_id):
    """Staff see every case; a person, the cases they have (or had) a task of in their lanes."""
    if not request.user.is_staff and not UserTask.objects.for_user(request.user).filter(case_id=case_id).exists():
        raise Http404(case_id)
    record = Case.objects.filter(case_id=case_id).first()
    every = Event.objects.filter(case_id=case_id).order_by("started", "pk")
    events, total = list(every[:STEPS]), every.count()
    tasks = list(UserTask.objects.filter(case_id=case_id).select_related("assignee").order_by("created"))
    messages = list(MessageSubscription.objects.filter(case_id=case_id).order_by("created"))
    if not (record or events or tasks or messages):
        raise Http404(case_id)

    workflow_id, _, execution_id = case_id.rpartition(":")
    workflow = workflow_of(workflow_id) if workflow_id else None
    graph = ProcessGraph(workflow) if workflow else None
    llm = llm_outputs(graph) if graph else set()
    waits = [*tasks, *messages]
    moments = [e.ended for e in events] + [w.completed or w.created for w in waits] + ([record.updated] if record else [])
    started = min([e.started for e in events] + [w.created for w in waits] + ([record.created] if record else []))
    last = max(moments)
    status = case_status(events, tasks, messages, last)
    open_at = {e.activity for e in events if e.status == "running"} | {w.activity for w in waits if w.status in w.WAITING}
    states = node_states(graph, {e.activity for e in events if e.status != "running"} - open_at, open_at) if graph else {}

    return render(request, "bpmn8n/case.html", {
        "case_id": case_id, "execution_id": execution_id, "status": status, "started": started, "last": last,
        "duration_ms": (last - started).total_seconds() * 1000,
        "process": (record and record.process) or next((e.process for e in events if e.process), "") or
                   (workflow or {}).get("name") or next((w.process for w in waits if w.process), ""),
        "steps": [step(e, graph, llm) for e in events], "more_steps": total - len(events),
        "decisions": [decision(e) for e in events if isinstance(e.data, dict) and e.data.get("decider")],
        "tasks": tasks, "subscriptions": messages,  # not "messages": the base template shows those
        "audit": AuditEntry.objects.filter(case_id=case_id)[:AUDIT] if request.user.is_staff else [],
        "variables": tree(record.variables if record else {}, llm), "llm": sorted(llm),
        "workflow": workflow, "states": states, "preview_url": settings.N8N_PREVIEW_URL,
        "n8n_url": n8n.editor_url(f"workflow/{workflow_id}/executions/{execution_id}") if workflow_id else "",
    })
