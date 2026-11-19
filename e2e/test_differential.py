"""F5, the token engine against n8n: real cases of the four ACME processes run by n8n on the running stack
(Laya deciding, people and messages answering through the API), then each case is rebuilt from its event
log with record_scenario's logic and replayed on the local token engine. The engine must take the path n8n
took, step by step: what lets scenario tests, the deadlock search and the simulator stand in for n8n.

Only what already runs on the stack is used there (the API, the event log); the replay is this checkout's.
ponytail: the order is compared exactly; ACME's parallel branches are one step each, which n8n runs in output
order as the engine does. Branches of several steps interleave on the engine (it overlaps them in time) while
n8n runs one branch after the other: such a process would compare each branch's order instead."""
import csv
import io
import json
import os
import time
import uuid
from datetime import datetime, timezone

import pytest

from conftest import N8N
from test_usecases import ALERTS, TICKETS

from bpmn8n.workflows.graph import ProcessGraph
from projects.acme.processes import expense_approval, incident_response, library_lending, ticket_triage

# what people answer and which message comes, whatever Laya decides (an unsure gateway hands over to a person)
ANSWERS = {"Triage Ticket": {"team": "technical"}, "Approve Expense": {"approved": True, "note": "Strategic customer."},
           "Review Alert": {"action": "none needed"}, "Fetch Book": {}, "Check Out Book": {"due_date": "2026-11-20"}}
MESSAGE = ("hold-book", {"hold": True})  # the reader waits for the book on loan
EXPENSES = [{"employee": "ann@acme.test", "category": "meal", "amount": 180, "attendees": 2,
             "justification": "Dinner with the Contoso procurement team to close the 2027 renewal contract."},
            {"employee": "bob@acme.test", "category": "other", "amount": 95, "attendees": 1,
             "justification": "Birthday present for my nephew."}]
SCAN = {"source": "scanner", "user": "", "raw": "Weekly vulnerability scan completed: no new findings."}
PROCESSES = {
    "ticket triage": (ticket_triage, {}, lambda: [payload for payload, _ in TICKETS.values()]),
    "expense approval": (expense_approval, {}, lambda: EXPENSES),
    "incident response": (incident_response, {}, lambda: [*(payload for payload, _ in ALERTS.values()), SCAN]),
    "library lending": (library_lending, {"hold_wait": (2, "seconds")},
                        lambda: [{"request_id": f"REQ-{uuid.uuid4().hex[:6]}", "reader": "mario@acme.test", "book": "Dune"}]),
}
OVER = ("success", "error", "crashed", "canceled")


@pytest.fixture(scope="module")
def from_log():
    """record_scenario's logic, with Django on a throwaway in-memory database: the replay's tasks write nothing
    real. Playwright's sync API keeps an event loop in this thread, hence DJANGO_ALLOW_ASYNC_UNSAFE."""
    import django
    from django.conf import settings
    if not settings.configured:
        os.environ.pop("POSTGRES_HOST", None)
        os.environ.update(DJANGO_SETTINGS_MODULE="bpmn8n.settings", DB_PATH=":memory:", DJANGO_ALLOW_ASYNC_UNSAFE="true")
        django.setup()
    from django.core.management import call_command

    from bpmn8n.api.tasks import discover
    from bpmn8n.workflows.scenarios import from_log
    call_command("migrate", verbosity=0)
    discover()
    return from_log


def executions(n8n_api, workflow_id):
    filter_ = json.dumps({"workflowId": workflow_id})
    return {run["id"]: run["status"] for run in n8n_api.get("/rest/executions", params={"filter": filter_}).json()["data"]["results"]}


def run_case(n8n_api, web_api, workflow_id, path, payload):
    """Starts a case and answers its people and messages until n8n ends it: its case id."""
    known = executions(n8n_api, workflow_id)
    assert n8n_api.post(f"{N8N}/webhook/{path}", data=payload).ok
    deadline, execution, sent = time.monotonic() + 120, None, False
    while time.monotonic() < deadline:
        runs = executions(n8n_api, workflow_id)
        execution = execution or next((e for e in runs if e not in known), None)
        if execution and runs[execution] in OVER:
            assert runs[execution] == "success", f"n8n ended the case {runs[execution]}"
            return f"{workflow_id}:{execution}"
        case = f"{workflow_id}:{execution}"
        for task in web_api.get("/api/usertasks").json():
            if task["case_id"] == case:
                assert web_api.post(f"/api/usertasks/{task['id']}/complete", data=ANSWERS[task["activity"]]).ok
        key = payload.get("request_id")
        if key and not sent and any(s["correlation_key"] == key for s in web_api.get("/api/messages/subscriptions").json()):
            sent = web_api.post(f"/api/messages/{MESSAGE[0]}", data={"correlation_key": key, "payload": MESSAGE[1]}).ok
        time.sleep(0.5)
    raise AssertionError(f"case {execution} of {path} did not end within 120 s")


def logged(web_api, case_id, since):
    """The case's events in the log's order, as record() reads them from the database: the event log gives each
    one's endpoint and resource (a message's name), the activity API its input and output."""
    rows = [r for r in csv.DictReader(io.StringIO(web_api.get("/api/eventlog.csv", params={"since": since}).text()))
            if r["case_id"] == case_id]
    workflow, execution = case_id.split(":")
    url = f"/api/executions/{workflow}/{execution}/activity"
    events = sorted(web_api.get(url).json()["recent"], key=lambda e: (e["started"], e["id"]))
    assert [r["activity"] for r in rows] == [e["node_name"] for e in events]
    return [{"activity": r["activity"], "endpoint": r["endpoint"], "resource": r["resource"],
             "data": web_api.get(f"{url}/{e['id']}").json()} for r, e in zip(rows, events)]


@pytest.mark.parametrize("process", PROCESSES)
def test_the_engine_takes_the_path_n8n_took(process, from_log, create_workflow, credential, n8n_api, web_api, laya_ready):
    build, options, payloads = PROCESSES[process]
    path = f"e2e-{uuid.uuid4().hex[:8]}"
    workflow_id = create_workflow(build(credential, path=path, **options), publish=True)["id"]
    workflow = build(path=path, **options).as_json()  # the same nodes and flows, without the credential
    graph = ProcessGraph(workflow)
    for payload in payloads():
        since = datetime.now(timezone.utc).isoformat()
        case_id = run_case(n8n_api, web_api, workflow_id, path, payload)
        events = logged(web_api, case_id, since)
        scenario, report = from_log(events, web_api.get(f"/api/cases/{case_id}").json()["variables"], workflow, case_id)
        assert report["status"] == "completed", (case_id, report["failure"], scenario)
        # n8n logs bpmn8n's steps; the engine also passes the trigger, joins, timers and parallel gateways
        assert [n for n in report["path"] if graph.element(n).logged] == [e["activity"] for e in events], \
            (case_id, report["failure"], scenario)
