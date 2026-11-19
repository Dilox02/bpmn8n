"""The ACME business processes as n8n workflows made of bpmn8n's BPMN nodes. Node fields use
process variables: empty fields bind by name, {{name}} picks another variable or fills text.

Run `python -m projects.acme.processes` to export them to examples/workflows/ for the n8n editor."""
import json
from pathlib import Path

from bpmn8n.workflows import Workflow, node_type  # noqa: F401  (the e2e tests build theirs too)

# --- UC1 customer service ------------------------------------------------------------------

# Branch descriptions state the customer's situation, not keyword lists: on the evaluation set
# keyword lists confused "asks for a price quote" with billing (e2e/test_laya_eval.py).
TICKET_TEAMS = {
    "billing": "problems with a payment already made: wrong or double charges, invoices, refunds",
    "technical": "the product does not work: bugs, errors, crashes, outages, login problems",
    "sales": "wants to buy more or new products, asks for a price quote, upgrade or demo",
}


# Below 0.7 confidence a person decides: on the evaluation set every automatic decision above it
# was right, ~35% of cases went to a person (reports/laya_eval.md).
MIN_CONFIDENCE = 0.7


def ticket_triage(credential=None, path="acme-ticket", min_confidence=MIN_CONFIDENCE):
    return (
        Workflow("ACME · Ticket triage", credential)
        .start("Ticket Received", path)
        .task("Register Ticket", "serviceTask", "register_ticket")  # customer, message: by name
        .gateway("Route Ticket", "exclusive", "Which team should handle this customer ticket?",
                 TICKET_TEAMS, "{{message}}", min_confidence)
        .task("Calculate Refund", "businessRuleTask", "calculate_refund", row=-1)
        .task("Open Bug Report", "sendTask", "open_bug_report", same_column=True, summary="{{message}}")
        .task("Notify Sales", "sendTask", "send_notification", row=1, same_column=True,
              to="sales@acme.test", subject="New sales lead: {{customer}}", body="{{message}}")
        .user_task("Triage Ticket", "Route the ticket of {{customer}}", "support-leads", row=2, same_column=True,
                   form=[("team", "Team", "string")], description="Laya was not confident enough to route it.")
        .flow("Ticket Received", "Register Ticket", "Route Ticket")
        .flow("Route Ticket", "Calculate Refund", output=0)
        .flow("Route Ticket", "Open Bug Report", output=1)
        .flow("Route Ticket", "Notify Sales", output=2)
        .flow("Route Ticket", "Triage Ticket", output=3)
    )


# --- UC2 finance -------------------------------------------------------------------------------

EXPENSE_PURPOSES = {
    "business": "a clear business purpose: client or customer meeting, conference, business trip, work event",
    "personal": "a personal purpose: family, friends, holiday, private gifts or shopping",
}


def expense_approval(credential=None, path="acme-expense", min_confidence=MIN_CONFIDENCE):
    return (
        Workflow("ACME · Expense approval", credential)
        .start("Expense Submitted", path)
        .task("Check Expense Policy", "businessRuleTask", "check_expense_policy")  # all by name
        .gateway("Assess Justification", "exclusive", "What is the purpose of this expense?",
                 EXPENSE_PURPOSES, "{{justification}}", min_confidence)
        .user_task("Approve Expense", "Approve {{amount}} EUR for {{employee}}", "managers",
                   form=[("approved", "Approve", "boolean"), ("note", "Note", "string")])
        .task("Notify Rejection", "sendTask", "send_notification", row=1, same_column=True,
              to="{{employee}}", subject="Expense rejected: personal purpose")
        .task("Record Expense", "serviceTask", "record_expense")  # employee, amount, approved: by name
        .task("Notify Employee", "sendTask", "send_notification", to="{{employee}}",
              subject="Expense {{status}}", body="{{note}}")
        .flow("Expense Submitted", "Check Expense Policy", "Assess Justification")
        .flow("Assess Justification", "Approve Expense", "Record Expense", "Notify Employee", output=0)
        .flow("Assess Justification", "Notify Rejection", output=1)
        .flow("Assess Justification", "Approve Expense", output=2)  # unsure -> the manager decides
    )


# --- UC3 security -------------------------------------------------------------------------------

# Short, plain yes/no questions: they scored best on the evaluation set (e2e/test_laya_eval.py).
INCIDENT_ACTIONS = {
    "isolate_host": "Is a computer infected (malware, worm, backdoor, cryptominer)?",
    "reset_credentials": "Did a user give their password to a fake page or email?",
    "notify_dpo": "Was a file with personal data of many people leaked?",
}


# Concrete yes/no questions and 0.7 (e2e/datasets/incidents.json: exact 10/16 as before, higher precision): an
# automatic reset or isolation is costly, an unsure alert goes to an analyst through the default flow.
def incident_response(credential=None, path="acme-incident", min_confidence=MIN_CONFIDENCE):
    return (
        Workflow("ACME · Incident response", credential)
        .start("Alert Received", path)
        .task("Normalize Alert", "scriptTask", "normalize_alert")  # source, raw: by name
        .gateway("Assess Incident", "inclusive", "", INCIDENT_ACTIONS, "{{summary}}", min_confidence)
        .task("Isolate Host", "serviceTask", "isolate_host", row=-1, hostname="{{hosts.0}}")
        .task("Reset Credentials", "serviceTask", "reset_credentials", same_column=True, username="{{user}}")
        .task("Notify DPO", "sendTask", "send_notification", row=1, same_column=True,
              to="dpo@acme.test", subject="Possible personal data breach (GDPR art. 33)", body="{{summary}}")
        .user_task("Review Alert", "Review the {{source}} alert", "soc-analysts", row=2, same_column=True,
                   form=[("action", "Action taken", "string")],
                   description="No automatic action was clearly needed; an analyst decides.")
        .flow("Alert Received", "Normalize Alert", "Assess Incident")
        .flow("Assess Incident", "Isolate Host", output=0)
        .flow("Assess Incident", "Reset Credentials", output=1)
        .flow("Assess Incident", "Notify DPO", output=2)
        .flow("Assess Incident", "Review Alert", output=3)  # default flow: no alert is dropped
    )


# --- UC4 library lending (course lesson 2, semi-formal spec) -----------------------------------
# 1 The System receives a book request · 2 The System gets the book status (service task)
# 3.1 If available: the librarian fetches the book (manual) and checks it out (user task); then, in
#     parallel, the System sends the checkout reply (send) and updates the catalogue (service)
# 3.2 If on loan: the System sends an on-loan reply (send) and waits for an event (event-based gateway):
#     3.2.1 decline-hold, or a week without answer: the System cancels the request (send) -> End
#     3.2.2 hold-book: the System holds the request (service), sends a hold reply (send),
#           waits two weeks (timer) -> 2

def library_lending(credential=None, path="acme-library", hold_wait=(2, "weeks"), answer_wait=(1, "weeks")):
    return (  # request_id and book bind by name in every task
        Workflow("ACME · Library lending", credential)
        .start("Request Received", path)
        .task("Get Book Status", "serviceTask", "get_book_status")
        .condition_gateway("Is Book Available?", "exclusive",
                           {"available": "{{available}}", "on loan": "not {{available}}"})
        .manual_task("Fetch Book", "Fetch {{book}} from the shelf", "librarians", row=-1)
        .user_task("Check Out Book", "Check out {{book}} for {{reader}}", "librarians",
                   form=[("due_date", "Due date", "string")], row=-1)
        .parallel_gateway("Lend Book", ["reply", "catalogue"], row=-1)
        .task("Send Checkout Reply", "sendTask", "send_notification", row=-1.5, to="{{reader}}",
              subject="{{book}} is ready for you, due {{due_date}}")
        .task("Update Catalogue", "serviceTask", "update_catalogue", row=-0.5, same_column=True)
        .column(3)
        .task("Send On-Loan Reply", "sendTask", "send_notification", row=1, to="{{reader}}",
              subject="{{book}} is on loan: reply hold-book to wait for it, or decline-hold")
        .event_gateway("Wait for Reader", ["hold-book", "decline-hold"], "{{request_id}}", answer_wait, row=1)
        .task("Hold Book", "serviceTask", "hold_book", row=1)
        .task("Send Hold Reply", "sendTask", "send_notification", row=1, to="{{reader}}",
              subject="Your hold on {{book}} is confirmed")
        .timer("Wait Two Weeks", *hold_wait, row=1)
        .column(6)
        .task("Cancel Request", "sendTask", "send_notification", row=2, to="{{reader}}",
              subject="Your request for {{book}} is cancelled")
        .flow("Request Received", "Get Book Status", "Is Book Available?")
        .flow("Is Book Available?", "Fetch Book", "Check Out Book", "Lend Book", output=0)
        .flow("Lend Book", "Send Checkout Reply", output=0)
        .flow("Lend Book", "Update Catalogue", output=1)
        .flow("Is Book Available?", "Send On-Loan Reply", "Wait for Reader", output=1)
        .flow("Wait for Reader", "Cancel Request", output=0)  # a week without answer
        .flow("Wait for Reader", "Hold Book", "Send Hold Reply", "Wait Two Weeks", "Get Book Status", output=1)
        .flow("Wait for Reader", "Cancel Request", output=2)  # decline-hold
    )


PROCESSES = {"ticket-triage": ticket_triage, "expense-approval": expense_approval,
             "incident-response": incident_response, "library-lending": library_lending}

if __name__ == "__main__":
    out = Path(__file__).resolve().parents[2] / "examples" / "workflows"
    out.mkdir(parents=True, exist_ok=True)
    for slug, build in PROCESSES.items():
        (out / f"{slug}.json").write_text(json.dumps(build().as_json(), indent=2))
        print(out / f"{slug}.json")
