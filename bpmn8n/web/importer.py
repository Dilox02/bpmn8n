"""The import wizard (staff): upload a CSV or XES, preview it, say which column is which and how timestamps
read, see the readiness report, then write the events under a process name in one transaction. The upload waits
in a temp file the session names, so the steps need no database row.
ponytail: an abandoned upload stays in the temp folder (the OS clears it); a task could sweep it."""
import os
import tempfile
import uuid
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import redirect, render
from django.urls import reverse

from ..analytics import importer
from ..models import AuditEntry

FOLDER = os.path.join(tempfile.gettempdir(), "bpmn8n-imports")
PREVIEW = 8


def stash(request, upload):
    """The upload kept for the next steps, named by the session."""
    drop(request)
    os.makedirs(FOLDER, mode=0o700, exist_ok=True)
    token = uuid.uuid4().hex
    with open(os.path.join(FOLDER, token), "wb") as f:
        for chunk in upload.chunks():
            f.write(chunk)
    request.session["import"] = {"token": token, "name": upload.name}


def drop(request):
    kept = request.session.pop("import", None)
    if kept:
        try:
            os.remove(os.path.join(FOLDER, kept["token"]))
        except OSError:
            pass


def load(request):
    """(headers, rows) of the kept upload; None when there is none (or it was swept)."""
    kept = request.session.get("import")
    if not kept:
        return None
    try:
        with open(os.path.join(FOLDER, kept["token"]), "rb") as f:
            return importer.read_table(f.read(), kept["name"])
    except OSError:
        return None


@staff_member_required
def import_log(request):
    context = {"formats": importer.FORMATS, "roles": importer.ROLES}
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "upload":
            upload = request.FILES.get("file")
            if not upload:
                messages.error(request, "Choose a file.")
            elif upload.size > importer.MAX_BYTES:
                messages.error(request, f"The file is larger than {importer.MAX_BYTES // 2**20} MB: use manage.py import_eventlog.")
            else:
                stash(request, upload)
                try:
                    importer.read_table(b"".join(upload.chunks()), upload.name)
                except importer.LogImportError as e:
                    drop(request)
                    messages.error(request, str(e))
        elif action == "cancel":
            drop(request)
        elif action in ("check", "import"):
            return step(request, action, context)
    return render(request, "bpmn8n/insights_import.html", wizard(request, context))


def wizard(request, context, roles=None, fmt="auto", process="", report=None):
    """The page's context: the preview of the kept upload, each column with its role, the choices so far."""
    table = load(request)
    if table:
        headers, rows = table
        guessed = roles or importer.guess_roles(headers)
        context.update(
            name=request.session["import"]["name"], total=len(rows), preview=[[r.get(h, "") for h in headers] for r in rows[:PREVIEW]],
            columns=[{"index": i, "header": h, "role": guessed.get(h, "attribute"), "sample": rows[0].get(h, "")}
                     for i, h in enumerate(headers)], headers=headers, fmt=fmt, process=process, report=report)
    return context


def step(request, action, context):
    table = load(request)
    if not table:
        messages.error(request, "The file is gone: upload it again.")
        return redirect(reverse("insights_import"))
    headers, rows = table
    post = request.POST
    roles = {h: post.get(f"role_{i}", "attribute") for i, h in enumerate(headers)}
    roles = {h: r if r in importer.ROLES else "attribute" for h, r in roles.items()}
    fmt, process = post.get("fmt_custom", "").strip() or post.get("fmt", "auto"), post.get("process", "").strip()
    report = None
    try:
        events, report = importer.prepare(rows, importer.Mapping.from_roles(roles), fmt)
        if action == "import":
            if not process:
                raise importer.LogImportError("Name the process these events belong to.")
            if not events:
                raise importer.LogImportError("Nothing to import: no row has a case, an activity and a timestamp.")
            written = importer.write(events, process)
            AuditEntry.write("eventlog.import", object_type="eventlog", object_id=process, events=written,
                             cases=report["cases"], file=request.session["import"]["name"])
            drop(request)
            messages.success(request, f"Imported {written} events of {report['cases']} cases as {process}.")
            return redirect(reverse("insights") + "?" + urlencode({"process": process}))
    except importer.LogImportError as e:
        messages.error(request, str(e))
    return render(request, "bpmn8n/insights_import.html", wizard(request, context, roles, fmt, process, report))
