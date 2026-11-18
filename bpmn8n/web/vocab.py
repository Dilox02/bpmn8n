"""One vocabulary: the words a page uses for every status, and the tone (tokens.css's pill colour family) each
one takes. Templates write {{ x.status|status_pill }} instead of spelling the words and colours out."""

STATUSES = {  # status: (label, tone)
    # what a case waits on (models/waiting.py)
    "open": ("open", "accent"),
    "claimed": ("claimed", "warning"),
    "completed": ("completed", "positive"),
    "cancelled": ("cancelled", "negative"),
    "suspended": ("suspended", "neutral"),
    "expired": ("expired", "negative"),
    # a case, inferred from its events and waits (web/cases.py)
    "running": ("running", "warning"),
    "waiting_person": ("waiting for a person", "accent"),
    "waiting_message": ("waiting for a message", "accent"),
    "failed": ("failed", "negative"),
    # a step of the event log (Event.status; "running" above while it runs)
    "complete": ("done", "positive"),
    "error": ("failed", "negative"),
}

# a node on the canvas, as a page posts it to the n8n preview (n8n-editor/preview.js, command bpmn8nStates):
# not reached yet (dimmed), where the case is now (pulsing), passed
NODE_STATES = ("waiting", "active", "done")


def label(status):
    return STATUSES.get(status, (status, ""))[0]


def tone(status):
    return STATUSES.get(status, ("", "neutral"))[1]
