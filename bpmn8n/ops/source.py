"""Workflows in git, n8n as the deployment target: the open counterpart of n8n's paid source control and
environments. A project's workflow.json is the source of truth. `diff` shows the drift between it and the
workflow in n8n, `push` deploys it (sub-processes first, never activated), `pull` writes the editor's changes
back into the file so `git diff` shows them (manage.py workflows)."""
import copy
import json
from functools import cached_property
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

from ..clients import n8n
from ..models import Project
from ..workflows.elements import node_type

KEYS = ("name", "nodes", "connections", "settings")  # what a project keeps; n8n adds ids, versions, dates
LAYOUT = ("id", "position", "webhookId")  # a node's identity in one instance and its place: not drift
CALL = node_type("callActivity")
ABSENT = object()  # a key one side does not have


class SourceError(Exception):
    pass


class Folder:
    """A project folder with a workflow.json, and the workflow n8n has for it."""

    def __init__(self, path):
        self.path = Path(path)
        self.file = self.path / "workflow.json"
        if not self.file.is_file():
            raise SourceError(f"{self.path}: no workflow.json")
        self.workflow = json.loads(self.file.read_text())

    def __str__(self):
        return str(self.path)

    @property
    def name(self):
        return self.workflow.get("name", "")

    @cached_property
    def remote(self):
        """By the Studio project's stored id, else by name; None when n8n has none."""
        project = Project.objects.filter(slug=self.path.name).exclude(n8n_id="").first()
        if project and (found := n8n.workflow(project.n8n_id)):
            return found
        named = n8n.workflows(self.name)
        if len(named) > 1:
            raise SourceError(f"{self}: n8n has {len(named)} workflows named {self.name!r} "
                              f"({', '.join(w['id'] for w in named)}): rename or archive all but one")
        return named[0] if named else None

    def remember(self, workflow_id):
        """A Studio project finds its workflow by id from now on."""
        Project.objects.filter(slug=self.path.name).update(n8n_id=workflow_id)


# --- diff ---------------------------------------------------------------------------------

def edges(workflow):
    return sorted(f"{source} [{kind} {i}] → {c['node']} [{c.get('type', kind)} {c.get('index', 0)}]"
                  for source, kinds in (workflow.get("connections") or {}).items()
                  for kind, ports in kinds.items() for i, port in enumerate(ports or []) for c in port or [])


def comparable(workflow):
    """What drift is made of: nodes by name without their ids and positions, credentials by type and name
    (ids differ between instances), connections as edges."""
    nodes = {}
    for node in workflow.get("nodes", []):
        node = {k: v for k, v in node.items() if k not in LAYOUT}
        if "credentials" in node:
            node["credentials"] = {kind: (ref or {}).get("name") for kind, ref in node["credentials"].items()}
        nodes[node["name"]] = node
    return {"workflow": {"name": workflow.get("name"), "settings": workflow.get("settings") or {}},
            "nodes": nodes, "connections": edges(workflow)}


def changes(before, after, path=""):
    """[{"path", "git", "n8n"}] down to the values that differ; a side without the value has no key."""
    if isinstance(before, dict) and isinstance(after, dict):
        return [change for key in sorted(before.keys() | after.keys(), key=str)
                for change in changes(before.get(key, ABSENT), after.get(key, ABSENT), f"{path}.{key}" if path else key)]
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [change for i, (b, a) in enumerate(zip(before, after)) for change in changes(b, a, f"{path}.{i}")]
    if before == after:
        return []
    return [{"path": path, **({"git": before} if before is not ABSENT else {}), **({"n8n": after} if after is not ABSENT else {})}]


def diff(local, remote):
    """git (before) against n8n (after)."""
    a, b = comparable(local), comparable(remote)
    common = sorted(a["nodes"].keys() & b["nodes"].keys())
    return {"workflow": changes(a["workflow"], b["workflow"]),
            "nodes": {"added": sorted(b["nodes"].keys() - a["nodes"].keys()),
                      "removed": sorted(a["nodes"].keys() - b["nodes"].keys()),
                      "changed": {name: found for name in common if (found := changes(a["nodes"][name], b["nodes"][name]))}},
            "connections": {"added": sorted(set(b["connections"]) - set(a["connections"])),
                            "removed": sorted(set(a["connections"]) - set(b["connections"]))}}


def drifted(report):
    return bool(report["workflow"] or any(report["nodes"].values()) or any(report["connections"].values()))


def compare(folder):
    if not (remote := folder.remote):
        return {"project": str(folder), "workflow": None, "status": "missing"}
    report = diff(folder.workflow, remote)
    return {"project": str(folder), "workflow": remote["id"], "status": "drift" if drifted(report) else "same", **report}


# --- push ---------------------------------------------------------------------------------

def target(node):
    found = node.get("parameters", {}).get("workflowId", "")
    return (found.get("value", ""), found.get("cachedResultName", "")) if isinstance(found, dict) else (found, "")


def resolve_credentials(workflow, available):
    """Each node's credentials as this n8n has them: the same id and type, else the one with the same type
    and name (another environment). Raises when one is missing."""
    missing = []
    for node in workflow["nodes"]:
        for kind, ref in (node.get("credentials") or {}).items():
            ref = ref or {}
            same = [c for c in available if c["type"] == kind and c["id"] == ref.get("id")] or \
                [c for c in available if c["type"] == kind and c["name"] == ref.get("name")]
            if len(same) == 1:
                node["credentials"][kind] = {"id": same[0]["id"], "name": same[0]["name"]}
            else:
                missing.append(f"{node['name']!r} needs the {kind} credential {ref.get('name')!r}, which this n8n "
                               f"{'has twice' if same else 'does not have'}")
    return missing


def plan(folders):
    """Folders in deployment order (the sub-processes of Call Activities first) and, per folder, its Call
    Activities whose sub-process is pushed with it: [(node name, sub-process folder)]."""
    calls, problems = {}, []
    for folder in folders:
        calls[folder] = []
        for node in folder.workflow.get("nodes", []):
            if node.get("type") != CALL:
                continue
            value, name = target(node)
            sub = next((f for f in folders if f is not folder and value and f.remote and f.remote["id"] == value), None) or \
                next((f for f in folders if f is not folder and name and f.name == name), None)
            if sub:
                calls[folder].append((node["name"], sub))
            elif not (value and n8n.workflow(value)):
                problems.append(f"{folder}: Call Activity {node['name']!r} calls workflow {name or value!r}, which this "
                                "n8n does not have: push its project together")
    if problems:
        raise SourceError("\n".join(problems))
    try:
        order = list(TopologicalSorter({f: {sub for _, sub in calls[f]} for f in folders}).static_order())
    except CycleError as e:
        raise SourceError(f"Call Activities call each other in a cycle: {', '.join(map(str, e.args[1]))}") from e
    return order, calls


def push(folders):
    """Creates or updates each project's workflow in n8n (never activates it). Everything is checked before
    the first write: credentials exist, every Call Activity's sub-process exists or is pushed first."""
    order, calls = plan(folders)
    available = n8n.credentials()
    payloads, problems = {}, []
    for folder in order:
        payloads[folder] = data = copy.deepcopy({"settings": {}, **{k: folder.workflow[k] for k in KEYS if k in folder.workflow}})
        problems += [f"{folder}: {p}" for p in resolve_credentials(data, available)]
    if problems:
        raise SourceError("\n".join(problems) + "\ncreate the credentials in n8n (./start.sh creates bpmn8n's), then push again")
    pushed, results = {}, []
    for folder in order:
        data = payloads[folder]
        nodes = {node["name"]: node for node in data["nodes"]}
        for name, sub in calls[folder]:  # the sub-process's id in this n8n, which may be new
            parameters = nodes[name]["parameters"]
            reference = parameters["workflowId"]
            parameters["workflowId"] = {**reference, "value": pushed[sub]} if isinstance(reference, dict) else pushed[sub]
        existing = folder.remote["id"] if folder.remote else ""
        saved = n8n.save_workflow(data, existing)
        pushed[folder] = saved["id"]
        folder.remember(saved["id"])
        results.append({"project": str(folder), "workflow": saved["id"],
                        "status": "draft" if saved.get("draft") else "updated" if existing == saved["id"] else "created"})
    return results


# --- pull ---------------------------------------------------------------------------------

def ordered(new, old):
    """`new` with its keys in `old`'s order (new keys after them, sorted) and named items matched by name, so
    a pull changes only the lines of what changed."""
    if isinstance(new, dict):
        old = old if isinstance(old, dict) else {}
        keys = [k for k in old if k in new] + sorted(k for k in new if k not in old)
        return {k: ordered(new[k], old.get(k)) for k in keys}
    if isinstance(new, list):
        old = old if isinstance(old, list) else []
        if all(isinstance(item, dict) and "name" in item for item in new):
            named = {item.get("name"): item for item in old if isinstance(item, dict)}
            return [ordered(item, named.get(item["name"])) for item in new]
        return [ordered(item, old[i] if i < len(old) else None) for i, item in enumerate(new)]
    return new


def pull(folder):
    """n8n's version into workflow.json (pretty JSON, the file's key order); the file is left alone when
    nothing changed."""
    if not (remote := folder.remote):
        raise SourceError(f"{folder}: n8n has no workflow named {folder.name!r}: push it first")
    new = ordered({k: remote[k] for k in KEYS if k in remote}, folder.workflow)
    status = "same" if new == folder.workflow else "updated"
    if status == "updated":
        folder.file.write_text(json.dumps(new, indent=2, ensure_ascii=False) + "\n")
        folder.workflow = new
    return {"project": str(folder), "workflow": remote["id"], "status": status}
