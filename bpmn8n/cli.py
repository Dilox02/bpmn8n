"""bpmn8n's operations: `python -m bpmn8n <command>`, or `bpmn8n <command>` once installed (./start.sh calls it).
  up       build the n8n nodes; start the models, Postgres, Redis, Django, n8n, its preview and SearXNG; migrate;
           create n8n's bpmn8n credential and API key (into .env). Safe to re-run: it rebuilds and reloads the nodes
  down     stop everything (data is kept)
  status   the containers, and whether each service answers
  doctor   `manage.py doctor` in the web container: every dependency, with errors and hints
  logs     follow the logs
  export   n8n's workflows -> workflows/*.json (version them with git)
  import   workflows/*.json -> n8n (published workflows stay published)
  deploy   `manage.py workflows push` (every project, or the folders given) in the web container
  diff     `manage.py workflows diff`: drift between git and n8n (exits with 1 on drift)
The n8n owner login that creates the credential: N8N_EMAIL and N8N_PASSWORD (the environment, else .env)."""
import argparse
import subprocess
import sys

from .ops.bootstrap import Bootstrap, N8nSession
from .ops.services import HOST_MODELS, SERVICES, CliError, Models, N8n, Postgres, Preview, Stack, Web, need, update_env

INFO = """
bpmn8n is up
  n8n editor      {n8n}
  docs            {web}/docs/   (the framework, and your tasks live)
  inbox           {web}/inbox/
  studio          {web}/studio/ (design a process with an LLM: admin → Settings first)
  activities      {web}/activities/ (every @task, with its decorator and fields)
  API + Swagger   {web}/api/docs
  process mining  {web}/api/processmining   (all of it: python manage.py mine)
  example flows   examples/workflows/*.json   (n8n: ⋯ → Import from File)
  stop            python -m bpmn8n down"""


def manage(stack, *args):
    """A manage.py command in the web container, where the stack's configuration is; its exit code."""
    return stack.compose("exec", "-T", "web", "python", "manage.py", *args, check=False).returncode


def write_env(stack):
    """.env: the n8n login and, on a Mac, where the containers reach the native models server. Other lines stay."""
    config = stack.config()
    values = {"N8N_EMAIL": config.N8N_EMAIL, "N8N_PASSWORD": config.N8N_PASSWORD} if config.N8N_EMAIL else {}
    for name, url in HOST_MODELS.items():  # elsewhere compose's defaults: the CPU containers
        if stack.apple_silicon:
            values[name] = url
        elif getattr(config, name) == url:
            values[name] = None
    if values:
        update_env(stack.env_file, values)


def up(stack, args):
    need("docker", "https://docs.docker.com/get-docker/")
    need("npm", "https://nodejs.org/")
    if stack.run(["docker", "info"], capture=True, check=False).returncode:
        raise CliError("Docker is not running")
    stack.say("… building the n8n BPMN nodes")
    nodes = stack.root / "n8n-nodes-bpmn8n"
    if not (nodes / "node_modules").exists():
        stack.run(["npm", "install", "--ignore-scripts", "--silent"], cwd=nodes)
    stack.run(["npm", "run", "build", "--silent"], cwd=nodes, capture=True)
    Postgres(stack).ensure()
    write_env(stack)
    for service in SERVICES[1:]:
        service(stack).start()
    for service in (N8n, Preview):  # load the freshly built nodes
        service(stack).restart()
    web, n8n = Web(stack), N8n(stack)
    web.wait(600)
    stack.compose("exec", "-T", "web", "python", "manage.py", "migrate", "--noinput", capture=True)  # web was up
    n8n.wait(300)
    if Bootstrap(N8nSession(), stack.config(), stack.env_file, stack.say).run():  # web reads .env at start
        stack.compose("up", "-d", "web", capture=True)
        web.wait(600)
    config = stack.config()
    stack.say(INFO.format(n8n=config.N8N_PUBLIC_URL, web=config.BPMN8N_PUBLIC_URL))


def down(stack, args):
    for service in reversed(SERVICES):
        service(stack).stop()


def status(stack, args):
    stack.compose("--profile", "cpu-models", "ps")
    results = [service(stack).health() for service in SERVICES]
    for r in results:
        ms = f"{r.ms:7.1f} ms" if r.ms is not None else ""
        stack.say(f"{'✓' if r.status == 'up' else '✗'} {r.name:9} {ms:>10}  {'' if r.status == 'up' else r.error}")
    return 0 if all(r.status == "up" for r in results) else 1


def doctor(stack, args):
    return manage(stack, "doctor")


def logs(stack, args):
    log = Models(stack).pid_file.with_name("models.log")
    if log.exists():
        stack.say("\n".join(log.read_text().splitlines()[-5:]))
    stack.compose("logs", "-f", "--tail", "20", "web", "n8n", check=False)


def export(stack, args):
    """n8n's own CLI: the open alternative to its paid source control."""
    stack.compose("exec", "-T", "n8n", "sh", "-c", "rm -rf /tmp/workflows && n8n export:workflow --all --separate "
                  "--pretty --output=/tmp/workflows/", capture=True)
    folder = stack.root / "workflows"
    folder.mkdir(exist_ok=True)
    stack.compose("cp", "n8n:/tmp/workflows/.", f"{folder}/", capture=True)
    stack.say(f"✓ {len(list(folder.glob('*.json')))} workflows in workflows/")


def import_(stack, args):
    """n8n's import unpublishes workflows: the ones published before are published again."""
    n8n = Bootstrap(N8nSession(), stack.config(), stack.env_file, stack.say)
    published = n8n.published() if n8n.login() else []
    stack.compose("cp", f"{stack.root / 'workflows'}/.", "n8n:/tmp/workflows/", capture=True)
    stack.compose("exec", "-T", "n8n", "n8n", "import:workflow", "--separate", "--input=/tmp/workflows/")
    n8n.publish(published)


def deploy(stack, args):
    return manage(stack, "workflows", "push", *args.args)


def diff(stack, args):
    return manage(stack, "workflows", "diff", *args.args)


COMMANDS = {"up": up, "down": down, "status": status, "doctor": doctor, "logs": logs, "export": export,
            "import": import_, "deploy": deploy, "diff": diff}


def parser():
    p = argparse.ArgumentParser(prog="bpmn8n", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = p.add_subparsers(dest="command", required=True, metavar="command")
    for name in COMMANDS:
        sub = commands.add_parser(name)
        if name in ("deploy", "diff"):
            sub.add_argument("args", nargs="*", help="projects/<name> ..., --json: passed to `workflows`")
    return p


def main(argv=None, stack=None):
    cli = parser()
    args, extra = cli.parse_known_args(argv)  # deploy and diff pass their options on to `manage.py workflows`
    if extra and args.command not in ("deploy", "diff"):
        cli.error(f"unrecognized arguments: {' '.join(extra)}")
    args.args = [*getattr(args, "args", []), *extra]
    try:
        return COMMANDS[args.command](stack or Stack(), args) or 0
    except CliError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        print(f"✗ {' '.join(map(str, e.cmd))}: exit {e.returncode}\n{e.stderr or ''}".rstrip(), file=sys.stderr)
        return e.returncode or 1
