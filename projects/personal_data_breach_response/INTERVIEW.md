# Personal data breach response

## Brief

Trigger: an employee or the monitoring system reports a security incident (reporter email, free-text description, detection time).
1. Register Incident (software): incident id, awareness time, the 72-hour deadline for the supervisory authority.
2. Contain Incident (IT security, lane it-security): records the containment actions; it comes first because a case waits on one person at a time.
3. Personal Data Involved? (Laya reads the description): yes or unsure -> DPO Risk Assessment; no -> the DPO confirms that no personal data is involved.
4. DPO Risk Assessment (lane dpo): risk to people (yes/no), high risk (yes/no), notes without needless identifiers.
5. If there is a risk: an LLM drafts the notification to the Garante from the DPO's notes only; the DPO reviews and sends it before the deadline.
6. If the risk is high: the DPO informs the people concerned in plain language.
7. Every incident is recorded in the breach register with the reasons for the decisions.
Personal data: the reporter's email; the description may contain personal data: it reaches Laya on this server only; the external LLM sees only the DPO's notes and the incident id. The DPO takes every decision.

## Who decides

**LLM agent**: One agent drafts the authority notification from the DPO's notes only; the DPO reviews and sends it.

**GDPR**: The description may hold personal data: only Laya on this server reads it. The external LLM sees the incident id and the DPO's notes, written without needless identifiers. The DPO takes every decision; the register keeps the reasons.

- **Personal Data Involved?** (gateway): routing free text into two known cases; below 0.7 the DPO decides, and a 'no' is always confirmed by the DPO
- **DPO Risk Assessment** (person): the DPO is accountable for the risk assessment
- **Draft Authority Notification** (llm_agent): drafting a formal notification under a 72-hour deadline saves the DPO real effort; it sees only the DPO's notes
- **Review and Send to Garante** (person): the notification commits the company

## Domain research

None.

## Interview

**user**: An employee or our monitoring system reports a security incident by describing it in free text. We triage it: is it a personal data breach, how severe is it, which systems are affected. IT starts containment right away in parallel. If personal data is involved, the Data Protection Officer assesses the risk for the people concerned. If the breach is likely to result in a risk to their rights, we must notify the supervisory authority (Garante) within 72 hours of becoming aware of it, with a reminder at 48 hours if it is not sent yet; if the risk is high we also inform the people concerned in plain language. A draft of the notification can be prepared automatically, but the DPO reviews and sends it. Every incident, notified or not, is recorded in the breach register with the reasons for our decisions.

**assistant**: What does a report contain, and who are the roles involved?

**user**: Reporter email, a free-text description, the detection time (ISO). Roles: it-security for containment, dpo for the assessment and the notifications.

**assistant**: Who decides whether personal data is involved, and what may the automatic draft see?

**user**: A model may pre-sort it, but the DPO always confirms. The draft must not see the raw description: only the DPO's notes and the incident id.

**assistant**: Trigger: an employee or the monitoring system reports a security incident (reporter email, free-text description, detection time).
1. Register Incident (software): incident id, awareness time, the 72-hour deadline for the supervisory authority.
2. Contain Incident (IT security, lane it-security): records the containment actions; it comes first because a case waits on one person at a time.
3. Personal Data Involved? (Laya reads the description): yes or unsure -> DPO Risk Assessment; no -> the DPO confirms that no personal data is involved.
4. DPO Risk Assessment (lane dpo): risk to people (yes/no), high risk (yes/no), notes without needless identifiers.
5. If there is a risk: an LLM drafts the notification to the Garante from the DPO's notes only; the DPO reviews and sends it before the deadline.
6. If the risk is high: the DPO informs the people concerned in plain language.
7. Every incident is recorded in the breach register with the reasons for the decisions.
Personal data: the reporter's email; the description may contain personal data: it reaches Laya on this server only; the external LLM sees only the DPO's notes and the incident id. The DPO takes every decision.
