import subprocess

import operations
from config_mirrors import (
    APT,
    APT_SECURITY,
    NODE_IMAGE,
    NPM,
    PROFILE,
    PYPI,
    UV_IMAGE,
    resolve_sources,
)
from configure import configure
from storage import Store


def test_saved_sources_apply_to_image_and_both_frontend_builders(configured, monkeypatch):
    store = configured("docker")
    form = configure(store)
    fields = {item.key: item for item in form.fields}
    assert not fields[NPM].active()
    form.edit(fields[PROFILE], "custom")
    assert fields[NPM].active()
    form.edit(fields[NPM], "https://registry.example.test/npm/")
    form.edit(fields[PYPI], "https://registry.example.test/pypi/simple")
    form.edit(fields[NODE_IMAGE], "registry.example.test/node:22.15.1-bookworm-slim")
    assert not form.errors()
    form.save()
    config = Store(store.path)

    release = store.var / "releases" / "test"
    (release / "backend").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        (release / "backend" / name).touch()
    builds, builders = [], []

    def execute(args, *, capture=False):
        if capture:
            assert args[:3] == ["docker", "image", "inspect"]
            return "1"
        builds.extend(args)

    monkeypatch.setattr(operations, "execute", execute)

    def temporary(settings, args):
        builders.append(args)
        (release / "frontend/.next").mkdir(parents=True, exist_ok=True)
        (release / "frontend/.next/BUILD_ID").write_text("test")

    monkeypatch.setattr(operations, "temporary", temporary)
    image = operations.build_image(config, release, {"backend": "123a", "frontend": "123b"})
    operations.compile_frontend(config, release, image)
    assert f"{NODE_IMAGE}=registry.example.test/node:22.15.1-bookworm-slim" in builds
    assert f"{PYPI}=https://registry.example.test/pypi/simple" in builds
    assert all(not str(arg).startswith(f"{NPM}=") for arg in builds)
    assert len(builders) == 2
    for command in builders:
        assert f"{NPM}=https://registry.example.test/npm/" in command
    context = builds[-1]
    assert all(
        (context / "docker" / script).is_file()
        for script in ("apt-mirrors.sh", "uv-sync.sh", "npm-ci.sh")
    )


def test_profiles_keep_custom_values_inactive_and_default_to_official(configured):
    store = configured("docker")
    config = configure(store)
    fields = {item.key: item for item in config.fields}
    config.edit(fields[PROFILE], "custom")
    config.edit(fields[PYPI], "https://private.example.test/pypi/simple")
    config.edit(fields[UV_IMAGE], "registry.example.test/uv:0.9.0")
    config.edit(fields[PROFILE], "tuna")
    config.save()
    settings = Store(store.path).doc["deployment"]
    assert settings[PYPI] == "https://private.example.test/pypi/simple"
    assert resolve_sources(settings, "deploy")[PYPI].endswith("tsinghua.edu.cn/pypi/web/simple")
    assert resolve_sources(settings, "deploy")[UV_IMAGE] == settings[UV_IMAGE]
    assert all(not value for value in resolve_sources({}, "deploy").values())


def test_apt_mirror_rewrites_only_requested_bookworm_uris(tmp_path):
    source = tmp_path / "debian.sources"
    source.write_text(
        "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm bookworm-updates\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\n"
        "Types: deb\nURIs: https://security.debian.org/debian-security\nSuites: bookworm-security\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
    )
    original = source.read_text()
    script = operations.ROOT / "docker/apt-mirrors.sh"
    mirrors = resolve_sources({PROFILE: "ustc"}, "deploy")
    subprocess.run(["sh", script, source], env={APT: mirrors[APT]}, check=True)
    assert source.read_text() == original.replace("http://deb.debian.org/debian", mirrors[APT])

    source.write_text(original)
    subprocess.run(["sh", script, source], env=mirrors, check=True)
    assert source.read_text() == (
        original.replace("http://deb.debian.org/debian", mirrors[APT]).replace(
            "https://security.debian.org/debian-security", mirrors[APT_SECURITY]
        )
    )
