# Erasmus learning agreement

## Brief

Trigger: a student submits a learning agreement (name, email, student id, host university, departure date, courses: one line per mapping "host course | ECTS | our course").
1. Check Agreement (software, business rule): total ECTS must be 24-33; each mapping is matched against the department's table of pre-approved equivalences; counts the submission (max 3).
2. If the ECTS are out of range: the student revises and resubmits (lane students) while submissions are left; after the third, the international office closes it (lane international-office) and the student is emailed.
3. If some mappings are not pre-approved: the professors give their opinion on them in one task (lane professors); all must come back before the coordinator decides.
4. The departmental coordinator (lane coordinators) approves or rejects the whole agreement; a rejection goes back to the student while submissions are left.
5. The international office signs it (lane international-office); the student is emailed.
Deadline: from submission, a parallel branch waits until 15 days before departure; if the agreement is not signed by then, the head of the international office (lane international-office-head) gets an alert task.
Personal data: student name, email, student id (needed to identify and inform the student); no special categories. People take every decision.

## Who decides

**LLM agent**: None: the checks are rules on numbers and a table; every judgement is a person's, and drafting saves nothing here.

**GDPR**: Student name, email and id identify and inform the student; courses are academic data. No special categories, no external LLM; people decide every step; the event log keeps each input and result.

- **ECTS Within Limits?** (condition_gateway): an explicit rule on the total ECTS
- **Opinions Needed?** (condition_gateway): the pre-approved table decides which mappings need a professor
- **Professor Opinions** (person): academic equivalence is a professor's judgement
- **Coordinator Decision** (person): the coordinator is accountable for the agreement

## Domain research

None.

## Interview

**user**: Before an Erasmus exchange, a student submits a learning agreement: the courses they will take at the host university, each with its ECTS credits and the course of our degree it replaces. The total must be between 24 and 33 ECTS per semester. Mappings found in the department's table of pre-approved equivalences are accepted; for every other mapping the professor of our course must give an opinion, and all opinions must come back before the departmental coordinator approves or rejects the whole agreement. The student can revise and resubmit, at most 3 times. The international office then signs it. If the agreement is not signed 15 days before the departure date, the head of the international office is alerted.

**assistant**: What data does the student submit, and in what form are the course mappings written?

**user**: Name, email, student id, host university, departure date (YYYY-MM-DD) and the courses as text, one mapping per line: host course | ECTS | our course it replaces.

**assistant**: Who are the people involved (roles), and what happens after a third rejected submission?

**user**: Lanes: students, professors (one opinions task listing every mapping outside the table), coordinators, international-office and international-office-head. After the third submission that fails the checks or is rejected, the international office closes the agreement and the student is told why.

**assistant**: When the agreement is signed, or closed, how is the student informed, and does the head of the office need to act on the alert?

**user**: By email in both cases. The alert 15 days before departure is a task for the head of the office, who decides what to do (call the coordinator, extend the deadline); if the agreement is already signed by then, no alert.

**assistant**: Trigger: a student submits a learning agreement (name, email, student id, host university, departure date, courses: one line per mapping "host course | ECTS | our course").
1. Check Agreement (software, business rule): total ECTS must be 24-33; each mapping is matched against the department's table of pre-approved equivalences; counts the submission (max 3).
2. If the ECTS are out of range: the student revises and resubmits (lane students) while submissions are left; after the third, the international office closes it (lane international-office) and the student is emailed.
3. If some mappings are not pre-approved: the professors give their opinion on them in one task (lane professors); all must come back before the coordinator decides.
4. The departmental coordinator (lane coordinators) approves or rejects the whole agreement; a rejection goes back to the student while submissions are left.
5. The international office signs it (lane international-office); the student is emailed.
Deadline: from submission, a parallel branch waits until 15 days before departure; if the agreement is not signed by then, the head of the international office (lane international-office-head) gets an alert task.
Personal data: student name, email, student id (needed to identify and inform the student); no special categories. People take every decision.
