"""In-memory fakes for the subscription sender's ports (``sender.py``).

  * ``InMemoryDeliveryStore`` — a dict-backed ``DeliveryStore`` with the same guards as
    ``PostgresDeliveryStore``: due rows claimed in ``(next_attempt_at, id)`` order, every move out
    of ``sending`` refused unless the row is still ``sending`` under the claim's own lease, and the
    attempt row kept even when the move is refused. Its ``clock`` is what the database's ``now()``
    is to ``PostgresDeliveryStore``.

These carry NO production logic; they stand in for Postgres so the sender's scenarios run offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .sender import Claim, DeliveryResult

__all__ = ["InMemoryDeliveryStore"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class InMemoryDeliveryStore:
    """``DeliveryStore`` over dicts, with the same guards as the Postgres adapter."""

    def __init__(self, *, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self.deliveries: dict[int, dict[str, Any]] = {}
        self.attempts: list[dict[str, Any]] = []
        self.outbox: dict[str, dict[str, Any]] = {}
        self.active: dict[str, bool] = {}
        self._next_id = 1

    async def claim(self, *, lease_s: int, limit: int) -> list[Claim]:
        now = self._clock()
        lease_until = now + timedelta(seconds=lease_s)
        due = sorted(
            (
                d
                for d in self.deliveries.values()
                if d["state"] in ("pending", "sending")
                and d["next_attempt_at"] <= now
                and (d["lease_until"] is None or d["lease_until"] < now)
            ),
            key=lambda d: (d["next_attempt_at"], d["id"]),
        )[:limit]
        claims = []
        for d in due:
            d.update(state="sending", lease_until=lease_until, updated_at=now)
            event = self.outbox[d["event_id"]]
            claims.append(
                Claim(
                    id=d["id"],
                    event_id=d["event_id"],
                    event_type=event["event_type"],
                    subscription_id=d["subscription_id"],
                    user_id=d["user_id"],
                    attempt=d["attempts"] + 1,
                    lease_until=lease_until,
                    payload_text=event["payload_text"],
                )
            )
        return claims

    async def is_active(self, subscription_id: str) -> bool:
        return self.active.get(subscription_id, False)

    def _owned(self, claim: Claim) -> Optional[dict[str, Any]]:
        d = self.deliveries.get(claim.id)
        if d and d["state"] == "sending" and d["lease_until"] == claim.lease_until:
            return d
        return None

    async def cancel(self, claim: Claim) -> bool:
        d = self._owned(claim)
        if d is None:
            return False
        d.update(state="cancelled", lease_until=None, updated_at=self._clock())
        return True

    async def record(self, claim: Claim, result: DeliveryResult) -> bool:
        now = self._clock()
        if claim.id in self.deliveries:
            self.attempts.append(
                {
                    "delivery_id": claim.id,
                    "attempt": claim.attempt,
                    "outcome": result.outcome,
                    "status_code": result.status_code,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                }
            )
        d = self._owned(claim)
        if d is None:
            return False
        d.update(
            state=result.state,
            attempts=claim.attempt,
            lease_until=None,
            last_status_code=result.status_code,
            last_error=result.error,
            updated_at=now,
        )
        if result.retry_in_s is not None:
            d["next_attempt_at"] = now + timedelta(seconds=result.retry_in_s)
        return True
