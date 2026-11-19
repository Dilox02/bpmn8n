"""Sentence embeddings from the models server (POST /v1/embed: {"texts": [...]} -> {"vectors": [[...]]})."""
from urllib.parse import urlsplit

from ..conf import settings
from .http import ModelClient, ServiceUnavailable


class EmbedError(ServiceUnavailable):
    pass


class EmbedClient(ModelClient):
    error = EmbedError

    def __init__(self, url=None):
        # the models server answers all its routes at one address: the Laya URL with the embed path
        url = url or urlsplit(settings.LAYA_URL)._replace(path="/v1/embed").geturl()
        super().__init__(url, settings.MODELS_API_KEY, timeout=5.0, busy_timeout=5.0)  # a page waits on it: fail fast

    def embed(self, texts):
        """One unit vector per text."""
        vectors = self.call({"texts": texts})["vectors"]
        if len(vectors) != len(texts):
            raise EmbedError("the models server returned a different number of vectors")
        return vectors
