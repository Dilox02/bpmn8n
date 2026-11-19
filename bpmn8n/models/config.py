"""bpmn8n's settings editable in the admin: one Settings row and the LLM providers it can use."""
from django.core.exceptions import ValidationError
from django.db import models

from ..clients.llm import LLMError, PROTOCOLS
from ..core.crypto import EncryptedText, encrypted


class Settings(models.Model):
    """The single settings row (Settings.load()); environment settings stay in bpmn8n/conf.py."""

    llm = models.ForeignKey("LLMProvider", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                            verbose_name="default LLM", help_text="The model bpmn8n Studio uses")

    class Meta:
        verbose_name = verbose_name_plural = "settings"

    def __str__(self):
        return "bpmn8n settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]

    @classmethod
    def llm_client(cls):
        if not (provider := cls.load().llm):
            raise LLMError("no default LLM: add a provider in /admin/ → Settings and choose it as default")
        return provider.client()


class LLMProvider(models.Model):
    """A chat model behind an API. The preset fills the protocol, URL and model left empty."""

    # preset: (protocol, base URL, default model). Google's is pinned: its "gemini-flash-latest" alias hung for
    # minutes at a time (October 2026), while a retired version fails at once, naming its successor
    PRESETS = {
        "google": ("openai", "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.5-flash"),
        "anthropic": ("anthropic", "https://api.anthropic.com", "claude-opus-5-5"),
        "openai": ("openai", "https://api.openai.com/v1", ""),
        "mistral": ("openai", "https://api.mistral.ai/v1", ""),
        "groq": ("openai", "https://api.groq.com/openai/v1", ""),
        "openrouter": ("openai", "https://openrouter.ai/api/v1", ""),
        # local models: their server on this machine, as Django in Docker reaches it (no API key needed;
        # with Django outside Docker use http://localhost:<port>/v1)
        "lmstudio": ("openai", "http://host.docker.internal:1234/v1", ""),
        "ollama": ("openai", "http://host.docker.internal:11434/v1", ""),
        "custom": ("openai", "", ""),  # any other OpenAI-compatible server (vLLM, llama.cpp, ...)
    }

    settings = models.ForeignKey(Settings, on_delete=models.CASCADE, related_name="providers", default=1, editable=False)
    name = models.CharField(max_length=100, unique=True)
    preset = models.CharField(max_length=20, choices=[(p, p) for p in PRESETS], default="google")
    base_url = models.URLField(blank=True, help_text="Empty: the preset's")
    model = models.CharField(max_length=100, blank=True, help_text="Empty: the preset's, e.g. gemini-3.5-flash")
    api_key = EncryptedText(blank=True, help_text="Empty for local models; stored encrypted")
    timeout = models.PositiveIntegerField(default=120, help_text="Seconds per reply: more for large local models")

    class Meta:
        verbose_name = "LLM provider"

    def __str__(self):
        return f"{self.name} ({self.model or self.PRESETS[self.preset][2]})"

    def clean(self):
        _, url, model = self.PRESETS[self.preset]
        if not (self.base_url or url):
            raise ValidationError({"base_url": "required for this preset"})
        if not (self.model or model):
            raise ValidationError({"model": "required for this preset"})

    def save(self, *args, **kwargs):
        Settings.load()  # the row providers belong to
        super().save(*args, **kwargs)

    @property
    def key(self):
        """The API key in clear; LLMError when it was encrypted with another key (core/crypto.py)."""
        if encrypted(self.api_key):
            raise LLMError(f"the API key of the LLM provider {self.name!r} does not decrypt: FIELD_ENCRYPTION_KEY or "
                           "SECRET_KEY changed since it was saved. Run `manage.py rotate_keys --from-secret-key <old>`, "
                           "or type the key again in /admin/ → Settings")
        return self.api_key

    def client(self):
        protocol, url, model = self.PRESETS[self.preset]
        return PROTOCOLS[protocol](self.base_url or url, self.key, self.model or model, self.timeout)

    def n8n_credential(self):
        """(n8n credential type, its data): the same model for the LLM agents of the workflows."""
        protocol, url, _ = self.PRESETS[self.preset]
        if self.preset == "google":
            return "googlePalmApi", {"host": "https://generativelanguage.googleapis.com", "apiKey": self.key}
        if protocol == "anthropic":
            return "anthropicApi", {"apiKey": self.key, "url": self.base_url or url}
        return "openAiApi", {"apiKey": self.key or "local", "url": self.base_url or url}  # local servers need none

    def n8n_chat_model(self, credential_id=None):
        """The n8n chat model node of an LLM agent (Workflow(chat_model=...)), with the credential if known."""
        model, kind = self.model or self.PRESETS[self.preset][2], self.n8n_credential()[0]
        node = {"googlePalmApi": ("lmChatGoogleGemini", 1.2, {"modelName": f"models/{model}", "options": {}}),
                "anthropicApi": ("lmChatAnthropic", 1.6, {"model": {"__rl": True, "mode": "id", "value": model},
                                                          "options": {}}),
                # only OpenAI itself has the Responses API; the compatible servers have chat completions
                "openAiApi": ("lmChatOpenAi", 1.3, {"model": {"__rl": True, "mode": "id", "value": model},
                                                    "responsesApiEnabled": self.preset == "openai", "options": {}})}
        type_, version, parameters = node[kind]
        return {"type": f"@n8n/n8n-nodes-langchain.{type_}", "typeVersion": version, "parameters": parameters,
                **({"credentials": {kind: {"id": credential_id, "name": f"bpmn8n LLM: {self.name}"}}}
                   if credential_id else {})}
