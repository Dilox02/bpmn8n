"""B1 one origin (the opt-in gateway) and B2's fallback (the same n8n account from the CLI): docs/spikes/one-origin.md."""
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from ..clients import n8n
from ..config import Config
from ..models import AuditEntry, Project

ROOT = Path(__file__).resolve().parents[2]
COMPOSE, CADDYFILE = (ROOT / "docker-compose.yml").read_text(), (ROOT / "deploy" / "Caddyfile").read_text()


class GatewayTest(SimpleTestCase):
    def test_public_url_turns_the_gateway_on_with_n8n_under_its_path(self):
        self.assertEqual(set(Config.from_env({}).compose_env()), {"BPMN8N_PUBLIC_URL", "N8N_PUBLIC_URL", "N8N_PREVIEW_URL"})
        local = Config.from_env({"PUBLIC_URL": "http://localhost:8090"}).compose_env()
        self.assertEqual({k: local[k] for k in ("COMPOSE_PROFILES", "GATEWAY_PORT", "N8N_PATH", "N8N_EDITOR_BASE_URL",
                                                "N8N_WEBHOOK_URL", "N8N_PUBLIC_URL", "N8N_PREVIEW_URL", "PREVIEW_ADDRESS",
                                                "N8N_PREVIEW_SECURE_COOKIE")},
                         {"COMPOSE_PROFILES": "one-origin", "GATEWAY_PORT": "8090", "N8N_PATH": "/n8n/",
                          "N8N_EDITOR_BASE_URL": "http://localhost:8090/n8n/", "N8N_WEBHOOK_URL": "http://localhost:8090/",
                          "N8N_PUBLIC_URL": "http://localhost:8090/n8n", "N8N_PREVIEW_URL": "http://preview.localhost:8090",
                          "PREVIEW_ADDRESS": "http://preview.localhost", "N8N_PREVIEW_SECURE_COOKIE": "false"})
        tls = Config.from_env({"PUBLIC_URL": "https://bpmn8n.example.org"}).compose_env()
        self.assertNotIn("GATEWAY_PORT", tls)  # behind the TLS proxy: the gateway keeps its local port
        self.assertEqual((tls["PREVIEW_ADDRESS"], tls["N8N_PREVIEW_SECURE_COOKIE"]),
                         ("http://preview.bpmn8n.example.org", "true"))

    def test_compose_reads_every_variable_the_config_hands_it(self):
        for name in Config.from_env({"PUBLIC_URL": "http://localhost:8090"}).compose_env():
            if name != "COMPOSE_PROFILES":  # docker compose's own
                self.assertIn("${" + name, COMPOSE, name)
        gateway = COMPOSE[COMPOSE.index("\n  gateway:"):]
        self.assertIn('profiles: ["one-origin"]', gateway)  # never started by default
        self.assertIn("./deploy/Caddyfile:/etc/caddy/Caddyfile:ro", gateway)
        hooks = re.search(r"\n      EXTERNAL_FRONTEND_HOOKS_URLS: (.+)", COMPOSE).group(1).split(";")
        self.assertTrue(all(h.startswith("${N8N_PATH:-/}static/bpmn8n-") for h in hooks), hooks)

    def test_the_caddyfile_routes_each_part(self):
        self.assertIn("handle_path /n8n/* {", CADDYFILE)  # n8n serves at its root: the prefix is stripped
        n8n_paths = re.search(r"@n8n path (.+)", CADDYFILE).group(1).split()
        self.assertTrue({"/webhook/*", "/webhook-waiting/*", "/form/*", "/form-waiting/*"} <= set(n8n_paths))
        self.assertIn("{$PREVIEW_ADDRESS:http://preview.localhost}", CADDYFILE)  # its own host
        self.assertIn("frame-ancestors 'self' {$BPMN8N_PUBLIC_URL", CADDYFILE)
        for name in set(re.findall(r"\{\$(\w+)", CADDYFILE)) - {"WEB_UPSTREAM", "N8N_UPSTREAM", "N8N_PREVIEW_UPSTREAM"}:
            self.assertIn(f"{name}: ", COMPOSE[COMPOSE.index("\n  gateway:"):], name)


class LinksAndCorsTest(TestCase):
    def test_links_to_the_editor_follow_one_setting(self):
        for public, expected in (("http://localhost:5678", "http://localhost:5678/workflow/w1"),
                                 ("http://localhost:8090/n8n/", "http://localhost:8090/n8n/workflow/w1"),
                                 ("/n8n", "/n8n/workflow/w1")):
            with self.subTest(public=public), override_settings(N8N_PUBLIC_URL=public):
                self.assertEqual(n8n.editor_url("/workflow/w1"), expected)
                self.assertEqual(Project(n8n_id="w1").n8n_url, expected)
        with override_settings(N8N_PUBLIC_URL="/n8n"):
            self.client.force_login(User.objects.create_user("sam", password="pw", is_staff=True))
            self.assertContains(self.client.get("/credentials/"), 'src="/n8n/home/credentials"')  # n8n inside bpmn8n, one origin

    @override_settings(API_TOKEN="secret", N8N_PUBLIC_URL="http://testserver/n8n")
    def test_same_origin_reads_get_no_cors_header_and_still_use_the_n8n_session(self):
        for path in ("/api/openapi.json", "/api/evaluations/workflow1/runs"):
            for origin in ({}, {"HTTP_ORIGIN": "http://testserver"}):
                with self.subTest(path=path, origin=origin):
                    self.client.cookies["n8n-auth"] = "session"
                    with patch("bpmn8n.api.evaluations.request_json", return_value={"data": {}}) as remote:
                        response = self.client.get(path, **origin)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse([h for h in response.headers if h.startswith("Access-Control-")])
                    if "evaluations" in path:
                        self.assertEqual(remote.call_args.kwargs["headers"], {"Cookie": "n8n-auth=session"})


class FakeN8n:
    """n8n's /rest for accounts: an owner (once set up) and its members."""

    def __init__(self, owner=None):
        self.users = [{"id": "1", "email": owner[0], "role": "global:owner", "password": owner[1]}] if owner else []
        self.calls = []

    def __call__(self, method, path, body=None, cookie=""):
        self.calls.append((method, path))
        signed_in = cookie == "n8n-auth=owner"
        if path == "/settings":
            return {"userManagement": {"showSetupOnFirstLoad": not self.users}}, ""
        if path == "/owner/setup":
            self.users.append({"id": "1", "email": body["email"], "role": "global:owner", "password": body["password"]})
            return {}, "n8n-auth=owner"
        if path == "/login":
            owner = self.users[0]
            if (body["emailOrLdapLoginId"], body["password"]) != (owner["email"], owner["password"]):
                raise n8n.N8nError("n8n POST /rest/login: HTTP 401", 401)
            return {}, "n8n-auth=owner"
        assert signed_in or path in ("/change-password", "/invitations/accept"), path
        user = next((u for u in self.users if path.startswith(f"/users/{u['id']}/")), None)
        if path == "/users":
            return {"items": [{k: u[k] for k in ("id", "email", "role")} for u in self.users]}, ""
        if path == "/invitations":
            self.users.append({"id": str(len(self.users) + 1), "email": body[0]["email"], "role": body[0]["role"]})
            return [{"user": {"id": self.users[-1]["id"], "inviteAcceptUrl": f"http://n8n/signup?token=t{self.users[-1]['id']}"}}], ""
        if path == "/invitations/accept" or path == "/change-password":
            self.users[int(body["token"][1:]) - 1]["password"] = body["password"]
            return {}, ""
        if user and path.endswith("/password-reset-link"):
            return {"link": f"http://n8n/change-password?token=t{user['id']}&mfaEnabled=false"}, ""
        if path == "/me/password":
            assert body["currentPassword"] == self.users[0]["password"]
            self.users[0]["password"] = body["newPassword"]
            return {}, ""
        raise AssertionError(path)


class SyncAccountTest(TestCase):
    def sync(self, fake, email, password, owner=None):
        with patch.object(n8n, "rest", fake):
            return n8n.sync_account(email, password, "Ann", "Lee", owner)

    def test_a_fresh_n8n_gets_the_user_as_its_owner(self):
        fake = FakeN8n()
        self.assertEqual(self.sync(fake, "ann@example.org", "Secret123"), "owner created")
        self.assertEqual(fake.users[0], {"id": "1", "email": "ann@example.org", "role": "global:owner", "password": "Secret123"})

    def test_the_owner_creates_a_member_then_resets_its_password(self):
        fake, owner = FakeN8n(("boss@example.org", "Boss1234")), ("boss@example.org", "Boss1234")
        self.assertEqual(self.sync(fake, "ann@example.org", "First123", owner), "member created")
        self.assertEqual(self.sync(fake, "ANN@example.org", "Second123", owner), "member updated")
        self.assertEqual([(u["email"], u["role"], u["password"]) for u in fake.users][1],
                         ("ann@example.org", "global:member", "Second123"))

    def test_the_owner_changes_only_its_own_password(self):
        fake = FakeN8n(("boss@example.org", "Boss1234"))
        self.assertEqual(self.sync(fake, "boss@example.org", "Boss5678", ("boss@example.org", "Boss1234")), "owner updated")
        self.assertEqual(fake.users[0]["password"], "Boss5678")
        with self.assertRaisesMessage(n8n.N8nError, "owner"):
            self.sync(FakeN8n(("boss@example.org", "Boss1234")), "ann@example.org", "x", None)  # no owner login

    def test_the_command_sets_the_same_password_on_both_sides_only_once_n8n_took_it(self):
        ann = User.objects.create_user("ann", email="ann@example.org", password="old", is_staff=True)
        User.objects.create_user("bob", email="bob@example.org", password="old")
        with self.assertRaisesMessage(CommandError, "no such staff user"):
            call_command("n8n_sync_owner", "bob", "--password", "New12345")
        failing = FakeN8n(("boss@example.org", "Boss1234"))
        with patch.object(n8n, "rest", failing), self.assertRaisesMessage(CommandError, "401"):
            call_command("n8n_sync_owner", "ann", "--password", "New12345", "--owner-email", "boss@example.org",
                         "--owner-password", "wrong")
        ann.refresh_from_db()
        self.assertTrue(ann.check_password("old"))
        out = StringIO()
        with patch.object(n8n, "rest", FakeN8n()):
            call_command("n8n_sync_owner", "ann", "--password", "New12345", stdout=out)
        ann.refresh_from_db()
        self.assertTrue(ann.check_password("New12345"))
        self.assertIn("owner created", out.getvalue())
        self.assertTrue(AuditEntry.objects.filter(action="user.n8n_sync", object_id=str(ann.pk)).exists())

    def test_rest_carries_the_secure_session_cookie_by_hand(self):
        class Reply:
            headers = type("H", (), {"get_all": lambda self, name: ["n8n-auth=abc; Path=/; HttpOnly; Secure"]})()
            def read(self): return b'{"data": {"id": "1"}}'  # noqa: E704
            def __enter__(self): return self  # noqa: E704
            def __exit__(self, *exc): return False  # noqa: E704
        with patch("urllib.request.urlopen", return_value=Reply()) as urlopen:
            self.assertEqual(n8n.rest("GET", "/users", cookie="n8n-auth=abc"), ({"id": "1"}, "n8n-auth=abc"))
        self.assertEqual(urlopen.call_args.args[0].get_header("Cookie"), "n8n-auth=abc")
