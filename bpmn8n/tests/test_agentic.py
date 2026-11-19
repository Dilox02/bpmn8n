"""Agentic patterns as vetted templates (J1), guardrails (J3), error paths (C3, J7), oversight modes (H10):
each macro's shape and that it passes the workflow check once a person follows it; the guardrail task on
every kind of personal data and its traps; the error output and its rules; the audit sample and its rule."""
from types import SimpleNamespace

from django.contrib.auth.models import Group
from django.test import RequestFactory, TestCase

from ..api.tasks import TASKS
from ..core.observability import CASE_HEADER
from ..studio.prompts import Prompts
from ..tasks import (AuditInput, CritiqueInput, GuardrailInput, audit_share, check_guardrails, draw_audit_sample,
                     read_critique)
from ..workflows import SpecError, Workflow
from ..workflows.check import WorkflowCheck
from ..workflows.graph import ProcessGraph


def reviewed(step, **kwargs):
    """A webhook, the pattern called "Pattern", then a person who approves its answer."""
    w = Workflow("Draft Reply").start("In", "in")
    getattr(w, step)("Pattern", **kwargs)
    w.user_task("Approve Reply", "Approve the reply", "staff", [("ok", "Approve", "boolean")])
    return w.flow("In", "Pattern", "Approve Reply").layout()


def problems(w, *rules):
    check = WorkflowCheck(w.as_json(), fetch=lambda _: None, start_data=["message"])
    return {(p.node, p.rule, p.level) for p in check.problems if not rules or p.rule in rules}


def errors(w):
    return {p for p in problems(w) if p[2] == "error"}


def hops(w):
    """{(source, output label, target)}"""
    g = ProcessGraph(w.as_json())
    return {(s, g.outputs(s)[i], t) for s in g.nodes for i, port in enumerate(g.outs[s]) for t in port}


class PatternsTest(TestCase):
    def setUp(self):
        Group.objects.create(name="staff")

    def test_a_prompt_chain_runs_its_agents_in_order_each_on_the_answer_before(self):
        w = reviewed("prompt_chain", prompts=["List the claims in {{message}}", "Draft a reply to {{previous}}"],
                     output="reply")
        self.assertIn(("Pattern 1: answer", "main", "Pattern 2: prompt"), hops(w))
        self.assertIn(("Pattern 2: answer", "main", "Approve Reply"), hops(w))
        self.assertEqual(w.node("Pattern 2: prompt")["parameters"]["input"]["value"]["prompt"],
                         "Draft a reply to {{reply_1}}")
        self.assertEqual(w.node("Pattern 2: answer")["parameters"]["assignments"]["assignments"][0]["name"], "reply")
        self.assertEqual(errors(w), set())
        with self.assertRaises(SpecError):  # J4: a budget of links
            reviewed("prompt_chain", prompts=["{{message}}"] * 6, output="reply")
        with self.assertRaises(SpecError):
            reviewed("prompt_chain", prompts=["{{previous}}", "b"], output="reply")

    def test_parallel_agents_are_joined_once_with_their_answers_by_key(self):
        w = reviewed("parallel_llm", prompts={"legal": "Legal view of {{message}}", "medical": "Medical view of {{message}}"},
                     output="views")
        self.assertTrue({("Pattern", "legal", "Pattern: legal: prompt"), ("Pattern", "medical", "Pattern: medical: prompt"),
                         ("Pattern: join", "main", "Pattern: answers"), ("Pattern: answers", "main", "Approve Reply")}
                        <= hops(w))
        join = w.node("Pattern: join")["parameters"]
        self.assertEqual((join["mode"], join["combineBy"], join["numberInputs"]), ("combine", "combineByPosition", 2))
        value = w.node("Pattern: answers")["parameters"]["assignments"]["assignments"][0]
        self.assertEqual((value["name"], value["type"]), ("views", "object"))
        self.assertIn('"legal": $json.views_legal', value["value"])
        self.assertEqual(errors(w), set())
        self.assertEqual(problems(w, "implicit-join", "wait"), set())

    def test_reflection_loops_back_with_the_critique_until_it_passes_or_the_budget_ends(self):
        w = reviewed("reflection", prompt="Draft a reply to {{message}}", critique="Polite, under 100 words",
                     output="reply", max_rounds=3)
        self.assertTrue({("Pattern: first round", "main", "Pattern: prompt"), ("Pattern: answer", "main", "Pattern: critic: prompt"),
                         ("Pattern: critic: answer", "main", "Pattern: verdict"), ("Pattern: verdict", "main", "Pattern: again?"),
                         ("Pattern: again?", "again", "Pattern: prompt"), ("Pattern: again?", "done", "Approve Reply")}
                        <= hops(w))
        self.assertIn("{{reply_critique}}", w.node("Pattern: prompt")["parameters"]["input"]["value"]["prompt"])
        self.assertEqual(w.node("Pattern: verdict")["parameters"]["input"]["value"]["max_rounds"], 3)
        self.assertEqual(errors(w), set())
        self.assertEqual(problems(w, "loop", "endless-loop", "variable"), set())  # a guaranteed exit, data on first entry
        with self.assertRaises(SpecError):
            reviewed("reflection", prompt="p", critique="c", output="reply", max_rounds=6)

    def test_the_critic_s_verdict_counts_the_rounds_and_always_ends(self):
        run = lambda critique, done: read_critique(RequestFactory().post("/"), CritiqueInput(  # noqa: E731
            critique=critique, reflection_round=done, max_rounds=2))
        self.assertEqual(run("**PASS** - clear and polite", 0).model_dump(),
                         {"passed": True, "again": False, "reflection_round": 1})
        self.assertEqual(run("REVISE: too long", 0).model_dump(), {"passed": False, "again": True, "reflection_round": 1})
        self.assertEqual(run("REVISE: still too long", 1).model_dump()["again"], False)  # the budget
        self.assertFalse(run("(The LLM did not answer: 503)", 0).passed)  # a failed critic never passes

    def test_a_guardrailed_agent_sends_a_blocked_text_to_a_person_and_the_redacted_prompt_to_the_llm(self):
        w = reviewed("guardrailed_agent", prompt="Draft a reply to {{message}}", output="reply", on_block="staff",
                     output_checks={"pii": True, "schema": ["reply"]})
        self.assertTrue({("Pattern: input check: passed?", "passed", "Pattern: prompt"),
                         ("Pattern: input check: passed?", "blocked", "Pattern: blocked"),
                         ("Pattern: answer", "main", "Pattern: output check"),
                         ("Pattern: output check: passed?", "blocked", "Pattern: blocked"),
                         ("Pattern: output check: passed?", "passed", "Approve Reply")} <= hops(w))
        self.assertEqual(w.node("Pattern: prompt")["parameters"]["input"]["value"]["prompt"], "{{redacted}}")
        self.assertEqual(w.node("Pattern: output check")["parameters"]["input"]["value"],
                         {"text": "{{reply}}", "checks": "pii,schema", "keys": "reply"})
        self.assertEqual(errors(w), set())

    def test_a_guardrail_alone_passes_or_blocks_and_its_settings_reach_the_task(self):
        w = reviewed("guardrail", text="{{message}}", checks={"pii": True, "length": 100, "blocklist": ["iban", "pin"]})
        self.assertEqual(w.node("Pattern")["parameters"]["input"]["value"],
                         {"text": "{{message}}", "checks": "pii,length,blocklist", "max_length": 100, "blocklist": "iban,pin"})
        self.assertIn(("Pattern: passed?", "output", "warning"), problems(w))  # "blocked" leads nowhere yet
        w.user_task("Fix Message", "Fix it", "staff", [("note", "Note", "string")]).flow("Pattern", "Fix Message", output="blocked")
        self.assertEqual(problems(w, "output"), set())
        for wrong in (["pii", "virus"], [], ["blocklist"]):
            with self.assertRaises(SpecError):
                reviewed("guardrail", text="{{message}}", checks=wrong)

    def test_every_pattern_is_a_spec_step(self):
        spec = {"name": "Draft Reply", "steps": [
            {"type": "start", "name": "In", "path": "in"},
            {"type": "guardrail", "name": "Screen Message", "text": "{{message}}", "checks": ["pii"]},
            {"type": "reflection", "name": "Draft Answer", "prompt": "Answer {{message}}", "critique": "Polite",
             "output": "answer"},
            {"type": "user_task", "name": "Approve Answer", "title": "Approve", "lane": "staff",
             "form": [["ok", "OK", "boolean"]]},
            {"type": "user_task", "name": "Answer Personally", "title": "Answer", "lane": "staff",
             "form": [["answer", "Answer", "string"]]}],
            "flows": [{"from": "In", "to": ["Screen Message", "Draft Answer", "Approve Answer"]},
                      {"from": "Screen Message", "branch": "blocked", "to": ["Answer Personally"]}]}
        w = Workflow.from_spec(spec)
        self.assertIn(("Screen Message: passed?", "blocked", "Answer Personally"), hops(w))
        self.assertEqual(errors(w), set())

    def test_studio_learns_the_patterns_but_not_their_plumbing_as_code_to_imitate(self):
        prompts = Prompts(SimpleNamespace(slug=""))
        self.assertTrue(all(f"- {step}(" in prompts.steps for step in ("guardrail", "reflection", "guardrailed_agent")))
        self.assertTrue(set(Workflow.STEP_TASKS) <= set(TASKS))
        self.assertNotIn("def check_guardrails", prompts.existing_code)


class GuardrailTaskTest(TestCase):
    def check(self, text, checks="pii", **settings):
        return check_guardrails(RequestFactory().post("/"), GuardrailInput(text=text, checks=checks, **settings))

    def test_each_kind_of_personal_data_is_found_and_redacted(self):
        for text, kind in (("write to mario.rossi@example.it today", "email"),
                           ("call me on +39 333 123 4567 after six", "phone"),
                           ("IBAN IT60 X054 2811 1010 0000 0123 456 please", "iban"),
                           ("IBAN IT60X0542811101000000123456", "iban"),
                           ("my fiscal code is RSSMRA85T10A562S", "fiscal_code"),
                           ("card 4111 1111 1111 1111 exp 12/29", "card")):
            result = self.check(text)
            self.assertEqual([(f.check, f.what) for f in result.findings], [("pii", kind)], text)
            self.assertFalse(result.passed)
            self.assertIn(f"[{kind.upper()}]", result.redacted)
            start, end = map(int, result.findings[0].where.split("-"))
            self.assertNotIn(text[start:end], result.redacted)

    def test_numbers_that_only_look_personal_are_left_alone(self):
        for text in ("card 4111 1111 1111 1112",  # 16 digits that fail Luhn
                     "IBAN IT61 X054 2811 1010 0000 0123 456",  # fails mod 97
                     "the meeting is on 2026-11-18 at 10:30 in room 12",
                     "the total is 1.250,00 EUR for 3 items",
                     "ticket ABC-2026-77, code XK12"):
            result = self.check(text)
            self.assertEqual((result.passed, result.findings, result.redacted), (True, [], text), text)

    def test_redaction_happens_whatever_the_checks(self):
        result = self.check("reply to anna@example.org", checks="length")
        self.assertTrue(result.passed)
        self.assertEqual(result.redacted, "reply to [EMAIL]")

    def test_length_blocklist_and_schema(self):
        self.assertEqual([f.check for f in self.check("x" * 11, "length", max_length=10).findings], ["length"])
        self.assertTrue(self.check("x" * 10, "length", max_length=10).passed)
        found = self.check("Send the PIN, then the pin again; pinball is fine", "blocklist", blocklist="pin, iban").findings
        self.assertEqual([(f.what, f.where) for f in found], [("pin", "9-12"), ("pin", "23-26")])
        self.assertTrue(self.check('```json\n{"reply": "ok", "why": "x"}\n```', "schema", keys="reply,why").passed)
        self.assertEqual([f.what for f in self.check('{"reply": "ok"}', "schema", keys="reply,why").findings], ["no key 'why'"])
        self.assertEqual([f.what for f in self.check("sure, here it is", "schema").findings], ["not JSON"])
        self.assertEqual([f.what for f in self.check("[1, 2]", "schema").findings], ["not a JSON object"])

    def test_data_is_checked_as_json_and_unknown_checks_are_refused(self):
        self.assertTrue(GuardrailInput(text={"a": "x"}).text == '{"a": "x"}')
        with self.assertRaises(ValueError):
            GuardrailInput(text="x", checks="pii,virus")


class ErrorPathsTest(TestCase):
    def setUp(self):
        Group.objects.create(name="staff")

    def flaky(self, **kwargs):
        return Workflow("Book Slot").start("In", "in").task(
            "Book Slot", "scriptTask", "compose_prompt", prompt="{{message}}", **kwargs).flow("In", "Book Slot")

    def test_on_error_and_retry_become_n8n_settings_and_an_error_output(self):
        node = self.flaky(on_error="branch", retry=2, retry_wait_ms=2000).node("Book Slot")
        self.assertEqual({k: node[k] for k in ("onError", "retryOnFail", "maxTries", "waitBetweenTries")},
                         {"onError": "continueErrorOutput", "retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000})
        self.assertEqual(self.flaky(on_error="continue").node("Book Slot")["onError"], "continueRegularOutput")
        self.assertNotIn("onError", self.flaky().node("Book Slot"))
        for wrong in ({"on_error": "ignore"}, {"retry": 5}, {"retry": 1, "retry_wait_ms": 9000}):
            with self.assertRaises(SpecError):
                self.flaky(**wrong)

    def test_the_error_output_is_flowed_and_checked(self):
        w = self.flaky(on_error="branch", retry=2)
        self.assertEqual(ProcessGraph(w.as_json()).outputs("Book Slot"), ["main", "error"])
        self.assertEqual(problems(w, "output"), {("Book Slot", "output", "warning")})  # the error leads nowhere
        w.user_task("Book By Phone", "Book by phone", "staff", [("slot", "Slot", "string")])
        w.flow("Book Slot", "Book By Phone", output="error")
        self.assertEqual(w.connections["Book Slot"]["main"], [[], [{"node": "Book By Phone", "type": "main", "index": 0}]])
        self.assertEqual(problems(w, "output", "error-path"), set())  # its main output may end the case

    def test_a_retried_step_whose_failure_reaches_no_person_is_an_info(self):
        self.assertEqual(problems(self.flaky(retry=2), "error-path"), {("Book Slot", "error-path", "info")})
        robot = self.flaky(on_error="branch", retry=2).timer("Wait", 30, "seconds").flow("Book Slot", "Wait", output="error")
        self.assertEqual(problems(robot, "error-path"), {("Book Slot", "error-path", "info")})
        self.assertEqual(problems(self.flaky(), "error-path"), set())  # no retries: nothing promised

    def test_the_error_path_does_not_carry_what_the_failed_step_would_have_produced(self):
        w = Workflow("Book Slot").start("In", "in").task("Book Slot", "scriptTask", "check_guardrails", on_error="branch")
        w.user_task("Book By Phone", "Book {{redacted}}", "staff", [("slot", "Slot", "string")])
        w.flow("In", "Book Slot").flow("Book Slot", "Book By Phone", output="error")
        self.assertIn(("Book By Phone", "variable", "error"), problems(w))


class OversightTest(TestCase):
    def setUp(self):
        Group.objects.create(name="staff")

    def audited(self, **kwargs):
        w = Workflow("Draft Reply").start("In", "in").llm_agent("Draft", "Reply to {{message}}", "reply",
                                                                oversight="audit", **kwargs)
        w.user_task("Audit Reply", "Audit the reply", "staff", [("ok", "Fine", "boolean")])
        w.task("Send Reply", "scriptTask", "compose_prompt", prompt="{{reply}}")
        return w.flow("In", "Draft", "Send Reply").flow("Draft", "Audit Reply", "Send Reply", output="audit").layout()

    def test_audit_draws_a_sample_and_the_rest_go_on(self):
        w = self.audited(risk="low", audit_rate=0.2)
        self.assertEqual(w.node("Draft: audit sample")["parameters"]["input"]["value"], {"rate": 0.2, "risk": "low"})
        self.assertTrue({("Draft: answer", "main", "Draft: audit sample"), ("Draft: audit?", "continue", "Send Reply"),
                         ("Draft: audit?", "audit", "Audit Reply")} <= hops(w))

    def test_a_low_risk_audited_agent_is_an_info_any_other_an_error(self):
        self.assertEqual(problems(self.audited(risk="low"), "human"), {("Draft", "human", "info")})
        self.assertEqual(problems(self.audited(), "human"), {("Draft", "human", "error")})
        bypass = self.audited(risk="low")  # a path that skips the sample is never audited
        bypass.flow("Draft: answer", "Send Reply")
        self.assertEqual(problems(bypass, "human"), {("Draft", "human", "error")})
        with self.assertRaises(SpecError):
            self.audited(risk="medium")

    def test_the_sample_is_deterministic_and_about_the_rate(self):
        def drawn(case, rate=0.1):
            request = RequestFactory().post("/", headers={CASE_HEADER: case})
            return draw_audit_sample(request, AuditInput(rate=rate)).audited
        self.assertEqual([drawn(str(i)) for i in range(50)], [drawn(str(i)) for i in range(50)])
        self.assertTrue(80 <= sum(drawn(str(i)) for i in range(1000)) <= 120)
        self.assertEqual(drawn("42"), audit_share("42") < 0.1)
        self.assertTrue(all(drawn(str(i), rate=1) for i in range(20)))
        self.assertTrue(draw_audit_sample(RequestFactory().post("/"), AuditInput(rate=0.01)).audited)  # no case: audited
