"""Production settings, JSON logs, /api/health, `manage.py doctor` and the Prometheus metrics."""
import json
import logging
import re
import sys
import time
from io import StringIO
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from ..models import Case, MessageSubscription, Project, UserTask
from ..ops import health
from ..ops.logs import JsonFormatter
from ..settings import production
from .support import add_events, step

PROD = {"SECRET_KEY": "a-long-random-production-key", "BPMN8N_API_TOKEN": "token", "ALLOWED_HOSTS": "bpmn8n.example.org,web"}


class ProductionSettingsTests(TestCase):
    def test_refuses_an_open_server(self):
        for env, reason in (({**PROD, "BPMN8N_API_TOKEN": ""}, "BPMN8N_API_TOKEN"),
                            ({k: v for k, v in PROD.items() if k != "SECRET_KEY"}, "SECRET_KEY"),
                            ({**PROD, "ALLOWED_HOSTS": "*"}, "ALLOWED_HOSTS"),
                            ({**PROD, "ALLOWED_HOSTS": "web,*"}, "not \\*"),
                            ({**PROD, "ALLOWED_HOSTS": ""}, "ALLOWED_HOSTS")):
            with self.subTest(env=env), self.assertRaisesRegex(ImproperlyConfigured, reason):
                production(env)

    def test_secure_defaults(self):
        settings = production(PROD)
        self.assertEqual(settings["ALLOWED_HOSTS"], ["bpmn8n.example.org", "web", "localhost"])
        self.assertTrue(settings["SESSION_COOKIE_SECURE"] and settings["CSRF_COOKIE_SECURE"])
        self.assertEqual(settings["SECURE_HSTS_SECONDS"], 31_536_000)
        self.assertTrue(settings["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertFalse(settings["SECURE_HSTS_PRELOAD"] or settings["SECURE_SSL_REDIRECT"])
        self.assertNotIn("SECURE_PROXY_SSL_HEADER", settings)
        self.assertIn("Manifest", settings["STORAGES"]["staticfiles"]["BACKEND"])

    def test_behind_a_tls_proxy(self):
        settings = production({**PROD, "SECURE_PROXY_SSL_HEADER": "X-Forwarded-Proto", "SECURE_SSL_REDIRECT": "1",
                               "HSTS_SECONDS": "600", "HSTS_PRELOAD": "1"})
        self.assertEqual(settings["SECURE_PROXY_SSL_HEADER"], ("HTTP_X_FORWARDED_PROTO", "https"))
        self.assertTrue(settings["SECURE_SSL_REDIRECT"] and settings["SECURE_HSTS_PRELOAD"])
        self.assertEqual(settings["SECURE_HSTS_SECONDS"], 600)
        self.assertEqual(settings["SECURE_REDIRECT_EXEMPT"], [r"^api/"])  # n8n calls the API over HTTP


class JsonLogTests(TestCase):
    def test_one_object_per_line_with_extra_fields_and_the_exception(self):
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.getLogger("bpmn8n").makeRecord(
                "bpmn8n", logging.ERROR, __file__, 1, "task %s failed", ("check",), sys.exc_info(),
                extra={"case_id": "wf:7"})
        line = JsonFormatter().format(record)
        self.assertNotIn("\n", line)
        entry = json.loads(line)
        self.assertEqual((entry["level"], entry["logger"], entry["message"], entry["case_id"]),
                         ("ERROR", "bpmn8n", "task check failed", "wf:7"))
        self.assertIn("ValueError: boom", entry["exception"])
        self.assertNotIn("args", entry)


def up(*args):
    pass


def down(*args):
    raise OSError("http://secret-host:5678/healthz refused")


@override_settings(REDIS_URL="redis://redis:6379/0", SEARCH_URL="http://searxng:8080", API_TOKEN="")
class HealthTests(TestCase):
    def health(self, n8n=up, models=up, redis=up, database=None):
        def http_get(url):
            (n8n if "healthz" in url and "searxng" not in url else models if url.endswith("/health") else up)(url)
        with mock.patch.object(health, "http_get", http_get), mock.patch.object(health, "redis_ping", redis), \
                mock.patch.object(health, "database", database or health.database):
            return self.client.get("/api/health")

    def test_all_up(self):
        response = self.health()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(set(body["checks"]), {"database", "redis", "n8n", "laya", "extractor", "search"})
        self.assertTrue(all(c["status"] == "up" and c["ms"] >= 0 for c in body["checks"].values()))

    def test_a_dependency_down_degrades_without_saying_where_it_is(self):
        for kwargs, name in (({"n8n": down}, "n8n"), ({"models": down}, "laya"), ({"redis": down}, "redis")):
            with self.subTest(name):
                response = self.health(**kwargs)
                self.assertEqual(response.status_code, 200)  # restarting web would not help
                self.assertEqual(response.json()["status"], "degraded")
                self.assertEqual(response.json()["checks"][name]["status"], "down")
                self.assertNotIn(b"secret-host", response.content)
                self.assertNotIn(b"refused", response.content)

    def test_database_down_is_503(self):
        response = self.health(database=down)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "down")

    @override_settings(REDIS_URL="", SEARCH_URL="")
    def test_what_is_not_configured_is_off(self):
        checks = self.health().json()["checks"]
        self.assertEqual((checks["redis"], checks["search"]), ({"status": "off"}, {"status": "off"}))

    @override_settings(API_TOKEN="secret")
    def test_health_needs_no_token(self):
        self.assertEqual(self.health().status_code, 200)

    def test_checks_run_in_parallel_with_a_timeout(self):
        self.assertLessEqual(health.TIMEOUT, 2)
        with mock.patch.object(health, "http_get", lambda url: time.sleep(0.3)):
            started = time.perf_counter()
            health.run()
        self.assertLess(time.perf_counter() - started, 0.9)  # 4 HTTP checks, not 1.2 s


@override_settings(REDIS_URL="", SEARCH_URL="http://searxng:8080", N8N_API_KEY="key", N8N_CREDENTIAL="abc",
                   API_TOKEN="token")
class DoctorTests(TestCase):
    def doctor(self, http_get=up):
        out = StringIO()
        with mock.patch.object(health, "http_get", http_get):
            try:
                call_command("doctor", stdout=out)
            except CommandError as e:
                return out.getvalue(), str(e)
        return out.getvalue(), None

    def test_all_up(self):
        out, error = self.doctor()
        self.assertIsNone(error)
        self.assertRegex(out, r"database\s+up\s+[\d.]+")
        self.assertRegex(out, r"redis\s+off\s+not configured")
        self.assertIn("overall: ok", out)

    def test_n8n_down_fails_with_the_reason(self):
        out, error = self.doctor(lambda url: down() if url.endswith("/healthz") and "searxng" not in url else None)
        self.assertEqual(error, "required and down: n8n")
        self.assertIn("refused", out)  # the operator sees why

    def test_models_down_is_not_fatal(self):
        out, error = self.doctor(lambda url: down() if url.endswith("/health") else None)
        self.assertIsNone(error)
        self.assertIn("overall: degraded", out)

    def test_hints_from_the_configuration(self):
        out, _ = self.doctor()
        self.assertIn("no default LLM provider", out)
        self.assertNotIn("N8N_API_KEY", out)
        with override_settings(N8N_API_KEY="", N8N_CREDENTIAL="", API_TOKEN="", DEBUG=True, SEARCH_URL=""):
            out, _ = self.doctor()
        for hint in ("no N8N_API_KEY", "no N8N_CREDENTIAL", "the API is open to anyone", "no SEARCH_URL"):
            self.assertIn(hint, out)


# a sample line: name{label="value",...} number (the exposition format's grammar, without timestamps)
SAMPLE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*"'
                    r'(,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\\n]|\\.)*")*\})? -?[0-9.e+-]+$')
META = re.compile(r"^# (HELP [a-zA-Z_:][a-zA-Z0-9_:]* .+|TYPE [a-zA-Z_:][a-zA-Z0-9_:]* (counter|gauge|summary))$")


class PrometheusTests(TestCase):
    def setUp(self):
        add_events(("1:1", "Expenses", [step("Check Policy", 0, 1500), step("Check Policy", 1, 500, "error")]),
                   ("1:2", "Expenses", [step('Say "hi"\\now', 0, 250)]))
        Case.objects.create(case_id="1:1", process="Expenses")
        Case.objects.create(case_id="1:2", process="Expenses")
        for status in ("open", "open", "claimed", "completed"):
            UserTask.objects.create(activity="Approve", title="Approve", lane="managers", status=status, resume_url="x")
        MessageSubscription.objects.create(activity="Wait", name="payment", correlation_key="42", resume_url="x")
        Project.objects.create(name="Claims", status=Project.READY)

    def metrics(self, **headers):
        return self.client.get("/api/metrics/prometheus", **headers)

    def test_exposition_format(self):
        response = self.metrics()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/plain; version=0.0.4"))
        text = response.content.decode()
        self.assertTrue(text.endswith("\n"))
        for line in text.splitlines():
            self.assertTrue(META.match(line) or SAMPLE.match(line), line)

    def test_series(self):
        text = self.metrics().content.decode()
        for sample in ('bpmn8n_endpoint_calls_total{endpoint="check policy"} 2',
                       'bpmn8n_endpoint_errors_total{endpoint="check policy"} 1',
                       'bpmn8n_endpoint_duration_seconds_sum{endpoint="check policy"} 2',
                       'bpmn8n_endpoint_duration_seconds_count{endpoint="check policy"} 2',
                       'bpmn8n_endpoint_calls_total{endpoint="say \\"hi\\"\\\\now"} 1',  # escaped
                       'bpmn8n_cases_started_total{process="Expenses"} 2',
                       'bpmn8n_user_tasks_waiting{lane="managers",status="claimed"} 1',
                       'bpmn8n_user_tasks_waiting{lane="managers",status="open"} 2',
                       'bpmn8n_message_subscriptions_open{message="payment"} 1',
                       'bpmn8n_studio_projects{status="ready"} 1',
                       "# TYPE bpmn8n_endpoint_duration_seconds summary"):
            self.assertIn(sample + "\n", text)
        self.assertNotIn('status="completed"', text)

    @override_settings(API_TOKEN="secret")
    def test_needs_the_api_token(self):
        self.assertEqual(self.metrics().status_code, 401)
        self.assertEqual(self.metrics(HTTP_AUTHORIZATION="Bearer secret").status_code, 200)
