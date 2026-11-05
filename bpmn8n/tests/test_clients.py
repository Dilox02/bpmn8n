import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from ..clients import web
from ..clients.extraction import field_specs, to_number
from ..clients.http import ServiceUnavailable, post_json
from ..clients.laya import LayaClient
from ..clients.llm import Anthropic, LLMError, OpenAICompatible
from ..models import LLMProvider, Settings
from .support import FakeLLM, LocalServer


class LayaRetryTest(TestCase):
    @patch("bpmn8n.clients.http.time.sleep")
    @patch("bpmn8n.clients.http.post_json")
    def test_busy_laya_is_retried(self, post, sleep):
        busy = ServiceUnavailable("503", status=503, retry_after=1)
        post.side_effect = [busy, busy, {"answers": {"q": {"choice": "a", "answer_confidence": 0.9}}}]
        self.assertEqual(LayaClient(url="http://laya", busy_timeout=10).choose("s", "q?", {"a": "x"}), ("a", 0.9))
        self.assertEqual(sleep.call_count, 2)

    @patch("bpmn8n.clients.http.time.sleep")
    @patch("bpmn8n.clients.http.post_json")
    def test_timed_out_attempt_is_retried(self, post, sleep):
        """A hung connection (seen through Docker Desktop's host proxy) must not lose the decision."""
        post.side_effect = [ServiceUnavailable("timed out", timed_out=True),
                            {"answers": {"q": {"choice": "a", "answer_confidence": 0.9}}}]
        self.assertEqual(LayaClient(url="http://laya", busy_timeout=30).choose("s", "q?", {"a": "x"}), ("a", 0.9))

    @patch("bpmn8n.clients.http.post_json", side_effect=ServiceUnavailable("503", status=503, retry_after=1))
    def test_still_busy_after_timeout_is_503(self, post):
        with self.settings(LAYA_BUSY_TIMEOUT=0):
            r = self.client.post("/api/gateways/exclusive", {"question": "q", "state": "s", "branches": {"a": "x"}},
                                 content_type="application/json")
        self.assertEqual((r.status_code, post.call_count), (503, 1))


class HttpTest(SimpleTestCase):
    def test_text_reply_is_a_success(self):
        """n8n's resume webhook answers 200 with a non-JSON body: that is not a failure."""
        with LocalServer(b"Workflow was started") as server:
            self.assertEqual(post_json(server.url, {}), {"text": "Workflow was started"})

    def test_model_calls_reuse_one_connection(self):
        with LocalServer() as server:
            for _ in range(5):
                post_json(server.url, {}, keep_alive=True)
            self.assertEqual(server.connections, 1)

    def test_http_error_carries_status(self):
        with LocalServer(b"busy", status=503) as server, self.assertRaises(ServiceUnavailable) as caught:
            post_json(server.url, {})
        self.assertTrue(caught.exception.busy)

    def test_silent_server_is_a_timeout(self):
        """Real post_json against a socket that accepts but never answers (a hung proxy)."""
        import socket
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            with self.assertRaises(ServiceUnavailable) as caught:
                post_json(f"http://127.0.0.1:{server.getsockname()[1]}/", {}, timeout=0.5)
        self.assertTrue(caught.exception.timed_out)
        self.assertFalse(caught.exception.busy)


class ExtractionTest(TestCase):
    def test_numbers_in_italian_and_english_notation(self):
        cases = {"49.90 EUR": 49.9, "49,90 €": 49.9, "1.299,50 €": 1299.5, "$1,299.50": 1299.5,
                 "12,000 units": 12000, "1 299,50": 1299.5, "about 42": 42, "no number": None}
        for text, number in cases.items():
            self.assertEqual(to_number(text), number, text)

    def test_values_go_to_gliner_and_judgments_to_laya(self):
        from projects.acme.tasks import Expense, RefundRequest
        specs = {s.name: s for s in field_specs(Expense)}
        self.assertFalse(specs["amount"].judged)
        self.assertEqual(specs["amount"].from_span("180,00 EUR"), 180.0)
        self.assertEqual(specs["attendees"].from_span("2 people"), 2)
        self.assertEqual(list(specs["category"].question["criteria"]), ["meal", "travel", "hotel", "other"])
        dup = {s.name: s for s in field_specs(RefundRequest)}["duplicate_charge"]
        self.assertEqual(dup.question["type"], "noul")
        self.assertEqual(dup.from_answer({"noul": 0.05}), (False, 0.95))

    @patch("bpmn8n.clients.extraction.LayaClient.ask",
           return_value={"duplicate_charge": {"noul": 0.92}, "amount": {"noul": 0.88}})
    @patch("bpmn8n.clients.extraction.Gliner.call",
           return_value={"results": [{"amount": {"value": "49,90 EUR", "confidence": 0.97}}]})
    def test_task_runs_from_text(self, gliner, laya):
        r = self.client.post("/api/tasks/calculate_refund/run",
                             {"text": "You charged us twice for 49,90 EUR", "verify": True},
                             content_type="application/json")
        self.assertEqual(r.json()["result"], {"refund": 49.9, "automatic": True})
        self.assertEqual(r.json()["extracted"], {"amount": {"value": 49.9, "confidence": 0.97, "verified": 0.88},
                                                 "duplicate_charge": {"value": True, "confidence": 0.92}})

    @patch("bpmn8n.clients.extraction.LayaClient.ask",
           return_value={"duplicate_charge": {"noul": 0.1}, "amount": {"noul": 0.05}})
    @patch("bpmn8n.clients.extraction.Gliner.call",
           return_value={"results": [{"amount": {"value": "4 years", "confidence": 0.8}}]})
    def test_value_laya_rejects_is_dropped(self, gliner, laya):
        r = self.client.post("/api/tasks/calculate_refund/run", {"text": "Customer for 4 years", "verify": True},
                             content_type="application/json")
        self.assertEqual(r.status_code, 422)  # "4 years" is not an amount: missing, not wrong
        self.assertNotIn("amount", r.json()["extracted"])

    @patch("bpmn8n.clients.extraction.LayaClient.ask", return_value={"duplicate_charge": {"noul": 0.6}})
    @patch("bpmn8n.clients.extraction.Gliner.call", return_value={"results": [{}]})
    def test_missing_fields_are_422_and_hand_values_fill_them(self, gliner, laya):
        url = "/api/tasks/calculate_refund/run"
        missing = self.client.post(url, {"text": "hello"}, content_type="application/json")
        self.assertEqual(missing.status_code, 422)
        self.assertIn("amount", missing.json()["problems"])
        filled = self.client.post(url, {"text": "hello", "fields": {"amount": 10}}, content_type="application/json")
        self.assertEqual(filled.json()["result"], {"refund": 10.0, "automatic": True})


class LLMTest(TestCase):
    def test_openai_compatible_providers_share_one_client(self):
        reply = {"choices": [{"message": {"content": 'Here it is:\n```json\n{"question": "Who approves?"}\n```'}}]}
        with LocalServer(json.dumps(reply).encode()) as server:
            llm = OpenAICompatible(server.url.rstrip("/"), "k3y", "gemini-2.5-flash")
            self.assertEqual(llm.ask("You interview.", [{"role": "user", "content": "Purchases"}]),
                             {"question": "Who approves?"})
            path, headers, body = server.requests[0]
        self.assertEqual((path, headers["Authorization"]), ("/chat/completions", "Bearer k3y"))
        sent = json.loads(body)
        self.assertEqual((sent["model"], sent["messages"][0]["role"], sent["messages"][1]["content"]),
                         ("gemini-2.5-flash", "system", "Purchases"))

    def test_a_reply_without_json_is_an_error(self):
        with LocalServer(json.dumps({"choices": [{"message": {"content": "Sorry."}}]}).encode()) as server:
            with self.assertRaisesRegex(LLMError, "did not reply with JSON"):
                OpenAICompatible(server.url, "", "m").ask("s", [])
            self.assertEqual(len(server.requests), 2)  # it got one more try
            self.assertNotIn("Authorization", server.requests[0][1])  # a local server needs no key

    def test_a_used_up_quota_fails_at_once(self):
        quota = b'{"error": {"code": 429, "message": "You exceeded your current quota"}}'
        with LocalServer(quota, status=429) as server, self.assertRaisesRegex(LLMError, "quota"):
            OpenAICompatible(server.url, "", "m").complete("s", [])
        self.assertEqual(len(server.requests), 1)

    def test_a_reasoning_model_thinks_first(self):
        reply = {"choices": [{"message": {"content": '<think>{maybe "x"?}</think>\n{"question": "Who?"}'}}]}
        with LocalServer(json.dumps(reply).encode()) as server:
            self.assertEqual(OpenAICompatible(server.url, "", "qwen/qwen3.5-9b").ask("s", []), {"question": "Who?"})

    def test_reasoning_with_only_its_closing_tag(self):
        """Qwen and DeepSeek in LM Studio: the chat template opens <think>, the reply only closes it."""
        llm = FakeLLM()
        llm.complete = lambda system, messages: 'the user wants {"a": 1}? no...</think>\n```json\n{"question": "Who?"}\n```'
        self.assertEqual(llm.ask("s", []), {"question": "Who?"})

    def test_code_comes_in_a_python_block_and_must_parse(self):
        llm = FakeLLM("def f(:\n    pass", "def f():\n    return 1")
        self.assertEqual(llm.code("Write f.", []), "def f():\n    return 1")
        self.assertIn("does not parse", llm.calls[1][1][-1]["content"])  # told why, once

    @patch("anthropic.Anthropic")
    def test_anthropic_uses_its_sdk_with_fallbacks(self, sdk):
        create = sdk.return_value.beta.messages.create
        create.return_value = SimpleNamespace(stop_reason="end_turn", content=[
            SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text='{"done": true}')])
        self.assertEqual(Anthropic("https://api.anthropic.com", "sk", "claude-opus-5-5").ask("s", []), {"done": True})
        kwargs = create.call_args.kwargs
        self.assertEqual((kwargs["fallbacks"], kwargs["betas"]), ("default", ["server-side-fallback-2026-07-01"]))
        create.return_value = SimpleNamespace(stop_reason="refusal", content=[])
        with self.assertRaisesRegex(LLMError, "declined"):
            Anthropic("https://api.anthropic.com", "sk", "claude-opus-5-5").complete("s", [])

    def test_providers_come_from_the_settings(self):
        with self.assertRaisesRegex(LLMError, "no default LLM"):
            Settings.llm_client()
        google = LLMProvider.objects.create(name="Gemini", preset="google", api_key="k")
        settings = Settings.load()
        settings.llm = google
        settings.save()
        client = Settings.llm_client()
        self.assertIsInstance(client, OpenAICompatible)
        self.assertEqual((client.base_url, client.model),
                         ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash"))
        self.assertIsInstance(LLMProvider(name="C", preset="anthropic").client(), Anthropic)
        local = LLMProvider(name="L", preset="lmstudio", model="qwen/qwen3.5-9b", timeout=600).client()
        self.assertEqual((local.base_url, local.api_key, local.timeout), ("http://host.docker.internal:1234/v1", "", 600))
        with self.assertRaisesRegex(ValidationError, "model"):  # OpenAI has no default model: say which one
            LLMProvider(name="O", preset="openai").full_clean()

    def test_ollama_uses_chat_completions_in_studio_and_n8n(self):
        provider = LLMProvider(name="Ollama", preset="ollama", model="local-demo", timeout=600)
        client = provider.client()
        self.assertIsInstance(client, OpenAICompatible)
        self.assertEqual(client.base_url, "http://host.docker.internal:11434/v1")
        self.assertEqual((client.api_key, client.timeout), ("", 600))
        kind, credential = provider.n8n_credential()
        self.assertEqual(kind, "openAiApi")
        self.assertEqual(credential["url"], client.base_url)
        self.assertFalse(provider.n8n_chat_model()["parameters"]["responsesApiEnabled"])


class WebTest(SimpleTestCase):
    def test_a_pages_text_and_only_public_addresses(self):
        parser = web.Text()
        parser.feed("<html><head><style>x{}</style></head><body><nav>Menu</nav><h1>Leave</h1><p>Ask 5 days "
                    "ahead.<script>steal()</script></p><footer>(c)</footer></body></html>")
        self.assertEqual(parser.parts, ["Leave", "Ask 5 days ahead."])
        for url in ("http://127.0.0.1:8000/admin/", "http://10.0.0.8/", "http://[::1]/", "file:///etc/passwd",
                    "http://localhost:5678/"):
            with self.assertRaises(web.WebError, msg=url):
                web.public(url)

    @patch("bpmn8n.clients.web.request_json")
    def test_search_results(self, request_json):
        request_json.return_value = {"results": [{"url": "https://a.example/", "title": "A", "content": "a"},
                                                 {"url": "javascript:alert(1)", "title": "B"}]}
        with self.settings(SEARCH_URL="http://searxng:8080/"):
            self.assertEqual(web.search("leave law"), [{"title": "A", "url": "https://a.example/", "snippet": "a"}])
        self.assertEqual(request_json.call_args.args[1], "http://searxng:8080/search?q=leave+law&format=json")
