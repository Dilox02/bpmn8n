"""Secrets at rest (the LLM providers' API keys): Fernet from `cryptography`, never hand-written crypto. The key is
derived with HKDF from FIELD_ENCRYPTION_KEY, or from SECRET_KEY when that is empty: changing whichever is in use
needs `manage.py rotate_keys --from-secret-key <the old one>`, else the stored keys no longer decrypt."""
import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.db import models

from ..conf import settings

PREFIX = "enc:v1:"


class CannotDecrypt(ValueError):
    """The value was encrypted with another key."""


def fernet(secret=None):
    """Fernet keyed by `secret` (default: FIELD_ENCRYPTION_KEY, else SECRET_KEY), through HKDF-SHA256."""
    material = secret or settings.FIELD_ENCRYPTION_KEY or settings.SECRET_KEY
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"bpmn8n field encryption").derive(material.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value, secret=None):
    """"enc:v1:<Fernet token>"; empty and already encrypted values as they are."""
    if not value or encrypted(value):
        return value
    return PREFIX + fernet(secret).encrypt(value.encode()).decode()


def decrypt(value, secret=None):
    """The plain text of an encrypted value; a plain one as it is. Raises CannotDecrypt."""
    if not encrypted(value):
        return value
    try:
        return fernet(secret).decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise CannotDecrypt("encrypted with another key") from None


class EncryptedText(models.TextField):
    """Text stored encrypted and read in clear. A value the current key cannot decrypt is read as it is stored
    ("enc:v1:…", never sent anywhere: see LLMProvider.key) and saved back unchanged, so a page that loads the row
    still opens and the value can be typed again."""

    def from_db_value(self, value, expression, connection):
        try:
            return decrypt(value)
        except CannotDecrypt:
            return value

    def get_prep_value(self, value):
        return encrypt(super().get_prep_value(value))
