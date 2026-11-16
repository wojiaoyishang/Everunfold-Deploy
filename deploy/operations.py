"""Deployment operations. Git failure is terminal; builds never modify source trees."""

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from config_model import Check
from storage import ROOT, read, write


def execute(args, *, cwd=None, env=None, capture=False, check=True):
    result = subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode:
        raise ValueError(
            f"{Path(args[0]).name} 执行失败（退出码 {result.returncode}）。请根据上方日志自行处理后重试。"
        )
    return result.stdout.strip() if capture else result.returncode


def repository(url):
    return url.rstrip("/").removesuffix(".git").replace("git@github.com:", "https://github.com/")


def update_sources(store):
    d = store.doc["deployment"]
    for role in ("backend", "frontend"):
        path = Path(d[role + "_source"])
        remote = d[role + "_repo"]
        if d["auto_pull"] == "1":
            if not path.exists() or not any(path.iterdir()):
                path.parent.mkdir(parents=True, exist_ok=True)
                print(f"[Git] clone {role} → {path}", flush=True)
                execute(["git", "clone", "--", remote, path])
            else:
                top = execute(["git", "-C", path, "rev-parse", "--show-toplevel"], capture=True)
                if Path(top).resolve() != path.resolve():
                    raise ValueError(f"{role} 保存位置不是独立 Git 仓库：{path}。请自行处理。")
                if execute(["git", "-C", path, "status", "--porcelain"], capture=True):
                    raise ValueError(
                        f"{role} 仓库有本地改动或未跟踪文件：{path}。请自行提交或处理后重试。"
                    )
                origin = execute(["git", "-C", path, "remote", "get-url", "origin"], capture=True)
                if repository(origin) != repository(remote):
                    raise ValueError(f"{role} origin 与配置的仓库不一致，请自行核对：{path}")
                print(f"[Git] {role}: git pull --ff-only", flush=True)
                execute(["git", "-C", path, "pull", "--ff-only"])
        if not path.is_dir():
            raise ValueError(f"{role} 源码目录不存在。请开启自动拉取或准备源码：{path}")
    for role, files in {
        "backend": ("pyproject.toml", "uv.lock", "deploy/runtime_check.py"),
        "frontend": (
            "package-lock.json",
            "contracts.lock.json",
            "tools/plugins/run.mts",
        ),
    }.items():
        for file in files:
            if not (Path(d[role + "_source"]) / file).is_file():
                raise ValueError(f"{role} 源码缺少 {file}，请检查目录和版本。")


def snapshot_backend(source, target):
    target.mkdir(parents=True)
    for name in (
        "app",
        "tools",
        "deploy",
        "migrations",
        "init.database.py",
        "pyproject.toml",
        "uv.lock",
        "README.md",
    ):
        path = Path(source) / name
        if path.is_dir():
            shutil.copytree(
                path,
                target / name,
                ignore=shutil.ignore_patterns(".venv", "__pycache__", "*.pyc"),
            )
        elif path.exists():
            shutil.copy2(path, target / name)


def ensure_docker():
    execute(["docker", "info"], capture=True)


def networks(d):
    return list(
        dict.fromkeys(
            [
                d["network"],
                *[s.strip() for s in d["extra_networks"].split(",") if s.strip()],
            ]
        )
    )


def ensure_networks(d):
    for index, name in enumerate(networks(d)):
        result = subprocess.run(["docker", "network", "inspect", name], capture_output=True)
        if result.returncode:
            if index == 0 and name not in {"bridge", "host", "none"}:
                execute(["docker", "network", "create", name])
            else:
                raise ValueError(f"Docker 网络不存在：{name}。请先创建或修改配置。")


def docker_create(d, args):
    base = ["docker", "create", "--network", d["network"]]
    if d["host_gateway"] == "1":
        base += ["--add-host", "host.docker.internal:host-gateway"]
    container = execute([*base, *args], capture=True)
    try:
        # Built-in bridge + user-defined networks must be attached separately.
        for name in networks(d)[1:]:
            execute(["docker", "network", "connect", name, container])
    except Exception:
        execute(["docker", "rm", "-f", container], check=False)
        raise
    return container


def temporary(d, args, capture=False):
    container = docker_create(d, args)
    try:
        output = execute(["docker", "start", "--attach", container], capture=capture)
        status = execute(
            ["docker", "container", "inspect", "--format", "{{.State.ExitCode}}", container],
            capture=True,
        )
        if status != "0":
            raise ValueError(f"临时容器执行失败（退出码 {status}）；请检查上方日志。")
        return output
    finally:
        execute(["docker", "rm", "-f", container], capture=True, check=False)


def mount(source, target, readonly=False):
    source = str(source)
    if any(c in source for c in ",\n\r"):
        raise ValueError("挂载路径不能包含逗号或换行。")
    return [
        "--mount",
        f"type=bind,source={source},target={target}" + (",readonly" if readonly else ""),
    ]


def build(store):
    d = store.doc["deployment"]
    if d["mode"] == "docker":
        ensure_docker()
        ensure_networks(d)
    else:
        node = execute(["node", "-p", "process.versions.node"], capture=True)
        if node.split(".")[0] != "22":
            raise ValueError("直接部署需要 Node.js 22（>=22.15.1）。")
        for tool in ("uv", "npm", "rsync"):
            if not shutil.which(tool):
                raise ValueError(f"直接部署需要 {tool}，请先安装。")
    update_sources(store)
    release = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + os.urandom(3).hex()
    directory = store.var / "releases" / release
    directory.mkdir(parents=True)
    snapshot_backend(d["backend_source"], directory / "backend")
    revisions, dirty = {}, {}
    for role in ("backend", "frontend"):
        result = subprocess.run(
            ["git", "-C", d[role + "_source"], "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        revisions[role] = result.stdout.strip() if result.returncode == 0 else "local"
        dirty[role] = bool(
            subprocess.run(
                ["git", "-C", d[role + "_source"], "status", "--porcelain"],
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    manifest = {"release": release, "mode": d["mode"], "revisions": revisions, "dirty": dirty}
    frontend_data = Path(d["frontend_data"])
    for path in (
        frontend_data / "npm-cache",
        directory / "plugins",
        store.var / "build" / release,
    ):
        path.mkdir(parents=True, exist_ok=True)
    print(f"[build] Everunfold {release}；输出目录 {directory}", flush=True)
    if d["mode"] == "docker":
        context = store.var / "build" / release / "image"
        (context / "backend").mkdir(parents=True)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copy2(directory / "backend" / name, context / "backend" / name)
        shutil.copytree(
            ROOT / "deploy",
            context / "deploy",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        shutil.copy2(ROOT / "docker/frontend-build.sh", context / "frontend-entrypoint.sh")
        image = "everunfold:" + release.lower()
        execute(
            [
                "docker",
                "build",
                "--progress=plain",
                "--build-arg",
                "BACKEND_REVISION=" + revisions["backend"],
                "--build-arg",
                "FRONTEND_REVISION=" + revisions["frontend"],
                "-t",
                image,
                "-f",
                ROOT / "docker/Dockerfile",
                context,
            ]
        )
        manifest["image"] = image
        builder = [
            "--user",
            d["uid"] + ":" + d["gid"],
            "--entrypoint",
            "/usr/local/bin/frontend-build",
            "-e",
            "FRONTEND_BUILD_WORKERS=" + d["build_workers"],
            "-e",
            "NODE_OPTIONS=--max-old-space-size=" + d["build_memory"],
            *mount(d["frontend_source"], "/workspace", True),
            *mount(directory, "/output"),
            *mount(frontend_data / "npm-cache", "/build/npm-cache"),
            *mount(directory / "plugins", "/plugins"),
        ]
        temporary(d, [*builder, image, "build", "frontend"])
        temporary(d, [*builder, image, "build-plugins"])
    else:
        clean = build_environment()
        execute(
            [
                "uv",
                "sync",
                "--project",
                directory / "backend",
                "--locked",
                "--no-dev",
                "--python",
                "3.12",
            ],
            env=clean,
        )
        env = {
            **clean,
            "SOURCE_ROOT": d["frontend_source"],
            "BUILD_ROOT": str(store.var / "build" / release / "work"),
            "OUTPUT_ROOT": str(directory),
            "PLUGIN_OUTPUT": str(directory / "plugins"),
            "npm_config_cache": str(frontend_data / "npm-cache"),
            "FRONTEND_BUILD_WORKERS": d["build_workers"],
            "NODE_OPTIONS": "--max-old-space-size=" + d["build_memory"],
        }
        for args in (("build", "frontend"), ("build-plugins",)):
            execute(["bash", ROOT / "docker/frontend-build.sh", *args], env=env)
    write(directory / "release.yaml", manifest)
    write(store.var / "built.yaml", manifest)
    print("[build] 构建成功；尚未替换运行中的服务。", flush=True)
    return manifest


def build_environment():
    return {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "CONFIG_FILE",
            "UV_PROJECT_ENVIRONMENT",
            "VIRTUAL_ENV",
            "EVERUNFOLD_PROJECT_ROOT",
            "NODE_ENV",
            "PLUGINS_DIRECTORY",
        }
    }


def layout_for(store, release):
    d = store.doc["deployment"]
    directory = store.var / "releases" / release["release"]
    docker = d["mode"] == "docker"
    if release["mode"] != d["mode"]:
        raise ValueError("部署方式已改变，请重新构建。")
    layout = {}
    for role in ("backend", "frontend"):
        layout[role + "_root"] = "/workspace/" + role if docker else str(directory / role)
        layout[role + "_config"] = (
            f"/config/{role}/config.yaml" if docker else str(store.path_for(role))
        )
        layout[role + "_data"] = "/data/" + role if docker else d[role + "_data"]
    layout["python"] = (
        "/opt/backend/venv/bin/python" if docker else str(directory / "backend/.venv/bin/python")
    )
    layout["node"] = "node" if docker else (shutil.which("node") or "node")
    if docker and d["tls_proxy_network"]:
        name = d["tls_proxy_network"]
        if name not in networks(d):
            raise ValueError("宿主反代网络必须是主网络或额外网络之一。")
        info = json.loads(execute(["docker", "network", "inspect", name], capture=True))[0]
        gateways = [item["Gateway"] for item in info["IPAM"]["Config"] if item.get("Gateway")]
        if info["Driver"] != "bridge" or not gateways:
            raise ValueError("宿主反代网络必须是有网关 IP 的网桥。")
        layout["tls_proxy_ips"] = ",".join(gateways)
        print(f"[network] 宿主反代 {name} → {layout['tls_proxy_ips']}", flush=True)
    write(store.runtime / "runtime.yaml", layout)
    return layout


def runtime_mounts(store, release):
    d = store.doc["deployment"]
    directory = store.var / "releases" / release["release"]
    args = [
        "--init",
        "--user",
        d["uid"] + ":" + d["gid"],
        *mount(directory / "backend", "/workspace/backend", True),
        *mount(directory / "frontend", "/workspace/frontend"),
        *mount(store.runtime, "/config"),
    ]
    for role in ("backend", "frontend"):
        args += mount(d[role + "_data"], "/data/" + role)
    for name in (
        "API_WORKERS",
        "WORKER_CONCURRENCY",
        "WORKER_POOL",
        "API_LOG_LEVEL",
        "WORKER_LOG_LEVEL",
        "SCHEDULER_LOG_LEVEL",
    ):
        if name in os.environ:
            args += ["-e", name]
    return args


def runtime_action(store, release, action):
    if store.doc["deployment"]["mode"] == "docker":
        temporary(
            store.doc["deployment"],
            [*runtime_mounts(store, release), release["image"], action],
        )
    else:
        execute(
            [
                sys.executable,
                ROOT / "deploy/runtime.py",
                action,
                "--layout",
                store.runtime / "runtime.yaml",
            ]
        )


def unit_name(store):
    return "everunfold-" + hashlib.sha256(str(store.path).encode()).hexdigest()[:10] + ".service"


def ensure_native_service():
    try:
        execute(["systemctl", "--user", "show-environment"], capture=True)
    except (OSError, ValueError):
        raise ValueError(
            "直接部署后台启动需要 systemd 用户会话。可先 build，再用 ./setup.sh --native run 在前台运行；后台常驻方法见 deploy/README.md。"
        ) from None


def systemd_quote(value):
    return (
        '"'
        + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        + '"'
    )


def install_unit(store):
    unit = Path.home() / ".config/systemd/user" / unit_name(store)
    unit.parent.mkdir(parents=True, exist_ok=True)
    command = " ".join(
        systemd_quote(value)
        for value in [
            sys.executable,
            ROOT / "deploy/runtime.py",
            "serve",
            "--layout",
            store.runtime / "runtime.yaml",
        ]
    )
    unit.write_text(
        "[Unit]\nDescription=Everunfold frontend and backend\nAfter=network-online.target\n\n"
        "[Service]\nType=simple\nExecStart=" + command + "\nRestart=on-failure\nRestartSec=5\n"
        "KillMode=mixed\nTimeoutStopSec=45\nUMask=0077\n\n[Install]\nWantedBy=default.target\n"
    )
    execute(["systemctl", "--user", "daemon-reload"])
    execute(["systemctl", "--user", "enable", "--now", unit.name])


def inspect_container(name):
    """Look up only containers; other Docker objects may share the same name."""
    result = subprocess.run(
        ["docker", "container", "inspect", name], capture_output=True, text=True
    )
    if result.returncode:
        detail = (result.stderr or "").strip()
        missing = {
            f"{prefix}No such {kind}: {name}"
            for prefix in ("Error: ", "Error response from daemon: ")
            for kind in ("container", "object")
        }
        if detail in missing:
            return None
        raise ValueError(
            f"无法查询 Docker 容器 {name}（退出码 {result.returncode}），未停止或删除任何容器。"
            f"请检查 Docker 服务、连接和权限。\n{detail or 'Docker 未返回错误详情。'}"
        )
    try:
        objects = json.loads(result.stdout)
    except (ValueError, TypeError):
        objects = None
    valid = isinstance(objects, list) and len(objects) == 1 and isinstance(objects[0], dict)
    existing = objects[0] if valid else {}
    config = existing.get("Config")
    container_id = existing.get("Id")
    if (
        not isinstance(config, dict)
        or not isinstance(container_id, str)
        or re.fullmatch(r"[a-f0-9]{64}", container_id) is None
        or (config.get("Labels") is not None and not isinstance(config["Labels"], dict))
    ):
        raise ValueError(
            f"Docker 返回的 {name} 容器详情格式无效，未停止或删除任何容器。"
            "请检查 Docker 客户端与服务端兼容性。"
        )
    return existing


def stop(store):
    d = store.doc["deployment"]
    if d["mode"] == "native":
        execute(["systemctl", "--user", "stop", unit_name(store)])
    else:
        existing = inspect_container(d["container"])
        if existing is None:
            return
        if (existing["Config"].get("Labels") or {}).get("org.everunfold.deployment") != str(
            store.path
        ):
            raise ValueError("同名容器不属于此部署，请修改容器名称。")
        # Operate on the verified ID so a replacement using the same name cannot
        # bypass the ownership check between inspect and stop/remove.
        execute(["docker", "stop", "--time", "40", existing["Id"]])
        execute(["docker", "rm", existing["Id"]])


def publish_plugins(source, target):
    # Package files have content hashes; manifests are replaced atomically last.
    target.mkdir(parents=True, exist_ok=True)
    paths = sorted(Path(source).rglob("*"), key=lambda p: (p.name == "manifest.json", str(p)))
    for path in paths:
        if not path.is_file():
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = destination.with_name("." + destination.name + ".new")
        shutil.copy2(path, temp)
        os.replace(temp, destination)


def start(store, release, *, initialize=False, foreground=False):
    d = store.doc["deployment"]
    directory = store.var / "releases" / release["release"]
    if not (directory / "frontend/.next/BUILD_ID").is_file():
        raise ValueError("缺少前端编译产物，请先构建。")
    if d["mode"] == "docker":
        ensure_docker()
        ensure_networks(d)
    elif not foreground:
        ensure_native_service()
    for role in ("backend", "frontend"):
        Path(d[role + "_data"]).mkdir(parents=True, exist_ok=True)
    # Stop before re-pairing. A failed prepare leaves the previous release on disk.
    if not foreground:
        stop(store)
    store.render()
    layout_for(store, release)
    runtime_action(store, release, "init" if initialize else "prepare")
    store.import_runtime()
    store.save()
    publish_plugins(directory / "plugins", Path(d["frontend_data"]) / "plugins")
    if foreground:
        runtime_action(store, release, "serve")
        return
    if d["mode"] == "docker":
        host = "[" + d["bind"] + "]" if ":" in d["bind"] else d["bind"]
        args = [
            "--name",
            d["container"],
            "--restart",
            "unless-stopped",
            "--stop-timeout",
            "45",
            "--label",
            "org.everunfold.deployment=" + str(store.path),
            *runtime_mounts(store, release),
        ]
        for role, key in (("backend", "API_PORT"), ("frontend", "FRONTEND_PORT")):
            args += [
                "-p",
                f"{host}:{d[role + '_port']}:{store.doc[role]['environment'][key]}",
            ]
        container = docker_create(d, [*args, release["image"], "serve"])
        execute(["docker", "start", container])
    else:
        install_unit(store)
    write(store.var / "active.yaml", release)
    print("[start] 服务已启动，等待两端就绪与首次同步…", flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if d["mode"] == "docker":
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    d["container"],
                    "python",
                    "/opt/everunfold/deploy/runtime.py",
                    "health",
                ],
                capture_output=True,
            )
        else:
            result = subprocess.run(
                [
                    sys.executable,
                    ROOT / "deploy/runtime.py",
                    "health",
                    "--layout",
                    store.runtime / "runtime.yaml",
                ],
                capture_output=True,
            )
        if result.returncode == 0:
            print("[ready] 前端与后端 /health/ready 均通过。", flush=True)
            return
        time.sleep(2)
    raise ValueError(
        "服务已启动，但 120 秒内尚未全部就绪。请运行 ./setup.sh logs 查看同步或启动原因。"
    )


async def probe_document(store):
    from probe import probe

    d = store.doc["deployment"]
    if d["mode"] == "native":
        return [Check(**item) for item in await probe(store.doc, d["backend_source"])]
    with tempfile.TemporaryDirectory(prefix="everunfold-probe-") as temp:
        path = Path(temp)
        write(path / "input.yaml", store.doc)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(ROOT / "deploy/main.py"),
            "_probe",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode:
            return [Check("Docker 检测", "error", output.decode(errors="replace")[-5000:])]
        return [Check(**item) for item in json.loads((path / "result.json").read_text())]


def docker_probe(path):
    doc = read(path / "input.yaml")
    d = doc["deployment"]
    ensure_docker()
    ensure_networks(d)
    fingerprint = hashlib.sha256(
        (ROOT / "uv.lock").read_bytes()
        + (ROOT / "docker/setup.Dockerfile").read_bytes()
        + (ROOT / "deploy/probe.py").read_bytes()
    ).hexdigest()[:12]
    image = "everunfold-setup:" + fingerprint
    exists = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if exists.returncode:
        execute(
            [
                "docker",
                "build",
                "--progress=plain",
                "-t",
                image,
                "-f",
                ROOT / "docker/setup.Dockerfile",
                ROOT,
            ]
        )
    args = [*mount(path, "/probe", True)]
    if Path(d["backend_source"]).is_dir():
        args += mount(d["backend_source"], "/source/backend", True)
    result = temporary(d, [*args, image, "/probe/input.yaml", "/source/backend"], capture=True)
    (path / "result.json").write_text(json.dumps(json.loads(result)))
