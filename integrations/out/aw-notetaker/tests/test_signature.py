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


def _rotated(ts: str, new: str, old: str) -> dict[str, str]:
    """A delivery in the 24 h after `rotate-secret`: the new secret in
    `X-Webhook-Signature`, the previous one in `X-Webhook-Signature-Previous`
    (webhook.v1 SignatureHeaders.rotated)."""
    headers = _headers(ts, secret=new)
    old_mac = hmac.new(old.encode(), f"{ts}.".encode() + BODY, hashlib.sha256)
    headers["X-Webhook-Signature-Previous"] = f"sha256={old_mac.hexdigest()}"
    return headers


def test_a_receiver_still_on_the_old_secret_accepts_the_previous_header() -> None:
    assert verify(BODY, _rotated("1000", "new", SECRET), SECRET, now=1100)


def test_a_receiver_already_on_the_new_secret_accepts_the_signature() -> None:
    assert verify(BODY, _rotated("1000", SECRET, "old"), SECRET, now=1100)


def test_neither_header_matching_is_rejected() -> None:
    assert not verify(BODY, _rotated("1000", "new", "old"), SECRET, now=1100)


def test_the_previous_header_alone_is_accepted() -> None:
    """Only the header's match counts, whichever of the two carries it."""
    headers = _rotated("1000", "new", SECRET)
    del headers["X-Webhook-Signature"]
    assert verify(BODY, headers, SECRET, now=1100)


def test_a_previous_signature_outside_the_window_is_rejected() -> None:
    assert not verify(BODY, _rotated("1000", "new", SECRET), SECRET, now=1000 + 301)


def test_the_window_edge_is_accepted() -> None:
    assert verify(BODY, _headers("1000"), SECRET, now=1000 + 300)
    assert verify(BODY, _headers("1300"), SECRET, now=1000)
