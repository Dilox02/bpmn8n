"""bpmn8n as an MCP server (stdio): an MCP client (Claude Code, Claude Desktop...) reads and drives the
state: Studio's projects, where the client itself is the LLM (no provider API key needed), and the
whole REST API (cases, user tasks, messages, event log, mining, workflow checks).

    .mcp.json (Claude Code, from this folder): docker compose exec -T web python manage.py mcp

The client does what Studio's LLM would: the interview (in its own chat), the design, the code;
bpmn8n keeps doing the rest (checks, examples, publishing, import into n8n), as in the web build.
"""
import json
import sys

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.test import Client

from bpmn8n.api import discover
from bpmn8n.conf import settings
from bpmn8n.models import Project
from bpmn8n.studio.build import ProjectBuilder, Skip, defines
from bpmn8n.studio.prompts import Prompts

S, OBJ = {"type": "string"}, {"type": "object"}


def schema(required=(), **props):
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = {
    "api": ("Calls bpmn8n's REST API (the routes of /api/openapi.json) and returns its JSON. Useful routes: "
            "GET /api/usertasks?status=open, POST /api/usertasks/{id}/complete {result}, GET /api/cases/{case_id}, "
            "GET /api/eventlog.csv?since=..., GET /api/metrics, GET /api/processmining?process=..., "
            "GET /api/messages/subscriptions, POST /api/messages/{name} {...}, "
            "POST /api/workflows/check {n8n workflow json}, POST /api/tasks/{name} {input}, GET /api/openapi.json.",
            schema(("method", "path"), method={"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]}, path=S, body=OBJ)),
    "studio_projects": ("Studio's projects: slug, name, status (interview, research, building, ready, failed).",
                        schema()),
    "studio_project": ("One Studio project's whole state: interview messages, research, brief, spec, the cards "
                       "of its last build, its n8n workflow.", schema(("slug",), slug=S)),
    "studio_create": ("A new Studio project from its name and description (the interview's first answer). "
                      "Then: interview the person yourself, studio_save the brief, get studio_prompt 'design'.",
                      schema(("name", "description"), name=S, description=S)),
    "studio_prompt": ("The instructions Studio gives its LLM for a step, with the live context (existing tasks, "
                      "lanes, the builder's steps, code conventions, the project so far). Follow them: "
                      "'interview' (questions to ask, the brief to write), 'design' (the spec's JSON shape), "
                      "'write' (one activity's module: pass activity).",
                      schema(("slug", "step"), slug=S, step={"enum": ["interview", "design", "write"]}, activity=S)),
    "studio_save": ("Saves the interview (messages: [{role: user|assistant, content}], the whole transcript), the "
                    "brief and/or the spec (the design JSON, as studio_prompt 'design' describes it); a spec the "
                    "build cannot use is refused with what is wrong.",
                    schema(("slug",), slug=S, messages={"type": "array", "items": OBJ}, brief=S, spec=OBJ)),
    "studio_write": ("Writes one activity's module (the whole Python file) into the project's draft.",
                     schema(("slug", "activity", "code"), slug=S, activity=S, code=S)),
    "studio_check": ("Imports the draft's modules, runs each activity's examples and checks the workflow: "
                     "{imports, examples, problems}. Fix with studio_write / studio_save until it is clean.",
                     schema(("slug",), slug=S)),
    "studio_publish": ("Once studio_check is clean: publishes projects/<slug>, imports the workflow into n8n and "
                       "reloads the server's tasks. The project becomes ready.", schema(("slug",), slug=S)),
}


class ToolError(Exception):
    pass


def project(slug):
    try:
        p = Project.objects.get(slug=slug).settle()
    except Project.DoesNotExist:
        raise ToolError(f"no project {slug!r}: see studio_projects") from None
    return p


def idle(slug):
    """A project no web job (research, build) is writing meanwhile."""
    if (p := project(slug)).busy:
        raise ToolError(f"{slug} is {p.status} in the web Studio: wait for it to end")
    return p


def api(method, path, body=None):
    headers = {"HTTP_AUTHORIZATION": f"Bearer {settings.API_TOKEN}"} if settings.API_TOKEN else {}
    r = Client().generic(method, path, json.dumps(body) if body is not None else "",
                         content_type="application/json", **headers)
    try:
        data = r.json()
    except ValueError:
        data = r.content.decode(errors="replace")[:5000]
    return {"status": r.status_code, "body": data}


def studio_projects():
    return [{"slug": p.slug, "name": p.name, "status": p.settle().status} for p in Project.objects.all()]


def studio_project(slug):
    p = project(slug)
    return {"slug": p.slug, "name": p.name, "status": p.status, "messages": p.messages, "research": p.research,
            "brief": p.brief, "spec": p.spec, "cards": [c.as_dict() for c in p.cards.all()], "n8n_url": p.n8n_url}


def studio_create(name, description):
    p = Project.objects.create(name=name, messages=[{"role": "user", "content": description}])
    return {"slug": p.slug}


def studio_prompt(slug, step, activity=None):
    p = project(slug)
    prompts = Prompts(p)
    if step == "write":
        a = {x["name"]: x for x in (p.spec or {}).get("activities", [])}.get(activity)
        if not a:
            raise ToolError(f"no activity {activity!r} in the spec: studio_save the design first")
        return prompts.write(a)
    return prompts.interview() if step == "interview" else prompts.design()


def studio_save(slug, messages=None, brief=None, spec=None):
    p = idle(slug)
    if messages is not None:
        if not all(isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
                   for m in messages):
            raise ToolError("messages: [{\"role\": \"user\" | \"assistant\", \"content\": \"...\"}]")
        p.messages = messages
    if brief is not None:
        p.brief = brief
    if spec is not None:
        if problems := ProjectBuilder(p).design_problems(spec):
            raise ToolError("fix the design:\n" + "\n".join(f"- {x}" for x in problems))
        spec["workflow"]["name"] = spec.get("name") or p.name
        p.spec = spec
    p.save(update_fields=["messages", "brief", "spec", "updated"])
    return "saved"


def studio_write(slug, activity, code):
    b = ProjectBuilder(idle(slug))
    if activity not in b.activities:
        raise ToolError(f"{activity!r} is not an activity of the spec")
    if not defines(code, activity):
        raise ToolError(f"the module must define the function {activity}")
    b.module(activity).write_text(f"{code.rstrip()}\n")
    return f"written {b.module(activity).relative_to(b.draft.parent.parent)}"


def studio_check(slug):
    b = ProjectBuilder(idle(slug))
    if not b.spec:
        raise ToolError("no spec yet: studio_save it first")
    missing = [n for n in b.activities if not b.module(n).exists()]
    if missing:
        raise ToolError(f"write these activities first: {', '.join(missing)}")
    return b.check()


def studio_publish(slug):
    b = ProjectBuilder(p := idle(slug))
    report = studio_check(slug)
    if errors := [x for x in report["problems"] if x["level"] == "error" or x["rule"] == "human"] + \
            [*report["imports"], *report["examples"]]:
        raise ToolError(f"the check is not clean: {errors}")
    done = {}
    for step in (b.publish, b.import_workflow, b.reload):
        try:
            done[step.__name__] = step()
        except Skip as e:
            done[step.__name__] = f"skipped: {e}"
    p.status = Project.READY
    p.save(update_fields=["status", "updated"])
    return done


def call(name, args):
    if name not in TOOLS:
        raise ToolError(f"unknown tool {name}")
    result = globals()[name](**args)
    return result if isinstance(result, str) else json.dumps(result, indent=1, ensure_ascii=False, default=str)


def handle(message):
    """One JSON-RPC message -> its reply, None for a notification."""
    method, params = message.get("method"), message.get("params") or {}
    if "id" not in message:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
                  "serverInfo": {"name": "bpmn8n", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, (d, s) in TOOLS.items()]}
    elif method == "tools/call":
        close_old_connections()  # a long-lived process: Postgres may have dropped the connection
        try:
            text, error = call(params["name"], params.get("arguments") or {}), False
        except Exception as e:  # the client shows it and can fix its call
            text, error = f"{type(e).__name__}: {e}", True
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


class Command(BaseCommand):
    help = __doc__

    def handle(self, **options):
        out, sys.stdout = sys.stdout, sys.stderr  # stdout is the protocol's: anything else printed goes to stderr
        discover()
        for line in sys.stdin:  # ponytail: one request at a time; a long studio_check blocks the others
            if line.strip() and (reply := handle(json.loads(line))):
                out.write(json.dumps(reply, default=str) + "\n")
                out.flush()
