"""Check Policy Status: an activity of Motor insurance claim (bpmn8n Studio). Edit freely."""
from ninja import Schema
from pydantic import Field

from bpmn8n.api import task


class CheckPolicyStatusInput(Schema):
    policy_number: str = Field(description="Customer policy number")
    event_date: str = Field(description="Date of the accident or theft (YYYY-MM-DD)")


class CheckPolicyStatusResult(Schema):
    policy_active: bool = Field(description="Whether the policy is currently active")
    covers_event_date: bool = Field(description="Whether the policy covers the specific event date")
    policy_valid: bool = Field(description="True if policy is active and covers event date")


@task("serviceTask")
def check_policy_status(request, data: CheckPolicyStatusInput) -> CheckPolicyStatusResult:
    """Checks whether the policy is active and covers the claim event date in the policy administration system."""
    # TODO: Query policy administration system API with policy_number and event_date
    if data.policy_number == "POL-1002":
        policy_active = False
        covers_event_date = False
    else:
        policy_active = True
        covers_event_date = True

    policy_valid = policy_active and covers_event_date

    return CheckPolicyStatusResult(
        policy_active=policy_active,
        covers_event_date=covers_event_date,
        policy_valid=policy_valid,
    )
