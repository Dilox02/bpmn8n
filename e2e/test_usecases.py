"""ACME use cases run end to end: n8n executes the BPMN workflow, bpmn8n runs the tasks, Laya
decides the gateways, people complete user tasks. The process-mining event log is the oracle
for the path each case took."""
import time
import uuid

import pytest
from playwright.sync_api import expect

from conftest import N8N, WEB, activities, wait_open, wait_user_task  # noqa: F401 (fixtures in conftest)
from projects.acme.processes import expense_approval, incident_response, library_lending, ticket_triage


# --- UC1 customer service: Laya routes tickets in any language -----------------------------

TICKETS = {
    "billing-en": ({"customer": "Northwind", "amount": 49, "duplicate_charge": True,
                    "message": "I was charged twice for my March invoice, please refund the duplicate payment."},
                   "Calculate Refund"),
    "technical-it": ({"customer": "Rossi Srl", "amount": 0, "duplicate_charge": False,
                      "message": "L'app si chiude ogni volta che apro le impostazioni, non riesco più a lavorare."},
                     "Open Bug Report"),
    "sales-es": ({"customer": "García SA", "amount": 0, "duplicate_charge": False,
                  "message": "Queremos comprar 50 licencias más. ¿Nos pueden enviar un presupuesto?"},
                 "Notify Sales"),
}


@pytest.mark.parametrize("ticket", TICKETS)
def test_uc1_ticket_is_routed_to_the_right_team(ticket, deploy, start_case, laya_ready):
    payload, team_task = TICKETS[ticket]
    rows = start_case(deploy(ticket_triage), payload, until=lambda acts: len(acts) == 3)
    assert activities(rows) == ["Register Ticket", "Route Ticket", team_task]


def test_uc1_unsure_gateway_hands_over_to_a_person(deploy, n8n_api, web_api, event_log, laya_ready):
    """BPMN default flow: below min_confidence the ticket goes to a person (abstention)."""
    path = deploy(ticket_triage, min_confidence=0.999)
    known = set(event_log.cases())
    payload = {"customer": "Initech", "message": "Hi, about my account...", "amount": 0, "duplicate_charge": False}
    assert n8n_api.post(f"{N8N}/webhook/{path}", data=payload).ok
    task = wait_user_task(web_api, "Triage Ticket")
    assert task["lane"] == "support-leads"
    done = web_api.post(f"/api/usertasks/{task['id']}/complete", data={"team": "technical"})
    assert done.ok, done.text()
    rows = event_log.wait_for_case(known, until=lambda acts: acts[-1] == "Triage Ticket")
    assert activities(rows) == ["Register Ticket", "Route Ticket", "Triage Ticket"]
    assert rows[-1]["resource"] == "api"


# --- UC2 finance: a manager approves in the inbox (human in the loop) --------------------------

def test_uc2_business_expense_is_approved_by_a_manager(deploy, n8n_api, web_api, event_log, manager, page,
                                                       laya_ready):
    path = deploy(expense_approval)
    known = set(event_log.cases())
    expense = {"employee": "ann@acme.test", "category": "meal", "amount": 180, "attendees": 2,
               "justification": "Dinner with the Contoso procurement team to close the 2027 renewal contract."}
    before = {t["id"] for t in web_api.get("/api/usertasks").json()}  # tasks other runs may have left open
    assert n8n_api.post(f"{N8N}/webhook/{path}", data=expense).ok
    task = wait_open(web_api, "/api/usertasks", lambda t: t["activity"] == "Approve Expense" and t["id"] not in before)

    page.goto(f"{WEB}/inbox/")  # -> login
    page.get_by_label("Username").fill(manager["username"])
    page.get_by_label("Password").fill(manager["password"])
    page.get_by_role("button", name="Sign in").click()
    page.locator(f'a.name[href="/inbox/{task["id"]}/"]').click()  # the inbox lists this case's task, its page works
    expect(page.get_by_role("main")).to_contain_text("within_policy")  # the case data the person decides on
    page.get_by_label("Approve").check()
    page.get_by_label("Note").fill("OK, strategic customer.")
    page.get_by_role("button", name="Complete").click()
    expect(page.get_by_role("status")).to_contain_text("completed")

    rows = event_log.wait_for_case(known, until=lambda acts: acts[-1] == "Notify Employee")
    assert activities(rows) == ["Check Expense Policy", "Assess Justification", "Approve Expense",
                                "Record Expense", "Notify Employee"]
    assert rows[2]["resource"] == manager["username"]


def test_uc2_personal_expense_is_rejected_without_bothering_anyone(deploy, start_case, laya_ready):
    expense = {"employee": "bob@acme.test", "category": "other", "amount": 95, "attendees": 1,
               "justification": "Birthday present for my nephew."}
    rows = start_case(deploy(expense_approval), expense, until=lambda acts: acts[-1] == "Notify Rejection")
    assert activities(rows) == ["Check Expense Policy", "Assess Justification", "Notify Rejection"]


# --- UC3 security: one alert, several actions (inclusive gateway) -------------------------------

ALERTS = {
    "malware": ({"source": "edr", "user": "",
                 "raw": "Trojan beaconing to a command-and-control server detected on laptop host-17."},
                {"Isolate Host"}),
    "phishing": ({"source": "mail", "user": "jbianchi",
                  "raw": "User jbianchi entered her password on a fake Microsoft login page from a phishing email."},
                 {"Reset Credentials"}),
    "customer-data-exfiltration": (
        {"source": "dlp", "user": "",
         "raw": "A CSV with 12,000 customer names, emails and home addresses was uploaded to an unknown server."},
        {"Notify DPO"}),
}


@pytest.mark.parametrize("alert", ALERTS)
def test_uc3_alert_triggers_every_needed_action(alert, deploy, start_case, event_log, laya_ready):
    payload, actions = ALERTS[alert]
    rows = start_case(deploy(incident_response), payload, until=lambda acts: actions <= set(acts))
    time.sleep(2)  # let any further branch finish
    rows = event_log.cases()[rows[0]["case_id"]]
    assert activities(rows)[:2] == ["Normalize Alert", "Assess Incident"]
    assert set(activities(rows)[2:]) == actions


def test_uc3_alert_with_no_clear_action_goes_to_an_analyst(deploy, n8n_api, web_api, laya_ready):
    """Default flow of the inclusive gateway: nothing is dropped silently."""
    path = deploy(incident_response)
    alert = {"source": "scanner", "user": "", "raw": "Weekly vulnerability scan completed: no new findings."}
    assert n8n_api.post(f"{N8N}/webhook/{path}", data=alert).ok
    task = wait_user_task(web_api, "Review Alert")
    assert task["lane"] == "soc-analysts" and "vulnerability scan" in task["context"]["summary"]
    assert web_api.post(f"/api/usertasks/{task['id']}/complete", data={"action": "none needed"}).ok


# --- UC4 library lending (lesson 2): message, timer, manual + user task, parallel, loop ----------

def test_uc4_book_on_loan_is_held_then_lent(deploy, n8n_api, web_api, event_log):
    path = deploy(library_lending, hold_wait=(2, "seconds"))
    known, request_id = set(event_log.cases()), f"REQ-{uuid.uuid4().hex[:6]}"
    request = {"request_id": request_id, "reader": "mario@acme.test", "book": "Dune"}
    assert n8n_api.post(f"{N8N}/webhook/{path}", data=request).ok

    # 3.2 on loan: the event-based gateway waits for hold-book OR decline-hold (correlated by request id)
    wait_open(web_api, "/api/messages/subscriptions", lambda s: s["correlation_key"] == request_id)
    hold = web_api.post("/api/messages/hold-book", data={"correlation_key": request_id, "payload": {"hold": True}})
    assert hold.ok, hold.text()
    late = web_api.post("/api/messages/decline-hold", data={"correlation_key": request_id})
    assert late.status == 404  # the race is over: hold-book won

    # two "weeks" later the book is back: the librarian fetches it (manual) and checks it out (user)
    fetch = wait_user_task(web_api, "Fetch Book")
    assert (fetch["kind"], fetch["lane"]) == ("manualTask", "librarians")
    assert web_api.post(f"/api/usertasks/{fetch['id']}/complete", data={}).ok
    checkout = wait_user_task(web_api, "Check Out Book")
    assert web_api.post(f"/api/usertasks/{checkout['id']}/complete", data={"due_date": "2026-11-20"}).ok

    rows = event_log.wait_for_case(known, until=lambda acts: {"Send Checkout Reply", "Update Catalogue"} <= set(acts))
    acts = [r["activity"] for r in rows]
    assert acts[:10] == ["Get Book Status", "Is Book Available?", "Send On-Loan Reply", "Wait for Reader", "Hold Book",
                         "Send Hold Reply", "Get Book Status", "Is Book Available?", "Fetch Book", "Check Out Book"]
    assert set(acts[10:]) == {"Send Checkout Reply", "Update Catalogue"}  # parallel branches
    assert (rows[3]["endpoint"], rows[3]["resource"]) == ("receiveTask", "message:hold-book")
    assert rows[6]["start_timestamp"] > rows[5]["end_timestamp"]  # after the timer
