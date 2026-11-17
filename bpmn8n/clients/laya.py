"""Compatible client for /v1/systemone. The class/config names remain stable for old workflows;
the model server now supplies pinned Bekko on CPU and reports its actual model identity."""
from ..conf import settings

from .http import ModelClient, ServiceUnavailable


class LayaError(ServiceUnavailable):
    pass


class LayaClient(ModelClient):
    error = LayaError

    def __init__(self, url=None, api_key=None, timeout=None, busy_timeout=None):
        super().__init__(url or settings.LAYA_URL, settings.LAYA_API_KEY if api_key is None else api_key,
                         settings.LAYA_TIMEOUT if timeout is None else timeout,
                         settings.LAYA_BUSY_TIMEOUT if busy_timeout is None else busy_timeout)

    def ask(self, state, questions):
        """POST /v1/systemone; returns the `answers` dict keyed like `questions`."""
        reply = self.call({"state": state, "questions": questions})
        self.model = reply.get("model", "decision")
        return reply["answers"]

    def choose(self, state, question, options):
        """One `choice` question. options: {label: description}. Returns (label, confidence)."""
        a = self.ask(state, {"q": {"type": "choice", "instructions": question, "criteria": options}})["q"]
        return a["choice"], a.get("answer_confidence", a.get("confidence"))

    def yes_no(self, state, questions):
        """Several `noul` questions in one forward pass. questions: {id: text}. Returns {id: P(yes)}."""
        answers = self.ask(state, {k: {"type": "noul", "instructions": q} for k, q in questions.items()})
        return {k: answers[k]["noul"] for k in questions}
