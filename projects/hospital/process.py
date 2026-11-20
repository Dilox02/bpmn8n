"""Export with python -m projects.hospital.process (no server or LLM needed)."""
import json
from pathlib import Path

from bpmn8n.workflows import Workflow


def appointment_booking(credential=None):
    workflow = (
        Workflow("Hospital · Intelligent appointment booking", credential)
        .start("Patient request", "hospital-appointment")
        .task("A · Clarify request", "serviceTask", "clarify_hospital_request", output="merge")
        .user_task("Patient reviews details", "Review the proposed service and preferences", "patients",
                   form=[("service", "Service", "string"), ("earliest", "First date (YYYY-MM-DD)", "string"),
                         ("latest", "Last date (YYYY-MM-DD)", "string"), ("morning", "Morning only", "boolean"),
                         ("continue_booking", "Continue booking", "boolean")])
        .condition_gateway("Continue?", "exclusive", {"yes": "{{continue_booking}}",
                                                       "closed": "not {{continue_booking}}"})
        .task("B · Check requirements", "businessRuleTask", "validate_hospital_request", output="merge")
        .condition_gateway("Valid?", "exclusive", {"valid": "{{valid}}", "staff": "not {{valid}}"})
        .task("C · Find slots", "serviceTask", "find_hospital_slots", output="merge")
        .condition_gateway("Slots available?", "exclusive", {"yes": "{{slots_available}}",
                                                              "alternatives": "not {{slots_available}}"})
        .user_task("Patient chooses slot", "Choose an available appointment", "patients",
                   context={"Available slots": "{{slots}}"}, form=[("slot_id", "Slot ID", "number"),
                                               ("selected", "Reserve this slot", "boolean")])
        .condition_gateway("Selected?", "exclusive", {"yes": "{{selected}}", "closed": "not {{selected}}"})
        .task("C · Reserve slot", "serviceTask", "reserve_hospital_slot", output="merge")
        .condition_gateway("Booked?", "exclusive", {"yes": "{{booked}}", "retry": "not {{booked}}"})
        .user_task("Patient sees confirmation", "Appointment confirmed", "patients", context={"Confirmation": "{{confirmation}}"},
                   form=[])
        .user_task("Patient changes preferences", "No matching slot. Choose other dates or stop", "patients",
                   form=[("service", "Service", "string"), ("earliest", "First date", "string"),
                         ("latest", "Last date", "string"), ("morning", "Morning only", "boolean"),
                         ("continue_booking", "Search again", "boolean")], row=1)
        .user_task("0 · Staff referral", "Resolve the booking request", "hospital-staff", context={"Reason": "{{reason}}"},
                   form=[("resolution", "Staff resolution", "string")], row=2)
        .manual_task("Request closed", "Patient ended the booking request", "patients", row=3)
        .flow("Patient request", "A · Clarify request", "Patient reviews details", "Continue?")
        .flow("Continue?", "B · Check requirements", "Valid?", output="yes")
        .flow("Continue?", "Request closed", output="closed")
        .flow("Valid?", "C · Find slots", "Slots available?", output="valid")
        .flow("Valid?", "0 · Staff referral", output="staff")
        .flow("Slots available?", "Patient chooses slot", "Selected?", output="yes")
        .flow("Slots available?", "Patient changes preferences", "Continue?", output="alternatives")
        .flow("Selected?", "C · Reserve slot", "Booked?", output="yes")
        .flow("Selected?", "Request closed", output="closed")
        .flow("Booked?", "Patient sees confirmation", output="yes")
        .flow("Booked?", "C · Find slots", output="retry")
    )
    colors = {"0": "#674497", "A": "#096D76", "B": "#96461D", "C": "#305C99"}
    for node in workflow.nodes:
        if node["name"][0] in colors:
            node["notes"] = f"Agent responsibility {node['name'][0]} ({colors[node['name'][0]]})"
    return workflow.layout()


if __name__ == "__main__":
    Path(__file__).with_name("workflow.json").write_text(
        json.dumps(appointment_booking().as_json(), indent=2, ensure_ascii=False), encoding="utf-8")
