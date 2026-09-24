"""meeting-api client, notetaker client, and ffmpeg transcode (spec §4.2)."""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from exporter.audio import webm_to_wav
from exporter.notetaker import Notetaker, NotetakerError
from exporter.vexa_client import MeetingApi, MeetingApiError


def test_meeting_api_sends_user_header_and_parses() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"recordings": [{"id": 7}]})

    api = MeetingApi("http://m", httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.list_recordings(user_id=3, meeting_id=9) == [{"id": 7}]
    assert seen[0].headers["X-User-Id"] == "3"
    assert seen[0].url.params["meeting_id"] == "9"


def test_meeting_api_error() -> None:
    api = MeetingApi(
        "http://m",
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    with pytest.raises(MeetingApiError):
        api.master(user_id=1, recording_id=2)


def test_meeting_api_master_parses_storage_path() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"storage_path": "aw-bots/x/master.webm"})

    api = MeetingApi("http://m", httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.master(user_id=4, recording_id=11) == {
        "storage_path": "aw-bots/x/master.webm"
    }
    assert seen[0].url.path == "/recordings/11/master"
    assert seen[0].url.params["type"] == "audio"
    assert seen[0].headers["X-User-Id"] == "4"


def test_meeting_api_transcript_parses_on_200() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"segments": []})

    api = MeetingApi("http://m", httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.transcript(user_id=5, meeting_id=42) == {"segments": []}
    assert seen[0].url.path == "/transcripts/by-id/42"
    assert seen[0].headers["X-User-Id"] == "5"


def test_meeting_api_transcript_404_returns_none() -> None:
    api = MeetingApi(
        "http://m",
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    assert api.transcript(user_id=1, meeting_id=2) is None


def test_notetaker_body_and_retry_on_5xx() -> None:
    calls: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.content)
        return httpx.Response(
            503 if len(calls) < 3 else 200, json={"status": "accepted"}
        )

    nt = Notetaker(
        "http://n",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    nt.process("vexa-1", "recordings/x/", "google_meet")
    assert len(calls) == 3
    assert json.loads(calls[0]) == {
        "meeting_id": "vexa-1",
        "s3_path": "recordings/x/",
        "platform": "google_meet",
        "idempotency_key": "vexa-1",
    }


def test_notetaker_4xx_not_retried() -> None:
    n = {"c": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(422)

    nt = Notetaker(
        "http://n",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    with pytest.raises(NotetakerError):
        nt.process("vexa-1", "recordings/x/", "zoom")
    assert n["c"] == 1


def test_notetaker_retries_on_connect_error_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("boom", request=req)
        return httpx.Response(200, json={"status": "accepted"})

    nt = Notetaker(
        "http://n",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda s: None,
    )
    nt.process("vexa-2", "recordings/y/", "zoom")
    assert calls["n"] == 2


def test_notetaker_raises_after_exhausting_retries() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    sleeps: list[float] = []
    nt = Notetaker(
        "http://n",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    with pytest.raises(NotetakerError):
        nt.process("vexa-3", "recordings/z/", "google_meet")
    assert calls["n"] == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_notetaker_retries_on_read_timeout_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("timed out", request=req)
        return httpx.Response(200, json={"status": "accepted"})

    sleeps: list[float] = []
    nt = Notetaker(
        "http://n",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    nt.process("vexa-4", "recordings/w/", "zoom")
    assert calls["n"] == 3
    assert sleeps == [2.0, 4.0]


def test_notetaker_raises_after_exhausting_retries_on_read_timeout() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out", request=req)

    sleeps: list[float] = []
    nt = Notetaker(
        "http://n",
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=sleeps.append,
    )
    with pytest.raises(NotetakerError):
        nt.process("vexa-5", "recordings/v/", "google_meet")
    assert calls["n"] == 4
    assert sleeps == [2.0, 4.0, 8.0]


def test_webm_to_wav_command_includes_mandatory_aresample_filter(
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    webm_to_wav(tmp_path / "in.webm", tmp_path / "out.wav", run=fake_run)
    assert captured[0][0] == "ffmpeg"
    assert "aresample=async=1:first_pts=0" in captured[0]


def test_webm_to_wav_raises_runtimeerror_with_last_20_stderr_lines(
    tmp_path: Path,
) -> None:
    stderr_lines = [f"line{i}" for i in range(30)]

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="\n".join(stderr_lines)
        )

    with pytest.raises(RuntimeError) as exc_info:
        webm_to_wav(tmp_path / "in.webm", tmp_path / "out.wav", run=fake_run)
    message = str(exc_info.value)
    assert "line29" in message
    assert "line0" not in message


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_webm_to_wav(tmp_path: Path) -> None:
    src = tmp_path / "in.webm"
    dst = tmp_path / "out.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "libopus", str(src)],
        check=True,
        capture_output=True,
    )
    webm_to_wav(src, dst)
    with wave.open(str(dst), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1
