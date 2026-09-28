"""meeting-api client (through the gateway), notetaker client, and ffmpeg
transcode (spec §4.2, design §1.9)."""

from __future__ import annotations

import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import httpx
import pytest

from exporter.audio import join_wavs, join_webm, webm_to_wav
from exporter.notetaker import Notetaker, NotetakerError
from exporter.vexa_client import MeetingApi, MeetingApiError
from tests.builders import wav_samples, write_constant_wav


KEY = "test-exporter-key"


def _api(handler: Any) -> MeetingApi:
    return MeetingApi(
        "http://gateway/", KEY, httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_meeting_api_reads_through_the_gateway_with_the_exporter_key() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"recordings": [{"id": 7}]})

    assert _api(handler).list_recordings(meeting_id=9) == [{"id": 7}]
    assert str(seen[0].url) == "http://gateway/recordings?meeting_id=9"
    assert seen[0].headers["X-API-Key"] == KEY
    assert seen[0].url.params["meeting_id"] == "9"


def test_meeting_api_never_sends_a_user_id_or_the_internal_secret() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/recordings":
            return httpx.Response(200, json={"recordings": []})
        return httpx.Response(200, json={"storage_path": "x"})

    api = _api(handler)
    api.list_recordings(meeting_id=1)
    api.master(recording_id=2)
    api.transcript(meeting_id=3)
    assert len(seen) == 3
    for req in seen:
        names = {name.lower() for name in req.headers}
        assert "x-user-id" not in names
        assert "x-internal-secret" not in names
        assert "authorization" not in names


def test_meeting_api_error() -> None:
    api = _api(lambda r: httpx.Response(404))
    with pytest.raises(MeetingApiError) as exc_info:
        api.master(recording_id=2)
    assert KEY not in str(exc_info.value)


def test_meeting_api_master_parses_storage_path() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"storage_path": "aw-bots/x/master.webm"})

    assert _api(handler).master(recording_id=11) == {
        "storage_path": "aw-bots/x/master.webm"
    }
    assert seen[0].url.path == "/recordings/11/master"
    assert seen[0].url.params["type"] == "audio"
    assert seen[0].headers["X-API-Key"] == KEY


def test_meeting_api_transcript_parses_on_200() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"segments": []})

    assert _api(handler).transcript(meeting_id=42) == {"segments": []}
    assert seen[0].url.path == "/transcripts/by-id/42"
    assert seen[0].headers["X-API-Key"] == KEY


def test_meeting_api_transcript_404_returns_none() -> None:
    assert _api(lambda r: httpx.Response(404)).transcript(meeting_id=2) is None


def test_meeting_api_transcript_error_raises() -> None:
    with pytest.raises(MeetingApiError):
        _api(lambda r: httpx.Response(403)).transcript(meeting_id=2)


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


def test_join_wavs_puts_each_part_after_its_silence(tmp_path: Path) -> None:
    write_constant_wav(tmp_path / "a.wav", 0.5, 7)
    write_constant_wav(tmp_path / "b.wav", 0.25, -3)
    dst = tmp_path / "joined.wav"

    join_wavs([(tmp_path / "a.wav", 0), (tmp_path / "b.wav", 30)], dst)

    samples, rate = wav_samples(dst.read_bytes())
    assert rate == 100
    assert samples == [7] * 50 + [0] * 30 + [-3] * 25


def test_join_wavs_refuses_parts_of_different_formats(tmp_path: Path) -> None:
    write_constant_wav(tmp_path / "a.wav", 0.5, 1, rate=100)
    write_constant_wav(tmp_path / "b.wav", 0.5, 1, rate=200)

    with pytest.raises(ValueError, match="format"):
        join_wavs(
            [(tmp_path / "a.wav", 0), (tmp_path / "b.wav", 0)], tmp_path / "out.wav"
        )


def test_join_webm_decodes_every_part_with_the_dtx_filter_and_pads_the_gaps(
    tmp_path: Path,
) -> None:
    captured: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    parts = [(tmp_path / "a.webm", 0.0), (tmp_path / "b.webm", 2.5)]
    join_webm(parts, tmp_path / "out.webm", run=fake_run)

    cmd = captured[0]
    assert cmd[0] == "ffmpeg" and cmd[-1] == str(tmp_path / "out.webm")
    assert [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"] == [
        str(tmp_path / "a.webm"),
        str(tmp_path / "b.webm"),
    ]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert graph.count("aresample=async=1:first_pts=0") == 2
    assert "atrim=duration=2.500000" in graph
    assert "concat=n=3:v=0:a=1" in graph


def test_join_webm_raises_runtimeerror_with_the_stderr_tail(tmp_path: Path) -> None:
    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="bad input")

    with pytest.raises(RuntimeError, match="bad input"):
        join_webm([(tmp_path / "a.webm", 0.0)], tmp_path / "out.webm", run=fake_run)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_join_webm_with_ffmpeg_keeps_the_gap_on_the_timeline(tmp_path: Path) -> None:
    for name in ("a", "b"):
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "libopus",
                str(tmp_path / f"{name}.webm"),
            ],
            check=True,
            capture_output=True,
        )
    joined = tmp_path / "joined.webm"

    join_webm([(tmp_path / "a.webm", 0.0), (tmp_path / "b.webm", 2.0)], joined)

    wav_path = tmp_path / "joined.wav"
    webm_to_wav(joined, wav_path)
    samples, rate = wav_samples(wav_path.read_bytes())
    assert abs(len(samples) / rate - 4.0) <= 0.1

    def loud(start_s: float, end_s: float) -> float:
        span = samples[int(start_s * rate) : int(end_s * rate)]
        return max(abs(v) for v in span)

    assert loud(0.2, 0.8) > 1000
    assert loud(1.3, 2.7) == 0
    assert loud(3.2, 3.8) > 1000
