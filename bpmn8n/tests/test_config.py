"""B8: one configuration model, read from the environment, with the public addresses derived from PUBLIC_URL."""
import json
import re
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase
from pydantic import ValidationError

from .. import conf
from ..config import SECRETS, Config, markdown

PACKAGE = Path(__file__).resolve().parents[1]
GUIDE = PACKAGE.parent / "docs" / "GUIDE.md"


class ConfigTest(SimpleTestCase):
    def test_defaults_are_the_local_stack(self):
        c = Config.from_env({})
        self.assertEqual((c.BPMN8N_PUBLIC_URL, c.N8N_PUBLIC_URL, c.N8N_PREVIEW_URL),
                         ("http://localhost:8000", "http://localhost:5678", "http://localhost:5679"))
        self.assertEqual((c.N8N_URL, c.LAYA_URL, c.LAYA_TIMEOUT, c.EVENT_STREAM_MAXLEN),
                         ("http://localhost:5678", "http://localhost:8001/v1/systemone", 10.0, 100_000))
        self.assertTrue(c.DEBUG and c.HSTS_INCLUDE_SUBDOMAINS)
        self.assertFalse(c.HSTS_PRELOAD or c.SECURE_SSL_REDIRECT)
        self.assertEqual((c.ALLOWED_HOSTS, c.EVENT_WEBHOOKS, c.BPMN8N_API_TOKEN), (["*"], [], ""))

    def test_one_public_url_gives_the_three_addresses(self):
        c = Config.from_env({"PUBLIC_URL": "https://bpmn8n.example.org/"})
        self.assertEqual(c.BPMN8N_PUBLIC_URL, "https://bpmn8n.example.org")
        self.assertEqual(c.N8N_PUBLIC_URL, "https://bpmn8n.example.org/n8n")
        self.assertEqual(c.N8N_PREVIEW_URL, "https://preview.bpmn8n.example.org")
        self.assertEqual(c.compose_env(), {"BPMN8N_PUBLIC_URL": c.BPMN8N_PUBLIC_URL,
                                           "N8N_PUBLIC_URL": c.N8N_PUBLIC_URL, "N8N_PREVIEW_URL": c.N8N_PREVIEW_URL})

    def test_an_explicit_address_wins_and_an_empty_one_is_derived(self):
        c = Config.from_env({"PUBLIC_URL": "https://b.example.org", "N8N_PUBLIC_URL": "https://n8n.example.org",
                             "N8N_PREVIEW_URL": ""})  # compose passes ${N8N_PREVIEW_URL:-} as empty
        self.assertEqual(c.N8N_PUBLIC_URL, "https://n8n.example.org")
        self.assertEqual(c.N8N_PREVIEW_URL, "https://preview.b.example.org")
        self.assertEqual(Config.from_env({"N8N_PUBLIC_URL": "http://h:1"}).N8N_PUBLIC_URL, "http://h:1")

    def test_values_are_typed(self):
        c = Config.from_env({"LAYA_TIMEOUT": "2.5", "EVENT_WEBHOOKS": "http://a, http://b,,", "DEBUG": "0",
                             "HSTS_INCLUDE_SUBDOMAINS": "0", "HSTS_PRELOAD": "1", "ALLOWED_HOSTS": "a.org,web"})
        self.assertEqual((c.LAYA_TIMEOUT, c.EVENT_WEBHOOKS, c.ALLOWED_HOSTS), (2.5, ["http://a", "http://b"], ["a.org", "web"]))
        self.assertEqual((c.DEBUG, c.HSTS_INCLUDE_SUBDOMAINS, c.HSTS_PRELOAD), (False, False, True))
        with self.assertRaises(ValidationError):
            Config.from_env({"WEB_WORKERS": "many"})

    def test_secrets_are_masked(self):
        c = Config.from_env({"BPMN8N_API_TOKEN": "tok-123", "N8N_API_KEY": "", "POSTGRES_PASSWORD": "pw-456",
                             "N8N_URL": "http://n8n:5678"})
        shown = c.shown()
        self.assertEqual((shown["BPMN8N_API_TOKEN"], shown["N8N_API_KEY"], shown["POSTGRES_PASSWORD"]),
                         ("set", "empty", "set"))
        self.assertEqual(shown["N8N_URL"], "http://n8n:5678")
        self.assertEqual(set(shown), set(Config.model_fields))
        self.assertLessEqual(SECRETS, set(Config.model_fields))

    def test_the_command_prints_the_configuration_without_secrets(self):
        config = Config.from_env({"BPMN8N_API_TOKEN": "tok-123", "PUBLIC_URL": "https://b.example.org"})
        with mock.patch("bpmn8n.management.commands.config.CONFIG", config):
            outputs = {}
            for flags in ((), ("--env",), ("--json",)):
                out = StringIO()
                call_command("config", *flags, stdout=out)
                outputs[flags] = out.getvalue()
        self.assertFalse(any("tok-123" in text for text in outputs.values()))
        self.assertIn("N8N_PUBLIC_URL=https://b.example.org/n8n\n", outputs[("--env",)])
        self.assertIn("BPMN8N_API_TOKEN=set\n", outputs[("--env",)])
        self.assertEqual(json.loads(outputs[("--json",)])["N8N_PREVIEW_URL"], "https://preview.b.example.org")
        self.assertRegex(outputs[()], r"BPMN8N_API_TOKEN +set")

    def test_django_reads_bpmn8n_settings_through_it(self):
        self.assertEqual(set(conf.DEFAULTS), {
            "LAYA_URL", "LAYA_API_KEY", "EXTRACTOR_URL", "OCR_URL", "MODELS_API_KEY", "LAYA_TIMEOUT", "LAYA_BUSY_TIMEOUT",
            "API_TOKEN", "FIELD_ENCRYPTION_KEY", "N8N_URL", "N8N_PUBLIC_URL", "N8N_PREVIEW_URL", "N8N_API_KEY",
            "N8N_CREDENTIAL", "GUNICORN_PID", "SEARCH_URL", "REDIS_URL", "EVENT_STREAM_MAXLEN", "EVENT_WEBHOOKS"})
        self.assertEqual(Config.from_env({"BPMN8N_API_TOKEN": "t"}).app_settings()["API_TOKEN"], "t")

    def test_nothing_else_reads_the_environment(self):
        reads = re.compile(r"os\.environ\.get\(|os\.environ\[|os\.getenv\(")
        found = [str(p.relative_to(PACKAGE)) for p in PACKAGE.rglob("*.py")
                 if "tests" not in p.parts and p.name != "config.py" and reads.search(p.read_text())]
        self.assertEqual(found, [], "read the variable through bpmn8n/config.py")

    def test_the_guide_lists_every_variable(self):
        self.assertIn(markdown(), GUIDE.read_text(), "stale: paste `manage.py config --md` into docs/GUIDE.md")
