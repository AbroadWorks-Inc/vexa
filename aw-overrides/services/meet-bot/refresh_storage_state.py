#!/usr/bin/env python3
"""Refresh the meet-bot Google ``storage_state`` and patch its K8s Secret.

Design §13.13 / §10B.3 cookie-refresh CronJob. Runs weekly inside the cluster
(reusing the meet-bot image). It:

1. Reads the current base64 ``storage_state`` from ``GOOGLE_NOTETAKER_STORAGE_STATE``
   (injected via the ``meet-bot-secrets`` ``envFrom``).
2. Opens Chromium with that session, visits ``accounts.google.com`` to let Google
   rotate/extend the cookies, and captures a fresh ``storage_state``.
3. PATCHes the ``meet-bot-secrets`` Secret key in place via the Kubernetes REST API.

If the stored session is already dead (Google shows a sign-in wall), the session
cannot be revived unattended: the job exits non-zero so the CronJob surfaces the
failure and an operator re-seeds via ``scripts/seed_meet_bot_storage_state.py``
(design §5.2.6).

Dependency note: the Secret patch uses ``httpx`` (already present via
``notetaker-common``) against the in-cluster API, **not** the ``kubernetes`` client
— that package is orchestrator-only per §10A.2, so pulling it into the meet-bot
image would be a disallowed new dependency.

Secret guardrail: the ``storage_state`` value is never logged. Only sizes / status.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger("refresh_storage_state")

_SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_API_SERVER = "https://kubernetes.default.svc"
_STATE_ENV = "GOOGLE_NOTETAKER_STORAGE_STATE"
_SECRET_KEY = "GOOGLE_NOTETAKER_STORAGE_STATE"
_LOCAL_STATE_PATH = "/tmp/google_storage_state_current.json"
_ACCOUNTS_URL = "https://accounts.google.com/"
_NAV_TIMEOUT_MS = 30_000


class RefreshError(RuntimeError):
    """Raised when the storage_state cannot be refreshed or the Secret patched."""


def decode_storage_state(value: str) -> dict[str, Any]:
    """Decode a ``storage_state`` env value into its JSON object.

    Accepts BOTH seeding formats: raw storage_state JSON (``kubectl create secret
    --from-file`` per design §10B.2) and base64-encoded JSON (``--from-literal``
    per ``scripts/seed_meet_bot_storage_state.py``). Raw JSON is detected by a
    leading ``{`` — ``{`` is not in the base64 alphabet, so this is unambiguous.

    Raises ``RefreshError`` if the value is empty, not valid base64, not JSON, or
    is missing the Playwright ``cookies`` key (i.e. a placeholder, not a session).
    """
    if not value or not value.strip():
        raise RefreshError(f"{_STATE_ENV} is empty")
    text = value.strip()
    if text.startswith("{"):
        candidate = text
    else:
        try:
            candidate = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise RefreshError(f"{_STATE_ENV} is not valid base64: {exc}") from exc
    try:
        state = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RefreshError(f"{_STATE_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(state, dict) or "cookies" not in state:
        raise RefreshError(
            f"{_STATE_ENV} is not a Playwright storage_state (no 'cookies' key)"
        )
    return state


def encode_secret_value(state_json: str) -> str:
    """Base64-encode a storage_state JSON string for a K8s Secret ``data`` field."""
    return base64.b64encode(state_json.encode("utf-8")).decode("ascii")


def looks_signed_in(final_url: str) -> bool:
    """Heuristic: is the post-navigation URL a signed-in Google page?

    A live session lands on the account UI (``myaccount.google.com`` or an
    ``accounts.google.com`` page that is not the sign-in/identifier flow). A dead
    session redirects to the sign-in / identifier / challenge flow.
    """
    url = (final_url or "").lower()
    signin_markers = (
        "/signin",
        "servicelogin",
        "/identifier",
        "/challenge",
        "accounts.google.com/v3/signin",
    )
    if any(marker in url for marker in signin_markers):
        return False
    return "google.com" in url


def build_secret_patch_url(api_server: str, namespace: str, secret_name: str) -> str:
    """Kubernetes REST URL for patching a namespaced Secret."""
    return f"{api_server}/api/v1/namespaces/{namespace}/secrets/{secret_name}"


def load_incluster_auth(sa_dir: Path = _SA_DIR) -> tuple[str, str, str]:
    """Read the in-cluster ServiceAccount token, namespace, and CA cert path.

    Returns ``(token, namespace, ca_cert_path)``. Raises ``RefreshError`` if the
    ServiceAccount projection is absent (i.e. not running inside a pod).
    """
    token_path = sa_dir / "token"
    ns_path = sa_dir / "namespace"
    ca_path = sa_dir / "ca.crt"
    if not token_path.is_file() or not ns_path.is_file():
        raise RefreshError(
            f"in-cluster ServiceAccount not found under {sa_dir} "
            "(this tool must run inside the cluster)"
        )
    token = token_path.read_text().strip()
    namespace = ns_path.read_text().strip()
    return token, namespace, str(ca_path)


def patch_secret_value(
    client: httpx.Client,
    url: str,
    token: str,
    new_value_b64: str,
    key: str = _SECRET_KEY,
) -> None:
    """PATCH a single key of a K8s Secret's ``data`` via a merge-patch.

    Raises ``RefreshError`` on any non-2xx response. The secret value is never
    logged.
    """
    resp = client.patch(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/merge-patch+json",
            "Accept": "application/json",
        },
        content=json.dumps({"data": {key: new_value_b64}}),
    )
    if resp.status_code >= 300:
        raise RefreshError(
            f"Secret patch failed: HTTP {resp.status_code} "
            f"(body {len(resp.content)} bytes)"
        )


def _capture_refreshed_state(current_state: dict[str, Any]) -> str:
    """Launch Chromium with the current session, refresh it, return new JSON.

    Playwright is imported lazily so the pure helpers above remain unit-testable
    in environments without a browser. Raises ``RefreshError`` if the session is
    no longer signed in.
    """
    from playwright.sync_api import sync_playwright  # lazy: needs a browser image

    Path(_LOCAL_STATE_PATH).write_text(json.dumps(current_state))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,  # runs under xvfb-run (CronJob command)
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--password-store=basic",
            ],
        )
        try:
            context = browser.new_context(storage_state=_LOCAL_STATE_PATH)
            page = context.new_page()
            # domcontentloaded (not networkidle): Google keeps long-poll connections
            # open, so networkidle rarely fires and would time out. Matches join.ts.
            page.goto(
                _ACCOUNTS_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
            )
            page.wait_for_timeout(3000)  # let any post-load redirect settle
            final_url = page.url
            if not looks_signed_in(final_url):
                raise RefreshError(
                    "stored Google session is no longer signed in "
                    "(sign-in wall detected); operator must re-seed"
                )
            fresh = context.storage_state()
            return json.dumps(fresh)
        finally:
            browser.close()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    secret_name = os.environ.get("SECRET_NAME", "meet-bot-secrets")
    try:
        current_b64 = os.environ.get(_STATE_ENV, "")
        current_state = decode_storage_state(current_b64)
        logger.info(
            "loaded current storage_state (%d cookies)",
            len(current_state.get("cookies", [])),
        )

        fresh_json = _capture_refreshed_state(current_state)
        new_value_b64 = encode_secret_value(fresh_json)
        logger.info("captured refreshed storage_state (%d bytes)", len(fresh_json))

        token, namespace, ca_path = load_incluster_auth()
        url = build_secret_patch_url(_API_SERVER, namespace, secret_name)
        with httpx.Client(verify=ca_path, timeout=30.0) as client:
            patch_secret_value(client, url, token, new_value_b64)
        logger.info("patched secret %s/%s key %s", namespace, secret_name, _SECRET_KEY)
        return 0
    except RefreshError as exc:
        logger.error("refresh failed: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level guard: report + non-zero exit
        logger.exception("unexpected error during refresh: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
