"""Writes use the BPMN credential; browser reads verify n8n's workflow permissions."""
import json
from urllib.parse import quote

from django.http import StreamingHttpResponse
from ninja import Query, Router
from ninja.errors import HttpError

from ..clients.http import ServiceUnavailable, request_json
from ..conf import settings
from ..core.evaluations import EvaluationStore, Sample
from .conditional import conditional, revision
from .stream import ChangeStream

router = Router(tags=["evaluations"])


def evaluation_read_auth(request):
    from . import token_auth
    if token_auth(request):
        return True
    # Preserve n8n's browser binding when checking access to this workflow.
    # The editor never receives the backend's API token.
    workflow_id = request.resolver_match.kwargs.get("workflow_id", "")
    cookie = request.COOKIES.get("n8n-auth", "")
    if not cookie:
        return False
    headers = {"Cookie": f"n8n-auth={cookie}"}
    browser_id = request.headers.get("browser-id") or request.GET.get("browserId", "")
    if browser_id:
        headers["browser-id"] = browser_id
    try:
        EvaluationStore.identifier(workflow_id)
        request_json("GET", f"{settings.N8N_URL}/rest/workflows/{quote(workflow_id, safe='')}",
                     headers=headers, timeout=10)
        return True
    except (ValueError, ServiceUnavailable):
        return False


@router.post("/samples")
def record_sample(request, payload: Sample):
    try:
        return EvaluationStore().write(payload)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None


@router.get("/{workflow_id}/stream", auth=evaluation_read_auth)
def stream(request, workflow_id: str):
    try:
        EvaluationStore.identifier(workflow_id)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    return ChangeStream(lambda: EvaluationStore().version(workflow_id)).response(request)


@router.get("/{workflow_id}/context", auth=evaluation_read_auth)
def context(request, workflow_id: str):
    store = EvaluationStore()
    try:
        runs = store.runs(workflow_id)
        folder = store.folder(runs[0]["project"]) if runs else None
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    paths = [folder / name for name in ("workflow.json", "evaluations.json")] if folder else []
    tag = revision([(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in paths if p.is_file() and not p.is_symlink()])
    if request.headers.get("If-None-Match") == tag:
        return conditional(request, {}, tag)
    workflow, description = {}, {}
    for path in paths:
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    if path.name == "workflow.json":
                        workflow = value
                    else:
                        description = value
            except (ValueError, OSError):
                continue
    return conditional(request, dict(title=description.get("title", workflow.get("name", "Valutazioni del workflow")),
        description=description.get("description", "Follow the workflow's metrics over time and check its thresholds."),
        stages=description.get("stages", []), metrics=description.get("metrics", {}), progress=description.get("progress"),
        preferred_run_label=description.get("preferred_run_label")), tag)


@router.get("/{workflow_id}/runs", auth=evaluation_read_auth)
def runs(request, workflow_id: str):
    try:
        data = {"runs": EvaluationStore().runs(workflow_id)}
        return conditional(request, data, revision(data))
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None


@router.get("/{workflow_id}/runs/{project}/{execution_id}", auth=evaluation_read_auth)
def run(request, workflow_id: str, project: str, execution_id: str,
        limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), points: int = Query(150, ge=1, le=200)):
    try:
        store = EvaluationStore()
        metadata = store.metadata(project, workflow_id, execution_id)
        etag = revision([metadata, limit, offset, points])
        if metadata and request.headers.get("If-None-Match") == etag:
            return conditional(request, {}, etag)
        result = store.read(project, workflow_id, execution_id, limit, offset, points)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    if not result["sample_count"]:
        raise HttpError(404, "Evaluation run not found")
    return conditional(request, result, etag)


@router.get("/{workflow_id}/runs/{project}/{execution_id}/export", auth=evaluation_read_auth)
def export(request, workflow_id: str, project: str, execution_id: str):
    store = EvaluationStore()
    try:
        metadata = store.metadata(project, workflow_id, execution_id)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    if not metadata:
        raise HttpError(404, "Evaluation run not found")
    response = StreamingHttpResponse(store.export(project, workflow_id, execution_id), content_type="application/json")
    response["Content-Disposition"] = f'attachment; filename="evaluation-{workflow_id}-{execution_id}.json"'
    return response
