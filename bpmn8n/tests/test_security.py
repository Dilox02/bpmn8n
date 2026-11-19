"""Security properties checked over every route, so a new endpoint or page cannot forget them:
the API token, login and staff on pages, CSRF on forms, no open redirect, no path traversal, escaped output."""
import json
import re

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver

from ..api import api
from ..models import Case, Event

# what answers without the API token, on purpose: health for probes, the element manifest and the OpenAPI document
# for the n8n editor (public metadata, no data)
PUBLIC_API = {"/api/health", "/api/elements", "/api/openapi.json", "/api/docs"}
# pages anyone may open
PUBLIC_PAGES = {"/", "/login/", "/logout/"}
TOKEN = "s3cret-token"
FRAMEWORK_PREFIXES = {"tasks", "gateways", "usertasks", "messages", "workflows", "evaluations", "executions", "analytics",
                      "variables", "history", "simulation", "events", "cases", "metrics", "eventlog.csv",
                      "processmining", "datasets", "health", "elements", "openapi.json", "docs", "audit"}
EVIL = '<script>alert("x")</script>'


def api_routes():
    """The framework's routes (and every @task's): a project may mount its own router (an external service's
    webhook that verifies its own signature), which is its business."""
    schema = api.get_openapi_schema()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if operation.get("operationId", "").startswith("projects_") or "/api/tasks/" not in path and \
                    path.split("/")[2] not in FRAMEWORK_PREFIXES:
                continue
            yield method.upper(), re.sub(r"\{[^}]+\}", "x1", path)


def page_routes(patterns=None, prefix=""):
    """Every Django page outside the API, the admin and static files, with dummy path arguments."""
    for p in patterns if patterns is not None else get_resolver().url_patterns:
        route = prefix + str(p.pattern)
        if route.startswith(("api/", "admin/", "static/", "^static")):
            continue
        if isinstance(p, URLResolver):
            yield from page_routes(p.url_patterns, route)
        elif isinstance(p, URLPattern):
            path = re.sub(r"<(?:int:)?(\w+)>", lambda m: "1" if "int:" in m.group(0) else "x1", route)
            yield "/" + re.sub(r"<\w+:(\w+)>", "x1", path)


@override_settings(API_TOKEN=TOKEN)
class ApiTokenTest(TestCase):
    def test_every_route_needs_the_token(self):
        """Without the token every route answers 401, except the public ones listed above."""
        open_routes = []
        for method, path in api_routes():
            if path in PUBLIC_API:
                continue
            response = getattr(self.client, method.lower())(path, content_type="application/json") if method != "GET" \
                else self.client.get(path)
            if response.status_code != 401:
                open_routes.append((method, path, response.status_code))
        self.assertEqual(open_routes, [])

    def test_a_wrong_token_is_refused(self):
        response = self.client.get("/api/usertasks", HTTP_AUTHORIZATION="Bearer nope")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.get("/api/usertasks", HTTP_AUTHORIZATION=f"Bearer {TOKEN}").status_code, 200)

    def test_the_public_routes_hold_no_case_data(self):
        secret = "case-data-8f3a91c2"  # not in any example: the OpenAPI document carries the tasks' examples
        Case.objects.create(case_id="wf:1", process="P", variables={"note": secret})
        for path in PUBLIC_API - {"/api/docs"}:
            self.assertNotIn(secret, self.client.get(path).content.decode(), path)


class PagesTest(TestCase):
    def setUp(self):
        self.person = User.objects.create_user("person", password="pw-123456")
        self.staff = User.objects.create_user("staff", password="pw-123456", is_staff=True)

    def test_every_page_needs_a_login(self):
        for path in page_routes():
            if path in PUBLIC_PAGES:
                continue
            response = self.client.get(path)
            with self.subTest(path):
                # a login redirect, or a page that does not take GET (405) or does not exist for dummy ids (404)
                self.assertTrue(response.status_code in (302, 404, 405) and
                                (response.status_code != 302 or "/login/" in response["Location"]),
                                (path, response.status_code, response.get("Location")))

    def test_staff_pages_refuse_other_people(self):
        self.client.force_login(self.person)
        for path in ("/studio/", "/simulation/", "/variables/", "/cases/"):
            with self.subTest(path):
                response = self.client.get(path)
                self.assertNotEqual(response.status_code, 200, path)

    def test_forms_need_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        for path in ("/theme/", "/variables/", "/simulation/", "/studio/", "/inbox/1/claim/"):
            with self.subTest(path):
                self.assertEqual(client.post(path, {"theme": "dark"}).status_code, 403, path)

    def test_the_theme_switch_does_not_redirect_elsewhere(self):
        response = self.client.post("/theme/", {"theme": "dark", "next": "https://evil.example/phish"})
        self.assertEqual(response["Location"], "/")
        response = self.client.post("/theme/", {"theme": "dark", "next": "//evil.example"})
        self.assertEqual(response["Location"], "/")

    def test_case_data_is_escaped(self):
        """Case variables, decisions and LLM answers come from outside: the pages print them as text."""
        Case.objects.create(case_id="wf:7", process=EVIL, variables={"note": EVIL, EVIL: 1})
        from django.utils import timezone
        now = timezone.now()
        Event.objects.create(case_id="wf:7", activity=EVIL, process=EVIL, endpoint="t", started=now, ended=now,
                             duration_ms=1, status="complete", resource=EVIL, data={"input": {"x": EVIL},
                                                                                    "output": {"y": EVIL}})
        self.client.force_login(self.staff)
        for path in ("/cases/wf:7/", f"/cases/?process={EVIL}", f"/insights/?process={EVIL}"):
            with self.subTest(path):
                body = self.client.get(path).content.decode()
                self.assertNotIn(EVIL, body, path)


@override_settings(API_TOKEN="")
class InputBoundsTest(TestCase):
    def test_variable_keys_cannot_escape_their_namespace(self):
        for key in ("../x", "a b", "a/b", "*", "a:b"):
            with self.subTest(key):
                response = self.client.put(f"/api/variables/{key}", json.dumps({"value": 1}),
                                           content_type="application/json")
                self.assertIn(response.status_code, (404, 422), key)

    def test_a_simulation_spec_is_never_written_outside_projects(self):
        staff = User.objects.create_user("s", password="pw-123456", is_staff=True, is_superuser=True)
        self.client.force_login(staff)
        from pathlib import Path
        from django.conf import settings
        for project in ("../bpmn8n", "../../tmp", "/etc", "acme/../../x"):
            with self.subTest(project):
                self.client.post("/simulation/", {"project": project, "save": "1", "spec": '{"unit": "minutes"}'})
                self.assertFalse((Path(settings.BASE_DIR).parent / "x" / "simulation.json").exists())
                self.assertFalse((Path(settings.BASE_DIR) / "bpmn8n" / "simulation.json").exists())

    def test_identifiers_in_paths_are_validated(self):
        for path in ("/api/evaluations/..%2F..%2Fetc/runs", "/api/history/..%2Fx", "/api/executions/a%2F..%2Fb/1/activity"):
            with self.subTest(path):
                self.assertIn(self.client.get(path).status_code, (401, 404, 422), path)
