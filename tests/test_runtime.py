import json
import os
import subprocess
import sys
from pathlib import Path

import operations
import pytest
import runtime
from storage import read, write


def test_docker_attaches_extra_network_before_start(monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "create"]:
            return "id"
        if args[:3] == ["docker", "container", "inspect"]:
            return "0"
        return ""

    monkeypatch.setattr(operations, "execute", execute)
    d = {"network": "bridge", "extra_networks": "1panel-network", "host_gateway": "1"}
    operations.temporary(d, ["everunfold:test", "prepare"])
    assert calls[0].count("--network") == 1
    assert calls[1] == ["docker", "network", "connect", "1panel-network", "id"]
    assert calls[2] == ["docker", "start", "--attach", "id"]
    assert calls[3] == ["docker", "container", "inspect", "--format", "{{.State.ExitCode}}", "id"]
    assert "host.docker.internal:host-gateway" in calls[0]


def test_nonzero_container_exit_is_failure(monkeypatch):
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return "7" if args[:3] == ["docker", "container", "inspect"] else "id"

    monkeypatch.setattr(operations, "execute", execute)
    with pytest.raises(ValueError, match="退出码 7"):
        operations.temporary(
            {"network": "bridge", "extra_networks": "", "host_gateway": "0"}, ["image"]
        )
    assert calls[-1] == ["docker", "rm", "-f", "id"]


@pytest.mark.parametrize("labels", [None, {}, {"org.everunfold.deployment": "/other/config.yaml"}])
def test_owned_container_only(configured, monkeypatch, labels):
    store = configured("docker")
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, json.dumps([{"Id": "a" * 64, "Config": {"Labels": labels}}])
        ),
    )
    mutations = []
    monkeypatch.setattr(operations, "execute", lambda args, **kwargs: mutations.append(args))
    with pytest.raises(ValueError, match="不属于"):
        operations.stop(store)
    assert not mutations


def test_same_named_network_is_not_treated_as_container(configured, monkeypatch):
    store = configured("docker")
    name = store.doc["deployment"]["container"]
    calls, mutations = [], []

    def run(args, **kwargs):
        calls.append(args)
        if args[:3] == ["docker", "container", "inspect"]:
            return subprocess.CompletedProcess(args, 1, "[]", f"Error: No such container: {name}\n")
        # Reproduce the old generic inspect lookup: a network has no Config.
        return subprocess.CompletedProcess(
            args, 0, json.dumps([{"Name": name, "Driver": "bridge"}])
        )

    monkeypatch.setattr(operations.subprocess, "run", run)
    monkeypatch.setattr(operations, "execute", lambda args, **kwargs: mutations.append(args))
    operations.stop(store)
    assert calls == [["docker", "container", "inspect", name]]
    assert not mutations


@pytest.mark.parametrize("prefix", ["Error: ", "Error response from daemon: "])
@pytest.mark.parametrize("kind", ["container", "object"])
def test_missing_container_can_be_skipped(configured, monkeypatch, prefix, kind):
    store = configured("docker")
    name = store.doc["deployment"]["container"]
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 1, "[]", f"{prefix}No such {kind}: {name}\n"
        ),
    )
    assert operations.inspect_container(name) is None


@pytest.mark.parametrize(
    "detail",
    [
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
        "permission denied while trying to connect to the Docker daemon socket",
        "Error response from daemon: internal server error",
        "",
    ],
)
def test_docker_lookup_errors_are_not_container_absence(configured, monkeypatch, detail):
    store = configured("docker")
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", detail),
    )
    mutations = []
    monkeypatch.setattr(operations, "execute", lambda args, **kwargs: mutations.append(args))
    with pytest.raises(ValueError, match="无法查询 Docker 容器"):
        operations.stop(store)
    assert not mutations


@pytest.mark.parametrize(
    "payload",
    [
        "invalid json",
        "[]",
        "{}",
        "[null]",
        '[{"Driver":"bridge"}]',
        json.dumps([{"Id": "a" * 64, "Config": None}]),
        json.dumps([{"Id": "a" * 64, "Config": {"Labels": []}}]),
        json.dumps([{"Id": "not-an-id", "Config": {"Labels": {}}}]),
    ],
)
def test_malformed_container_details_abort_without_keyerror(configured, monkeypatch, payload):
    store = configured("docker")
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, payload, ""),
    )
    mutations = []
    monkeypatch.setattr(operations, "execute", lambda args, **kwargs: mutations.append(args))
    with pytest.raises(ValueError, match="容器详情格式无效"):
        operations.stop(store)
    assert not mutations


def test_stop_removes_only_the_verified_container_id(configured, monkeypatch):
    store = configured("docker")
    container_id = "c" * 64
    payload = [
        {"Id": container_id, "Config": {"Labels": {"org.everunfold.deployment": str(store.path)}}}
    ]
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )
    mutations = []
    monkeypatch.setattr(operations, "execute", lambda args, **kwargs: mutations.append(args))
    operations.stop(store)
    assert mutations == [
        ["docker", "stop", "--time", "40", container_id],
        ["docker", "rm", container_id],
    ]


@pytest.mark.parametrize("owner", [True, False])
def test_sync_uses_the_running_container_owned_by_this_deployment(configured, monkeypatch, owner):
    store = configured("docker")
    container_id = "d" * 64
    payload = [
        {
            "Id": container_id,
            "Config": {
                "Labels": {
                    "org.everunfold.deployment": str(store.path) if owner else "/other/config.yaml"
                },
            },
            "State": {"Running": True},
        }
    ]
    monkeypatch.setattr(
        operations.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(payload), ""),
    )
    calls = []
    monkeypatch.setattr(operations, "execute", lambda args: calls.append(args))
    if owner:
        operations.trigger_sync(store)
        assert calls == [
            ["docker", "exec", container_id, "python", "/opt/everunfold/deploy/runtime.py", "sync"],
        ]
    else:
        with pytest.raises(ValueError, match="不属于"):
            operations.trigger_sync(store)
        assert not calls


def test_start_resumes_existing_build_without_git_or_npm(configured, monkeypatch):
    import main

    store = configured("docker")
    release = {
        "release": "successful-build",
        "mode": "docker",
        "image": "everunfold:successful-build",
    }
    write(store.var / "built.yaml", release)
    starts = []
    monkeypatch.setattr(main, "build", lambda *_: pytest.fail("start must not rebuild"))
    monkeypatch.setattr(
        main, "update_sources", lambda *_: pytest.fail("start must not pull sources")
    )
    monkeypatch.setattr(
        main, "start", lambda target, artifact, **kwargs: starts.append((target, artifact, kwargs))
    )
    main.perform(store, "start")
    assert starts == [(store, release, {"initialize": False, "foreground": False})]


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


@pytest.mark.parametrize("plugins_fail", [False, True])
def test_rebuild_frontend_uses_saved_image_and_stages_only_on_success(
    configured, monkeypatch, plugins_fail
):
    import main

    store = configured("docker")
    deployment = store.doc["deployment"]
    source = Path(deployment["frontend_source"])
    for name in ("package-lock.json", "contracts.lock.json", "tools/plugins/run.mts"):
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    previous = {
        "release": "existing-build",
        "mode": "docker",
        "image": "everunfold:existing-build",
        "revisions": {"backend": "original-api", "frontend": "old-web"},
        "dirty": {"backend": False, "frontend": False},
    }
    old_backend = store.var / "releases" / "existing-build/backend"
    old_backend.mkdir(parents=True)
    (old_backend / "marker").write_text("original snapshot")
    write(store.var / "built.yaml", previous)
    write(store.var / "active.yaml", previous)
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "create"]:
            output = next(value for value in args if value.endswith(",target=/output"))
            stage = Path(output.removeprefix("type=bind,source=").removesuffix(",target=/output"))
            if args[-2:] == ["build", "frontend"]:
                build_id = stage / "frontend/.next/BUILD_ID"
                build_id.parent.mkdir(parents=True)
                build_id.write_text("new frontend")
                return "host-id"
            assert args[-1] == "build-plugins"
            (stage / "plugins/manifest.json").write_text("{}")
            return "plugin-id"
        if args[:3] == ["docker", "container", "inspect"]:
            return "1" if args[-1] == "plugin-id" and plugins_fail else "0"
        return ""

    monkeypatch.setattr(operations, "ensure_networks", lambda _: None)
    monkeypatch.setattr(operations, "execute", execute)
    monkeypatch.setattr(operations, "source_revision", lambda _: ("new-web", True))
    if plugins_fail:
        with pytest.raises(ValueError, match="临时容器执行失败"):
            main.perform(store, "rebuild-frontend")
    else:
        main.perform(store, "rebuild-frontend")
    assert read(store.var / "active.yaml") == previous
    built = read(store.var / "built.yaml")
    if plugins_fail:
        assert built == previous
    else:
        assert built["image"] == previous["image"]
        assert built["revisions"] == {"backend": "original-api", "frontend": "new-web"}
        new_release = store.var / "releases" / built["release"]
        assert new_release != old_backend.parent
        assert (new_release / "backend/marker").read_text() == "original snapshot"
        assert (new_release / "frontend/.next/BUILD_ID").read_text() == "new frontend"
    builders = [args for args in calls if args[:2] == ["docker", "create"]]
    assert len(builders) == 2
    for args in builders:
        assert previous["image"] in args
        assert "FRONTEND_BUILD_WORKERS=" + deployment["build_workers"] in args
        assert "NODE_OPTIONS=--max-old-space-size=" + deployment["build_memory"] in args
        assert any(value.endswith("npm-cache,target=/build/npm-cache") for value in args)
    assert all(args[1] not in {"build", "stop", "restart"} for args in calls)


@pytest.mark.parametrize("owned", [True, False])
def test_restart_operates_on_owned_id_without_replacing_deployment(configured, monkeypatch, owned):
    import main

    store = configured("docker")
    container = "e" * 64
    mutations = []
    configuration = store.path.read_bytes()

    def run(args, **kwargs):
        if args[:3] == ["docker", "container", "inspect"]:
            label = str(store.path) if owned else "/other/config.yaml"
            data = [{"Id": container, "Config": {"Labels": {"org.everunfold.deployment": label}}}]
            return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
        mutations.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(operations.subprocess, "run", run)
    if owned:
        main.perform(store, "restart")
        assert mutations == [
            ["docker", "restart", "--time", "45", container],
            ["docker", "exec", container, "python", "/opt/everunfold/deploy/runtime.py", "health"],
        ]
    else:
        with pytest.raises(ValueError, match="不属于此部署"):
            main.perform(store, "restart")
        assert not mutations
    assert store.path.read_bytes() == configuration
    assert not (store.var / "active.yaml").exists()


@pytest.mark.parametrize("frontend_bind", ["2001:db8::2", ""])
def test_start_uses_candidate_mount_and_existing_image(configured, monkeypatch, frontend_bind):
    store = configured("docker")
    store.doc["deployment"].update(backend_bind="0.0.0.0", frontend_bind=frontend_bind)
    release = {"release": "new-web", "mode": "docker", "image": "everunfold:old-image"}
    directory = store.var / "releases/new-web/frontend/.next"
    directory.mkdir(parents=True)
    (directory / "BUILD_ID").write_text("new-web")
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return "a" * 64 if args[:2] == ["docker", "create"] else ""

    monkeypatch.setattr(operations, "execute", execute)
    monkeypatch.setattr(operations, "ensure_networks", lambda _: None)
    monkeypatch.setattr(operations, "inspect_container", lambda _: None)
    monkeypatch.setattr(operations, "runtime_action", lambda *args: None)
    monkeypatch.setattr(operations, "wait_until_ready", lambda *args: None)
    operations.start(store, release)
    create = next(args for args in calls if args[:2] == ["docker", "create"])
    expected = ["0.0.0.0:3434:8001"]
    if frontend_bind:
        expected.append("[2001:db8::2]:3000:3000")
    assert [port for flag, port in zip(create, create[1:]) if flag == "-p"] == expected
    assert create[-2:] == [release["image"], "serve"]
    assert any(value.endswith("new-web/frontend,target=/workspace/frontend") for value in create)
    assert calls[-1] == ["docker", "start", "a" * 64]
    assert read(store.var / "active.yaml") == release
