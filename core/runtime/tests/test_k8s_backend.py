"""Kubernetes backend. The lifecycle test needs a real cluster and is skipped without one.
The kubectl-answer tests stub kubectl and run anywhere."""
import json
import logging
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


def test_a_failed_pod_keeps_its_log_and_is_then_deleted(monkeypatch, caplog):
    """The exit is stored before the pod object is removed, and a later read keeps it.

    kubectl answers NotFound with exit 0. Deleting first would wipe a real failure.
    """
    body = {"status": {"phase": "Failed", "containerStatuses": [
        {"state": {"terminated": {"exitCode": 1}}},
    ]}}
    calls: list[tuple] = []
    gone = {"pod": False}

    def fake_kubectl(*args, check=True, stdin=None):
        calls.append(tuple(args))
        if args[0] == "logs":
            assert not gone["pod"]
            return SimpleNamespace(
                returncode=0, stdout="DisconnectedError EndCause 72\n", stderr="",
            )
        if args[0] == "delete":
            stored = runtime.store.get("w1").status
            assert stored.state is RuntimeState.stopped
            assert stored.exitCode == 1
            gone["pod"] = True
            return SimpleNamespace(returncode=0, stdout="deleted", stderr="")
        if gone["pod"]:
            return SimpleNamespace(
                returncode=1, stdout="",
                stderr='Error from server (NotFound): pods "vexa-w1" not found',
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")

    runtime = _running_runtime(monkeypatch, fake_kubectl)
    with caplog.at_level(logging.INFO, logger="runtime_kernel.k8s"):
        got = runtime.get("w1")
    assert got.state is RuntimeState.stopped
    assert got.exitCode == 1
    assert got.stopReason is StopReason.failed
    kinds = [c[0] for c in calls]
    assert kinds.index("logs") < kinds.index("delete")
    assert "DisconnectedError EndCause 72" in caplog.text
    assert "removed exited pod vexa-w1" in caplog.text
    again = runtime.get("w1")
    assert again.exitCode == 1
    assert again.stopReason is StopReason.failed
    assert [c[0] for c in calls].count("delete") == 1


def test_a_succeeded_pod_is_removed_without_copying_its_log(monkeypatch, caplog):
    calls: list[str] = []

    def fake_kubectl(*args, check=True, stdin=None):
        calls.append(args[0])
        if args[0] == "logs":
            raise AssertionError("a clean exit does not copy the container log")
        if args[0] == "get":
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"status": {"phase": "Succeeded"}}), stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runtime = _running_runtime(monkeypatch, fake_kubectl)
    with caplog.at_level(logging.INFO, logger="runtime_kernel.k8s"):
        got = runtime.get("w1")
    assert got.state is RuntimeState.stopped
    assert got.exitCode == 0
    assert got.stopReason is StopReason.completed
    assert calls.count("delete") == 1
    assert "removed exited pod vexa-w1" in caplog.text


def test_a_running_pod_is_not_deleted(monkeypatch):
    calls: list[str] = []

    def fake_kubectl(*args, check=True, stdin=None):
        calls.append(args[0])
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"status": {"phase": "Running"}}), stderr="",
        )

    runtime = _running_runtime(monkeypatch, fake_kubectl)
    assert runtime.get("w1").state is RuntimeState.running
    assert "delete" not in calls
    assert "logs" not in calls


def _pod_item(name: str, workload_id: str, phase: str, exit_code: int | None = None) -> dict:
    status: dict = {"phase": phase}
    if exit_code is not None:
        status["containerStatuses"] = [{"state": {"terminated": {"exitCode": exit_code}}}]
    return {
        "metadata": {
            "name": name,
            "labels": {"runtime.managed": "true", "runtime.workload_id": workload_id},
        },
        "status": status,
    }


def test_reap_removes_finished_pods_and_leaves_live_ones(monkeypatch, caplog):
    pods = {"items": [
        _pod_item("vexa-live", "live", "Running"),
        _pod_item("vexa-pend", "pend", "Pending"),
        _pod_item("vexa-fail", "fail", "Failed", 1),
        _pod_item("vexa-ok", "ok", "Succeeded", 0),
        {"metadata": {"name": "vexa-nolabel", "labels": {"runtime.managed": "true"}},
         "status": {"phase": "Failed"}},
    ]}
    deleted: list[str] = []
    holder: dict = {}

    def fake_kubectl(*args, check=True, stdin=None):
        if args[0] == "get" and args[1] == "pods":
            return SimpleNamespace(returncode=0, stdout=json.dumps(pods), stderr="")
        if args[0] == "logs":
            assert args[1] == "vexa-fail"
            return SimpleNamespace(returncode=0, stdout="denied your request\n", stderr="")
        if args[0] == "delete":
            name = args[2]
            wid = {"vexa-fail": "fail", "vexa-ok": "ok"}[name]
            stored = holder["runtime"].store.get(wid).status
            assert stored.state is RuntimeState.stopped
            assert stored.exitCode == (1 if wid == "fail" else 0)
            deleted.append(name)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(k8s_backend, "_kubectl", fake_kubectl)
    runtime = Runtime(backend=K8sBackend(namespace="ns"), profiles={})
    holder["runtime"] = runtime
    runtime.store.set(WorkloadRecord(
        spec=WorkloadSpec(workloadId="fail", profile="meeting-bot", env={}),
        status=WorkloadStatus(
            workloadId="fail", profile="meeting-bot",
            state=RuntimeState.running, backend=BackendKind.k8s,
        ),
        owner="",
    ))
    with caplog.at_level(logging.INFO, logger="runtime_kernel.k8s"):
        removed = runtime.reap_exited_workloads()
    assert removed == 2
    assert deleted == ["vexa-fail", "vexa-ok"]
    failed = runtime.store.get("fail").status
    assert failed.state is RuntimeState.stopped
    assert failed.exitCode == 1
    assert failed.stopReason is StopReason.failed
    assert failed.profile == "meeting-bot"
    finished = runtime.store.get("ok").status
    assert finished.state is RuntimeState.stopped
    assert finished.exitCode == 0
    assert finished.profile == "adopted"
    assert finished.stopReason is StopReason.completed
    assert runtime.store.get("live") is None
    assert runtime.store.get("pend") is None
    assert "denied your request" in caplog.text
    assert runtime.reap_exited_workloads() == 0
    assert deleted == ["vexa-fail", "vexa-ok"]


def test_a_respawned_pod_is_reaped_again_when_it_exits(monkeypatch, caplog):
    """The pod name is prefix + workload id. A second life must not count as already reaped."""
    phase = {"value": "Running"}
    deletes: list[str] = []

    def fake_kubectl(*args, check=True, stdin=None):
        if args[0] == "create":
            phase["value"] = "Running"
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "get" and args[1] == "pod":
            if phase["value"] == "Running":
                body = {"status": {"phase": "Running"}}
            else:
                body = {"status": {"phase": "Failed", "containerStatuses": [
                    {"state": {"terminated": {"exitCode": 1}}},
                ]}}
            return SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")
        if args[0] == "logs":
            return SimpleNamespace(returncode=0, stdout=f"life-{len(deletes) + 1}\n", stderr="")
        if args[0] == "delete":
            deletes.append(args[2])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(k8s_backend, "_kubectl", fake_kubectl)
    runtime = Runtime(
        backend=K8sBackend(namespace="ns"),
        profiles={"meeting-bot": Runnable(image="bot:1")},
    )
    spec = WorkloadSpec(workloadId="w1", profile="meeting-bot", env={})
    assert runtime.create(spec).state is RuntimeState.running
    phase["value"] = "Failed"
    with caplog.at_level(logging.ERROR, logger="runtime_kernel.k8s"):
        first = runtime.get("w1")
    assert first.exitCode == 1
    assert deletes == ["vexa-w1"]
    assert "life-1" in caplog.text
    assert runtime.create(spec).state is RuntimeState.running
    phase["value"] = "Failed"
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="runtime_kernel.k8s"):
        second = runtime.get("w1")
    assert second.exitCode == 1
    assert second.stopReason is StopReason.failed
    assert deletes == ["vexa-w1", "vexa-w1"]
    assert "life-2" in caplog.text


def test_a_failed_delete_is_retried_on_the_next_pass(monkeypatch):
    pods = {"items": [_pod_item("vexa-fail", "fail", "Failed", 1)]}
    deletes = {"n": 0}

    def fake_kubectl(*args, check=True, stdin=None):
        if args[0] == "get" and args[1] == "pods":
            return SimpleNamespace(returncode=0, stdout=json.dumps(pods), stderr="")
        if args[0] == "logs":
            return SimpleNamespace(returncode=0, stdout="join timeout\n", stderr="")
        if args[0] == "delete":
            deletes["n"] += 1
            if deletes["n"] == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="apiserver timeout")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(k8s_backend, "_kubectl", fake_kubectl)
    runtime = Runtime(backend=K8sBackend(namespace="ns"), profiles={})
    assert runtime.reap_exited_workloads() == 0
    stored = runtime.store.get("fail").status
    assert stored.state is RuntimeState.stopped
    assert stored.exitCode == 1
    assert runtime.reap_exited_workloads() == 1
    assert deletes["n"] == 2
    assert runtime.reap_exited_workloads() == 0
    assert deletes["n"] == 2


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
