"""Shared settings for the conformance suites.

§1.10: the gateway signs the identity it forwards with ``GATEWAY_IDENTITY_SECRET`` and meeting-api
believes ``x-user-id`` only with that signature. The shipped gateway and the shipped meeting-api
run in this one process, so one test-only value configures both, as one Secret does in a deploy.
"""

import pytest


@pytest.fixture(autouse=True)
def _gateway_identity(monkeypatch):
    monkeypatch.setenv(
        "GATEWAY_IDENTITY_SECRET", "test-gateway-identity-secret-conformance"
    )
