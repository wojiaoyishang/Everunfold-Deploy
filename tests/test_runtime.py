import os
import subprocess
import sys

import operations
import pytest
import runtime


def test_docker_attaches_extra_network_before_start(monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "create"]:
            return "id"
        if args[:2] == ["docker", "inspect"]:
            return "0"
        return ""

    monkeypatch.setattr(operations, "execute", execute)
    d = {"network": "bridge", "extra_networks": "1panel-network", "host_gateway": "1"}
    operations.temporary(d, ["everunfold:test", "prepare"])
    assert calls[0].count("--network") == 1
    assert calls[1] == ["docker", "network", "connect", "1panel-network", "id"]
    assert calls[2] == ["docker", "start", "--attach", "id"]
    assert "host.docker.internal:host-gateway" in calls[0]


def test_nonzero_container_exit_is_failure(monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return "7" if args[:2] == ["docker", "inspect"] else "id"

    monkeypatch.setattr(operations, "execute", execute)
    with pytest.raises(ValueError, match="退出码 7"):
        operations.temporary(
            {"network": "bridge", "extra_networks": "", "host_gateway": "0"}, ["image"]
        )
    assert calls[-1] == ["docker", "rm", "-f", "id"]


def test_owned_container_only(configured, monkeypatch):
    store = configured("docker")
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, '[{"Config":{"Labels":{}}}]'),
    )
    with pytest.raises(ValueError, match="不属于"):
        operations.stop(store)


def test_supervisor_stops_all_children_on_unexpected_exit(tmp_path, monkeypatch):
    marker = tmp_path / "pid"
    monkeypatch.setattr(
        runtime,
        "commands",
        lambda layout: {
            "long": (
                "backend",
                [
                    sys.executable,
                    "-c",
                    f"import os,time;open({str(marker)!r},'w').write(str(os.getpid()));time.sleep(60)",
                ],
            ),
            "short": ("frontend", [sys.executable, "-c", "import time;time.sleep(.5)"]),
        },
    )
    monkeypatch.setattr(runtime, "environment", lambda *args: os.environ.copy())
    assert (
        runtime.supervise(
            {
                "backend_root": str(tmp_path),
                "frontend_root": str(tmp_path),
                "backend_config": "b",
                "frontend_config": "f",
            }
        )
        == 1
    )
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_runtime_mounts_and_readonly_boundary(configured):
    store = configured("docker")
    args = operations.runtime_mounts(store, {"release": "r1"})
    assert any("target=/workspace/backend,readonly" in s for s in args)
    assert any("target=/workspace/frontend" in s and "readonly" not in s for s in args)
    assert any("target=/config" in s and "readonly" not in s for s in args)
    assert any("target=/data/backend" in s for s in args)
    assert any("target=/data/frontend" in s for s in args)
