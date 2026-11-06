"""bpmn8n Studio's build, from the interview's brief to a project folder that extends the framework:

    projects/<slug>/tasks/<activity>.py   one module per new activity: its Schemas and @task function
    projects/<slug>/examples.json         the activities' examples: the tests they must pass
    projects/<slug>/process.py            the workflow as a spec for bpmn8n's builder (Workflow.from_spec)
    projects/<slug>/workflow.json         the same workflow for n8n (⋯ → Import from File)
    projects/<slug>/INTERVIEW.md          the brief, who decides and why, the domain research, the interview

Each step is a card (the kanban). The design comes with examples (input -> expected output) for every
new activity; the code is written one module at a time; then a separate process imports it, runs the
examples and checks the workflow (manage.py check_project), and whatever fails goes back to the LLM:
only the failing module, or the workflow. The project is written to projects/_<slug> and becomes
projects/<slug> only once everything passes: a broken project never reaches the server. The build runs
in its own process (manage.py build_project), not in a web worker."""
import ast
import json
import keyword
import os
import pprint
import shutil
import signal
import subprocess
import sys
from functools import cached_property
from pathlib import Path

from ..api.tasks import TASK_KINDS
from ..clients import n8n
from ..conf import settings
from ..models import Project, Settings
from ..workflows.builder import SpecError, Workflow
from .jobs import ROOT, Board, Skip, background
from .prompts import Prompts

PROJECTS = ROOT / "projects"
ROUNDS = 4  # check -> fix rounds before giving up


class BuildError(Exception):
    pass


def defines(code, name):
    return any(isinstance(n, ast.FunctionDef) and n.name == name for n in ast.parse(code).body)


class ProjectBuilder:
    def __init__(self, project, llm=None):
        self.project = project
        if llm:
            self.llm = llm
        self.draft, self.final = PROJECTS / f"_{project.slug}", PROJECTS / project.slug
        self.prompts = Prompts(project)

    @classmethod
    def start(cls, project):
        """Runs the build in its own process."""
        Settings.llm_client()  # no LLM, no build: say so now
        project.status = Project.BUILDING
        project.save(update_fields=["status", "updated"])
        background("build_project", project)

    @cached_property
    def llm(self):
        """The default LLM, needed only by the steps that ask it (an MCP client writes its own)."""
        return Settings.llm_client()

    @property
    def spec(self):
        return self.project.spec

    @property
    def activities(self):
        return {a["name"]: a for a in self.spec["activities"]}

    def run(self):
        board = Board(self.project)
        shutil.rmtree(self.draft, ignore_errors=True)  # what a build that died left behind
        try:
            board.step("Design the process", self.design)
            for name in self.activities:
                board.step(f"Write {name}", lambda n=name: self.write(n))
            for title, do in (("Test the activities and check the workflow", self.verify),
                              (f"Publish projects/{self.project.slug}", self.publish),
                              ("Import into n8n", self.import_workflow), ("Load the tasks into the server", self.reload)):
                board.step(title, do)
            self.project.status = Project.READY
        except Exception:  # the card that failed says why
            self.project.status = Project.FAILED
        self.project.save(update_fields=["status", "updated"])

    # --- steps ------------------------------------------------------------------------

    def design(self):
        """The spec: who decides, the new activities with their examples, the workflow. A spec the
        build cannot use goes back once, with what is wrong."""
        messages = [{"role": "user", "content": f"Process name: {self.project.name}\n\nBrief:\n{self.project.brief}"}]
        for _ in range(2):
            spec = self.llm.ask(self.prompts.design(), messages)
            if not (problems := self.design_problems(spec)):
                break
            messages += [{"role": "assistant", "content": json.dumps(spec)},
                         {"role": "user", "content": "Fix the design:\n" + "\n".join(f"- {p}" for p in problems)}]
        else:
            raise BuildError("; ".join(problems))
        spec["workflow"]["name"] = spec.get("name") or self.project.name
        self.project.spec = spec
        self.project.save(update_fields=["spec", "updated"])
        llm = (spec.get("assessment") or {}).get("llm") or ""
        return f"{len(spec['workflow'].get('steps', []))} steps, {len(spec['activities'])} new activities" + \
            (f"\nLLM agent: {llm}" if llm else "")

    def design_problems(self, spec):
        if not isinstance(spec.get("workflow"), dict) or not isinstance(spec.setdefault("activities", []), list):
            return [f"it has no workflow: {json.dumps(spec)[:300]}"]
        taken = {t.name for t in self.prompts.tasks}
        problems, seen = [], set()
        for a in spec["activities"]:
            name = a.get("name", "") if isinstance(a, dict) else ""
            if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
                problems.append(f"activity {name or a!r}: its name must be a Python identifier (verb_noun)")
            elif name in seen:
                problems.append(f"activity {name}: designed twice; describe it once")
            elif name in taken:
                problems.append(f"activity {name}: an existing task has this name; reuse it in a task step or rename it")
            elif a.get("kind") not in TASK_KINDS:
                problems.append(f"activity {name}: kind must be one of {', '.join(TASK_KINDS)}")
            elif not a.get("examples") or not all(isinstance(e, dict) and isinstance(e.get("input"), dict)
                                                  and isinstance(e.get("output"), dict) for e in a["examples"]):
                problems.append(f"activity {name}: give it 2 or 3 examples, each {{\"input\": {{...}}, \"output\": {{...}}}}")
            seen.add(name)
        return problems

    def write(self, name):
        """One activity's module; it must define the function the workflow calls."""
        messages = [{"role": "user", "content": f"Write the module of {name}."}]
        for _ in range(2):
            code = self.llm.code(self.prompts.write(self.activities[name]), messages)
            if defines(code, name):
                self.module(name).write_text(f"{code}\n")
                return f"{len(code.splitlines())} lines"
            messages += [{"role": "assistant", "content": code},
                         {"role": "user", "content": f"It does not define the function {name}: write the whole module again."}]
        raise BuildError(f"no valid code for {name}")

    def verify(self):
        """Imports the code, runs the examples and checks the workflow in a separate process (a fresh
        registry of tasks), sending what fails back to the LLM: each failing module, then the workflow."""
        for round_ in range(1, ROUNDS + 1):
            report = self.check()
            # a person decides: Studio's workflows meet it everywhere, not only after LLM agents
            errors = [p for p in report["problems"] if p["level"] == "error" or p["rule"] == "human"]
            failing = self.failing_modules(report)
            if not (failing or errors):
                tested = sum(len(a["examples"]) for a in self.spec["activities"])
                notes = "".join(f"\n{p['level']}: {p['node'] + ': ' if p['node'] else ''}{p['message']}"
                                for p in report["problems"])
                return f"passed in {round_} round(s), {tested} examples{notes}"
            if round_ == ROUNDS:
                raise BuildError(f"still failing after {ROUNDS} rounds: {failing or errors}")
            for name, failure in failing.items():
                self.fix(name, failure)
            if errors:
                self.fix_workflow(errors)

    def failing_modules(self, report):
        """activity -> what fails in its module."""
        package = f"{PROJECTS.name}.{self.draft.name}.tasks"
        failing = {}
        for module, error in report["imports"].items():
            if not module.startswith(f"{package}."):  # its package or app: not an activity the LLM wrote
                raise BuildError(f"{module} does not import:\n{error}")
            failing[module.rpartition(".")[2]] = f"It does not import:\n{error}"
        for name, problems in report["examples"].items():
            failing[name] = "Its examples fail:\n" + "\n".join(f"- {p}" for p in problems)
        return failing

    def fix(self, name, failure):
        path = self.module(name)
        code = self.llm.code(self.prompts.fix_code(self.activities.get(name, {"name": name}), path.read_text(), failure),
                             [{"role": "user", "content": f"Fix the module of {name}."}])
        path.write_text(f"{code}\n")

    def fix_workflow(self, errors):
        problems = "\n".join(f"- {p['node'] or '(workflow)'}: {p['message']}" for p in errors)
        fixed = self.llm.ask(self.prompts.fix_workflow(problems), [{"role": "user", "content": "Fix the workflow."}])
        if isinstance(fixed.get("workflow"), dict):
            self.spec["workflow"] = {**fixed["workflow"], "name": self.spec["workflow"]["name"]}
            self.project.save(update_fields=["spec", "updated"])

    def check(self):
        """The draft written out and checked: {"imports", "examples", "problems"} (ProjectCheck). A spec
        the builder rejects is a problem too; the activities are tested all the same."""
        self.write_spec()
        path, spec_problems = self.draft / "workflow.json", []
        try:
            path.write_text(json.dumps(self.workflow(), indent=2))
        except SpecError as e:
            path.unlink(missing_ok=True)
            spec_problems.append({"node": "", "rule": "spec", "level": "error", "message": str(e)})
        result = subprocess.run([sys.executable, "manage.py", "check_project", "--json", str(self.draft)],
                                cwd=ROOT, capture_output=True, text=True, timeout=300,
                                # no .pyc: a fixed module of the same size, written within the second, would run stale
                                env={**os.environ, "BPMN8N_DRAFT": self.draft.name, "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            report = json.loads(result.stdout)
        except ValueError:  # it did not get to the report: Django itself failed to start
            raise BuildError(f"the check did not run:\n{(result.stderr or result.stdout)[-3000:]}") from None
        report["problems"] += spec_problems
        return report

    def publish(self):
        a = self.spec.get("assessment") or {}
        choices = "".join(f"\n- **{c.get('step')}** ({c.get('by')}): {c.get('why')}" for c in a.get("choices", []))
        transcript = "\n\n".join(f"**{m['role']}**: {m['content']}" for m in self.project.messages)
        research = "".join(f"\n\n### {t['topic'][:200]}\n\n{t['notes']}\n\n"
                           + "\n".join(f"{i}. [{s['title']}]({s['url']})" for i, s in enumerate(t["sources"], 1))
                           for t in self.project.research_topics) or "\n\nNone."
        (self.draft / "INTERVIEW.md").write_text(f"# {self.project.name}\n\n## Brief\n\n{self.project.brief}\n\n"
                                                 f"## Who decides\n\n**LLM agent**: {a.get('llm', '')}\n\n"
                                                 f"**GDPR**: {a.get('gdpr', '')}\n{choices}\n\n"
                                                 f"## Domain research{research}\n\n## Interview\n\n{transcript}\n")
        if self.final.exists():  # a rebuild of this project, never a folder Studio did not write
            if not (self.final / "INTERVIEW.md").exists():
                raise BuildError(f"projects/{self.project.slug} exists and was not built by Studio: left as it is")
            shutil.rmtree(self.final)
        self.draft.rename(self.final)
        return f"projects/{self.project.slug}/: tasks/ ({len(self.activities)} activities), examples.json, " \
               "process.py, workflow.json, INTERVIEW.md"

    def import_workflow(self):
        note = "\nThe LLM agents have no credential: choose one on their chat model nodes in n8n" \
            if self.agents and not self.llm_credential else ""
        try:
            created = n8n.save_workflow(self.workflow(), self.project.n8n_id)
        except n8n.N8nError as e:
            if e.status in (None, 401, 403):
                rebuild = f" over workflow {self.project.n8n_id} (re-run ./start.sh: its key can update workflows)" \
                    if self.project.n8n_id and e.status == 403 else ""
                raise Skip(f"{e}. Import projects/{self.project.slug}/workflow.json in the n8n editor{rebuild} "
                           "(⋯ → Import from File)") from e
            raise
        self.project.n8n_id = created["id"]
        self.project.save(update_fields=["n8n_id", "updated"])
        if created.get("draft"):
            note += "\nSaved as a draft of the active workflow: publish it in the n8n editor to run it"
        return f"{settings.N8N_PUBLIC_URL}/workflow/{created['id']}{note}"

    def reload(self):
        """gunicorn reloads its workers on SIGHUP: they start with the new project's tasks."""
        try:
            os.kill(int(Path(settings.GUNICORN_PID).read_text()), signal.SIGHUP)
        except (OSError, ValueError) as e:
            raise Skip("restart the Django server to load the new tasks") from e
        return "the new tasks are in the n8n nodes' menus within seconds"

    # --- helpers ----------------------------------------------------------------------

    def tasks_package(self):
        """The draft's tasks/ package, created on first use: with no new activity, still an app."""
        folder = self.draft / "tasks"
        if not folder.exists():
            folder.mkdir(parents=True)
            (self.draft / "__init__.py").touch()
            (folder / "__init__.py").write_text(f'"""{self.project.name}: one module per activity (bpmn8n Studio)."""\n')
        return folder

    def module(self, name):
        return self.tasks_package() / f"{name}.py"

    def write_spec(self):
        """process.py (the workflow's spec, whose start data the check reads) and examples.json."""
        self.tasks_package()
        (self.draft / "examples.json").write_text(json.dumps(
            {name: a["examples"] for name, a in self.activities.items()}, indent=2, ensure_ascii=False) + "\n")
        (self.draft / "process.py").write_text(
            f'"""{self.project.name}: the workflow, for bpmn8n\'s builder. workflow.json is the same, for n8n."""\n'
            "from bpmn8n.workflows import Workflow\n\n"
            f"SPEC = {pprint.pformat(self.spec['workflow'], width=110, sort_dicts=False)}\n\n\n"
            "def workflow(credential=None):\n    return Workflow.from_spec(SPEC, credential)\n")

    @property
    def agents(self):
        return any(step.get("type") == "llm_agent" for step in self.spec["workflow"].get("steps", []))

    @cached_property
    def llm_credential(self):
        """The id of the n8n credential with admin → Settings' LLM, created or updated once per build."""
        if not (provider := Settings.load().llm):
            return None
        try:
            return n8n.ensure_credential(f"bpmn8n LLM: {provider.name}", *provider.n8n_credential())
        except n8n.N8nError:
            return None  # the chat model nodes are left without it: chosen in n8n

    def workflow(self):
        credential = {"id": settings.N8N_CREDENTIAL, "name": "bpmn8n"} if settings.N8N_CREDENTIAL else None
        provider = Settings.load().llm if self.agents else None
        chat_model = provider.n8n_chat_model(self.llm_credential) if provider else None
        return Workflow.from_spec(self.spec["workflow"], credential, chat_model).as_json()
