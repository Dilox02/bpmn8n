"""bpmn8n as an MCP server (stdio): an MCP client (Claude Code, Claude Desktop...) reads and drives the
state: Studio's projects, where the client itself is the LLM (no provider API key needed), and the
whole REST API (cases, user tasks, messages, event log, mining, workflow checks).

    .mcp.json (Claude Code, from this folder): docker compose exec -T web python manage.py mcp

The client does what Studio's LLM would: the interview (in its own chat), the design, the code;
bpmn8n keeps doing the rest (checks, examples, publishing, import into n8n), as in the web build.
"""
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Literal

from django.core.management.base import BaseCommand
from django.test import Client
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from bpmn8n.api import discover
from bpmn8n.conf import settings
from bpmn8n.models import Project
from bpmn8n.studio.build import ProjectBuilder, Skip, defines
from bpmn8n.studio.prompts import Prompts

PROTOCOL_VERSION = "2025-06-18"


class Arguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ApiArguments(Arguments):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    body: dict[str, Any] | None = None


class ProjectArguments(Arguments):
    slug: str


class CreateArguments(Arguments):
    name: str = Field(min_length=1, max_length=Project._meta.get_field("name").max_length)
    description: str


class PromptArguments(ProjectArguments):
    step: Literal["interview", "design", "write"]
    activity: str | None = None


class Message(Arguments):
    role: Literal["user", "assistant"]
    content: str


class SaveArguments(ProjectArguments):
    messages: list[Message] | None = None
    brief: str | None = None
    spec: dict[str, Any] | None = None


class WriteArguments(ProjectArguments):
    activity: str
    code: str


TOOLS = {
    "api": ("Calls bpmn8n's REST API (the routes of /api/openapi.json) and returns its JSON. Useful routes: "
            "GET /api/usertasks?status=open, POST /api/usertasks/{id}/complete {result}, GET /api/cases/{case_id}, "
            "GET /api/eventlog.csv?since=..., GET /api/metrics, GET /api/processmining?process=..., "
            "GET /api/messages/subscriptions, POST /api/messages/{name} {...}, "
            "GET /api/evaluations/{workflow_id}/runs, GET /api/evaluations/{workflow_id}/runs/{project}/{execution_id}, "
            "POST /api/workflows/check {n8n workflow json}, POST /api/tasks/{name} {input}, GET /api/openapi.json.",
            ApiArguments),
    "studio_projects": ("Studio's projects: slug, name, status (interview, research, building, ready, failed).",
                        Arguments),
    "studio_project": ("One Studio project's whole state: interview messages, research, brief, spec, the cards "
                       "of its last build, its n8n workflow.", ProjectArguments),
    "studio_create": ("A new Studio project from its name and description (the interview's first answer). "
                      "Then: interview the person yourself, studio_save the brief, get studio_prompt 'design'.",
                      CreateArguments),
    "studio_prompt": ("The instructions Studio gives its LLM for a step, with the live context (existing tasks, "
                      "lanes, the builder's steps, code conventions, the project so far). Follow them: "
                      "'interview' (questions to ask, the brief to write), 'design' (the spec's JSON shape), "
                      "'write' (one activity's module: pass activity).",
                      PromptArguments),
    "studio_save": ("Saves the interview (messages: [{role: user|assistant, content}], the whole transcript), the "
                    "brief and/or the spec (the design JSON, as studio_prompt 'design' describes it); a spec the "
                    "build cannot use is refused with what is wrong.",
                    SaveArguments),
    "studio_write": ("Writes one activity's module (the whole Python file) into the project's draft.",
                     WriteArguments),
    "studio_check": ("Imports the draft's modules, runs each activity's examples, plays its scenarios "
                     "(scenarios.json) and checks the workflow: {imports, examples, scenarios, problems}; a failing "
                     "scenario is also a problem. Fix with studio_write / studio_save until it is clean.",
                     ProjectArguments),
    "studio_publish": ("Once studio_check is clean: publishes projects/<slug>, imports the workflow into n8n and "
                       "reloads the server's tasks. The project becomes ready.", ProjectArguments),
}


class ToolError(Exception):
    pass


class InvalidArguments(ValueError):
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


def call(name, args, handler=None):
    if not isinstance(name, str) or name not in TOOLS:
        raise InvalidArguments(f"unknown tool {name!r}")
    try:
        values = TOOLS[name][1].model_validate(args).model_dump(exclude_unset=True)
    except ValidationError as e:
        raise InvalidArguments("; ".join(f"{'.'.join(map(str, p['loc']))}: {p['msg']}"
                                         for p in e.errors(include_input=False, include_url=False))) from None
    if handler:
        return handler(name, values)
    result = globals()[name](**values)
    return result if isinstance(result, str) else json.dumps(result, indent=1, ensure_ascii=False, default=str)


def error_reply(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle(message, tool_handler=None):
    """One JSON-RPC message -> its reply, None for a notification."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or \
            not isinstance(message.get("method"), str):
        return error_reply(None, -32600, "invalid JSON-RPC request")
    if "id" not in message:
        return None
    id_ = message["id"]
    if not isinstance(id_, (int, str)) or isinstance(id_, bool):
        return error_reply(None, -32600, "request id must be an integer or string")
    method, params = message["method"], message.get("params", {})
    if not isinstance(params, dict):
        return error_reply(id_, -32602, "params must be an object")
    if method == "initialize":
        if not isinstance(params.get("protocolVersion", PROTOCOL_VERSION), str):
            return error_reply(id_, -32602, "protocolVersion must be a string")
        result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "bpmn8n", "version": "1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": s.model_json_schema()}
                            for n, (d, s) in TOOLS.items()]}
    elif method == "tools/call":
        try:
            result = call(params.get("name"), params.get("arguments", {}), tool_handler)
            if tool_handler:
                return {"jsonrpc": "2.0", "id": id_, "result": result}
            text, error = result, False
        except InvalidArguments as e:
            return error_reply(id_, -32602, str(e))
        except Exception as e:  # the client shows it and can fix its call
            text, error = f"{type(e).__name__}: {e}", True
        result = {"content": [{"type": "text", "text": text}], "isError": error}
    else:
        return error_reply(id_, -32601, f"no method {method}")
    return {"jsonrpc": "2.0", "id": id_, "result": result}


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument("--tool-request", action="store_true", help="Serve one internal tool request on stdin")

    def fresh_tool(self, name, values):
        """A session can outlive publications: reload task routes and Studio's catalog per tool."""
        result = subprocess.run([sys.executable, str(Path(settings.BASE_DIR) / "manage.py"), "mcp", "--tool-request"],
                                input=json.dumps({"name": name, "arguments": values}), text=True,
                                capture_output=True, timeout=600)  # check allows 300s; publishing also imports into n8n
        if result.returncode:
            raise ToolError(f"the tool request did not run:\n{result.stderr[-3000:]}")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise ToolError("the tool request returned an invalid response") from None

    def handle(self, **options):
        out, sys.stdout = sys.stdout, sys.stderr  # stdout is the protocol's: anything else printed goes to stderr
        if options.get("tool_request"):
            discover()
            reply = handle({"jsonrpc": "2.0", "id": 0, "method": "tools/call", "params": json.load(sys.stdin)})
            out.write(json.dumps(reply["result"], default=str) + "\n")
            out.flush()
            return
        for line in sys.stdin:  # ponytail: one request at a time; a long studio_check blocks the others
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (ValueError, RecursionError):
                reply = error_reply(None, -32700, "invalid JSON")
            else:
                reply = handle(message, tool_handler=self.fresh_tool)
            if reply is not None:
                out.write(json.dumps(reply, default=str) + "\n")
                out.flush()
