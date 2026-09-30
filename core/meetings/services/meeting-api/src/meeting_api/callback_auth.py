"""Who may call meeting-api's callback routes (design §1.10).

The bots and the runtime reach meeting-api directly, not through the gateway, so the identity
guard exempts their routes and each route checks its caller here:

- ``POST /bots/internal/callback/lifecycle``: the bot sends ``x-internal-secret``, the
  ``INTERNAL_API_SECRET`` meeting-api put in its invocation. It must equal meeting-api's own,
  compared in constant time.
- ``POST /runtime/callback``: the runtime posts to the ``callbackUrl`` meeting-api gave it, verbatim
  and with no headers of its own. That URL carries a per-bot token,
  ``<MEETING_API_URL>/runtime/callback?t=<token>``, where ``token`` is the hex HMAC-SHA256 of
  ``"aw-runtime-callback.<workloadId>"`` keyed by ``INTERNAL_API_SECRET``. meeting-api names the
  workload when it spawns the bot and the runtime echoes ``workloadId`` in every event, so the route
  recomputes the token from the event body. The rule is pinned by
  ``core/meetings/contracts/runtime-callback/token.vectors.json``.

Without ``INTERNAL_API_SECRET`` every callback is refused. The token is never logged: uvicorn's
access log line for ``/runtime/callback`` has its query replaced (``install_access_log_redaction``).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Optional

INTERNAL_SECRET_HEADER = "x-internal-secret"
RUNTIME_CALLBACK_PATH = "/runtime/callback"
TOKEN_PARAM = "t"
_TOKEN_CONTEXT = "aw-runtime-callback."
_REDACTED_PATH = f"{RUNTIME_CALLBACK_PATH}?{TOKEN_PARAM}=[redacted]"


def internal_secret() -> str:
    """``INTERNAL_API_SECRET``, or ``""`` when it is not configured."""
    return os.getenv("INTERNAL_API_SECRET") or ""


def secret_matches(presented: Optional[str], expected: str) -> bool:
    """True when both are set and equal, compared in constant time."""
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


def runtime_callback_token(secret: str, workload_id: str) -> str:
    """The per-bot token for ``workload_id``'s runtime callbacks."""
    message = f"{_TOKEN_CONTEXT}{workload_id}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def tokened_callback_url(callback_url: str, workload_id: str, secret: str) -> str:
    """``callback_url`` (``<MEETING_API_URL>/runtime/callback``) with ``workload_id``'s token, as
    handed to the runtime. Without a secret there is no token to give, and the route refuses the
    callbacks."""
    if not secret:
        return callback_url
    return f"{callback_url}?{TOKEN_PARAM}={runtime_callback_token(secret, workload_id)}"


def runtime_callback_token_ok(
    token: Optional[str], workload_id: Optional[str], secret: str
) -> bool:
    """True when ``token`` is ``workload_id``'s, compared in constant time."""
    if not secret or not token or not workload_id:
        return False
    expected = runtime_callback_token(secret, workload_id)
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


class _RedactRuntimeCallbackToken(logging.Filter):
    """Replaces the query of a ``/runtime/callback`` request in uvicorn's access log line, whose
    arguments are ``(client, method, path with query, http version, status)``."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) >= 3
            and isinstance(args[2], str)
            and args[2].startswith(f"{RUNTIME_CALLBACK_PATH}?")
        ):
            record.args = (*args[:2], _REDACTED_PATH, *args[3:])
        return True


def install_access_log_redaction() -> None:
    """Keep the runtime-callback token out of uvicorn's access log (idempotent)."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactRuntimeCallbackToken) for f in access.filters):
        access.addFilter(_RedactRuntimeCallbackToken())
