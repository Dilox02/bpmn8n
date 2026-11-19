from django.test import SimpleTestCase, TestCase, override_settings

from .support import add_events, step


@override_settings(N8N_API_KEY="")  # no n8n to read workflows from
class MiningTest(TestCase):
    def setUp(self):
        add_events(
            ("1", "", [step("Register", 0), step("Route", 1), step("Refund", 2, 500)]),
            ("2", "", [step("Register", 60), step("Route", 61), step("Triage", 62, 1_800_000, resource="anna")]),
            ("3", "", [step("Register", 120), step("Route", 121), step("Refund", 122, status="error")]),
            ("4", "", [step("Scan", 120)]),
            ("", "", [step("Register", 130, 5)]),  # a direct API call, not part of a case
        )

    def test_discovers_variants_branches_and_timings(self):
        processes = self.client.get("/api/processmining").json()
        self.assertEqual(set(processes), {"Register", "Scan"})  # no process recorded: named by the first activity
        p = processes["Register"]
        self.assertEqual((p["cases"], p["closed_cases"]), (3, 3))
        self.assertEqual(p["variants"][0], {"trace": ["Register", "Route", "Refund"], "cases": 2})
        branches = {e["to"]: e["probability"] for e in p["directly_follows"] if e["from"] == "Route"}
        self.assertEqual(branches, {"Refund": 0.667, "Triage": 0.333})
        self.assertEqual(p["activities"]["Triage"]["median_ms"], 1_800_000)
        self.assertEqual(p["activities"]["Triage"]["resources"], {"anna": 1})
        self.assertEqual(p["activities"]["Refund"]["errors"], 1)
        self.assertEqual(p["performance"]["lead_time_s"]["max"], 2 * 60 + 1800)
        self.assertEqual(p["conformance"]["checked_cases"], 0)  # no model to check against
        recent = self.client.get("/api/processmining", {"since": "2026-01-01T11:30:00Z"}).json()
        self.assertEqual(recent["Register"]["cases"], 1)

    def test_discovered_model_is_bpmn(self):
        r = self.client.get("/api/processmining/discovered.bpmn", {"process": "Register"})
        self.assertEqual(r["Content-Type"], "application/xml")
        self.assertIn(b"definitions", r.content)
        self.assertIn(b'name="Triage"', r.content)


class DecisionRulesTest(SimpleTestCase):
    def test_a_tree_explains_the_branch_from_the_data(self):
        from ..mining import ProcessAnalysis
        states = [{"amount": a, "vip": a % 3 == 0, "note": f"free text {a}"} for a in range(0, 3000, 100)]
        rules = ProcessAnalysis.rules(states, ["big" if s["amount"] > 1000 else "small" for s in states])
        self.assertEqual(rules["fit_accuracy"], 1.0)
        self.assertIn("amount <= 1050", rules["tree"])
        self.assertNotIn("note", rules["tree"])  # free text is no rule material

    def test_lists_in_the_state_are_left_out(self):
        """A gateway that read a list (an alert's hosts) used to crash the whole mining report."""
        from ..mining import ProcessAnalysis
        states = [{"amount": a, "hosts": [f"h-{a}"], "meta": {"k": a}} for a in range(0, 3000, 100)]
        rules = ProcessAnalysis.rules(states, ["big" if s["amount"] > 1000 else "small" for s in states])
        self.assertEqual(rules["fit_accuracy"], 1.0)
        self.assertNotIn("hosts", rules["tree"])


LIBRARY = [("Get Book Status", 0), ("Is Book Available?", 1), ("Fetch Book", 2), ("Check Out Book", 3),
           ("Send Checkout Reply", 4), ("Update Catalogue", 4)]


@override_settings(N8N_API_KEY="")
class ConformanceTest(TestCase):
    """Cases checked against the n8n workflow they ran (PM4Py alignments)."""

    def setUp(self):
        import json
        from pathlib import Path
        from ..mining import WorkflowModel
        workflow = json.loads((Path(__file__).parents[2] / "examples/workflows/library-lending.json").read_text())
        self.models = {"lib": WorkflowModel(workflow)}

    def mine(self):
        from ..mining import Mining
        return Mining(models=self.models)

    def test_cases_as_modeled_conform(self):
        add_events(("lib:1", "Library", [step(a, m) for a, m in LIBRARY]),
                   ("lib:2", "Library", [step("Get Book Status", 0), step("Is Book Available?", 1),
                                         step("Send On-Loan Reply", 2), step("Cancel Request", 3)]))  # timed out: silent
        self.assertEqual(self.mine().processes["Library"].conformance["conformant"], 1.0)

    def test_deviations_are_named_and_kept_out_of_training(self):
        skipped = [s for s in LIBRARY if s[0] != "Is Book Available?"]
        add_events(("lib:1", "Library", [step(a, m) for a, m in LIBRARY]),
                   ("lib:2", "Library", [step(a, m + 10) for a, m in skipped]))
        mining = self.mine()
        conformance = mining.processes["Library"].conformance
        self.assertEqual((conformance["checked_cases"], conformance["conformant"]), (2, 0.5))
        self.assertEqual(conformance["deviations"], {"skipped Is Book Available?": 1})
        self.assertEqual(set(mining.training().split.index), {"lib:1"})

    def test_running_cases_are_not_judged(self):
        from django.utils import timezone
        add_events(("lib:1", "Library", [step("Get Book Status", 0)]), at=timezone.now())
        self.assertEqual(self.mine().processes["Library"].conformance["checked_cases"], 0)


@override_settings(N8N_API_KEY="")
class TrainingDataTest(TestCase):
    def setUp(self):
        decision = lambda amount, branch, score: {"decider": "laya", "input": {"question": "Big?", "state": {"amount": amount}},
                                                  "output": {"branches": [branch], "scores": {branch: score}}}
        add_events(*((f"wf:{i}", "Refunds", [
            step("Register", i * 60, data={"input": {"amount": i * 100}, "output": {"ticket": f"T{i}"}}),
            step("Is It Big?", i * 60 + 1, data=decision(i * 100, "big" if i > 4 else "small", 0.9 if i % 2 else 0.55)),
            step("Approve" if i > 4 else "Refund", i * 60 + 2)]) for i in range(10)))

    def training(self):
        from ..mining import Mining
        return Mining().training()

    def test_temporal_split_without_leakage(self):
        t = self.training()
        self.assertEqual(t.split.value_counts().to_dict(), {"train": 7, "val": 1, "test": 2})
        self.assertEqual(list(t.split[t.split == "train"].index), [f"wf:{i}" for i in range(7)])  # the oldest

    def test_prefixes_carry_targets_and_only_past_data(self):
        rows = list(self.training().prefixes("train"))
        self.assertEqual(len(rows), 7 * 3)
        first, last = rows[0], rows[2]
        self.assertEqual((first["next_activity"], first["outcome"], first["known"]), ("Is It Big?", "Refund", {"amount": 0, "ticket": "T0"}))
        self.assertEqual((last["next_activity"], last["remaining_s"], len(last["prefix"])), ("■", 0, 3))
        self.assertEqual(first["remaining_s"], 2 * 60 + 1 - 1)  # until the last activity ends

    def test_decisions_and_card(self):
        t = self.training()
        decisions = list(t.decisions())
        self.assertEqual(len(decisions), 10)
        self.assertEqual({(d["taken"][0], d["outcome"]) for d in decisions}, {("small", "Refund"), ("big", "Approve")})
        card = t.card()
        self.assertEqual(card["targets"]["decision: Is It Big?"]["big"], {"train": 2, "val": 1, "test": 2})
        self.assertIn("decision: Is It Big?: 'big' only 2x in train", card["warnings"])

    def test_decision_mining(self):
        from ..mining import Mining
        report = Mining().processes["Refunds"].decisions()["Is It Big?"]
        self.assertEqual(report["branches"], {"small": 5, "big": 5})
        self.assertEqual(report["low_certainty"], 0.5)
        self.assertEqual(report["outcomes"]["big"], {"Approve": 5})
        self.assertIsNone(report["rules"])  # 10 decisions: too few to learn rules from

    def test_datasets_endpoint_streams_jsonl(self):
        import json
        r = self.client.get("/api/datasets/decisions", {"split": "test"})
        self.assertEqual([json.loads(line)["case_id"] for line in r.streaming_content], ["wf:8", "wf:9"])


class WorkflowModelTest(SimpleTestCase):
    def test_a_disabled_step_passes_through_and_a_chat_model_is_no_start(self):
        """ProcessGraph's semantics: n8n runs a disabled node as a silent pass-through; an agent's chat
        model is a sub-node, not a second way into the process."""
        from pm4py.objects.bpmn.obj import BPMN
        from ..mining import WorkflowModel
        from ..workflows import Workflow
        model = {"type": "@n8n/n8n-nodes-langchain.lmChatOllama", "typeVersion": 1, "parameters": {}}
        workflow = (Workflow("Drafts", chat_model=model).start("In", "in").task("Read", "serviceTask", "register_ticket")
                    .task("Off", "serviceTask", "register_ticket").llm_agent("Draft", "Reply to {{message}}", "draft")
                    .flow("In", "Read", "Off", "Draft").as_json())
        next(n for n in workflow["nodes"] if n["name"] == "Off")["disabled"] = True
        wf = WorkflowModel(workflow)
        start = next(n for n in wf.bpmn.get_nodes() if isinstance(n, BPMN.NormalStartEvent))
        self.assertEqual([f.get_target().get_name() for f in wf.bpmn.get_flows() if f.get_source() is start], ["In"])
        self.assertNotIn("Draft: model", wf.nodes)
        self.assertEqual(wf.outputs["Off"], [["Draft: prompt"]])  # not a gap in the flow
        net, _, _ = wf.petri_net()
        self.assertEqual(sorted(t.label for t in net.transitions if t.label), ["Draft: prompt", "Draft: usage", "Read"])  # Off is silent
