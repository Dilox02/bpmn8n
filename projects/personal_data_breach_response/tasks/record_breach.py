"""Record in Breach Register: an activity of Personal data breach response (bpmn8n Studio). Edit freely."""
from ninja import Schema
from pydantic import Field

from bpmn8n.api import task


class RecordBreachInput(Schema):
    incident_id: str = Field(description="The incident")
    personal_data: bool = Field(False, description="Was personal data involved?")
    risk_to_people: bool = Field(False, description="Likely risk to people's rights?")
    high_risk: bool = Field(False, description="High risk?")
    assessment_notes: str = Field("", description="The DPO's reasons")


class RecordBreachResult(Schema):
    register_entry: str = Field(description="The register's line for this incident")
    authority_notified: bool = Field(description="Must the authority have been notified?")


@task("businessRuleTask")
def record_breach(request, data: RecordBreachInput) -> RecordBreachResult:
    """Writes the incident, the decisions and their reasons to the breach register (GDPR art. 33(5))."""
    # TODO: the breach register (a table or the GRC tool); the event log keeps the line meanwhile
    notify = data.personal_data and data.risk_to_people
    entry = (f"{data.incident_id}: personal data {'yes' if data.personal_data else 'no'}; "
             f"risk {'high' if data.high_risk else 'yes' if data.risk_to_people else 'no'}; "
             f"authority {'notified' if notify else 'not notified'}; reasons: {data.assessment_notes or '-'}")
    return RecordBreachResult(register_entry=entry, authority_notified=notify)
