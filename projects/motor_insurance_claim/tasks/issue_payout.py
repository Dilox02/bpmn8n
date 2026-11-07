"""Issue Payout: an activity of Motor insurance claim (bpmn8n Studio). Edit freely."""
import uuid

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task


class IssuePayoutInput(Schema):
    iban: str = Field(description="Customer IBAN for bank transfer")
    amount: float = Field(description="Approved payout amount")
    recipient_name: str = Field(description="Name of the bank account holder")


class IssuePayoutResult(Schema):
    payout_id: str = Field(description="Transaction confirmation ID")
    payout_status: str = Field(description="Status of bank transfer submission")


@task("serviceTask")
def issue_payout(request, data: IssuePayoutInput) -> IssuePayoutResult:
    """Executes bank transfer payout to the customer's IBAN via finance gateway."""
    # TODO: submit payment request to the banking / payment gateway API
    payout_id = f"PAY-{uuid.uuid4().hex[:8].upper()}"
    return IssuePayoutResult(payout_id=payout_id, payout_status="submitted")
