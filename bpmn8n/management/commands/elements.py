"""Prints the BPMN elements a workflow can contain (bpmn8n/workflows/elements.py), as JSON: what
/api/elements serves. --builder-params rewrites n8n-nodes-bpmn8n/test/builder-params.json, the parameters
the Python builder writes on each bpmn8n node, which the TypeScript nodes must declare (drift test)."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand

from bpmn8n.workflows import Workflow
from bpmn8n.workflows.elements import NODE, TASK_KINDS, manifest

FIXTURE = Path(__file__).resolve().parents[3] / "n8n-nodes-bpmn8n" / "test" / "builder-params.json"

# One call per builder step, and one more per optional argument that writes other parameters. A new step,
# or a new parameter, needs its sample here (a test says so), then `manage.py elements --builder-params`.
SAMPLES = {
    "start": [{"path": "p"}],
    "subprocess_start": [{}],
    "task": [*({"kind": kind, "endpoint": "t"} for kind in TASK_KINDS),
             {"kind": "serviceTask", "endpoint": "t", "text": "{{message}}"},
             {"kind": "serviceTask", "endpoint": "t", "on_error": "branch", "retry": 2}],
    "gateway": [{"kind": kind, "question": "q", "branches": {"a": "b"}} for kind in ("exclusive", "inclusive")],
    "condition_gateway": [{"kind": kind, "conditions": {"a": "true"}, "decision_scope": "technical"}
                          for kind in ("exclusive", "inclusive")],
    "parallel_gateway": [{"branches": ["a", "b"]}],
    "receive_task": [{"message": "m", "correlation_key": "{{k}}"}],
    "event_gateway": [{"messages": ["m"], "correlation_key": "{{k}}", "timeout": (1, "days")}],
    "error_end": [{"code": "E", "message": "m"}],
    "call_activity": [{"workflow_id": "1"}],
    "timer": [{"amount": 1, "unit": "days"}, {"until": "{{d}}"}],
    "manual_task": [{"title": "t", "lane": "l", "context": {"A": "{{a}}"}},
                    {"title": "t", "lane": "l", "due_at": "{{d}}", "priority": "high"}],
    "user_task": [{"title": "t", "lane": "l", "form": [["a", "A", "string"]], "description": "d"},
                  {"title": "t", "lane": "l", "form": [{"name": "c", "type": "choice", "options": ["x"]}],
                   "due_in": (2, "days"), "deadline": True, "priority": "urgent", "assignee": "{{a}}",
                   "not_same_as": ["s"], "completion": 2},
                  {"title": "t", "lane": "l", "form": [], "due_at": "{{d}}", "completion": "any"}],
    "merge": [{}],
    "llm_agent": [{"prompt": "p {{a}}", "output": "o", "instructions": "i"},
                  {"prompt": "p", "output": "o", "oversight": "audit", "risk": "low"}],
    "metrics": [{"project": "p", "metrics": [{"name": "m", "value": 1, "threshold": 1}], "label": "l"}],
    "global_variable": [{"key": "k"}, {"key": "k", "operation": "set", "value": {"a": 1}, "ttl": 60},
                        {"key": "k", "operation": "increment", "by": 2}],
    "guardrail": [{"text": "{{t}}", "checks": {"pii": True, "length": 9, "blocklist": ["w"], "schema": ["k"]},
                   "on_block": "l"}],
    "prompt_chain": [{"prompts": ["p {{a}}", "q {{previous}}"], "output": "o"}],
    "parallel_llm": [{"prompts": {"a": "p", "b": "q"}, "output": "o"}],
    "reflection": [{"prompt": "p", "critique": "c", "output": "o"}],
    "guardrailed_agent": [{"prompt": "p {{a}}", "output": "o", "on_block": "l"}],
}


def builder_params():
    """n8n type of each bpmn8n node -> the top-level parameters the builder writes on it."""
    workflow, params = Workflow("Every step"), {}
    for step, calls in SAMPLES.items():
        for i, kwargs in enumerate(calls):
            getattr(workflow, step)(f"{step} {i}", **kwargs)
    for node in workflow.nodes:
        if node["type"].startswith(NODE):
            params.setdefault(node["type"], set()).update(node["parameters"])
    return {t: sorted(keys) for t, keys in sorted(params.items())}


class Command(BaseCommand):
    help = __doc__
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--builder-params", dest="write", action="store_true", help=f"rewrite {FIXTURE.name}")

    def handle(self, write, **options):
        if write:
            FIXTURE.write_text(json.dumps(builder_params(), indent=2) + "\n")
            self.stdout.write(f"wrote {FIXTURE}")
        else:
            self.stdout.write(json.dumps(manifest(), indent=2))
