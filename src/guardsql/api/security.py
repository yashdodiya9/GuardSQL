"""API-key authentication and per-client rate limiting.

The server stores only SHA-256 hashes of keys, so a leaked .env or config dump does not give
anyone a usable key. Keys are random 256-bit tokens, so a fast hash is appropriate: a slow
password hash exists to protect low-entropy secrets, and these are not.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
import time
from collections.abc import Callable

from fastapi import Request

from guardsql.config import Settings

_CLIENT_NAME = re.compile(r"[A-Za-z0-9_.-]{1,40}")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def parse_api_keys(raw: str) -> dict[str, str]:
    """'alice:<hash>,bob:<hash>' -> {'alice': '<hash>', 'bob': '<hash>'}. Never echoes a hash."""
    clients: dict[str, str] = {}
    for entry in filter(None, (part.strip() for part in raw.split(","))):
        name, separator, digest = entry.partition(":")
        digest = digest.lower()
        if not separator or not _CLIENT_NAME.fullmatch(name) or not _SHA256_HEX.fullmatch(digest):
            raise ValueError("API_KEYS entries must look like name:sha256hex")
        if name in clients:
            raise ValueError(f"API_KEYS lists client {name!r} twice")
        clients[name] = digest
    return clients


def extract_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


class ClientAuth:
    def __init__(self, clients: dict[str, str]) -> None:
        self._clients = dict(clients)

    @classmethod
    def from_settings(cls, settings: Settings) -> ClientAuth:
        clients = parse_api_keys(settings.api_keys)
        if not clients and settings.app_env == "production":
            # Fail closed: an open API in front of an LLM and a database is not a default.
            raise RuntimeError("API_KEYS must be set when APP_ENV=production")
        return cls(clients)

    @property
    def enabled(self) -> bool:
        return bool(self._clients)

    def identify(self, presented: str | None) -> str | None:
        """The client's name, or None. Compares every entry so timing does not reveal which."""
        if not presented:
            return None
        digest = hash_key(presented)
        match: str | None = None
        for name, expected in self._clients.items():
            if hmac.compare_digest(digest, expected):
                match = name
        return match


class RateLimiter:
    """Token bucket per client: `burst` requests at once, refilled at `per_minute`."""

    def __init__(
        self, per_minute: int, burst: int, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._rate = per_minute / 60.0
        self._capacity = float(burst)
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # client -> (tokens, last seen)

    @classmethod
    def from_settings(cls, settings: Settings) -> RateLimiter:
        return cls(settings.rate_limit_per_minute, settings.rate_limit_burst)

    def check(self, client: str) -> float | None:
        """None if the request may proceed, otherwise the seconds until it could."""
        if self._rate <= 0:
            return None
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(client, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._rate)
            if tokens >= 1.0:
                self._buckets[client] = (tokens - 1.0, now)
                return None
            self._buckets[client] = (tokens, now)
            return (1.0 - tokens) / self._rate
