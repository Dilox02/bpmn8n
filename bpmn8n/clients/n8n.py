"""n8n's public API (/api/v1 with N8N_API_KEY): read workflows (conformance, checks), create and update
them (Studio); its editor's REST API (/rest, signed in) for accounts; and the links a browser follows to it."""
import json
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

from ..conf import settings
from .http import ServiceUnavailable, request_json


class N8nError(ServiceUnavailable):
    pass


def request(method, path, body=None):
    if not settings.N8N_API_KEY:
        raise N8nError("no N8N_API_KEY: ./start.sh creates one")
    try:
        return request_json(method, f"{settings.N8N_URL}/api/v1{path}", body,
                            {"X-N8N-API-KEY": settings.N8N_API_KEY}, timeout=10)
    except ServiceUnavailable as e:
        raise N8nError(f"n8n: {e}", e.status) from e


def workflow(workflow_id):
    """The workflow, None without N8N_API_KEY or if it no longer exists."""
    try:
        return request("GET", f"/workflows/{workflow_id}")
    except N8nError:
        return None


def create_workflow(data):
    """Creates it (not active) and returns it, with its id."""
    return request("POST", "/workflows", {k: data[k] for k in ("name", "nodes", "connections", "settings")})


def save_workflow(data, workflow_id=""):
    """A rebuild updates its workflow in place (same id, same webhook): a second copy could not be
    activated while the first holds the webhook path. Creates it the first time, or when it is gone."""
    if workflow_id:
        try:
            return request("PUT", f"/workflows/{workflow_id}", {k: data[k] for k in ("name", "nodes", "connections", "settings")})
        except N8nError as e:
            if e.status == 403 and "saved as a draft" in str(e):  # n8n 2: an active workflow's change waits
                return {"id": workflow_id, "draft": True}         # for someone to publish it (Studio never does)
            if e.status != 404:
                raise
    return create_workflow(data)


def pages(path, **query):
    """Every item of a listing, following n8n's nextCursor."""
    items, cursor = [], ""
    while True:
        page = request("GET", f"{path}?{urlencode({'limit': 100, **query, **({'cursor': cursor} if cursor else {})})}")
        items += page.get("data", [])
        if not (cursor := page.get("nextCursor")):
            return items


def workflows(name):
    """The workflows named `name` (archived ones left out)."""
    return [w for w in pages("/workflows", name=name) if w.get("name") == name and not w.get("isArchived")]


def credentials():
    """Every credential: id, name and type (never the secret)."""
    return pages("/credentials")


def ensure_credential(name, type_, data):
    """The id of the credential named `name`, created or updated with `data` (the key may have changed)."""
    found = next((c["id"] for c in credentials() if c["name"] == name and c["type"] == type_), None)
    if found:
        request("PATCH", f"/credentials/{found}", {"name": name, "type": type_, "data": data})
        return found
    return request("POST", "/credentials", {"name": name, "type": type_, "data": data})["id"]


def editor_url(path=""):
    """Where a browser opens `path` in n8n's editor: under N8N_PUBLIC_URL, an origin of its own or a path on
    bpmn8n's origin ("/n8n" behind the one-origin gateway, docs/GUIDE.md)."""
    return "/".join(filter(None, (settings.N8N_PUBLIC_URL.rstrip("/"), path.lstrip("/"))))


def rest(method, path, body=None, cookie=""):
    """n8n's editor REST API (/rest): returns its `data` and the n8n-auth cookie it set, if any. The cookie is
    Secure, so it is carried by hand: bpmn8n reaches n8n over plain HTTP inside the network."""
    request = urllib.request.Request(f"{settings.N8N_URL}/rest{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", **({"Cookie": cookie} if cookie else {})})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 (N8N_URL is configuration)
            data = json.loads(response.read() or b"{}").get("data")
            auth = [c.split(";")[0] for c in response.headers.get_all("Set-Cookie") or [] if c.startswith("n8n-auth=")]
            return data, auth[0] if auth else ""
    except urllib.error.HTTPError as e:
        raise N8nError(f"n8n {method} /rest{path}: HTTP {e.code} {e.read()[:200].decode(errors='replace')}", e.code) from e
    except OSError as e:
        raise N8nError(f"n8n {method} /rest{path}: {e}") from e


def _token(url):
    return parse_qs(urlsplit(url).query)["token"][0]


def sync_account(email, password, first_name, last_name, owner=None):
    """The n8n account of a bpmn8n staff user, with the same email and password (n8n Community Edition takes no
    identity from bpmn8n: docs/spikes/one-origin.md). A fresh n8n gets it as its owner; otherwise the owner
    (`owner`: email, password) signs in and creates it as a member, or sets its password. Returns what it did."""
    if rest("GET", "/settings")[0]["userManagement"].get("showSetupOnFirstLoad"):
        rest("POST", "/owner/setup", {"email": email, "password": password, "firstName": first_name, "lastName": last_name})
        return "owner created"
    if not owner:
        raise N8nError("n8n already has an owner: give its email and password (N8N_EMAIL, N8N_PASSWORD)")
    _, cookie = rest("POST", "/login", {"emailOrLdapLoginId": owner[0], "password": owner[1]})
    users = rest("GET", "/users", cookie=cookie)[0]["items"]
    found = next((u for u in users if u["email"].lower() == email.lower()), None)
    if found and found.get("role") == "global:owner":
        if found["email"].lower() != owner[0].lower():
            raise N8nError(f"{email} is n8n's owner: give its own password as the owner's")
        if password != owner[1]:
            rest("PATCH", "/me/password", {"currentPassword": owner[1], "newPassword": password}, cookie)
        return "owner updated"
    if found:
        link = rest("GET", f"/users/{found['id']}/password-reset-link", cookie=cookie)[0]["link"]
        rest("POST", "/change-password", {"token": _token(link), "password": password})
        return "member updated"
    invited = rest("POST", "/invitations", [{"email": email, "role": "global:member"}], cookie)[0][0]["user"]
    rest("POST", "/invitations/accept", {"token": _token(invited["inviteAcceptUrl"]), "firstName": first_name,
                                         "lastName": last_name, "password": password})
    return "member created"
