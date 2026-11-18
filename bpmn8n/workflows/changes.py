"""What changed between two versions of an n8n workflow, as a list a person reads: steps added, removed and
renamed, settings of a step changed (before and after), flows added and removed. Steps are matched by
n8n's node id, so a renamed step is a rename, not a removal plus an addition; a move on the canvas is not a
change. Pure Python, no Django."""
import hashlib
import json

from .elements import element

LAYOUT = ("id", "name", "position", "webhookId")  # what identifies or places a step, not what it does
LONG = 200  # characters of a value shown in a change


def content_hash(workflow):
    """sha256 of the workflow's nodes and connections, keys sorted."""
    content = {"nodes": workflow.get("nodes", []), "connections": workflow.get("connections", {})}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def changes(before, after):
    """[{"change", "node"?, "text", ...}] turning `before` into `after`. change is one of name,
    node_added, node_removed, node_renamed, parameter (with path, before, after), connection_added,
    connection_removed (with source, output, target)."""
    found = []
    if before.get("name") != after.get("name"):
        found.append({"change": "name", "before": before.get("name"), "after": after.get("name"),
                      "text": f"Workflow renamed from “{before.get('name')}” to “{after.get('name')}”"})
    old, new = _nodes(before), _nodes(after)
    for key in old.keys() - new.keys():
        found.append({"change": "node_removed", "node": old[key]["name"], "text": f"Removed “{old[key]['name']}”"})
    for key in sorted(new.keys() - old.keys(), key=lambda k: new[k]["name"]):
        node = new[key]
        found.append({"change": "node_added", "node": node["name"],
                      "text": f"Added “{node['name']}” ({element(node).label.lower()})"})
    for key in sorted(old.keys() & new.keys(), key=lambda k: new[k]["name"]):
        a, b = old[key], new[key]
        if a["name"] != b["name"]:
            found.append({"change": "node_renamed", "node": b["name"], "before": a["name"], "after": b["name"],
                          "text": f"Renamed “{a['name']}” to “{b['name']}”"})
        settings_a, settings_b = _flat({k: v for k, v in a.items() if k not in LAYOUT}), _flat({k: v for k, v in b.items() if k not in LAYOUT})
        for path in sorted(settings_a.keys() | settings_b.keys()):
            was, now = settings_a.get(path), settings_b.get(path)
            if was != now:
                found.append({"change": "parameter", "node": b["name"], "path": path, "before": _short(was),
                              "after": _short(now), "text": f"“{b['name']}”: {path} {_short(was)} → {_short(now)}"})
    old_flows, new_flows = _flows(before, old), _flows(after, new)
    names = {**{k: n["name"] for k, n in old.items()}, **{k: n["name"] for k, n in new.items()}}
    nodes = {**old, **new}
    for kind, flows in (("connection_removed", old_flows - new_flows), ("connection_added", new_flows - old_flows)):
        for source, port, index, target in sorted(flows, key=lambda f: (names[f[0]], f[2], names[f[3]])):
            output = _output(nodes[source], port, index)
            found.append({"change": kind, "source": names[source], "output": output, "target": names[target],
                          "text": f"{'Removed' if kind == 'connection_removed' else 'Added'} the flow "
                                  f"“{names[source]}”{f' ({output})' if output else ''} → “{names[target]}”"})
    return found


def _nodes(workflow):
    """{node id (its name when it has none): node}"""
    return {n.get("id") or n["name"]: n for n in workflow.get("nodes", [])}


def _flows(workflow, nodes):
    """{(source id, connection type, output index, target id)}, from n8n's connections keyed by node name."""
    by_name = {n["name"]: key for key, n in nodes.items()}
    flows = set()
    for source, kinds in (workflow.get("connections") or {}).items():
        for port, outputs in (kinds or {}).items():
            for index, targets in enumerate(outputs or []):
                for target in targets or []:
                    if source in by_name and target.get("node") in by_name:
                        flows.add((by_name[source], port, index, by_name[target["node"]]))
    return flows


def _output(node, port, index):
    """The name of a node's output: a gateway's branch ("default", "timeout"), "" for a plain one."""
    if port != "main":
        return port
    outputs = element(node).outputs(node.get("parameters"))
    return outputs[index] if outputs != ["main"] and index < len(outputs) else ("" if index == 0 else f"output {index + 1}")


def _flat(value, path=""):
    """{"parameters.branches.values.0.name": "approved", ...}: the leaves of a node's settings."""
    if isinstance(value, dict) and value:
        return {k: v for key, item in value.items() for k, v in _flat(item, f"{path}.{key}" if path else key).items()}
    if isinstance(value, list) and value:
        return {k: v for i, item in enumerate(value) for k, v in _flat(item, f"{path}.{i}").items()}
    return {path: value}


def _short(value):
    text = "—" if value is None else json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= LONG else text[:LONG] + f"… ({len(text)} characters)"
