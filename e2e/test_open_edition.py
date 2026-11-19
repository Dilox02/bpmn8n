"""The features that replace n8n's paid ones, and people's deadlines, on the running stack (n8n + Django + Redis)."""
import uuid

from conftest import activities, wait_open  # noqa: F401 (fixtures in conftest)

from bpmn8n.workflows import Workflow


def limit_check(credential, path, key):
    """Counts its cases in a global variable and routes by a limit every workflow shares."""
    return (
        Workflow(f"e2e global variables {key}", credential)
        .start("Request Received", path)
        .global_variable("Count Request", f"{key}.count", operation="increment")
        .condition_gateway("Over the Limit?", "exclusive", {"high": f"{{{{amount}}}} > {{{{vars.{key}.limit}}}}",
                                                            "low": f"{{{{amount}}}} <= {{{{vars.{key}.limit}}}}"},
                           decision_scope="technical")
        .manual_task("Review High Amount", f"Review {{{{amount}}}} ({key})", "", row=-1)
        .task("Notify Low Amount", "sendTask", "send_notification", to="e2e@acme.test", subject="low: {{amount}}")
        .flow("Request Received", "Count Request", "Over the Limit?")
        .flow("Over the Limit?", "Review High Amount", output="high")
        .flow("Over the Limit?", "Notify Low Amount", output="low")
    )


def test_global_variables_are_shared_by_every_case(deploy, start_case, n8n_api, web_api):
    from conftest import N8N
    key = f"e2e{uuid.uuid4().hex[:6]}"
    assert web_api.put(f"/api/variables/{key}.limit", data={"value": 1000}).ok
    path = deploy(limit_check, key=key)
    assert n8n_api.post(f"{N8N}/webhook/{path}", data={"amount": 1500}).ok
    # a person's step enters the event log when it is done: until then it is an open task
    review = wait_open(web_api, "/api/usertasks", lambda t: t["activity"] == "Review High Amount" and key in t["title"])
    assert review["title"] == f"Review 1500 ({key})"
    low = start_case(path, {"amount": 500}, until=lambda acts: "Notify Low Amount" in acts)
    assert activities(low)[-2:] == ["Over the Limit?", "Notify Low Amount"]
    assert web_api.get(f"/api/variables/{key}.count").json()["value"] == 2  # both cases counted, atomically
    # the limit changes for every workflow at once, with no redeploy
    assert web_api.put(f"/api/variables/{key}.limit", data={"value": 2000}).ok
    again = start_case(path, {"amount": 1500}, until=lambda acts: "Notify Low Amount" in acts)
    assert "Review High Amount" not in activities(again)
    assert web_api.post(f"/api/usertasks/{review['id']}/complete", data={}).ok
    for name in ("limit", "count"):
        web_api.delete(f"/api/variables/{key}.{name}")


def deadline(credential, path):
    """A person has a few seconds; then the case escalates through the task's timeout output."""
    return (
        Workflow("e2e deadline", credential)
        .start("Claim Received", path)
        .user_task("Approve Claim", "Approve claim {{claim}}", "", form=[("approved", "Approve", "boolean")],
                   due_in=(5, "seconds"), deadline=True, priority="urgent")
        .manual_task("Escalate Claim", "Escalate claim {{claim}}", "", row=1)
        .manual_task("Pay Claim", "Pay claim {{claim}}", "")
        .flow("Claim Received", "Approve Claim")
        .flow("Approve Claim", "Escalate Claim", output="timeout")
        .flow("Approve Claim", "Pay Claim", output="done")
    )


def test_a_deadline_escalates_through_the_timeout_output(deploy, n8n_api, web_api):
    from conftest import N8N
    path = deploy(deadline)
    claim = uuid.uuid4().hex[:6]
    assert n8n_api.post(f"{N8N}/webhook/{path}", data={"claim": claim}).ok
    task = wait_open(web_api, "/api/usertasks", lambda t: t["activity"] == "Approve Claim" and claim in t["title"])
    assert (task["priority"], task["deadline"]) == ("urgent", True) and task["due"]
    # n8n's wait tracker checks about once a minute: the timeout fires within ~70 s of the due time
    escalation = wait_open(web_api, "/api/usertasks", lambda t: t["activity"] == "Escalate Claim" and claim in t["title"],
                           timeout=180)
    expired = web_api.get("/api/usertasks", params={"status": "expired"}).json()
    assert task["id"] in [t["id"] for t in expired]  # the person can no longer complete it
    done = web_api.post(f"/api/usertasks/{task['id']}/complete", data={"approved": True})
    assert done.status == 410, done.text()
    waiting = web_api.get("/api/usertasks").json()
    assert not [t for t in waiting if t["activity"] == "Pay Claim" and claim in t["title"]]
    assert web_api.post(f"/api/usertasks/{escalation['id']}/complete", data={}).ok  # the escalation is a real task


def failing_notification(credential, path):
    """The notification lacks its recipient: the task fails, and its error output takes the case to a person."""
    return (
        Workflow("e2e error path", credential)
        .start("Order Received", path)
        .task("Notify Customer", "sendTask", "send_notification", subject="Order {{order}}", on_error="branch")
        .manual_task("Fix Notification", "Fix the notification of order {{order}}", "", row=1)
        .manual_task("Close Order", "Close order {{order}}", "")
        .flow("Order Received", "Notify Customer", "Close Order")
        .flow("Notify Customer", "Fix Notification", output="error")
    )


def test_a_failing_task_takes_its_error_output_to_a_person(deploy, n8n_api, web_api):
    from conftest import N8N
    path = deploy(failing_notification)
    order = uuid.uuid4().hex[:6]
    assert n8n_api.post(f"{N8N}/webhook/{path}", data={"order": order}).ok  # no "to": the task cannot run
    fix = wait_open(web_api, "/api/usertasks", lambda t: t["activity"] == "Fix Notification" and order in t["title"])
    assert not [t for t in web_api.get("/api/usertasks").json() if t["activity"] == "Close Order" and order in t["title"]]
    assert web_api.post(f"/api/usertasks/{fix['id']}/complete", data={}).ok
