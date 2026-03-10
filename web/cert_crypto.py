"""
Encrypt/decrypt CoT push client certificates at rest.
Uses Fernet (symmetric) with key derived from SECRET_KEY.
Cert content is never returned by any API; only the CoT sender may decrypt for TLS.
PKCS#12 (.p12/.pfx) load extracts certificate and private key for storage.
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


def load_pkcs12_to_pem(p12_bytes: bytes, password: str = None) -> tuple:
    """
    Load a PKCS#12 (.p12/.pfx) file and return (cert_pem, key_pem) as strings.
    password: optional; use None or empty string if the p12 is not password-protected.
    Raises ValueError on invalid or wrong password.
    """
    if not p12_bytes or len(p12_bytes) < 10:
        raise ValueError("P12 file is empty or too short")
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12, Encoding, PrivateFormat, NoEncryption
    except ImportError as e:
        raise ValueError(f"P12 support unavailable: {e}") from e

    # None or empty string => no password. Try None first; some p12s need b"" instead.
    pw = None
    if password is not None and str(password).strip():
        pw = str(password).strip().encode("utf-8")

    def load(p12_data, pwd):
        private_key, certificate, _ = pkcs12.load_key_and_certificates(p12_data, pwd)
        if private_key is None or certificate is None:
            raise ValueError("P12 does not contain a private key and certificate")
        key_pem = private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        ).decode("utf-8")
        cert_pem = certificate.public_bytes(Encoding.PEM).decode("utf-8")
        return (cert_pem.strip(), key_pem.strip())

    try:
        return load(p12_bytes, pw)
    except ValueError as e:
        # Cryptography raises ValueError for bad password; don't re-raise blindly — retry and/or friendly message
        msg = str(e).lower()
        if pw is None and ("mac" in msg or "decrypt" in msg or "password" in msg or "invalid" in msg or "pkcs12" in msg):
            try:
                return load(p12_bytes, b"")
            except Exception:
                pass
        if "password" in msg or "mac" in msg or "decrypt" in msg or "invalid" in msg or "pkcs12" in msg:
            hint = " This file is password-protected (we tried no password and empty). Use the export password from your TAK Server, or re-export the .p12 without a password." if pw is None else " If the file has no password, leave the field blank."
            raise ValueError("Wrong or invalid P12 password." + hint) from e
        raise
    except Exception as e:
        msg = str(e).lower()
        if pw is None and ("mac" in msg or "decrypt" in msg or "password" in msg or "invalid" in msg or "pkcs12" in msg):
            try:
                return load(p12_bytes, b"")
            except Exception:
                pass
        if "password" in msg or "mac" in msg or "decrypt" in msg or "invalid" in msg or "pkcs12" in msg:
            hint = " This file is password-protected (we tried no password and empty). Use the export password from your TAK Server, or re-export the .p12 without a password." if pw is None else " If the file has no password, leave the field blank."
            raise ValueError("Wrong or invalid P12 password." + hint) from e
        raise ValueError(f"Failed to read P12 file: {e}") from e
