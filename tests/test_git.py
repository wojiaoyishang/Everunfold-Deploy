import subprocess
from pathlib import Path

import pytest
from operations import update_sources


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def origin(path, role):
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.invalid")
    files = (
        ("pyproject.toml", "uv.lock", "deploy/runtime_check.py")
        if role == "backend"
        else ("package-lock.json", "contracts.lock.json", "tools/plugins/run.mts")
    )
    for name in files:
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("test\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")


def repositories(store, tmp_path):
    d = store.doc["deployment"]
    d["auto_pull"] = "1"
    for role in ("backend", "frontend"):
        remote = tmp_path / (role + "-origin")
        origin(remote, role)
        d[role + "_repo"] = str(remote)
    return d


def test_clone_then_fast_forward(configured, tmp_path):
    store = configured()
    d = repositories(store, tmp_path)
    update_sources(store)
    remote = Path(d["backend_repo"])
    (remote / "change").write_text("updated")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "update")
    update_sources(store)
    assert (Path(d["backend_source"]) / "change").read_text() == "updated"


def test_dirty_checkout_and_non_git_directory_untouched(configured, tmp_path):
    store = configured()
    d = repositories(store, tmp_path)
    update_sources(store)
    local = Path(d["backend_source"])
    (local / "keep.txt").write_text("keep")
    with pytest.raises(ValueError, match="本地改动"):
        update_sources(store)
    assert (local / "keep.txt").read_text() == "keep"
    other = tmp_path / "not-git"
    other.mkdir()
    (other / "file").write_text("keep")
    d["backend_source"] = str(other)
    with pytest.raises(ValueError):
        update_sources(store)
    assert (other / "file").read_text() == "keep"


def test_diverged_history_requires_manual_resolution(configured, tmp_path):
    store = configured()
    d = repositories(store, tmp_path)
    update_sources(store)
    local, remote = Path(d["backend_source"]), Path(d["backend_repo"])
    for path, name in ((local, "local"), (remote, "remote")):
        git(path, "config", "user.name", "Test")
        git(path, "config", "user.email", "test@example.invalid")
        (path / name).write_text(name)
        git(path, "add", ".")
        git(path, "commit", "-m", name)
    head = git(local, "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        update_sources(store)
    assert git(local, "rev-parse", "HEAD") == head


def test_updates_disabled_do_not_call_git(configured, tmp_path):
    store = configured()
    d = repositories(store, tmp_path)
    update_sources(store)
    d["auto_pull"] = "0"
    d["backend_repo"] = "https://invalid.example/no-network"
    (Path(d["backend_source"]) / "uncommitted").write_text("local")
    update_sources(store)


def test_frontend_only_update_does_not_check_or_pull_backend(configured, tmp_path):
    store = configured("docker")
    d = repositories(store, tmp_path)
    update_sources(store)
    (Path(d["backend_source"]) / "keep").write_text("uncommitted backend change")
    frontend = Path(d["frontend_repo"])
    (frontend / "change").write_text("updated frontend")
    git(frontend, "add", ".")
    git(frontend, "commit", "-m", "update")
    update_sources(store, ("frontend",))
    assert (Path(d["frontend_source"]) / "change").read_text() == "updated frontend"
