"""Kubernetes backend. The lifecycle test needs a real cluster and is skipped without one.
The kubectl-answer tests stub kubectl and run anywhere."""
import json
import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest

import runtime_kernel.k8s_backend as k8s_backend
from runtime_kernel import Runtime
from runtime_kernel.backend import WorkloadHandle
from runtime_kernel.k8s_backend import K8sBackend
from runtime_kernel.models import BackendKind, RuntimeState, StopReason, WorkloadSpec, WorkloadStatus
from runtime_kernel.profiles import Runnable
from runtime_kernel.store import WorkloadRecord


def _k8s_ok() -> bool:
    if not shutil.which("kubectl"):
        return False
    return subprocess.run(["kubectl", "get", "nodes"], capture_output=True).returncode == 0


def _answer(*, code: int, stdout: str = "", stderr: str = ""):
    def fake_kubectl(*args, check=True, stdin=None):
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    return fake_kubectl


def _running_runtime(monkeypatch, kubectl) -> Runtime:
    monkeypatch.setattr(k8s_backend, "_kubectl", kubectl)
    runtime = Runtime(backend=K8sBackend(namespace="ns"), profiles={})
    spec = WorkloadSpec(workloadId="w1", profile="meeting-bot", env={})
    runtime.store.set(WorkloadRecord(
        spec=spec,
        status=WorkloadStatus(
            workloadId="w1", profile="meeting-bot",
            state=RuntimeState.running, backend=BackendKind.k8s,
        ),
        owner="",
    ))
    runtime._handles["w1"] = WorkloadHandle(id="w1", impl="vexa-w1")
    return runtime


def _pod_exists(name: str) -> bool:
    return subprocess.run(["kubectl", "get", "pod", name], capture_output=True).returncode == 0


def _phase(name: str) -> str:
    return subprocess.run(
        ["kubectl", "get", "pod", name, "-o", "jsonpath={.status.phase}"],
        capture_output=True, text=True,
    ).stdout.strip()


def test_a_kubectl_timeout_leaves_the_running_bot_running(monkeypatch):
    runtime = _running_runtime(monkeypatch, _answer(
        code=1, stderr="Unable to connect to the server: dial tcp 10.0.0.1:443: i/o timeout",
    ))
    got = runtime.get("w1")
    assert got.state is RuntimeState.running
    assert got.exitCode is None
    with pytest.raises(RuntimeError, match="kubectl delete pod vexa-w1 failed"):
        runtime.destroy("w1")
    assert runtime.store.get("w1").status.state is RuntimeState.running
    with pytest.raises(RuntimeError, match="kubectl delete pod vexa-w1 failed"):
        runtime.stop("w1")
    assert runtime.store.get("w1").status.state is RuntimeState.stopping


def test_a_missing_pod_is_a_real_exit_and_a_delete_of_it_is_accepted(monkeypatch):
    runtime = _running_runtime(monkeypatch, _answer(
        code=1, stderr='Error from server (NotFound): pods "vexa-w1" not found',
    ))
    got = runtime.get("w1")
    assert got.state is RuntimeState.stopped
    assert got.exitCode == 0
    assert got.stopReason is StopReason.completed
    runtime.destroy("w1")
    assert runtime.store.get("w1").status.state is RuntimeState.destroyed


def test_a_succeeded_pod_is_still_a_clean_exit(monkeypatch):
    runtime = _running_runtime(monkeypatch, _answer(
        code=0, stdout=json.dumps({"status": {"phase": "Succeeded"}}),
    ))
    got = runtime.get("w1")
    assert got.state is RuntimeState.stopped
    assert got.exitCode == 0
    assert got.stopReason is StopReason.completed


def test_a_failed_pod_keeps_the_container_exit_code(monkeypatch):
    body = {"status": {"phase": "Failed", "containerStatuses": [
        {"state": {"terminated": {"exitCode": 137}}},
    ]}}
    runtime = _running_runtime(monkeypatch, _answer(code=0, stdout=json.dumps(body)))
    got = runtime.get("w1")
    assert got.state is RuntimeState.stopped
    assert got.exitCode == 137
    assert got.stopReason is StopReason.failed


def test_a_running_pod_stays_running(monkeypatch):
    runtime = _running_runtime(monkeypatch, _answer(
        code=0, stdout=json.dumps({"status": {"phase": "Running"}}),
    ))
    assert runtime.get("w1").state is RuntimeState.running


@pytest.mark.skipif(not _k8s_ok(), reason="no reachable kubernetes cluster")
def test_k8s_backend_real_pod_lifecycle():
    name = "vexa-rt-k8stest"
    subprocess.run(["kubectl", "delete", "pod", name, "--ignore-not-found",
                    "--grace-period=0", "--force"], capture_output=True)  # clean slate
    rt = Runtime(
        backend=K8sBackend(),
        profiles={"test": Runnable(image="alpine", command=["sleep", "30"])},
        grace_sec=30.0,
    )
    spec = WorkloadSpec(workloadId="rt-k8stest", profile="test", env={"VEXA_X": "y"})
    try:
        rt.create(spec)
        assert rt.get("rt-k8stest").state is RuntimeState.running
        assert _pod_exists(name)                              # a REAL pod object exists

        deadline = time.time() + 90                           # wait for it to actually schedule & run
        while time.time() < deadline and _phase(name) != "Running":
            time.sleep(1)
        assert _phase(name) == "Running"                      # genuinely scheduled & running

        rt.stop("rt-k8stest")
        assert rt.get("rt-k8stest").state is RuntimeState.stopped

        rt.destroy("rt-k8stest")
        assert rt.get("rt-k8stest").state is RuntimeState.destroyed
        assert not _pod_exists(name)                          # pod actually removed
    finally:
        subprocess.run(["kubectl", "delete", "pod", name, "--ignore-not-found",
                        "--grace-period=0", "--force"], capture_output=True)
