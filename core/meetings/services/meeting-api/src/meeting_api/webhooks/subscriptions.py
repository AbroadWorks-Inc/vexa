"""An account's webhook subscriptions, read from admin-api (§1.8, §2.7).

Subscriptions live in admin-api. meeting-api reads an account's active ones through admin-api's
internal door ``GET {ADMIN_API_URL}/internal/users/{id}/webhook-subscriptions`` (header
``X-Internal-Secret``), which answers ``{"user_id", "subscriptions": [...]}``: per subscription
its ``id``, ``url``, ``events``, the sealed ``secret_enc`` (base64) with its ``enc_key_id``, and
the ``previous_*`` secret only while it is still valid. No plaintext secret ever crosses; the
sender opens them with ``secret_box.py`` at signing time.

Each account's answer is cached for ``CACHE_TTL_S`` (30 s). ``find`` reads the account again once
when a subscription isn't in the cached answer (one created after it was read). A read that fails
(no admin edge configured, a transport error, a non-200 answer, a body that isn't the shape above)
raises ``SubscriptionsUnavailable`` and is never cached: an account's subscribers are unknown,
never "none".

The cache decides which subscribers exist and what they look like; it doesn't decide whether a
subscription is still active. The publisher and the sender re-read ``webhook_subscriptions.active``
from Postgres at the moment it matters (``intake/outbox.py``, ``sender.py``).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

__all__ = [
    "CACHE_TTL_S",
    "AdminSubscriptions",
    "Subscription",
    "SubscriptionSource",
    "SubscriptionsUnavailable",
]

CACHE_TTL_S = 30.0
READ_TIMEOUT_S = 10.0


class SubscriptionsUnavailable(Exception):
    """An account's subscriptions can't be read right now. The message never carries a secret."""


@dataclass(frozen=True)
class Subscription:
    """One active subscription as admin-api's internal read returns it; secrets still sealed."""

    id: str
    url: str
    events: tuple[str, ...]
    secret_enc: bytes
    enc_key_id: str
    previous_secret_enc: Optional[bytes] = None
    previous_enc_key_id: Optional[str] = None
    previous_secret_expires_at: Optional[datetime] = None

    def wants(self, event_type: str) -> bool:
        """``events == []`` means every event."""
        return not self.events or event_type in self.events

    def previous_live(self, now: datetime) -> bool:
        """Whether the previous secret still signs (the 24 h after a rotation)."""
        return (
            self.previous_secret_enc is not None
            and self.previous_enc_key_id is not None
            and self.previous_secret_expires_at is not None
            and self.previous_secret_expires_at > now
        )

    def __repr__(self) -> str:
        return f"Subscription(id={self.id!r}, events={list(self.events)!r})"


class SubscriptionSource(Protocol):
    """Where the publisher and the sender read an account's subscriptions."""

    async def for_account(self, user_id: int) -> list[Subscription]: ...

    async def find(
        self, user_id: int, subscription_id: str
    ) -> Optional[Subscription]: ...


def _bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise ValueError("not a base64 string")
    return base64.b64decode(value, validate=True)


def _instant(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("not a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse(body: Any) -> list[Subscription]:
    rows = body.get("subscriptions") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError("no subscriptions list")
    subs = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("a subscription is not an object")
        sid, url, key_id = row.get("id"), row.get("url"), row.get("enc_key_id")
        events = row.get("events") or []
        if not (
            isinstance(sid, str) and isinstance(url, str) and isinstance(key_id, str)
        ):
            raise ValueError("a subscription lacks id, url or enc_key_id")
        if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
            raise ValueError("events is not a list of strings")
        previous = row.get("previous_secret_enc")
        subs.append(
            Subscription(
                id=sid,
                url=url,
                events=tuple(events),
                secret_enc=_bytes(row.get("secret_enc")),
                enc_key_id=key_id,
                previous_secret_enc=None if previous is None else _bytes(previous),
                previous_enc_key_id=row.get("previous_enc_key_id"),
                previous_secret_expires_at=(
                    None
                    if row.get("previous_secret_expires_at") is None
                    else _instant(row["previous_secret_expires_at"])
                ),
            )
        )
    return subs


class AdminSubscriptions:
    """``SubscriptionSource`` over admin-api's internal read, cached per account."""

    def __init__(
        self,
        base_url: str,
        internal_secret: str,
        *,
        ttl_s: float = CACHE_TTL_S,
        timeout_s: float = READ_TIMEOUT_S,
        transport: Any = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._secret = internal_secret or ""
        self._ttl_s = ttl_s
        self._timeout_s = timeout_s
        self._transport = transport
        self._monotonic = monotonic
        self._cache: dict[int, tuple[float, list[Subscription]]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._secret)

    async def _read(self, user_id: int) -> list[Subscription]:
        import httpx

        if not self.configured:
            raise SubscriptionsUnavailable(
                "ADMIN_API_URL and INTERNAL_API_SECRET are not both configured"
            )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/internal/users/{user_id}/webhook-subscriptions",
                    headers={"X-Internal-Secret": self._secret},
                )
        except httpx.HTTPError as exc:
            raise SubscriptionsUnavailable(type(exc).__name__) from exc
        if resp.status_code != 200:
            raise SubscriptionsUnavailable(f"admin-api answered {resp.status_code}")
        try:
            return _parse(resp.json())
        except (ValueError, binascii.Error) as exc:
            raise SubscriptionsUnavailable(
                "admin-api's subscription read is malformed"
            ) from exc

    async def _load(self, user_id: int, *, fresh: bool) -> list[Subscription]:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(user_id)
            if cached is not None and not fresh:
                read_at, subs = cached
                if self._monotonic() - read_at < self._ttl_s:
                    return subs
            started = self._monotonic()
            subs = await self._read(user_id)
            self._cache[user_id] = (started, subs)
            return subs

    async def for_account(self, user_id: int) -> list[Subscription]:
        return list(await self._load(user_id, fresh=False))

    async def find(self, user_id: int, subscription_id: str) -> Optional[Subscription]:
        for fresh in (False, True):
            for sub in await self._load(user_id, fresh=fresh):
                if sub.id == subscription_id:
                    return sub
        return None
