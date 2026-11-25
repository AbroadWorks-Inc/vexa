"""Stage-2 (API) gate — drive the full runtime.v1 lifecycle OVER HTTP, assert the status responses +
the delivered RuntimeEvents all conform to the frozen contract."""
import json
from pathlib import Path

import jsonschema
from fastapi.testclient import TestClient
from referencing import Registry, Resource

from runtime_kernel import Runtime
from runtime_kernel.api import create_app
from conftest import INTERNAL

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "runtime.v1" / "runtime.schema.json").read_text()
)
_REGISTRY = Registry().with_resource(SCHEMA["$id"], Resource.from_contents(SCHEMA))


def _conforms(obj: dict, shape: str) -> None:
    jsonschema.Draft202012Validator(
        {"$ref": f"{SCHEMA['$id']}#/$defs/{shape}"}, registry=_REGISTRY
    ).validate(obj)


def test_lifecycle_over_http_conforms():
    events = []
    app = create_app(Runtime(profiles={"test": ["sleep", "30"]}, grace_sec=3.0), deliver=events.append)
    client = TestClient(app, headers=INTERNAL)

    r = client.post("/workloads", json={"workloadId": "w1", "profile": "test", "env": {}})
    assert r.status_code == 201
    _conforms(r.json(), "WorkloadStatus")

    assert client.get("/workloads/w1").json()["state"] == "running"
    assert any(s["workloadId"] == "w1" for s in client.get("/workloads").json())

    s = client.post("/workloads/w1/stop", json={"reason": "stopped"})
    assert s.status_code == 200 and s.json()["state"] == "stopped"
    _conforms(s.json(), "WorkloadStatus")

    d = client.delete("/workloads/w1")
    assert d.status_code == 200 and d.json()["state"] == "destroyed"

    # the API delivered the full legal lifecycle, every event conforming to runtime.v1
    assert [e.state.value for e in events] == ["starting", "running", "stopping", "stopped", "destroyed"]
    for e in events:
        _conforms(json.loads(e.model_dump_json(exclude_none=True)), "RuntimeEvent")


def test_unknown_profile_is_400_and_unknown_workload_404():
    client = TestClient(create_app(Runtime(profiles={})), headers=INTERNAL)
    assert client.post("/workloads", json={"workloadId": "x", "profile": "nope", "env": {}}).status_code == 400
    assert client.get("/workloads/missing").status_code == 404


def test_double_create_over_http_touches_not_respawns():
    """runtime.v1 idempotent create (ADR 0027): a second POST /workloads for a running workloadId
    returns its live status — no second spawn, no duplicate lifecycle events."""
    events = []
    app = create_app(Runtime(profiles={"test": ["sleep", "30"]}, grace_sec=3.0), deliver=events.append)
    client = TestClient(app, headers=INTERNAL)
    try:
        first = client.post("/workloads", json={"workloadId": "w1", "profile": "test", "env": {}})
        assert first.status_code == 201 and first.json()["state"] == "running"

        touched = client.post("/workloads", json={"workloadId": "w1", "profile": "test", "env": {}})
        assert touched.status_code == 201 and touched.json()["state"] == "running"
        _conforms(touched.json(), "WorkloadStatus")

        # one spawn's worth of events — the touch emitted nothing new
        assert [e.state.value for e in events] == ["starting", "running"]
    finally:
        client.post("/workloads/w1/stop", json={"reason": "stopped"})
        client.delete("/workloads/w1")


def test_every_route_but_health_requires_the_internal_secret():
    """The workload and schedule surfaces start processes and fire requests on a caller's word: a
    request without the internal secret, or with a wrong one, is refused before the kernel is asked;
    a runtime with no secret configured admits nobody."""
    rt = Runtime(profiles={})
    client = TestClient(create_app(rt, internal_secret="runtime-test-secret"))
    routes = [("POST", "/workloads"), ("GET", "/workloads"), ("GET", "/workloads/w"),
              ("POST", "/workloads/w/stop"), ("DELETE", "/workloads/w"),
              ("POST", "/schedule"), ("GET", "/schedule"), ("DELETE", "/schedule/j")]
    for method, path in routes:
        assert client.request(method, path).status_code == 401, (method, path)
        assert client.request(method, path, headers={"x-internal-secret": "wrong"}).status_code == 401
    assert client.get("/workloads", headers={"x-internal-secret": "runtime-test-secret"}).status_code == 200
    assert client.get("/health").status_code in (200, 503)

    unset = TestClient(create_app(Runtime(profiles={}), internal_secret=""))
    assert unset.get("/workloads", headers={"x-internal-secret": ""}).status_code == 401
