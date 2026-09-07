from __future__ import annotations

from cryptography.fernet import Fernet, MultiFernet

from src.config import env


def _fernets() -> MultiFernet:
    # First key is used for new encryptions; any key in the list can decrypt,
    # which is what makes key rotation possible later without a migration —
    # add a new key to the front, redeploy, old ciphertexts still decrypt.
    keys = [k.strip() for k in env("TOKEN_ENCRYPTION_KEYS").split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt_token(plaintext: str) -> str:
    return _fernets().encrypt(plaintext.encode()).decode()


def decrypt_token(ciphertext: str) -> str:
    return _fernets().decrypt(ciphertext.encode()).decode()
