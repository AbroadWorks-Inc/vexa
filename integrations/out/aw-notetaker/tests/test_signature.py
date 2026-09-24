import hashlib
import hmac

from exporter.signature import verify

SECRET = "test-secret"
BODY = b'{"event_type":"meeting.completed"}'


def _headers(ts: str, secret: str = SECRET, body: bytes = BODY) -> dict[str, str]:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return {
        "X-Webhook-Signature": f"sha256={mac.hexdigest()}",
        "X-Webhook-Timestamp": ts,
    }


def test_valid() -> None:
    assert verify(BODY, _headers("1000"), SECRET, now=1100)


def test_wrong_secret() -> None:
    assert not verify(BODY, _headers("1000", secret="other"), SECRET, now=1100)


def test_tampered_body() -> None:
    assert not verify(BODY + b" ", _headers("1000"), SECRET, now=1100)


def test_stale() -> None:
    assert not verify(BODY, _headers("1000"), SECRET, now=1000 + 301)


def test_future_beyond_window() -> None:
    assert not verify(BODY, _headers("2000"), SECRET, now=1000)


def test_missing_headers() -> None:
    assert not verify(BODY, {}, SECRET, now=1000)


def test_empty_secret_fails_closed() -> None:
    assert not verify(BODY, _headers("1000", secret=""), "", now=1000)


def test_non_numeric_timestamp() -> None:
    assert not verify(BODY, _headers("abc"), SECRET, now=1000)


def test_header_lookup_is_case_insensitive() -> None:
    h = {k.lower(): v for k, v in _headers("1000").items()}
    assert verify(BODY, h, SECRET, now=1000)
