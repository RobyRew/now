"""Sealing the Spotify sessions at rest.

An sp_dc cookie is a whole Spotify web session, so it is only ever written to disk sealed: AES-256-GCM with a fresh
96-bit nonce per write, and the slot number as associated data, so a sealed session copied into another slot's row
does not open there. The key comes from the environment (NOW_KEY) and is never written to the volume, so a backup of
the volume holds ciphertext only.

Layout of a sealed value: version (1 byte) | key id (4 bytes) | nonce (12 bytes) | ciphertext and tag.
The key id is the first 4 bytes of SHA-256(key); it says which key sealed a value without saying anything about the
key, so a value sealed under NOW_KEY_PREVIOUS can be recognised and sealed again under NOW_KEY.
"""

from __future__ import annotations

import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = b"\x01"
_HEADER = 1 + 4 + 12


class SealError(Exception):
    """A sealed value that no configured key opens (wrong key, wrong slot, or damaged)."""


def key_id(key: bytes) -> bytes:
    return hashlib.sha256(key).digest()[:4]


def _associated_data(slot: int) -> bytes:
    return f"now:sp_dc:slot={slot}".encode()


class Sealer:
    """Seals with the current key; opens with the current or the previous one."""

    def __init__(self, key: bytes, previous: bytes | None = None) -> None:
        if len(key) != 32 or (previous is not None and len(previous) != 32):
            raise ValueError("keys must be 32 bytes")
        self._kid = key_id(key)
        self._keys = {self._kid: AESGCM(key)}
        if previous is not None and key_id(previous) != self._kid:
            self._keys[key_id(previous)] = AESGCM(previous)

    def seal(self, plaintext: str, slot: int) -> bytes:
        nonce = os.urandom(12)
        body = self._keys[self._kid].encrypt(nonce, plaintext.encode(), _associated_data(slot))
        return VERSION + self._kid + nonce + body

    def open(self, sealed: bytes, slot: int) -> tuple[str, bool]:
        """The plaintext, and whether it should be sealed again under the current key."""
        if len(sealed) <= _HEADER or sealed[:1] != VERSION:
            raise SealError("not a sealed value")
        kid, nonce, body = sealed[1:5], sealed[5:17], sealed[17:]
        aead = self._keys.get(kid)
        if aead is None:
            raise SealError("sealed under a key that is not configured")
        try:
            plaintext = aead.decrypt(nonce, body, _associated_data(slot))
        except InvalidTag:
            raise SealError("does not open with this key and slot") from None
        return plaintext.decode(), kid != self._kid
