"""PII protection: keyed hash for matching, Fernet encryption for storage.

Key source: env var AU_PII_KEY (a Fernet key) if set, otherwise a key file
next to the database ("<db path>.key"), generated on first use so a clean clone
runs with zero setup. Production would fetch the key from a KMS / secret manager
instead of a local file and rotate it there.
"""

import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet


class Pii:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)
        self._hmac_key = hashlib.sha256(b"phone-hmac:" + key).digest()

    @classmethod
    def load(cls, db_path: str) -> "Pii":
        env = os.environ.get("AU_PII_KEY")
        if env:
            return cls(env.encode())
        key_file = Path(str(db_path) + ".key")
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
        return cls(key_file.read_bytes().strip())

    def phone_hash(self, canonical_phone: str) -> str:
        """Deterministic keyed hash, used to match leads and events by phone."""
        return hmac.new(self._hmac_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()
