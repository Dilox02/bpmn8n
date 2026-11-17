"""Pinned, offline CPU Bekko adapter for bpmn8n's Choice/Noul/Score protocol.

Run ``python tools/decision_runtime.py --self-test`` without loading any weights.
Instantiate DecisionRuntime only after downloading the pinned public snapshot.
"""
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys

MODEL_ID = "hotchpotch/bekko-system-one-v0-400m"
REVISION = "1960df5602bd93cc8d336fdebd9fb68a30926e13"
SOURCE_SHA256 = "cb0f8126709d4cab3726f6d6475b386e6d8143cd57736da2c5d66710a52353ee"
ALLOW_PATTERNS = ["0_BekkoInference/*", "inference_v0.py", "modules.json",
                  "config_sentence_transformers.json", "requirements.txt", "README.md"]


def native_request(state, questions):
    if not isinstance(questions, dict) or len(questions) > 64:
        raise ValueError("questions must be an object containing at most 64 questions")
    decisions = []
    for qid, question in questions.items():
        if not isinstance(qid, str) or not qid or not isinstance(question, dict):
            raise ValueError("Every question needs a nonempty string id and an object definition")
        kind = question.get("type")
        instruction = question.get("instructions", "")
        if not isinstance(instruction, str):
            raise ValueError("instructions must be a string")
        criteria = question.get("criteria")
        if kind == "choice":
            if isinstance(criteria, list) and all(isinstance(label, str) for label in criteria):
                if len(set(criteria)) != len(criteria):
                    raise ValueError("choice labels must be distinct")
                criteria = {label: label for label in criteria}
            if not isinstance(criteria, dict) or not 2 <= len(criteria) <= 32:
                raise ValueError("choice needs 2..32 distinct named criteria")
            if any(not isinstance(k, str) or not k for k in criteria):
                raise ValueError("choice labels must be nonempty strings")
            values = [(key, value, None) for key, value in criteria.items()]
        elif kind == "noul":
            criteria = criteria or {}
            if not isinstance(criteria, dict) or set(criteria) - {"true", "false", "yes", "no"}:
                raise ValueError("noul criteria may contain only true/false or yes/no descriptions")
            values = [
                ("false", criteria.get("false", criteria.get("no", "The answer to the question is no.")), None),
                ("true", criteria.get("true", criteria.get("yes", "The answer to the question is yes.")), None),
            ]
        elif kind == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 32 or any(x is None for x in criteria):
                raise ValueError("score needs 2..32 ordered level descriptions, starting at index 0")
            values = [(str(i), value, i) for i, value in enumerate(criteria)]
        else:
            raise ValueError(f"Unsupported decision type {kind!r}; expected choice, noul or score")
        decisions.append({
            "id": qid, "kind": "judgment", "type": kind,
            "instructions_json": json.dumps(instruction, ensure_ascii=False, allow_nan=False),
            "system_prompt": "", "documents": [], "scoring": None,
            "criteria": [{"id": key, "description_json": json.dumps(value, ensure_ascii=False, allow_nan=False),
                          "value": number} for key, value, number in values],
        })
    return {"state_json": json.dumps(state, ensure_ascii=False, allow_nan=False), "decisions": decisions}


class DecisionRuntime:
    """One reusable CPU model; predict() never fetches files or generates text.

    answer_confidence is the model's maximum candidate probability. It is not
    calibrated on this project's domain; use the measured abstention results.
    """

    def __init__(self, model_path=None, *, model=None):
        self.model = model
        self.metadata = {"model": MODEL_ID, "revision": REVISION, "device": "cpu",
                         "confidence_method": "maximum_probability_uncalibrated"}
        if model is not None:
            return
        if model_path is None:
            from huggingface_hub import snapshot_download
            model_path = snapshot_download(
                MODEL_ID, revision=REVISION, local_files_only=True, token=False,
                allow_patterns=ALLOW_PATTERNS,
            )
        root = Path(model_path).resolve()
        source = root / "inference_v0.py"
        if hashlib.sha256(source.read_bytes()).hexdigest() != SOURCE_SHA256:
            raise ValueError("The local Bekko inference source differs from the inspected pinned revision")
        # Import the reviewed local file, rather than fetching or executing remote Hub code.
        module_name = "_bpmn8n_pinned_bekko_inference"
        module = sys.modules.get(module_name)
        if module is None:
            spec = importlib.util.spec_from_file_location(module_name, source)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
        self.model = module.BekkoInference.load(str(root / "0_BekkoInference"), local_files_only=True)
        self.model.to("cpu").eval().requires_grad_(False)
        self.model.set_attention_implementation("sdpa", device="cpu")

    def predict(self, state, questions):
        request = native_request(state, questions)
        if not request["decisions"]:
            return {"model": MODEL_ID, "answers": {}}
        raw = self.model.predict(request, batch_size=16, token_budget=8192,
                                 context_length=1024, query_length=768, document_length=256,
                                 show_progress_bar=False)
        answers = {}
        for decision in request["decisions"]:
            qid, kind = decision["id"], decision["type"]
            prediction = raw[qid]
            probabilities = prediction["probabilities"]
            expected = {item["id"] for item in decision["criteria"]}
            if not isinstance(probabilities, dict) or set(probabilities) != expected:
                raise ValueError("Decision output has missing or unexpected candidate probabilities")
            if any(type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
                   for p in probabilities.values()) or abs(sum(probabilities.values()) - 1) > 1e-5:
                raise ValueError("Decision output contains invalid probabilities")
            confidence = max(probabilities.values())
            answer = {"type": kind, "answer_confidence": confidence, "confidence": confidence,
                      "probabilities": probabilities}
            if kind == "choice":
                answer["choice"] = max(probabilities, key=probabilities.get)
            elif kind == "noul":
                answer["noul"] = probabilities["true"]
            else:
                answer["score"] = sum(int(key) * p for key, p in probabilities.items())
                answer["legend"] = {str(i): str(value) for i, value in enumerate(questions[qid]["criteria"])}
            answers[qid] = answer
        return {"model": MODEL_ID, "answers": answers}


def self_test():
    class FakeModel:
        def predict(self, request, **kwargs):
            return {item["id"]: {"probabilities": {
                criterion["id"]: probability for criterion, probability in zip(item["criteria"], [.25, .75])
            }} for item in request["decisions"]}

    runtime = DecisionRuntime(model=FakeModel())
    answer = runtime.predict("sample", {
        "c": {"type": "choice", "criteria": {"a": "first", "b": "second"}},
        "n": {"type": "noul", "instructions": "Does the condition hold?"},
        "s": {"type": "score", "criteria": ["low", "high"]},
    })["answers"]
    assert answer["c"]["choice"] == "b" and answer["c"]["answer_confidence"] == .75
    assert answer["n"]["noul"] == .75 and answer["s"]["score"] == .75
    assert runtime.predict("sample", {})["answers"] == {}
    for invalid in [{"q": {"type": "other"}}, {"q": {"type": "score", "criteria": ["one"]}},
                    {"q": {"type": "choice", "criteria": ["same", "same"]}}]:
        try:
            runtime.predict("sample", invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("Malformed decision was accepted")
    print("Choice/Noul/Score conversion and input checks passed; no model weights loaded")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
    elif sys.argv[1:] == ["--download"]:
        from huggingface_hub import snapshot_download
        print(snapshot_download(MODEL_ID, revision=REVISION, token=False, allow_patterns=ALLOW_PATTERNS))
    else:
        raise SystemExit("Use --self-test or --download; import DecisionRuntime to load the offline CPU model")
