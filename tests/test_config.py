import copy
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from configure import configure
from pair import PairingError, validate_connection
from runtime import commands, environment
from storage import Store, read, write


def test_combined_settings_and_data(configured):
    store = configured("docker")
    b, f = store.doc["backend"], store.doc["frontend"]
    assert "PUBLIC_SITE_URL" not in b["application"]
    assert b["application"]["media"]["uploads_path"] == "/data/backend/uploads"
    assert f["application"]["settings"]["directory"] == "/data/frontend/settings"
    assert b["assistant"]["receiver"] == "http://127.0.0.1:3000"
    assert f["application"]["backend"]["base_url"] == "http://127.0.0.1:8001"
    for key in ("inbound_env", "outbound_env"):
        name = b["assistant"][key]
        assert b["environment"][name] == f["environment"][name]
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    store.render()
    for role in ("backend", "frontend"):
        assert read(store.path_for(role)) == store.doc[role]


def test_imported_master_keys_not_regenerated(configured):
    store = configured()
    key = store.doc["backend"]["application"]["auth"]["application_master_key_env"]
    secret = store.doc["backend"]["environment"][key]
    form = configure(Store(store.path))
    assert form.get(key) == secret
    assert next(field for field in form.fields if field.key == key).generate is None
    with pytest.raises(ValueError):
        form.edit(next(field for field in form.fields if field.key == key), "")


def test_repeated_form_save_keeps_field_bindings(configured):
    store = configured()
    form = configure(store)
    for port in ("8111", "8222", "8333"):
        form.edit(next(field for field in form.fields if field.key == "backend_listen"), port)
        form.save()
        assert read(store.path)["backend"]["environment"]["API_PORT"] == port


def test_first_form_repeat_save_does_not_rotate_password_hash(tmp_path):
    store = Store(tmp_path / "Deploy/config.yaml")
    form = configure(store, "native")
    form.edit(
        next(f for f in form.fields if f.key == "database"),
        "postgresql+asyncpg://test@localhost/test",
    )
    form.save()
    hashes = {
        role: store.doc[role]["application"]["nodes_monitor"]["password_hash"]
        for role in ("backend", "frontend")
    }
    form.save()
    assert hashes == {
        role: store.doc[role]["application"]["nodes_monitor"]["password_hash"]
        for role in ("backend", "frontend")
    }


def test_browser_edits_survive_reopen_and_unrelated_save(configured):
    store = configured()
    store.render()
    stale = Store(store.path)
    current = read(store.path_for("frontend"))
    current["application"]["PUBLIC_SITE_URL"] = "https://edited.example.test"
    write(store.path_for("frontend"), current)
    stale.doc["deployment"]["build_workers"] = "4"
    stale.save()
    stale.render()
    assert (
        read(stale.path)["frontend"]["application"]["PUBLIC_SITE_URL"]
        == "https://edited.example.test"
    )


def test_conflicting_edit_rejected_without_overwrite(configured):
    store = configured()
    store.render()
    original = copy.deepcopy(store.doc)
    current = read(store.path_for("frontend"))
    current["application"]["PUBLIC_SITE_URL"] = "https://browser.example.test"
    write(store.path_for("frontend"), current)
    store.doc["frontend"]["application"]["PUBLIC_SITE_URL"] = "https://setup.example.test"
    with pytest.raises(ValueError, match="同时被修改"):
        store.save()
    assert read(store.path) == original
    assert read(store.path_for("frontend")) == current


def test_repeated_browser_edits_are_not_false_conflicts(configured):
    store = configured()
    store.render()
    for index in range(3):
        current = read(store.path_for("frontend"))
        current["application"]["PUBLIC_SITE_URL"] = f"https://edit{index}.example.test"
        write(store.path_for("frontend"), current)
        store = Store(store.path)
        store.save()
    assert store.doc["frontend"]["application"]["PUBLIC_SITE_URL"] == "https://edit2.example.test"


def test_concurrent_setup_edits_merge_or_conflict(configured):
    initial = configured()
    a, b = Store(initial.path), Store(initial.path)
    a.doc["deployment"]["build_workers"] = "4"
    a.save()
    b.doc["deployment"]["build_memory"] = "4096"
    b.save()
    assert b.doc["deployment"]["build_workers"] == "4"
    c = Store(initial.path)
    c.doc["deployment"]["build_workers"] = "6"
    c.save()
    a.doc["deployment"]["build_workers"] = "5"
    with pytest.raises(ValueError, match="同时被修改"):
        a.save()


def test_missing_master_cannot_regenerate_existing_instance_keys(configured):
    store = configured()
    store.render()
    store.path.unlink()
    with pytest.raises(ValueError, match="已有运行配置"):
        Store(store.path)


def test_monitoring_saves_distinct_credentials(configured):
    store = configured()
    form = configure(store)
    edits = {
        "metrics_enabled": "1",
        "metrics_peers": "192.0.2.5",
        "frontend_metrics_enabled": "1",
        "frontend_metrics_peers": "192.0.2.0/24",
        "frontend_direct_peers": "198.51.100.0/24",
    }
    for key, value in edits.items():
        form.edit(next(item for item in form.fields if item.key == key), value)
    form.save()
    doc = read(store.path)
    backend = doc["backend"]["environment"]
    frontend = doc["frontend"]["environment"]
    assert backend["EVERUNFOLD_METRICS_TOKEN"] != backend["EVERUNFOLD_METRICS_NODE_TOKEN"]
    assert backend["EVERUNFOLD_METRICS_NODE_TOKEN"] == frontend["EVERUNFOLD_METRICS_NODE_TOKEN"]
    assert frontend["EVERUNFOLD_FRONTEND_METRICS_TOKEN"] not in (
        backend["EVERUNFOLD_METRICS_TOKEN"],
        backend["EVERUNFOLD_METRICS_NODE_TOKEN"],
        "",
    )
    assert frontend["EVERUNFOLD_FRONTEND_METRICS_ALLOWED_PEERS"] == "198.51.100.0/24"
    assert frontend["EVERUNFOLD_METRICS_NODE_ALLOWED_PEERS"] == "127.0.0.1/32,::1/128,192.0.2.0/24"
    assert backend["EVERUNFOLD_METRICS_FRONTEND_URL"] == "http://127.0.0.1:3000"


def test_log_format_is_shared_with_both_processes(configured, monkeypatch):
    store = configured()
    variable = "EVERUNFOLD_LOG_FORMAT"
    monkeypatch.delenv(variable, raising=False)
    legacy = ("EVERUNFOLD_LOKI_QUERY_ALLOWED_PEERS", "EVERUNFOLD_LOKI_PUSH_ALLOWED_PEERS")
    for role in ("backend", "frontend"):
        env = store.doc[role]["environment"]
        assert env.pop(variable) == "json"
        env.update(dict.fromkeys(legacy, "192.0.2.8"))
    store.save()
    form = configure(Store(store.path))
    field = next(item for item in form.fields if item.key == "log_format")
    assert field.value == "json"
    form.edit(field, "text")
    form.save()
    store = Store(store.path)
    store.render()
    layout = {role + "_config": str(store.path_for(role)) for role in ("backend", "frontend")}
    layout.update({role + "_root": str(store.path.parent) for role in ("backend", "frontend")})
    layout.update(
        {role + "_data": str(store.path.parent / role) for role in ("backend", "frontend")}
    )
    for role in ("backend", "frontend"):
        env = read(store.path_for(role))["environment"]
        assert env[variable] == "text"
        assert not set(legacy).intersection(env)
    assert environment(layout, "backend")[variable] == "text"
    assert variable not in environment(layout, "frontend")
    monkeypatch.setenv(variable, "json")
    for role in ("backend", "frontend"):
        assert environment(layout, role)[variable] == "json"
    assert not (store.var / "monitoring").exists()


def test_docker_monitoring_network_resolves_only_attached_subnets(configured, monkeypatch):
    from types import SimpleNamespace

    store = configured("docker")
    form = configure(store)
    fields = {item.key: item for item in form.fields}
    form.edit(fields["extra_networks"], "1panel-network")
    form.edit(fields["frontend_metrics_enabled"], "1")
    seen = []

    def inspect(arguments, **_):
        assert arguments[:3] == ["docker", "network", "inspect"]
        seen.append(arguments[-1])
        return SimpleNamespace(stdout="172.21.0.0/16,fd01::/64,\n")

    monkeypatch.setattr("configure.subprocess.run", inspect)
    form.edit(fields["frontend_direct_peers"], "1panel-network,192.0.2.8")
    assert fields["frontend_direct_peers"].value == "172.21.0.0/16,fd01::/64,192.0.2.8/32"
    assert seen == ["1panel-network"]
    with pytest.raises(ValueError, match="加入主网络"):
        form.edit(fields["frontend_direct_peers"], "unrelated-network")


def test_environment_isolation_and_worker_overrides(configured, monkeypatch):
    store = configured()
    form = configure(store)
    worker_field = next(field for field in form.fields if field.key == "frontend_workers")
    assert worker_field.value == "1"
    form.edit(worker_field, "3")
    form.save()
    store.render()
    layout = {
        "backend_config": str(store.path_for("backend")),
        "frontend_config": str(store.path_for("frontend")),
        "backend_root": "/backend",
        "frontend_root": "/frontend",
        "backend_data": "/b-data",
        "frontend_data": "/f-data",
        "python": "python",
    }
    monkeypatch.setenv("CONFIG_FILE", "/wrong/unified.yaml")
    monkeypatch.setenv("API_WORKERS", "3")
    assert read(store.path_for("frontend"))["environment"]["FRONTEND_WORKERS"] == "3"
    assert store.doc["deployment"]["build_workers"] == "2"
    monkeypatch.setenv("FRONTEND_WORKERS", "2")
    assert environment(layout, "backend")["CONFIG_FILE"] == str(store.path_for("backend"))
    assert "FRONTEND_WORKERS" not in environment(layout, "backend")
    front = environment(layout, "frontend")
    assert front["CONFIG_FILE"] == str(store.path_for("frontend"))
    assert front["FRONTEND_WORKERS"] == "2"
    assert store.doc["backend"]["assistant"]["inbound_env"] not in front
    assert commands(layout)["api"][1][commands(layout)["api"][1].index("--workers") + 1] == "3"
    worker = commands(layout)["worker"][1]
    assert worker[worker.index("--concurrency") + 1] == "1"
    monkeypatch.setenv("WORKER_POOL", "prefork")
    monkeypatch.setenv("WORKER_AUTOSCALE", "4,1")
    worker = commands(layout)["worker"][1]
    assert worker[worker.index("--autoscale") + 1] == "4,1"
    assert "--concurrency" not in worker


def test_runtime_configs_use_real_backend_loader(configured):
    source = Path(os.environ.get("TEST_BACKEND", "/workspace/Everunfold/Everunfold-Backend"))
    python = source / ".venv/bin/python"
    if not python.exists():
        pytest.skip("set TEST_BACKEND to a prepared source checkout")
    store = configured()
    store.render()
    code = "from app.core.config import load_settings; s=load_settings(); assert s.setup_frontend[0]=='web-main'; assert len(s.setup_frontend[1].nextjs_to_fastapi_token_env)>0"
    result = subprocess.run(
        [python, "-c", code],
        cwd=source,
        env={**os.environ, "CONFIG_FILE": str(store.path_for("backend"))},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_paths_and_port_collision_rejected(configured):
    store = configured("docker")
    form = configure(store)
    for key, value in (
        ("backend_data", form.get("backend_source")),
        ("frontend_listen", form.get("backend_listen")),
    ):
        form.edit(next(item for item in form.fields if item.key == key), value)
    assert any("嵌套" in error for error in form.errors())
    assert any("监听端口" in error for error in form.errors())


def test_publish_addresses_upgrade_independently_and_keep_local_peers(configured):
    store = configured("docker")
    deployment = store.doc["deployment"]
    for role in ("backend", "frontend"):
        deployment.pop(role + "_bind")
    deployment["bind"] = "192.0.2.2"
    form = configure(store)
    assert form.get("backend_bind") == form.get("frontend_bind") == "192.0.2.2"

    def edit(key, value):
        form.edit(next(field for field in form.fields if field.key == key), value)

    edit("backend_bind", "0.0.0.0")
    edit("frontend_port", form.get("backend_port"))
    assert any("绑定 IP 和端口冲突" in issue for issue in form.errors())
    edit("frontend_bind", "")
    edit("backend_peers", "192.0.2.4, 198.51.100.0/24")
    assert not form.errors(), form.errors()
    form.save()
    assert deployment["backend_bind"] == "0.0.0.0"
    assert deployment["frontend_bind"] == ""
    assert store.doc["backend"]["application"]["internal_auth"]["allowed_peer_ips"] == [
        "192.0.2.4",
        "198.51.100.0/24",
        "127.0.0.1",
        "::1",
    ]


def test_pair_refuses_foreign_node_token_and_epoch(configured):
    store = configured()
    b, f = store.doc["backend"], store.doc["frontend"]
    target = SimpleNamespace(
        nextjs_to_fastapi_token_env=b["assistant"]["inbound_env"],
        fastapi_to_nextjs_token_env=b["assistant"]["outbound_env"],
    )
    validate_connection(f, "web-main", target, b["environment"], "epoch")
    f["application"]["FRONTEND_NODE_ID"] = "another"
    with pytest.raises(PairingError, match="不是此部署节点"):
        validate_connection(f, "web-main", target, b["environment"], "epoch")

    f["application"]["FRONTEND_NODE_ID"] = "web-main"
    f["application"]["settings"]["source_epoch"] = "old-epoch"
    with pytest.raises(PairingError, match="UUID"):
        validate_connection(f, "web-main", target, b["environment"], "epoch")
    f["environment"][target.nextjs_to_fastapi_token_env] = "changed"
    with pytest.raises(PairingError, match="令牌不一致"):
        validate_connection(f, "web-main", target, b["environment"], "epoch")


def test_tui_can_import_custom_source_before_database_entry(configured, tmp_path):
    old = configured()
    locations = {}
    for role in ("backend", "frontend"):
        locations[role] = tmp_path / ("original-" + role)
        write(locations[role] / "config.yaml", old.doc[role])
    fresh = Store(tmp_path / "NewDeploy/config.yaml")
    form = configure(fresh)
    for role, path in locations.items():
        form.edit(next(f for f in form.fields if f.key == role + "_source"), str(path))
    assert any("PostgreSQL" in message for message in form.errors())
    with pytest.raises(ValueError, match="导入原配置"):
        form.save()
    form.import_existing()
    loaded = Store(fresh.path)
    assert loaded.doc["backend"]["environment"] == old.doc["backend"]["environment"]
    assert read(locations["backend"] / "config.yaml") == old.doc["backend"]
