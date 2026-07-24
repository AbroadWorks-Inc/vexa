"""Unit tests for refresh_storage_state.py (cookie-refresh CronJob tool).

These cover the pure/IO-boundary helpers. The Playwright browser step
(``_capture_refreshed_state``) needs a real browser image and is exercised by the
in-cluster CronJob / E2E, not here — ``playwright`` is imported lazily so this
module imports fine without it.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest

import refresh_storage_state as rss  # noqa: E402


def _valid_state_b64(cookies: int = 2) -> str:
    state = {
        "cookies": [{"name": f"c{i}", "value": "x"} for i in range(cookies)],
        "origins": [],
    }
    return base64.b64encode(json.dumps(state).encode()).decode()


# --- decode_storage_state ---------------------------------------------------


def test_decode_storage_state_valid_base64() -> None:
    # --from-literal seeding: env value is base64(json)
    state = rss.decode_storage_state(_valid_state_b64(3))
    assert isinstance(state, dict)
    assert len(state["cookies"]) == 3


def test_decode_storage_state_valid_raw_json() -> None:
    # --from-file seeding: env value is the raw JSON (design §10B.2)
    raw = json.dumps({"cookies": [{"name": "SID", "value": "x"}], "origins": []})
    state = rss.decode_storage_state(raw)
    assert len(state["cookies"]) == 1


def test_decode_storage_state_raw_placeholder_no_cookies() -> None:
    # Raw "{}" placeholder (not base64) must also be rejected.
    with pytest.raises(rss.RefreshError, match="cookies"):
        rss.decode_storage_state("{}")


@pytest.mark.parametrize("bad", ["", "   ", "\n"])
def test_decode_storage_state_empty(bad: str) -> None:
    with pytest.raises(rss.RefreshError, match="empty"):
        rss.decode_storage_state(bad)


def test_decode_storage_state_bad_base64() -> None:
    with pytest.raises(rss.RefreshError, match="base64"):
        rss.decode_storage_state("not@@base64@@")


def test_decode_storage_state_not_json() -> None:
    b64 = base64.b64encode(b"hello world not json").decode()
    with pytest.raises(rss.RefreshError, match="JSON"):
        rss.decode_storage_state(b64)


def test_decode_storage_state_placeholder_no_cookies() -> None:
    # The 2-byte "{}" placeholder that currently sits in the secret.
    b64 = base64.b64encode(b"{}").decode()
    with pytest.raises(rss.RefreshError, match="cookies"):
        rss.decode_storage_state(b64)


# --- encode_secret_value ----------------------------------------------------


def test_encode_secret_value_roundtrip() -> None:
    original = json.dumps({"cookies": [{"name": "SID", "value": "abc"}]})
    encoded = rss.encode_secret_value(original)
    assert base64.b64decode(encoded).decode() == original


# --- looks_signed_in --------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://myaccount.google.com/",
        "https://accounts.google.com/",
        "https://accounts.google.com/b/0/AccountChooser",
    ],
)
def test_looks_signed_in_true(url: str) -> None:
    assert rss.looks_signed_in(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://accounts.google.com/v3/signin/identifier",
        "https://accounts.google.com/ServiceLogin",
        "https://accounts.google.com/signin/v2/challenge/pwd",
        "https://accounts.google.com/v3/signin/challenge",
        "",
    ],
)
def test_looks_signed_in_false(url: str) -> None:
    assert rss.looks_signed_in(url) is False


# --- build_secret_patch_url -------------------------------------------------


def test_build_secret_patch_url() -> None:
    url = rss.build_secret_patch_url(
        "https://kubernetes.default.svc", "notetaker-bots", "meet-bot-secrets"
    )
    assert url == (
        "https://kubernetes.default.svc/api/v1/namespaces/"
        "notetaker-bots/secrets/meet-bot-secrets"
    )


# --- load_incluster_auth ----------------------------------------------------


def test_load_incluster_auth_reads_projection(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("tok-123\n")
    (tmp_path / "namespace").write_text("notetaker-bots\n")
    (tmp_path / "ca.crt").write_text("CA")
    token, namespace, ca_path = rss.load_incluster_auth(tmp_path)
    assert token == "tok-123"
    assert namespace == "notetaker-bots"
    assert ca_path == str(tmp_path / "ca.crt")


def test_load_incluster_auth_missing(tmp_path: Path) -> None:
    with pytest.raises(rss.RefreshError, match="ServiceAccount not found"):
        rss.load_incluster_auth(tmp_path)


# --- patch_secret_value (httpx.MockTransport) -------------------------------


def test_patch_secret_value_success() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["ctype"] = request.headers.get("content-type")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"kind": "Secret"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rss.patch_secret_value(
            client,
            "https://k8s/api/v1/namespaces/notetaker-bots/secrets/meet-bot-secrets",
            token="tok-xyz",
            new_value_b64="QUJD",
        )

    assert captured["method"] == "PATCH"
    assert captured["auth"] == "Bearer tok-xyz"
    assert captured["ctype"] == "application/merge-patch+json"
    assert captured["body"] == {"data": {"GOOGLE_NOTETAKER_STORAGE_STATE": "QUJD"}}


@pytest.mark.parametrize("status", [401, 403, 404, 409, 500])
def test_patch_secret_value_non_2xx_raises(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "nope"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(rss.RefreshError, match=f"HTTP {status}"):
            rss.patch_secret_value(
                client,
                "https://k8s/api/v1/namespaces/notetaker-bots/secrets/meet-bot-secrets",
                token="tok",
                new_value_b64="QUJD",
            )
