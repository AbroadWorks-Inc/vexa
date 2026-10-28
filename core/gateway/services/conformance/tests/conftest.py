"""Shared settings for the conformance suites.

§1.10: the gateway signs the identity it forwards with the active key of the
``GATEWAY_IDENTITY_KEYS`` ring and meeting-api believes ``x-user-id`` only with that signature. The
shipped gateway and the shipped meeting-api run in this one process, so one test-only ring
configures both, as one Secret does in a deploy.
"""

import pytest

#: A test-only one-key ring (32 bytes, base64) and its kid.
RING = '{"conformance": "dGVzdC1nYXRld2F5LWlkZW50aXR5LWNvbmZvcm1hbmM="}'


@pytest.fixture(autouse=True)
def _gateway_identity(monkeypatch):
    monkeypatch.setenv("GATEWAY_IDENTITY_KEYS", RING)
    monkeypatch.setenv("GATEWAY_IDENTITY_ACTIVE_KEY", "conformance")
