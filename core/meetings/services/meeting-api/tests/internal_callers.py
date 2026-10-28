"""The bot's and the runtime's credentials, for meeting-api's callback tests (design §1.10).

The bot sends ``x-internal-secret`` on its lifecycle callbacks (``BOT``), and the runtime posts to
the tokened ``callbackUrl`` meeting-api gave it (``runtime_callback(workload_id)``). The suite's
``INTERNAL_API_SECRET`` is ``INTERNAL_SECRET`` (``conftest.py``). The token rule is the shared one
(``core/meetings/contracts/runtime-callback``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

INTERNAL_SECRET = "test-internal-api-secret-meeting-api"
BOT = {"x-internal-secret": INTERNAL_SECRET}


def load_token_vectors() -> dict[str, Any]:
    rel = Path("meetings") / "contracts" / "runtime-callback" / "token.vectors.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            return json.loads((parent / rel).read_text())
    raise FileNotFoundError(str(rel))


def runtime_token(workload_id: str, secret: str = INTERNAL_SECRET) -> str:
    message = f"aw-runtime-callback.{workload_id}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def runtime_callback(workload_id: str) -> str:
    """The path the runtime posts ``workload_id``'s events to."""
    return f"/runtime/callback?t={runtime_token(workload_id)}"
