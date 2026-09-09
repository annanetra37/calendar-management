"""Symmetric encryption for secrets stored at rest (Google refresh tokens)."""

from __future__ import annotations

import functools

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class TokenDecryptionError(RuntimeError):
    """The stored ciphertext cannot be read with the configured key."""


@functools.lru_cache(maxsize=1)
def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenDecryptionError(
            "Stored token could not be decrypted; FERNET_KEY may have been rotated."
        ) from exc


def generate_key() -> str:
    """Helper for operators: `python -c 'from app.crypto import generate_key; ...'`."""
    return Fernet.generate_key().decode()
