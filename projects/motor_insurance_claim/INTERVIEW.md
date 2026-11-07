# Motor insurance claim

## Brief

The process starts when a customer submits a car accident or theft claim through a web form with personal data (name, email, IBAN - required for payout and communications), policy number, event date, event type (accident or theft), amount claimed, injured flag (true/false), text description, and an optional photo of the joint accident report.

1. Initial Automated Processing (Software):
   - If a joint accident report photo is attached, `read_document_text` (OCR serviceTask) extracts the text.
   - System checks policy status in the policy admin system (active and covering event date).
   - System calculates fraud indicators: days since event (> 30 days) and claim history in past 12 months (>= 3 claims).

2. Exception & Policy Validation:
   - If the policy is inactive or does not cover the event date, the case is routed to a Claims Handler (role: claims-handlers) to review and confirm the rejection.

3. Theft Police Report Handling (Timer/Message Correlation):
   - If event type is theft, the process waits for the customer's police report upload (correlated by claim ID). A reminder email (`send_notification`) is sent after 5 days. If no report is received after 10 days, a Claims Handler (role: claims-handlers) reviews and closes the claim.

4. Routing & Decision (Human Tasks with Accountable Roles):
   - Fraud Signals Present (reported > 30 days or >= 3 claims in 12 months): routed to Anti-Fraud Unit (role: anti-fraud) for investigation and decision.
   - Claim > 15,000 EUR OR Injuries Present (injured = true): routed to Senior Adjuster (role: senior-adjusters) for review and decision.
   - Claim < 1,500 EUR and No Injuries: fast-tracked to Claims Handler (role: claims-handlers) for fast approval.
   - Claim 1,500 EUR to 15,000 EUR and No Injuries: routed to Claims Handler (role: claims-handlers) for standard evaluation.

5. Execution & Outcome:
   - If claim is approved, Finance Team (role: finance) processes the bank transfer using the customer's IBAN.
   - In all cases, an outcome notification email (`send_notification`) with approval/rejection decision and detailed reasons is sent to the customer's email address.

## Who decides

**LLM agent**: None: claim routing relies on deterministic rule gateways, OCR reads documents locally, and humans take all approval decisions; an LLM agent is not needed.

**GDPR**: Personal data: customer name, email, and IBAN are processed for identification, decision communication, and payment disbursement. All OCR and policy processing occur locally; no personal data is transmitted to external LLM providers. Human adjusters authorize all approvals and rejections, and all event steps are logged for auditability.

- **Route Claim Decision** (condition_gateway): explicit business rules on claim amount, injury flag, and fraud indicators determine which role must review the claim.
- **Investigate Fraud Claim** (person): fraud investigation requires human judgement, accountability, and evidence evaluation before approving or denying a claim.
- **Review High Value/Injury Claim** (person): claims over 15,000 EUR or involving bodily injury carry high financial and legal risk, requiring senior adjuster review.
- **Fast-Track Claim Review** (person): a claims handler must confirm basic claim details and grant formal human approval before payment processing.
- **Standard Claim Review** (person): claims handlers assess moderate-value claims and decide on approval or rejection.

## Domain research

None.

## Interview

**user**: A customer reports a car accident or a theft through our web form: they write what happened in their own words and give their policy number, the date of the event and the amount they claim; they may attach a photo of the joint accident report (constatazione amichevole). We check that the policy is active and covers the date. Small claims (under 1,500 EUR, no injuries) are fast-tracked but a claims handler still confirms the payout. Above 15,000 EUR or with injuries a senior adjuster decides. Fraud signals (reported more than 30 days after the event, or 3 or more claims in the last 12 months) send the claim to the anti-fraud unit. For thefts we need the police report: we ask the customer and wait up to 10 days, with a reminder after 5; without it the claim is closed. Approved claims are paid by bank transfer and the customer is told the outcome either way.

**user**: Please go on with the interview: the research failed but you can ask me directly.

**assistant**: Chi gestisce i sinistri di importo compreso tra 1.500 € e 15.000 € (senza feriti né sospetti di frode), e l'IBAN per il bonifico del risarcimento viene inviato già nel modulo iniziale o viene richiesto dopo l'approvazione?

**user**: Please continue in English. Claims between 1,500 and 15,000 EUR with no injuries and no fraud signals are decided by a claims handler (lane: claims-handlers). The IBAN is asked in the initial web form, together with the customer's name, email, policy number, event date, event type (accident or theft), amount claimed and whether anyone was injured. The policy register is our policy admin system (for the demo, synthetic data is fine). Senior adjusters are the lane senior-adjusters, the anti-fraud unit is the lane anti-fraud. Payment is done by the finance team after approval (lane finance), the customer gets an email with the outcome and the reasons.

**assistant**: When a claim is submitted, are the policy validation (checking if active and covered) and the fraud signal detection (reported > 30 days late, 3+ claims in 12 months) done automatically by software, or does a person perform these checks?

**user**: Both automatically by software: the policy check against the policy register, the fraud signals by counting the customer's claims of the last 12 months and the days between the event and the report. An inactive or not-covering policy is rejected by a claims handler who confirms it (no automatic rejection). If a photo of the joint accident report is attached, read it with OCR and attach the text for the handler. For thefts, the customer uploads the police report by replying to our email (a message to the process, correlated by claim id); a reminder email after 5 days, and after 10 days without it a claims handler closes the claim.

**assistant**: The process starts when a customer submits a car accident or theft claim through a web form with personal data (name, email, IBAN - required for payout and communications), policy number, event date, event type (accident or theft), amount claimed, injured flag (true/false), text description, and an optional photo of the joint accident report.

1. Initial Automated Processing (Software):
   - If a joint accident report photo is attached, `read_document_text` (OCR serviceTask) extracts the text.
   - System checks policy status in the policy admin system (active and covering event date).
   - System calculates fraud indicators: days since event (> 30 days) and claim history in past 12 months (>= 3 claims).

2. Exception & Policy Validation:
   - If the policy is inactive or does not cover the event date, the case is routed to a Claims Handler (role: claims-handlers) to review and confirm the rejection.

3. Theft Police Report Handling (Timer/Message Correlation):
   - If event type is theft, the process waits for the customer's police report upload (correlated by claim ID). A reminder email (`send_notification`) is sent after 5 days. If no report is received after 10 days, a Claims Handler (role: claims-handlers) reviews and closes the claim.

4. Routing & Decision (Human Tasks with Accountable Roles):
   - Fraud Signals Present (reported > 30 days or >= 3 claims in 12 months): routed to Anti-Fraud Unit (role: anti-fraud) for investigation and decision.
   - Claim > 15,000 EUR OR Injuries Present (injured = true): routed to Senior Adjuster (role: senior-adjusters) for review and decision.
   - Claim < 1,500 EUR and No Injuries: fast-tracked to Claims Handler (role: claims-handlers) for fast approval.
   - Claim 1,500 EUR to 15,000 EUR and No Injuries: routed to Claims Handler (role: claims-handlers) for standard evaluation.

5. Execution & Outcome:
   - If claim is approved, Finance Team (role: finance) processes the bank transfer using the customer's IBAN.
   - In all cases, an outcome notification email (`send_notification`) with approval/rejection decision and detailed reasons is sent to the customer's email address.
