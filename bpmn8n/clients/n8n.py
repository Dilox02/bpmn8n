"""n8n's public API (/api/v1 with N8N_API_KEY): read workflows (conformance, checks), create and update
them (Studio)."""
from urllib.parse import urlencode

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
