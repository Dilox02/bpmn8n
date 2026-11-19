"""n8n's side of `bpmn8n up`: as n8n's owner (N8N_EMAIL, N8N_PASSWORD) it creates the credential bpmn8n's nodes
use and an API key with the scopes bpmn8n needs, and writes their id and key to .env, which every later
`docker compose …` reads. Also republishes the workflows an import unpublished. Secrets go to .env, never to the
output."""
import json
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .services import update_env

N8N = "http://localhost:5678"
CREDENTIAL = {"name": "bpmn8n (web:8000)", "type": "bpmn8nApi"}
KEY_LABEL = "bpmn8n"
# process mining reads workflows, Studio creates and updates them (never activates) and gives their LLM agents
# the credential of admin → Settings' LLM
SCOPES = ["workflow:read", "workflow:list", "workflow:create", "workflow:update",
          "credential:list", "credential:create", "credential:update"]
OWNER_HINT = "create the owner account at http://localhost:5678, then: N8N_EMAIL=... N8N_PASSWORD=... ./start.sh"


class N8nSession:
    """n8n over HTTP, keeping the owner's session cookie: call(method, path, body, headers) -> (status, JSON)."""

    def __init__(self, url=N8N):
        self.url, self.opener = url, build_opener(HTTPCookieProcessor(CookieJar()))

    def __call__(self, method, path, body=None, headers=None):
        request = Request(self.url + path, method=method, data=None if body is None else json.dumps(body).encode(),
                          headers={"content-type": "application/json", **(headers or {})})
        try:
            with self.opener.open(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except HTTPError as e:
            status, raw = e.code, e.read()
        except URLError:
            return 0, {}
        try:
            return status, json.loads(raw) if raw else {}
        except ValueError:
            return status, {}


def ok(status):
    return 200 <= status < 300


class Bootstrap:
    def __init__(self, http, config, env_file, say=print):
        self.http, self.config, self.env_file, self.say = http, config, env_file, say
        self.changed = False  # .env changed: web must be recreated to read it

    def run(self):
        """True when .env changed."""
        if self.login():
            self.credential()
            self.api_key()
        return self.changed

    def login(self):
        email = self.config.N8N_EMAIL
        if email and ok(self.http("POST", "/rest/login", {"emailOrLdapLoginId": email,
                                                           "password": self.config.N8N_PASSWORD})[0]):
            return True
        self.say(f"! n8n: {OWNER_HINT}")
        return False

    def credential(self):
        """The credential bpmn8n's nodes reach the API with; its id goes to .env for Studio."""
        found = self.http("GET", "/rest/credentials")[1].get("data", [])
        cid = next((c["id"] for c in found if c.get("type") == CREDENTIAL["type"]), "")
        if not cid:
            status, data = self.http("POST", "/rest/credentials", {**CREDENTIAL, "data": {
                "baseUrl": "http://web:8000", "apiToken": self.config.BPMN8N_API_TOKEN}})
            cid = ok(status) and data.get("data", {}).get("id") or ""
            if cid:
                self.say(f"✓ n8n credential '{CREDENTIAL['name']}' created")
        if cid and cid != self.config.N8N_CREDENTIAL:
            update_env(self.env_file, {"N8N_CREDENTIAL": cid})
            self.changed = True

    def api_key(self):
        """A working key stays; otherwise the old one with bpmn8n's label goes (labels are unique) and a new one
        is created."""
        if self.config.N8N_API_KEY and self.key_works(self.config.N8N_API_KEY):
            return
        keys = self.http("GET", "/rest/api-keys")[1].get("data", [])
        for key in keys.get("items", []) if isinstance(keys, dict) else keys:
            if key.get("label") == KEY_LABEL:
                self.http("DELETE", f"/rest/api-keys/{key['id']}")
        status, data = self.http("POST", "/rest/api-keys", {"label": KEY_LABEL, "scopes": SCOPES, "expiresAt": None})
        raw = ok(status) and data.get("data", {}).get("rawApiKey")
        if not raw:
            self.say("! n8n API key not created: conformance checking and Studio's import stay off")
            return
        update_env(self.env_file, {"N8N_API_KEY": raw})
        self.changed = True
        self.say("✓ n8n API key created (workflows: read, list, create, update; credentials: list, create, update)")

    def key_works(self, key):
        """Reads workflows and credentials, and may update workflows (a key from before Studio did gets 403)."""
        headers = {"X-N8N-API-KEY": key}
        return (ok(self.http("GET", "/api/v1/workflows?limit=1", headers=headers)[0])
                and ok(self.http("GET", "/api/v1/credentials?limit=1", headers=headers)[0])
                and self.http("PUT", "/api/v1/workflows/0", {}, headers)[0] != 403)

    def published(self):
        """Ids of the active workflows."""
        return [w["id"] for w in self.http("GET", "/rest/workflows")[1].get("data", []) if w.get("active")]

    def publish(self, workflow_ids):
        """Publishes these workflows again (n8n's import unpublishes them)."""
        for wid in workflow_ids:
            version = self.http("GET", f"/rest/workflows/{wid}")[1].get("data", {}).get("versionId")
            if ok(self.http("POST", f"/rest/workflows/{wid}/activate", {"versionId": version})[0]):
                self.say(f"✓ published again: {wid}")
