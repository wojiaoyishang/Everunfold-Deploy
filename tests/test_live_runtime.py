"""Opt-in real PostgreSQL/Redis + already-built Next.js integration (no live user services)."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

import pytest
from configure import configure
from operations import layout_for, publish_plugins, runtime_action
from storage import ROOT, Store, read


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_real_initialize_pair_sync_restart(tmp_path):
    source = os.environ.get("EVERUNFOLD_TEST_BUILD_ROOT")
    pg = os.environ.get("EVERUNFOLD_TEST_PG_BIN")
    redis = os.environ.get("EVERUNFOLD_TEST_REDIS")
    if not all((source, pg, redis)):
        pytest.skip("requires isolated service binaries and an already-built native release")
    assert Path(source).resolve().name.startswith("everunfold-unified-check-"), (
        "only use a disposable everunfold-unified-check-* instance"
    )
    pg = Path(pg)
    store = Store(Path(source) / "Deploy/config.yaml")
    # This opt-in fixture creates a new, disposable database on every invocation.
    store.doc["frontend"]["application"].get("settings", {}).pop("source_epoch", None)
    store.render()
    release = read(store.var / "built.yaml")
    pg_port, redis_port, api_port, front_port = (port() for _ in range(4))
    environment = {**os.environ, "PYTHONUNBUFFERED": "1"}
    processes, logs = [], []

    def spawn(name, command):
        stream = (tmp_path / (name + ".log")).open("w")
        logs.append(stream)
        process = subprocess.Popen(
            command, env=environment, stdout=stream, stderr=subprocess.STDOUT
        )
        processes.append(process)
        return process

    def ready_socket(number, process):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            assert process.poll() is None
            try:
                with socket.create_connection(("127.0.0.1", number), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("test dependency did not start")

    subprocess.run(
        [
            pg / "initdb",
            "-D",
            tmp_path / "pg",
            "-U",
            "test",
            "--locale=C",
            "--encoding=UTF8",
            "--auth=trust",
            "-L",
            os.environ["EVERUNFOLD_TEST_PG_SHARE"],
        ],
        env=environment,
        check=True,
        capture_output=True,
    )
    try:
        postgres = spawn(
            "postgres",
            [
                pg / "postgres",
                "-D",
                tmp_path / "pg",
                "-h",
                "127.0.0.1",
                "-p",
                str(pg_port),
                "-k",
                str(tmp_path),
            ],
        )
        redis_process = spawn(
            "redis",
            [
                redis,
                "--bind",
                "127.0.0.1",
                "--port",
                str(redis_port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(tmp_path),
            ],
        )
        ready_socket(pg_port, postgres)
        ready_socket(redis_port, redis_process)
        form = configure(store, "native")
        values = {
            "database": f"postgresql+asyncpg://test@127.0.0.1:{pg_port}/postgres",
            "redis": f"redis://127.0.0.1:{redis_port}/0",
            "broker": f"redis://127.0.0.1:{redis_port}/1",
            "result": f"redis://127.0.0.1:{redis_port}/2",
            "backend_listen": str(api_port),
            "frontend_listen": str(front_port),
            "api": f"http://127.0.0.1:{api_port}",
            "site": f"http://127.0.0.1:{front_port}",
            "backend_data": str(tmp_path / "bdata"),
            "frontend_data": str(tmp_path / "fdata"),
        }
        for key, value in values.items():
            form.edit(next(item for item in form.fields if item.key == key), value)
        assert not form.errors()
        form.save()
        for role in ("backend", "frontend"):
            Path(store.doc["deployment"][role + "_data"]).mkdir()
        store.render()
        layout_for(store, release)
        runtime_action(store, release, "init")
        store.import_runtime()
        store.save()
        assert store.doc["frontend"]["application"]["settings"]["source_epoch"]
        directory = store.var / "releases" / release["release"]
        publish_plugins(directory / "plugins", tmp_path / "fdata/plugins")
        for attempt in range(2):
            supervisor = spawn(
                "runtime" + str(attempt),
                [
                    sys.executable,
                    ROOT / "deploy/runtime.py",
                    "serve",
                    "--layout",
                    store.runtime / "runtime.yaml",
                ],
            )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline and supervisor.poll() is None:
                try:
                    for number in (api_port, front_port):
                        with urlopen(
                            f"http://127.0.0.1:{number}/health/ready", timeout=2
                        ) as response:
                            assert response.status == 200
                    break
                except (OSError, AssertionError):
                    time.sleep(0.5)
            else:
                pytest.fail(
                    "both services failed to become ready:\n"
                    + (tmp_path / ("runtime" + str(attempt) + ".log")).read_text()[-8000:]
                )
            with urlopen(f"http://127.0.0.1:{api_port}/", timeout=3) as response:
                assert "夕阳" in json.load(response)["msg"]
            supervisor.send_signal(signal.SIGTERM)
            assert supervisor.wait(timeout=45) == 0
            assert (
                "inventory_token_missing"
                not in (tmp_path / ("runtime" + str(attempt) + ".log")).read_text()
            )
        # Explicit initialization is idempotent, preserving existing records.
        runtime_action(store, release, "init")
    finally:
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
            try:
                child.wait(timeout=45)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for stream in logs:
            stream.close()
