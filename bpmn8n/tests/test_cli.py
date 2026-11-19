"""B9: the `bpmn8n` CLI, its services and n8n's bootstrap, with a fake runner and a fake n8n: no docker, no network."""
import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from .. import cli
from ..ops.bootstrap import SCOPES, Bootstrap
from ..ops.services import (
    HOST_MODELS,
    CliError,
    Models,
    N8n,
    Postgres,
    Preview,
    Redis,
    Search,
    Stack,
    Web,
    read_env,
    update_env,
)


class Runner:
    """Records the commands; answers each with the first rule whose text is in it: (returncode, stdout)."""

    def __init__(self, rules=None):
        self.rules, self.calls = list((rules or {}).items()), []

    def __call__(self, args, *, cwd=None, env=None, capture=False, check=True):
        command = " ".join(map(str, args))
        self.calls.append(command)
        code, out = next((answer for text, answer in self.rules if text in command), (0, ""))
        if callable(code):
            code, out = code()
        if check and code:
            raise subprocess.CalledProcessError(code, args, out, "failed")
        return subprocess.CompletedProcess(args, code, out, "")

    def ran(self, text):
        return [c for c in self.calls if text in c]


class Http:
    """The HTTP check: URLs that answer, the others raise."""

    def __init__(self, up=()):
        self.up, self.asked = set(up), []

    def __call__(self, url):
        self.asked.append(url)
        if url not in self.up:
            raise ConnectionError(f"{url}: refused")


def stack(tmp, runner=None, http=None, mac=False, lines=None):
    return Stack(root=Path(tmp), run=runner or Runner(), get=http or Http(), spawn=mock.Mock(return_value=4242),
                 sleep=lambda s: None, say=(lines.append if lines is not None else lambda text: None), apple_silicon=mac)


class FakeN8n:
    """n8n's REST and public API: (method, path) -> (status, body); records every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, path, body=None, headers=None):
        self.calls.append((method, path, body, headers))
        return self.routes.get((method, path), (404, {}))


class ServicesTest(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_postgres_starts_when_down_and_creates_n8n_database(self):
        probes = iter([1, 0])  # pg_isready: down, then up
        runner = Runner({"pg_isready": (lambda: (next(probes), ""), ""), "SELECT 1": (0, "")})
        Postgres(stack(self.tmp, runner)).ensure()
        self.assertEqual(len(runner.ran("docker compose up -d --wait postgres")), 1)
        self.assertEqual(runner.ran("createdb"), ["docker compose exec -T postgres createdb -U bpmn8n n8n"])

    def test_postgres_up_with_its_databases_is_left_alone(self):
        runner = Runner({"SELECT 1": (0, "1\n")})
        Postgres(stack(self.tmp, runner)).ensure()
        self.assertEqual(runner.ran(" up "), [])
        self.assertEqual(runner.ran("createdb"), [])

    def test_redis_answers_ping_in_its_container(self):
        runner = Runner({"redis-cli": (1, "")})
        self.assertEqual(Redis(stack(self.tmp, runner)).health().status, "down")
        self.assertEqual(runner.calls, ["docker compose exec -T redis redis-cli ping"])

    def test_http_services_and_their_compose_services(self):
        http = Http(up={"http://localhost:8000/api/health", "http://localhost:5678/rest/settings"})
        runner = Runner()
        s = stack(self.tmp, runner, http)
        self.assertEqual([c(s).health().status for c in (Web, N8n, Preview)], ["up", "up", "down"])
        self.assertIn("http://localhost:5679/healthz", http.asked)
        N8n(s).start()
        N8n(s).stop()
        Preview(s).restart()
        self.assertEqual(runner.calls, ["docker compose up -d n8n n8n-worker", "docker compose stop n8n n8n-worker",
                                        "docker compose restart n8n-preview"])

    def test_search_is_up_when_its_container_runs(self):
        s = stack(self.tmp, Runner({"ps --status running": (0, "web\nsearxng\n")}))
        self.assertEqual(Search(s).health().status, "up")
        down = Search(stack(self.tmp, Runner({"ps --status running": (0, "web\n")}))).health()
        self.assertEqual((down.status, down.error), ("down", "CliError: not running: searxng"))

    def test_wait_gives_up(self):
        with self.assertRaisesRegex(CliError, "web not ready after 6s"):
            Web(stack(self.tmp)).wait(6)

    def test_native_models_server_on_a_mac(self):
        runner, http, lines = Runner(), Http(), []
        s = stack(self.tmp, runner, http, mac=True, lines=lines)
        s.spawn.side_effect = lambda *a, **k: http.up.add("http://127.0.0.1:8001/health") or 4242
        Models(s).start()
        self.assertEqual(len(runner.ran("tools/decision_runtime.py --download")), 1)
        self.assertEqual(s.spawn.call_args.args[0][1], "tools/models_server.py")
        self.assertEqual((Path(self.tmp) / ".logs" / "models.pid").read_text(), "4242\n")
        Models(s).start()  # running: left alone
        self.assertEqual((s.spawn.call_count, lines[-1]), (1, "✓ models server already running"))
        Models(s).stop()
        self.assertEqual(runner.ran("kill"), ["kill 4242", "pkill -f tools/models_server.py"])
        self.assertFalse((Path(self.tmp) / ".logs" / "models.pid").exists())

    def test_models_in_cpu_containers_elsewhere(self):
        runner = Runner()
        Models(stack(self.tmp, runner)).start()
        Models(stack(self.tmp, runner)).stop()
        self.assertEqual(runner.calls, ["docker compose --profile cpu-models up -d models laya",
                                        "docker compose --profile cpu-models stop models laya"])


class EnvFileTest(SimpleTestCase):
    def test_update_keeps_unrelated_lines_in_place(self):
        path = Path(tempfile.mkdtemp()) / ".env"
        path.write_text("# mine\nSECRET_KEY=s\nN8N_API_KEY=old\nLAYA_URL=x\nWEB_WORKERS=8\n")
        update_env(path, {"N8N_API_KEY": "new", "LAYA_URL": None, "N8N_CREDENTIAL": "7"})
        self.assertEqual(path.read_text(), "# mine\nSECRET_KEY=s\nN8N_API_KEY=new\nWEB_WORKERS=8\nN8N_CREDENTIAL=7\n")
        self.assertEqual(read_env(path)["N8N_CREDENTIAL"], "7")

    def test_write_env_points_the_containers_at_the_native_models_on_a_mac_only(self):
        tmp = tempfile.mkdtemp()
        env = Path(tmp) / ".env"
        env.write_text("N8N_EMAIL=o@x.org\nN8N_PASSWORD=pw\nOTHER=1\n")
        with mock.patch.dict("os.environ", {}, clear=True):
            cli.write_env(stack(tmp, mac=True))
            self.assertEqual(read_env(env), {"N8N_EMAIL": "o@x.org", "N8N_PASSWORD": "pw", "OTHER": "1", **HOST_MODELS})
            update_env(env, {"OCR_URL": "http://elsewhere/ocr"})  # set by hand: kept
            cli.write_env(stack(tmp))
            self.assertEqual(read_env(env), {"N8N_EMAIL": "o@x.org", "N8N_PASSWORD": "pw", "OTHER": "1",
                                             "OCR_URL": "http://elsewhere/ocr"})


class BootstrapTest(SimpleTestCase):
    def setUp(self):
        self.env = Path(tempfile.mkdtemp()) / ".env"
        self.env.write_text("KEEP=1\n")
        self.lines = []

    def bootstrap(self, routes, **config):
        from ..config import Config

        self.n8n = FakeN8n(routes)
        values = {"N8N_EMAIL": "o@x.org", "N8N_PASSWORD": "pw-secret", "BPMN8N_API_TOKEN": "tok", **config}
        return Bootstrap(self.n8n, Config(**values), self.env, self.lines.append)

    LOGIN = {("POST", "/rest/login"): (200, {})}

    def test_without_the_owner_login_it_only_says_how(self):
        self.assertFalse(self.bootstrap({}, N8N_EMAIL="").run())
        self.assertEqual(self.n8n.calls, [])
        self.assertIn("N8N_EMAIL=... N8N_PASSWORD=...", self.lines[0])

    def test_a_wrong_password_says_so(self):
        self.assertFalse(self.bootstrap({("POST", "/rest/login"): (401, {})}).run())
        self.assertEqual(len(self.n8n.calls), 1)

    def test_creates_the_credential_and_the_api_key(self):
        routes = {**self.LOGIN,
                  ("GET", "/rest/credentials"): (200, {"data": [{"id": "9", "type": "httpBasicAuth"}]}),
                  ("POST", "/rest/credentials"): (200, {"data": {"id": "c1"}}),
                  ("GET", "/rest/api-keys"): (200, {"data": [{"id": "k0", "label": "bpmn8n"}, {"id": "k1", "label": "ci"}]}),
                  ("POST", "/rest/api-keys"): (200, {"data": {"rawApiKey": "n8n-raw-key"}})}
        self.assertTrue(self.bootstrap(routes).run())
        posted = {(m, p): b for m, p, b, h in self.n8n.calls if m == "POST"}
        self.assertEqual(posted[("POST", "/rest/login")], {"emailOrLdapLoginId": "o@x.org", "password": "pw-secret"})
        self.assertEqual(posted[("POST", "/rest/credentials")], {
            "name": "bpmn8n (web:8000)", "type": "bpmn8nApi", "data": {"baseUrl": "http://web:8000", "apiToken": "tok"}})
        self.assertEqual(posted[("POST", "/rest/api-keys")], {"label": "bpmn8n", "scopes": SCOPES, "expiresAt": None})
        self.assertEqual([p for m, p, b, h in self.n8n.calls if m == "DELETE"], ["/rest/api-keys/k0"])  # its label only
        self.assertEqual(read_env(self.env), {"KEEP": "1", "N8N_CREDENTIAL": "c1", "N8N_API_KEY": "n8n-raw-key"})
        output = "\n".join(self.lines)
        self.assertIn("n8n API key created", output)
        self.assertNotIn("n8n-raw-key", output)
        self.assertNotIn("pw-secret", output)

    def test_a_working_setup_changes_nothing(self):
        headers = {"X-N8N-API-KEY": "good"}
        routes = {**self.LOGIN,
                  ("GET", "/rest/credentials"): (200, {"data": [{"id": "c1", "type": "bpmn8nApi"}]}),
                  ("GET", "/api/v1/workflows?limit=1"): (200, {"data": []}),
                  ("GET", "/api/v1/credentials?limit=1"): (200, {"data": []}),
                  ("PUT", "/api/v1/workflows/0"): (404, {})}
        self.assertFalse(self.bootstrap(routes, N8N_API_KEY="good", N8N_CREDENTIAL="c1").run())
        self.assertEqual([c[3] for c in self.n8n.calls if c[1].startswith("/api/v1")], [headers] * 3)
        self.assertEqual(self.env.read_text(), "KEEP=1\n")

    def test_a_key_that_cannot_update_workflows_is_replaced(self):
        routes = {**self.LOGIN,
                  ("GET", "/rest/credentials"): (200, {"data": [{"id": "c1", "type": "bpmn8nApi"}]}),
                  ("GET", "/api/v1/workflows?limit=1"): (200, {}), ("GET", "/api/v1/credentials?limit=1"): (200, {}),
                  ("PUT", "/api/v1/workflows/0"): (403, {}),
                  ("GET", "/rest/api-keys"): (200, {"data": {"items": [{"id": "k0", "label": "bpmn8n"}]}}),
                  ("POST", "/rest/api-keys"): (200, {"data": {"rawApiKey": "fresh"}})}
        self.assertTrue(self.bootstrap(routes, N8N_API_KEY="old", N8N_CREDENTIAL="c1").run())
        self.assertIn(("DELETE", "/rest/api-keys/k0", None, None), self.n8n.calls)
        self.assertEqual(read_env(self.env)["N8N_API_KEY"], "fresh")

    def test_import_publishes_again_what_was_published(self):
        routes = {("GET", "/rest/workflows"): (200, {"data": [{"id": "a", "active": True}, {"id": "b", "active": False}]}),
                  ("GET", "/rest/workflows/a"): (200, {"data": {"versionId": "v3"}}),
                  ("POST", "/rest/workflows/a/activate"): (200, {})}
        n8n = self.bootstrap(routes)
        n8n.publish(n8n.published())
        self.assertIn(("POST", "/rest/workflows/a/activate", {"versionId": "v3"}, None), self.n8n.calls)
        self.assertEqual(self.lines, ["✓ published again: a"])


class CommandsTest(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_deploy_and_diff_run_workflows_in_the_web_container(self):
        runner = Runner({"workflows diff": (1, "")})
        s = stack(self.tmp, runner)
        self.assertEqual(cli.main(["diff", "--json"], s), 1)  # drift: workflows' own exit code
        self.assertEqual(cli.main(["deploy", "projects/acme"], s), 0)
        self.assertEqual(runner.calls, ["docker compose exec -T web python manage.py workflows diff --json",
                                        "docker compose exec -T web python manage.py workflows push projects/acme"])

    def test_arguments(self):
        with mock.patch("sys.stderr", StringIO()):
            for argv in ([], ["start"], ["status", "--json"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    cli.main(argv, stack(self.tmp))
        self.assertEqual(set(cli.COMMANDS), {"up", "down", "status", "doctor", "logs", "export", "import", "deploy", "diff"})

    def test_status_says_what_is_down_and_exits_with_1(self):
        lines = []
        runner = Runner({"ps --status running": (0, "searxng\n")})
        http = Http(up={"http://localhost:8000/api/health"})
        self.assertEqual(cli.main(["status"], stack(self.tmp, runner, http, lines=lines)), 1)
        self.assertEqual(runner.calls[0], "docker compose --profile cpu-models ps")
        self.assertTrue(any(line.startswith("✓ web") for line in lines))
        self.assertTrue(any(line.startswith("✗ n8n") and "refused" in line for line in lines))

    def test_a_failing_command_is_reported(self):
        err = StringIO()
        with mock.patch("sys.stderr", err):
            self.assertEqual(cli.main(["export"], stack(self.tmp, Runner({"export:workflow": (3, "")}))), 3)
        self.assertIn("exit 3", err.getvalue())

    def test_down_stops_everything_in_reverse(self):
        runner = Runner()
        cli.main(["down"], stack(self.tmp, runner))
        self.assertEqual(runner.calls[0], "docker compose stop searxng")
        self.assertEqual(runner.calls[-1], "docker compose stop postgres")

    def test_up(self):
        (Path(self.tmp) / "n8n-nodes-bpmn8n" / "node_modules").mkdir(parents=True)
        runner = Runner({"SELECT 1": (0, "1")})
        http = Http(up={"http://localhost:8000/api/health", "http://localhost:5678/rest/settings",
                        "http://127.0.0.1:8001/health"})
        n8n = FakeN8n({("POST", "/rest/login"): (200, {}),
                       ("GET", "/rest/credentials"): (200, {"data": [{"id": "c1", "type": "bpmn8nApi"}]}),
                       ("GET", "/rest/api-keys"): (200, {"data": []}),
                       ("POST", "/rest/api-keys"): (200, {"data": {"rawApiKey": "k"}})})
        env = {"N8N_EMAIL": "o@x.org", "N8N_PASSWORD": "pw", "PUBLIC_URL": "https://b.example.org"}
        lines = []
        with mock.patch.object(cli, "need"), mock.patch.object(cli, "N8nSession", return_value=n8n), \
                mock.patch.dict("os.environ", env, clear=True):
            self.assertEqual(cli.main(["up"], stack(self.tmp, runner, http, mac=True, lines=lines)), 0)
        compose = [c.removeprefix("docker compose ") for c in runner.calls if c.startswith("docker compose")]
        self.assertEqual(runner.calls[:2], ["docker info", "npm run build --silent"])
        self.assertEqual(compose[-9:], [
            "up -d redis", "up -d web", "up -d n8n n8n-worker", "up -d n8n-preview", "up -d searxng",
            "restart n8n n8n-worker", "restart n8n-preview", "exec -T web python manage.py migrate --noinput",
            "up -d web"])  # recreated: .env has the new key and credential
        self.assertEqual(read_env(Path(self.tmp) / ".env")["N8N_API_KEY"], "k")
        self.assertIn("n8n editor      https://b.example.org/n8n", "\n".join(lines))
