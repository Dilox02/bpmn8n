"""Calculate Fraud Indicators: activity for motor insurance claim (bpmn8n Studio)."""
from datetime import datetime

from ninja import Schema
from pydantic import Field

from bpmn8n.api import task

# Mock metrics for demonstration and testing
KNOWN_POLICIES = {
    "POL-8812": {"days_since_event": 45, "claims_past_12m": 1},
    "POL-3311": {"days_since_event": 5, "claims_past_12m": 0},
}


class CalculateFraudIndicatorsInput(Schema):
    policy_number: str = Field(description="Customer policy number")
    event_date: str = Field(description="Date of the incident (YYYY-MM-DD)")


class CalculateFraudIndicatorsResult(Schema):
    days_since_event: int = Field(description="Number of days elapsed between event date and report date")
    claims_past_12m: int = Field(description="Number of claims filed under this policy in the past 12 months")
    fraud_signal: bool = Field(description="True if reported >30 days late or >=3 prior claims")


@task("serviceTask")
def calculate_fraud_indicators(request, data: CalculateFraudIndicatorsInput) -> CalculateFraudIndicatorsResult:
    """Calculates fraud risk metrics such as days elapsed since event date and claim count in past 12 months."""
    # TODO: Query database/claims service for past 12m claims and actual submission timestamp
    if data.policy_number in KNOWN_POLICIES:
        metrics = KNOWN_POLICIES[data.policy_number]
        days_since_event = metrics["days_since_event"]
        claims_past_12m = metrics["claims_past_12m"]
    else:
        try:
            event_dt = datetime.strptime(data.event_date, "%Y-%m-%d").date()
            days_since_event = max(0, (datetime.now().date() - event_dt).days)
        except ValueError:
            days_since_event = 0
        claims_past_12m = 0

    fraud_signal = days_since_event > 30 or claims_past_12m >= 3

    return CalculateFraudIndicatorsResult(
        days_since_event=days_since_event,
        claims_past_12m=claims_past_12m,
        fraud_signal=fraud_signal,
    )
