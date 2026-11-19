"""The stack the `bpmn8n` CLI runs (bpmn8n/cli.py): one Service per part, each wrapping docker compose and its
health check (bpmn8n/ops/health.py's Result and timing). Everything goes through a Stack, whose runner, HTTP
check and clock the tests replace: no docker there."""
import os
import platform
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from .health import Result, http_get, timed

ROOT = Path(__file__).resolve().parents[2]
MODELS_URL = "http://127.0.0.1:8001"
# the models server on the Mac, as the containers reach it; elsewhere compose's defaults (CPU containers)
HOST_MODELS = {"LAYA_URL": "http://host.docker.internal:8001/v1/systemone",
               "EXTRACTOR_URL": "http://host.docker.internal:8001/v1/extract",
               "OCR_URL": "http://host.docker.internal:8001/v1/ocr"}
# pillow + torchvision: EmbeddingGemma 2
MODEL_PACKAGES = ["sentence-transformers==6.1.0", "gliner2[local]", "protobuf", "sentencepiece", "tiktoken",
                  "pyobjc-framework-Vision", "pillow", "torchvision"]


class CliError(Exception):
    """What the CLI reports as "✗ <message>" before exiting with 1."""


def run(args, *, cwd=None, env=None, capture=False, check=True):
    return subprocess.run([str(a) for a in args], cwd=cwd, env=env, check=check, text=True, capture_output=capture)


def spawn(args, *, cwd, log):
    """Starts a background process that outlives the CLI; returns its pid."""
    with open(log, "w") as out:
        return subprocess.Popen([str(a) for a in args], cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True).pid


def apple_silicon():
    return platform.system() == "Darwin" and platform.machine() == "arm64"


def need(command, hint):
    if not shutil.which(command):
        raise CliError(f"missing {command}: {hint}")


def read_env(path):
    """NAME=value lines of a .env file (comments and blank lines skipped)."""
    lines = path.read_text().splitlines() if path.exists() else []
    return dict(line.split("=", 1) for line in lines if "=" in line and not line.lstrip().startswith("#"))


def update_env(path, values):
    """Sets the given names in a .env file (None removes one), in place: every other line stays as it was."""
    lines, seen = (path.read_text().splitlines() if path.exists() else []), set()
    out = []
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if "=" in line and name in values and not line.lstrip().startswith("#"):
            seen.add(name)
            if values[name] is not None:
                out.append(f"{name}={values[name]}")
        else:
            out.append(line)
    out += [f"{name}={value}" for name, value in values.items() if name not in seen and value is not None]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n")
    tmp.replace(path)


@dataclass
class Stack:
    """The checkout and the ways to act on it: the runner, the HTTP check (raises unless the URL answers 2xx),
    the clock and the output."""

    root: Path = field(default_factory=lambda: ROOT if (ROOT / "docker-compose.yml").exists() else Path.cwd())
    run: Callable = run
    get: Callable = http_get
    spawn: Callable = spawn
    sleep: Callable = time.sleep
    say: Callable = print
    apple_silicon: bool = field(default_factory=apple_silicon)

    @property
    def env_file(self):
        return self.root / ".env"

    def config(self):
        """The environment over .env, as docker compose reads them; read again each time (`up` writes .env)."""
        return Config.from_env({**read_env(self.env_file), **os.environ})

    def compose(self, *args, capture=False, check=True):
        """docker compose, with the addresses compose cannot derive from PUBLIC_URL itself."""
        return self.run(["docker", "compose", *args], cwd=self.root, capture=capture, check=check,
                        env={**os.environ, **self.config().compose_env()})


class Service:
    """A part of the stack: its compose services, and how to tell it is up (a URL on the host, else a command in
    its container, else its containers running)."""

    name = ""
    services = ()
    profile = ()
    url = ""
    probe = ()

    def __init__(self, stack):
        self.stack = stack

    def compose(self, *args, **kwargs):
        return self.stack.compose(*self.profile, *args, **kwargs)

    def check(self):
        """Raises unless the service is up."""
        if self.url:
            self.stack.get(self.url)
        elif self.probe:
            self.compose("exec", "-T", self.services[0], *self.probe, capture=True)
        else:
            running = self.compose("ps", "--status", "running", "--services", capture=True).stdout.split()
            if missing := [s for s in self.services if s not in running]:
                raise CliError(f"not running: {', '.join(missing)}")

    def health(self) -> Result:
        return timed(self.name, self.check)

    def start(self):
        self.compose("up", "-d", *self.services)

    def stop(self):
        self.compose("stop", *self.services)

    def restart(self):
        self.compose("restart", *self.services, capture=True)

    def wait(self, seconds=180):
        self.stack.say(f"… {self.name}")
        for _ in range(0, seconds, 2):
            if self.health().status == "up":
                self.stack.say(f"✓ {self.name}")
                return
            self.stack.sleep(2)
        raise CliError(f"{self.name} not ready after {seconds}s")

    def ensure(self, seconds=180):
        """Starts it unless it is up, then waits until it is."""
        if self.health().status != "up":
            self.start()
        self.wait(seconds)


class Postgres(Service):
    name, services, probe = "postgres", ("postgres",), ("pg_isready", "-U", "bpmn8n")

    def start(self):
        self.compose("up", "-d", "--wait", *self.services, capture=True)

    def ensure(self, seconds=180):
        """Up, with n8n's own database next to bpmn8n's on the same server."""
        super().ensure(seconds)
        found = self.compose("exec", "-T", "postgres", "psql", "-U", "bpmn8n", "-d", "bpmn8n", "-tAc",
                             "SELECT 1 FROM pg_database WHERE datname = 'n8n'", capture=True).stdout
        if "1" not in found:
            self.compose("exec", "-T", "postgres", "createdb", "-U", "bpmn8n", "n8n", capture=True)


class Redis(Service):
    name, services, probe = "redis", ("redis",), ("redis-cli", "ping")


class N8n(Service):
    # /rest/settings answers once the REST API does (/healthz before)
    name, services, url = "n8n", ("n8n", "n8n-worker"), "http://localhost:5678/rest/settings"


class Web(Service):
    name, services, url = "web", ("web",), "http://localhost:8000/api/health"


class Preview(Service):
    name, services, url = "preview", ("n8n-preview",), "http://localhost:5679/healthz"


class Search(Service):
    name, services = "search", ("searxng",)  # no port on the host: its container running


class Models(Service):
    """Laya and GLiNER2.5: the native models server on an Apple Silicon Mac (Laya on the GPU, OCR with Vision),
    CPU containers elsewhere."""

    name = "models"

    def __init__(self, stack):
        super().__init__(stack)
        if stack.apple_silicon:
            self.url = f"{MODELS_URL}/health"
        else:
            self.services, self.profile = ("models", "laya"), ("--profile", "cpu-models")

    @property
    def pid_file(self):
        return self.stack.root / ".logs" / "models.pid"

    def start(self):
        if not self.stack.apple_silicon:
            self.stack.say("  Laya and GLiNER run on CPU in Docker; the first start downloads torch and the weights (minutes)")
            return super().start()
        if self.health().status == "up":
            return self.stack.say("✓ models server already running")
        venv = self.stack.root / ".venv-models"
        python = venv / "bin" / "python"
        probe = self.stack.run([python, "-c", "import sentence_transformers, gliner2, Vision"], capture=True, check=False)
        if probe.returncode:
            need("uv", "https://docs.astral.sh/uv/ (brew install uv)")
            self.stack.say("… installing the decision runtime, GLiNER2.5 and Vision for OCR (first run only)")
            if not python.exists():
                self.stack.run(["uv", "venv", "-q", "--python", "3.12", venv])
            self.stack.run(["uv", "pip", "install", "-q", "--python", python, *MODEL_PACKAGES])
        self.stack.run([python, "tools/decision_runtime.py", "--download"], cwd=self.stack.root, capture=True)
        self.pid_file.parent.mkdir(exist_ok=True)
        pid = self.stack.spawn([python, "tools/models_server.py"], cwd=self.stack.root,
                               log=self.pid_file.with_name("models.log"))
        self.pid_file.write_text(f"{pid}\n")
        self.wait(900)

    def stop(self):
        if not self.stack.apple_silicon:
            return super().stop()
        if self.pid_file.exists():
            if not self.stack.run(["kill", self.pid_file.read_text().strip()], capture=True, check=False).returncode:
                self.stack.say("✓ models server stopped")
            self.pid_file.unlink()
        self.stack.run(["pkill", "-f", "tools/models_server.py"], capture=True, check=False)


SERVICES = (Postgres, Redis, Models, Web, N8n, Preview, Search)  # in start order
