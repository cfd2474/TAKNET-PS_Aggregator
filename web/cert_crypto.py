"""
Encrypt/decrypt CoT push client certificates at rest.
Uses Fernet (symmetric) with key derived from SECRET_KEY.
Cert content is never returned by any API; only the CoT sender may decrypt for TLS.
"""

import os
import base64
import hashlib


def _fernet_key():
    secret = os.environ.get("SECRET_KEY", "taknet-ps-dev-key-change-me")
    digest = hashlib.sha256(secret.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_cert(data: str) -> str:
    """Encrypt a cert/key PEM string for storage. Returns base64-encoded ciphertext."""
    if not data or not data.strip():
        raise ValueError("empty cert data")
    try:
        from cryptography.fernet import Fernet
        f = Fernet(_fernet_key())
        return f.encrypt(data.encode()).decode()
    except Exception as e:
        raise ValueError(f"encrypt failed: {e}")


def decrypt_cert(encrypted: str) -> str:
    """Decrypt stored cert/key. For backend use only (e.g. CoT sender). Never expose to API."""
    if not encrypted:
        raise ValueError("empty encrypted data")
    try:
        from cryptography.fernet import Fernet
        f = Fernet(_fernet_key())
        return f.decrypt(encrypted.encode()).decode()
    except Exception as e:
        raise ValueError(f"decrypt failed: {e}")
