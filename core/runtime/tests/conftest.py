"""The runtime API admits only callers presenting INTERNAL_API_SECRET (api.py); the suite runs with a
test-only value, and a client that drives the API sends it as ``INTERNAL``."""
import os

os.environ.setdefault("INTERNAL_API_SECRET", "runtime-unit-test-internal-secret")
INTERNAL = {"x-internal-secret": os.environ["INTERNAL_API_SECRET"]}
