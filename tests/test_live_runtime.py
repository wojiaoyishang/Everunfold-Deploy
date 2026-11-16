"""Opt-in real PostgreSQL/Redis + already-built Next.js integration (no live user services)."""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import asyncpg
import pytest
from configure import configure
from operations import layout_for, publish_plugins, runtime_action
from storage import ROOT, Store, lock, read


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def simulate_previous_glossary_build(pg_port):
    connection = await asyncpg.connect(
        host="127.0.0.1", port=pg_port, user="test", database="postgres"
    )
    try:
        async with connection.transaction():
            data = json.loads(
                await connection.fetchval("SELECT data FROM site_settings WHERE id = 1 FOR UPDATE")
            )
            glossary = next(
                item["info"]
                for item in data["_backend_plugins"]
                if item["info"]["id"] == "everunfold.glossary"
            )
            glossary["build"] = "0" * 64
            await connection.execute(
                "UPDATE site_settings SET data = $1 WHERE id = 1", json.dumps(data)
            )
    finally:
        await connection.close()


async def exhaust_content_delivery(pg_port):
    connection = await asyncpg.connect(
        host="127.0.0.1", port=pg_port, user="test", database="postgres"
    )
    try:
        return await connection.fetchval(
            """
            UPDATE settings_delivery_events
            SET attempts = 10000, applied_at = NULL, error = 'fixture_retries_exhausted'
            WHERE node_id = 'web-main'
              AND resource = 'public-content'
              AND (source_epoch, revision) = (
                SELECT source_epoch, content_generation FROM site_settings WHERE id = 1
              )
            RETURNING id
            """
        )
    finally:
        await connection.close()


async def wait_for_retried_delivery(pg_port, event):
    connection = await asyncpg.connect(
        host="127.0.0.1", port=pg_port, user="test", database="postgres"
    )
    try:
        for _ in range(100):
            row = await connection.fetchrow(
                "SELECT attempts, applied_at, error FROM settings_delivery_events WHERE id = $1",
                event,
            )
            if row["applied_at"] is not None:
                assert row["attempts"] < 10000 and row["error"] is None
                return
            await asyncio.sleep(0.2)
        pytest.fail("manually triggered content delivery did not finish")
    finally:
        await connection.close()


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
        runtime_action(store, release, "prepare-init")
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
            glossary_url = f"http://127.0.0.1:{api_port}/v1/plugins/everunfold.glossary/index"
            with urlopen(glossary_url, timeout=3) as response:
                assert json.load(response)["code"] == 200
            if attempt == 0:
                asyncio.run(simulate_previous_glossary_build(pg_port))
                with pytest.raises(HTTPError) as failure:
                    urlopen(glossary_url, timeout=3)
                with failure.value as response:
                    assert response.code == 503
                    assert json.load(response)["code"] == 50389
                # Preparing a different catalog while an API still owns the data directory
                # must fail before it can take the old plugin routes offline.
                with pytest.raises(ValueError, match="退出码"):
                    runtime_action(store, release, "prepare")
                event = asyncio.run(exhaust_content_delivery(pg_port))
                assert event is not None, "first startup did not enqueue the current public content"
                # The foreground setup command would still hold this lock.
                with lock(store.var / "operation.lock"):
                    subprocess.run(
                        [sys.executable, ROOT / "deploy/main.py", "--native", "sync"],
                        env={**environment, "CONFIG_FILE": str(store.path)},
                        check=True,
                    )
                with urlopen(glossary_url, timeout=3) as response:
                    assert json.load(response)["code"] == 200
                asyncio.run(wait_for_retried_delivery(pg_port, event))
            supervisor.send_signal(signal.SIGTERM)
            assert supervisor.wait(timeout=45) == 0
            assert (
                "inventory_token_missing"
                not in (tmp_path / ("runtime" + str(attempt) + ".log")).read_text()
            )
            if attempt == 0:
                # A normal start against a current schema repairs the drift without migrating.
                runtime_action(store, release, "prepare")
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
