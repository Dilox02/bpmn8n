"""Workflow history: every version of an n8n workflow bpmn8n has seen, kept as long as you like (n8n keeps
24 hours for free, longer is paid), the version each case runs (Case.process_version), and restore.

A version is n8n's `versionId`. n8n 2 also has the published version (`activeVersionId`): production
cases run it, so a case records it when there is one. n8n's public API shows only the current versions,
so bpmn8n sees a version when a case starts on it, or when `manage.py snapshot_workflows` runs (cron)."""
import logging
import time

from django.db.models import Count, Value
from django.db.models.functions import Left, StrIndex

from ..clients import n8n
from ..conf import settings
from ..models import Case, WorkflowVersion
from ..workflows.changes import content_hash

log = logging.getLogger("bpmn8n")

VERSION_TTL = 30  # seconds a workflow's current version is trusted: one n8n read per workflow, not per case
_current = {}  # workflow id -> (monotonic deadline, version id), per worker process
# what the public API accepts back in a workflow's settings when it is restored
RESTORED_SETTINGS = ("executionOrder", "errorWorkflow", "timezone", "saveManualExecutions", "saveExecutionProgress",
                     "saveDataErrorExecution", "saveDataSuccessExecution", "executionTimeout", "callerPolicy")


def record(data):
    """Keeps the versions an n8n workflow JSON carries, if unseen: the published one (n8n 2's activeVersion)
    and the saved one (versionId; without it, older n8n: its content hash). Returns the version new cases run."""
    workflow_id, versions = str(data["id"]), []
    active = data.get("activeVersion")
    if data.get("activeVersionId") and isinstance(active, dict) and active.get("nodes") is not None:
        versions.append((data["activeVersionId"], {**data, "nodes": active["nodes"], "connections": active.get("connections", {})}))
    versions.append((data.get("versionId") or f"sha256:{content_hash(data)[:16]}", data))
    kept = {vid: WorkflowVersion.objects.get_or_create(workflow_id=workflow_id, version_id=vid, defaults={
        "name": body.get("name", ""), "workflow": body, "content_hash": content_hash(body)})[0] for vid, body in versions}
    runs = data.get("activeVersionId") or versions[-1][0]
    return kept.get(runs) or WorkflowVersion(workflow_id=workflow_id, version_id=runs, name=data.get("name", ""))


def snapshot(workflow_id):
    """The version of the workflow new cases run, kept if unseen; None without N8N_API_KEY or when n8n
    cannot say."""
    data = n8n.workflow(workflow_id)
    return record(data) if data and data.get("id") is not None else None


def current_version(workflow_id):
    """The n8n version id new cases of the workflow run, "" when n8n cannot say: read at most once per
    VERSION_TTL per workflow (and per process), never without N8N_API_KEY."""
    if not settings.N8N_API_KEY or not workflow_id:
        return ""
    if (hit := _current.get(workflow_id)) and hit[0] > time.monotonic():
        return hit[1]
    try:
        version = snapshot(workflow_id)
    except Exception:  # a version label is never worth failing a case for
        log.exception("cannot read the version of workflow %s", workflow_id)
        version = None
    _current[workflow_id] = (time.monotonic() + VERSION_TTL, version.version_id if version else "")
    return _current[workflow_id][1]


def stamp(case_id):
    """Records on a new case the version of the workflow it runs ("<workflow id>:<execution id>")."""
    workflow_id, colon, _ = case_id.partition(":")
    if colon and (version := current_version(workflow_id)):
        Case.objects.filter(pk=case_id, process_version="").update(process_version=version)


def restore(version):
    """Saves `version` back to n8n as the workflow's newest version, never activating it (on n8n 2 an
    active workflow keeps running its published version: the restored one waits as a draft). Returns
    the new version."""
    data = version.workflow
    body = {"name": data.get("name") or version.name, "nodes": data["nodes"], "connections": data.get("connections", {}),
            "settings": {k: v for k, v in (data.get("settings") or {}).items() if k in RESTORED_SETTINGS}}
    try:
        n8n.request("PUT", f"/workflows/{version.workflow_id}", body)
    except n8n.N8nError as e:
        if not (e.status == 403 and "saved as a draft" in str(e)):  # n8n 2: kept as a draft, as wanted
            raise
    _current.pop(version.workflow_id, None)
    return snapshot(version.workflow_id)


def workflows_in_n8n():
    """Every workflow in n8n, as returned by its API (nodes included), page by page."""
    cursor = ""
    while True:
        page = n8n.request("GET", f"/workflows?limit=100{f'&cursor={cursor}' if cursor else ''}")
        yield from page.get("data", [])
        if not (cursor := page.get("nextCursor")):
            return


def seen_workflow_ids():
    """The workflows bpmn8n knows: from cases ("<workflow id>:…") and from earlier snapshots."""
    from_cases = workflow_case_counts().keys()
    return sorted({*from_cases, *WorkflowVersion.objects.values_list("workflow_id", flat=True)})


def workflow_case_counts():
    """{workflow id: number of cases}, from the case ids."""
    rows = (Case.objects.filter(case_id__contains=":")
            .annotate(workflow=Left("case_id", StrIndex("case_id", Value(":")) - 1))
            .values("workflow").annotate(n=Count("pk")).values_list("workflow", "n"))
    return dict(rows)
