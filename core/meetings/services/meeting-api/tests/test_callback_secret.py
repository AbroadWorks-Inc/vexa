"""The bot's and the runtime's callbacks prove who sent them (design §1.10).

- ``POST /bots/internal/callback/lifecycle`` needs ``x-internal-secret`` equal to
  ``INTERNAL_API_SECRET``; missing or wrong is 401 and changes nothing.
- ``POST /runtime/callback`` needs the per-bot token meeting-api put in the ``callbackUrl`` it gave
  the runtime; missing, wrong or another bot's is 401 and changes nothing. The token never reaches a
  log line, including uvicorn's access log.
- The recording upload keeps its bearer (the internal secret or a MeetingToken), now compared in
  constant time.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

from internal_callers import (
    BOT,
    INTERNAL_SECRET,
    load_token_vectors,
    runtime_callback,
    runtime_token,
)
from meeting_api import create_app
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.bot_spawn.service import request_bot
from meeting_api.callback_auth import (
    install_access_log_redaction,
    runtime_callback_token,
    tokened_callback_url,
)

LIFECYCLE = "/bots/internal/callback/lifecycle"
TOKENS = load_token_vectors()


@pytest.mark.parametrize("vector", TOKENS["vectors"], ids=lambda v: v["workload_id"])
def test_the_token_matches_the_shared_vectors(vector):
    assert vector["message"] == f"aw-runtime-callback.{vector['workload_id']}"
    assert (
        runtime_callback_token(TOKENS["secret"], vector["workload_id"])
        == vector["token"]
    )


def test_the_callback_url_carries_the_token():
    url = tokened_callback_url(
        "http://meeting-api:8080/runtime/callback", "mtg-1-abcdef12", TOKENS["secret"]
    )
    token = TOKENS["vectors"][0]["token"]
    assert url == f"http://meeting-api:8080/runtime/callback?t={token}"
    assert tokened_callback_url("http://m/runtime/callback", "mtg-1-abcdef12", "") == (
        "http://m/runtime/callback"
    )


def test_a_spawn_hands_the_runtime_a_tokened_callback_url():
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    asyncio.run(
        request_bot(
            repo,
            runtime,
            user_id=1,
            platform="google_meet",
            native_meeting_id="abc-defg-hij",
            redis_url="redis://r",
            token_secret="secret",
            meeting_api_url="http://meeting-api:8080",
            internal_secret=INTERNAL_SECRET,
        )
    )
    spec = runtime.specs[-1]
    token = runtime_token(spec["workloadId"])
    assert spec["callbackUrl"] == f"http://meeting-api:8080/runtime/callback?t={token}"
    invocation = json.loads(spec["env"]["VEXA_BOT_CONFIG"])
    assert invocation["internalSecret"] == INTERNAL_SECRET
    assert invocation["meetingApiCallbackUrl"] == (
        "http://meeting-api:8080/bots/internal/callback/lifecycle"
    )


# ── the bot's lifecycle callback ───────────────────────────────────────────────────────────────


def _seeded():
    repo = InMemoryMeetingRepo()
    m = asyncio.run(
        repo.create_meeting(
            user_id=1, platform="google_meet", native_meeting_id="m1", data={}
        )
    )
    asyncio.run(repo.create_session(meeting_id=m["id"], session_uid="sess-uid"))
    return repo, m["id"]


def test_the_bot_callback_with_the_secret_is_accepted(goldens):
    repo, _ = _seeded()
    client = TestClient(create_app(meeting_repo=repo))
    r = client.post(LIFECYCLE, headers=BOT, json=goldens["joining"])
    assert r.status_code == 200, r.text
    assert r.json()["meeting_status"] == "joining"


@pytest.mark.parametrize(
    "headers",
    [{}, {"x-internal-secret": "wrong"}, {"x-internal-secret": INTERNAL_SECRET + "x"}],
    ids=["missing", "wrong", "longer"],
)
def test_the_bot_callback_without_the_secret_is_401(goldens, headers):
    repo, meeting_id = _seeded()
    client = TestClient(create_app(meeting_repo=repo))
    r = client.post(LIFECYCLE, headers=headers, json=goldens["joining"])
    assert r.status_code == 401
    assert asyncio.run(repo.get_meeting(meeting_id))["status"] == "requested"


def test_without_a_configured_secret_every_bot_callback_is_401(goldens, monkeypatch):
    monkeypatch.delenv("INTERNAL_API_SECRET")
    repo, _ = _seeded()
    client = TestClient(create_app(meeting_repo=repo))
    assert (
        client.post(LIFECYCLE, headers=BOT, json=goldens["joining"]).status_code == 401
    )


# ── the runtime's callback ─────────────────────────────────────────────────────────────────────


def test_the_runtime_callback_with_its_token_is_accepted():
    client = TestClient(create_app())
    r = client.post(
        runtime_callback("mtg-1-abcdef12"),
        json={"workloadId": "mtg-1-abcdef12", "state": "running"},
    )
    assert r.status_code == 200, r.text


@pytest.mark.parametrize(
    "path",
    [
        "/runtime/callback",
        "/runtime/callback?t=",
        f"/runtime/callback?t={'0' * 64}",
        runtime_callback("mtg-2-00000000"),
        f"/runtime/callback?t={runtime_token('mtg-1-abcdef12', secret='another-secret')}",
    ],
    ids=["missing", "empty", "wrong", "another-bots", "another-secret"],
)
def test_the_runtime_callback_without_its_token_is_401(path):
    client = TestClient(create_app())
    r = client.post(path, json={"workloadId": "mtg-1-abcdef12", "state": "destroyed"})
    assert r.status_code == 401


def test_a_runtime_callback_without_a_workload_is_401():
    client = TestClient(create_app())
    assert (
        client.post(runtime_callback(""), json={"state": "destroyed"}).status_code
        == 401
    )


def test_without_a_configured_secret_every_runtime_callback_is_401(monkeypatch):
    monkeypatch.delenv("INTERNAL_API_SECRET")
    client = TestClient(create_app())
    r = client.post(
        runtime_callback("mtg-1-abcdef12"),
        json={"workloadId": "mtg-1-abcdef12", "state": "running"},
    )
    assert r.status_code == 401


def test_a_refused_runtime_callback_drives_no_terminal(monkeypatch):
    import meeting_api.lifecycle.reconcile as reconcile

    driven = []

    async def spy(*args, **kwargs):
        driven.append(args)

    monkeypatch.setattr(reconcile, "synthesize_terminal_for_dead_workload", spy)
    client = TestClient(create_app())
    client.post(
        runtime_callback("mtg-2-00000000"),
        json={"workloadId": "mtg-1-abcdef12", "state": "destroyed"},
    )
    assert driven == []
    client.post(
        runtime_callback("mtg-1-abcdef12"),
        json={"workloadId": "mtg-1-abcdef12", "state": "destroyed"},
    )
    assert len(driven) == 1


def test_the_token_never_reaches_a_log_line(capsys, caplog):
    token = runtime_token("mtg-1-abcdef12")
    client = TestClient(create_app())
    with caplog.at_level(logging.DEBUG):
        client.post(
            runtime_callback("mtg-1-abcdef12"),
            json={"workloadId": "mtg-1-abcdef12", "state": "running"},
        )
        client.post(
            f"/runtime/callback?t={token[:-1]}0",
            json={"workloadId": "mtg-1-abcdef12", "state": "running"},
        )
    out = capsys.readouterr()
    # The test client's own httpx logger stands for the runtime's side of the call, not ours.
    ours = [r.getMessage() for r in caplog.records if not r.name.startswith("httpx")]
    everything = out.out + out.err + "\n".join(ours)
    assert "runtime_callback" in everything
    assert token not in everything and token[:-1] not in everything


def test_the_access_log_line_carries_no_token():
    install_access_log_redaction()
    install_access_log_redaction()
    access = logging.getLogger("uvicorn.access")
    token = runtime_token("mtg-1-abcdef12")
    record = access.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.5:4312", "POST", f"/runtime/callback?t={token}", "1.1", 200),
        None,
    )
    assert access.filter(record)
    assert token not in record.getMessage()
    assert "/runtime/callback?t=[redacted]" in record.getMessage()
    other = access.makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.5:4312", "GET", "/meetings?limit=5", "1.1", 200),
        None,
    )
    access.filter(other)
    assert "/meetings?limit=5" in other.getMessage()
    assert (
        len(
            [
                f
                for f in access.filters
                if type(f).__name__ == "_RedactRuntimeCallbackToken"
            ]
        )
        == 1
    )


def test_the_app_installs_the_access_log_redaction():
    create_app()
    names = [type(f).__name__ for f in logging.getLogger("uvicorn.access").filters]
    assert "_RedactRuntimeCallbackToken" in names


# ── the recording upload's bearer ──────────────────────────────────────────────────────────────


def _upload(client, bearer, media_type="audio"):
    return client.post(
        "/internal/recordings/upload",
        headers={"Authorization": f"Bearer {bearer}".encode("latin-1")},
        data={
            "metadata": json.dumps(
                {"session_uid": "no-such-session", "media_type": media_type}
            )
        },
        files={"file": ("chunk.webm", b"\x1a\x45\xdf\xa3", "video/webm")},
    )


@pytest.mark.parametrize("media_type", ["audio", "signal"])
def test_the_upload_compares_the_internal_secret_in_constant_time(
    monkeypatch, media_type
):
    import meeting_api.callback_auth as auth

    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    client = TestClient(create_app())
    # The internal secret passes auth; the upload itself then fails on its content.
    assert _upload(client, INTERNAL_SECRET, media_type).status_code not in (401, 500)
    assert calls and all(t == (bytes, bytes) for t in calls)
    # A wrong secret of the same length, and a non-ASCII one, fall through to the MeetingToken
    # check and are refused.
    assert _upload(client, "x" * len(INTERNAL_SECRET), media_type).status_code == 401
    assert _upload(client, "s\xe9cret", media_type).status_code == 401
