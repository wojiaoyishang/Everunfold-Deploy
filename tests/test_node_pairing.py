import base64
import json
import stat
import subprocess
from pathlib import Path

import node_pairing
import pytest
from storage import Store, read


def test_export_saves_only_backend_credentials_and_reuses_them(configured, monkeypatch, tmp_path):
    store = configured("docker")
    store.render()
    original = read(store.path)
    local = original["backend"]["assistant"]["node"]
    runtime = store.path_for("backend")
    captured = []

    monkeypatch.setattr(
        node_pairing,
        "deployment_container",
        lambda _: {"Id": "a" * 64, "State": {"Running": True}},
    )

    def run(command, **kwargs):
        assert command[0:4] == ["docker", "exec", "-w", "/workspace/backend"]
        assert command[6] == "a" * 64
        assert kwargs["capture_output"]
        path = runtime.with_name(Path(command[5].partition("=")[2]).name)
        captured.append(path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        doc = read(path)
        meta = doc["assistant"]
        bundle = {
            "format": "everunfold-node-connection/v1",
            "node_id": meta["node"],
            "backend_public_url": "https://api.example.test",
            "source_epoch": "12345678-1234-4234-8234-123456789abc",
            "tokens": {
                direction: {
                    "name": meta[f"{direction}_env"],
                    "value": doc["environment"][meta[f"{direction}_env"]],
                }
                for direction in ("inbound", "outbound")
            },
        }
        encoded = base64.b64encode(json.dumps(bundle).encode()).decode()
        return subprocess.CompletedProcess(command, 0, node_pairing.MARKER + encoded, "")

    monkeypatch.setattr(node_pairing.subprocess, "run", run)
    with pytest.raises(ValueError, match="独立节点"):
        node_pairing.export_node(store.path, local, "https://receiver.example.test")
    with pytest.raises(ValueError, match="接收器"):
        node_pairing.export_node(store.path, "remote", "http://receiver.example.test", True)

    encoded = node_pairing.export_node(store.path, "remote", "https://receiver.example.test")
    assert node_pairing.export_node(store.path, "remote") == encoded
    assert len(captured) == 2 and all(not path.exists() for path in captured)
    saved = read(store.path)
    assert saved["frontend"] == original["frontend"]
    assert read(runtime) == original["backend"]
    for key, value in original["backend"]["environment"].items():
        assert saved["backend"]["environment"][key] == value
    assert saved["backend"]["assistant"]["node"] == local
    assert len(saved["backend"]["environment"]) == len(original["backend"]["environment"]) + 2

    path = tmp_path / "remote.json"
    node_pairing.write_bundle(path, encoded, "remote")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_bytes())["node_id"] == "remote"
    with pytest.raises(ValueError, match="文件已存在"):
        node_pairing.write_bundle(path, encoded, "remote")

    # A second wizard with unsaved edits merges an independently exported pairing.
    store.doc["deployment"]["frontend_port"] = "3001"
    store.save()
    assert read(store.path)["backend"] == saved["backend"]
    assert Store(store.path).doc["deployment"]["frontend_port"] == "3001"
