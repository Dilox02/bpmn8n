"""Platform checks: tasks discovered from OpenAPI, BPMN palette in n8n, real Laya gateways."""
from playwright.sync_api import expect

import uuid

from conftest import N8N, WEB
from projects.acme.processes import Workflow, node_type

BPMN_NODES = ("Service Task", "Script Task", "Business Rule Task", "Send Task", "Receive Task", "User Task",
              "Manual Task", "Exclusive Gateway", "Inclusive Gateway", "Parallel Gateway", "Timer Event")


def test_swagger_documents_bpmn_tasks(page):
    page.goto(f"{WEB}/api/docs")
    expect(page.get_by_text("/api/tasks/register_ticket", exact=True)).to_be_visible()
    expect(page.get_by_role("heading", name="businessRuleTask")).to_be_visible()


def test_palette_has_bpmn_nodes(n8n_page):
    n8n_page.goto("/workflow/new")
    n8n_page.get_by_test_id("node-creator-plus-button").click()
    n8n_page.get_by_test_id("node-creator-search-bar").fill("BPMN")  # codex alias finds them all
    results = n8n_page.get_by_test_id("node-creator-item-name")  # (n8n may add community nodes)
    for name in BPMN_NODES:
        expect(results.get_by_text(name, exact=True)).to_be_visible()
    n8n_page.keyboard.press("Escape")
    for name in BPMN_NODES:
        n8n_page.get_by_test_id("node-creator-plus-button").click()
        n8n_page.get_by_test_id("node-creator-search-bar").fill(name)
        expect(n8n_page.get_by_test_id("node-creator-item-name").get_by_text(name, exact=True)).to_be_visible()
        n8n_page.keyboard.press("Escape")


def test_task_node_reads_openapi(n8n_page, create_workflow, credential):
    """Picking a task fills the Input fields from the endpoint's request schema (H1)."""
    wf = create_workflow(Workflow("e2e discovery", credential)
                         .add("Start", "n8n-nodes-base.manualTrigger", {}, bpmn=False)
                         .add("Service Task", node_type("serviceTask"), {"task": ""})
                         .flow("Start", "Service Task"))
    n8n_page.goto(f"/workflow/{wf['id']}")
    n8n_page.get_by_test_id("canvas-node").filter(has_text="Service Task").dblclick()
    expect(n8n_page.get_by_test_id("credentials-label")).to_be_visible()  # NDV fully rendered
    n8n_page.get_by_test_id("parameter-input-task").locator("input").click()
    n8n_page.get_by_text("Register Ticket", exact=True).click()
    for field in ("Customer", "Message", "Channel", "Output"):  # n8n's fields, below bpmn8n's card (its "Output" heading)
        expect(n8n_page.get_by_test_id("node-parameters").get_by_text(field, exact=True).last).to_be_visible()


def test_exclusive_gateway_with_laya(web_api, laya_ready):
    r = web_api.post("/api/gateways/exclusive", data={
        "question": "Which team should handle this customer ticket?",
        "state": "I was charged twice for my subscription this month, please refund one payment.",
        "branches": {"billing": "payments, refunds", "technical": "bugs, crashes", "sales": "prices, quotes"},
    })
    assert r.ok, r.text()
    assert r.json()["branches"] == ["billing"], r.json()


def test_inclusive_gateway_with_laya(web_api, laya_ready):
    r = web_api.post("/api/gateways/inclusive", data={
        "question": "Does the customer",
        "state": "You charged me twice this month. I want my money back for the duplicate charge.",
        "branches": {"refund": "ask for a refund?", "bug": "report a crash or bug in the app?"},
    })
    assert r.ok, r.text()
    assert r.json()["branches"] == ["refund"], r.json()


def test_gateway_is_configured_from_the_n8n_ui(n8n_page, create_workflow, credential):
    """Decide With switches the form between data conditions and Laya (question, confidence)."""
    wf = create_workflow(Workflow("e2e gateway ui", credential)
                         .add("Start", "n8n-nodes-base.manualTrigger", {}, bpmn=False)
                         .condition_gateway("Is Book Available?", "exclusive",
                                            {"available": "={{ $json.available }}", "on loan": "={{ !$json.available }}"})
                         .flow("Start", "Is Book Available?"))
    n8n_page.goto(f"/workflow/{wf['id']}")
    n8n_page.get_by_test_id("canvas-node").filter(has_text="Is Book Available?").dblclick()
    params = n8n_page.get_by_test_id("node-parameters")
    expect(params.get_by_text("Decide With")).to_be_visible()
    expect(params.get_by_text("Question")).to_have_count(0)
    n8n_page.get_by_test_id("parameter-input-decider").locator("input").click()
    n8n_page.get_by_text("Laya (Reads Text)").click()
    expect(params.get_by_text("Question")).to_be_visible()
    expect(params.get_by_text("Min Confidence")).to_be_visible()


def test_task_output_keeps_the_input_when_asked(create_workflow, credential, n8n_api):
    """Output "Input + Task Result": the incoming fields travel on with the task's result."""
    path = f"e2e-output-{uuid.uuid4().hex[:6]}"
    create_workflow(Workflow("e2e output", credential)
                    .start("Start", path, respond="lastNode")
                    .task("Normalize Alert", "scriptTask", "normalize_alert", output="merge")  # by name
                    .flow("Start", "Normalize Alert"), publish=True)
    item, = n8n_api.post(f"{N8N}/webhook/{path}", data={"source": "edr", "raw": "beacon from host-7"}).json()
    assert item["summary"] == "beacon from host-7" and item["hosts"] == ["host-7"]  # task result
    assert item["body"] == {"source": "edr", "raw": "beacon from host-7"}  # input kept


def test_task_input_is_extracted_from_text_by_gliner(create_workflow, credential, n8n_api):
    """Input From = Text: GLiNER2.5 reads an email, the business rule runs on what it found."""
    path = f"e2e-gliner-{uuid.uuid4().hex[:6]}"
    create_workflow(Workflow("e2e gliner", credential)
                    .start("Start", path, respond="lastNode")
                    .task("Check Expense Policy", "businessRuleTask", "check_expense_policy",
                          text="{{email}}")
                    .flow("Start", "Check Expense Policy"), publish=True)
    email = "Dinner with the Contoso procurement team, 4 people, total 180 EUR. Paid by ann@acme.test"
    item, = n8n_api.post(f"{N8N}/webhook/{path}", data={"email": email}).json()
    assert item["within_policy"] is True and item["limit"] == 240  # meal, 4 attendees
    assert {k: v["value"] for k, v in item["extraction"].items()} == {
        "employee": "ann@acme.test", "category": "meal", "amount": 180.0, "attendees": 4}


def test_text_input_is_configured_from_the_n8n_ui(n8n_page, create_workflow, credential):
    wf = create_workflow(Workflow("e2e text input ui", credential)
                         .add("Start", "n8n-nodes-base.manualTrigger", {}, bpmn=False)
                         .task("Check Expense Policy", "businessRuleTask", "check_expense_policy", text="")
                         .flow("Start", "Check Expense Policy"))
    n8n_page.goto(f"/workflow/{wf['id']}")
    n8n_page.get_by_test_id("canvas-node").filter(has_text="Check Expense Policy").dblclick()
    expect(n8n_page.get_by_test_id("credentials-label")).to_be_visible()  # NDV fully rendered
    params = n8n_page.get_by_test_id("node-parameters")
    for label in ("Input From", "Text", "Min Confidence", "Verify Values with Laya"):
        expect(params.get_by_text(label, exact=True)).to_be_visible()
    expect(params.get_by_text("win over the extracted ones")).to_be_visible()
