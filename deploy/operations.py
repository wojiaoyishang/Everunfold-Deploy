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
from storage import ROOT, lock, read, write
from worker_capacity import PROCESS_KEYS, capacity_arguments


def execute(args, *, cwd=None, env=None, capture=False, check=True, stdin=None):
    result = subprocess.run(
        [str(a) for a in args],
        cwd=cwd,
        env=env,
        stdin=stdin,
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


def update_sources(store, roles=("backend", "frontend")):
    d = store.doc["deployment"]
    for role in roles:
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
    required = {
        "backend": ("pyproject.toml", "uv.lock", "deploy/runtime_check.py"),
        "frontend": (
            "package-lock.json",
            "contracts.lock.json",
            "tools/plugins/run.mts",
        ),
    }
    for role in roles:
        for file in required[role]:
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


def release_directory(store):
    release = time.strftime("%Y%m%dT%H%M%S", time.gmtime()) + "-" + os.urandom(3).hex()
    directory = store.var / "releases" / release
    directory.mkdir(parents=True)
    print(f"[build] Everunfold {release}；输出目录 {directory}", flush=True)
    return directory


def source_revision(source):
    result = subprocess.run(
        ["git", "-C", source, "rev-parse", "HEAD"], capture_output=True, text=True
    )
    revision = result.stdout.strip() if result.returncode == 0 else "local"
    dirty = bool(
        subprocess.run(
            ["git", "-C", source, "status", "--porcelain"], capture_output=True, text=True
        ).stdout.strip()
    )
    return revision, dirty


def save_build(store, directory, manifest):
    write(directory / "release.yaml", manifest)
    write(store.var / "built.yaml", manifest)
    print("[build] 构建成功；尚未替换运行中的服务。", flush=True)
    return manifest


def build_image(store, directory, revisions):
    context = store.var / "build" / directory.name / "image"
    (context / "backend").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(directory / "backend" / name, context / "backend" / name)
    shutil.copytree(
        ROOT / "deploy", context / "deploy", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy2(ROOT / "docker/frontend-build.sh", context / "frontend-entrypoint.sh")
    image = "everunfold:" + directory.name.lower()
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
    return image


def compile_frontend(store, directory, image=None, *, plugins_only=False):
    d = store.doc["deployment"]
    cache = Path(d["frontend_data"]) / "npm-cache"
    plugins = directory / "plugins"
    for path in (cache, plugins):
        path.mkdir(parents=True, exist_ok=True)
    memory = "--max-old-space-size=" + d["build_memory"]
    commands = (
        (("build-plugins",),) if plugins_only else (("build", "frontend"), ("build-plugins",))
    )
    if image:
        builder = [
            "--user",
            d["uid"] + ":" + d["gid"],
            "--entrypoint",
            "/usr/local/bin/frontend-build",
            "-e",
            "FRONTEND_BUILD_WORKERS=" + d["build_workers"],
            "-e",
            "NODE_OPTIONS=" + memory,
            *mount(d["frontend_source"], "/workspace", True),
            *mount(directory, "/output"),
            *mount(cache, "/build/npm-cache"),
            *mount(plugins, "/plugins"),
        ]
        for args in commands:
            temporary(d, [*builder, image, *args])
    else:
        env = {
            **build_environment(),
            "SOURCE_ROOT": d["frontend_source"],
            "BUILD_ROOT": str(
                directory / "work"
                if plugins_only
                else store.var / "build" / directory.name / "work"
            ),
            "OUTPUT_ROOT": str(directory),
            "PLUGIN_OUTPUT": str(plugins),
            "npm_config_cache": str(cache),
            "FRONTEND_BUILD_WORKERS": d["build_workers"],
            "NODE_OPTIONS": memory,
        }
        for args in commands:
            execute(["bash", ROOT / "docker/frontend-build.sh", *args], env=env)
    if not plugins_only and not (directory / "frontend/.next/BUILD_ID").is_file():
        raise ValueError("缺少前端编译产物，无法发布构建版本。")


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
    directory = release_directory(store)
    snapshot_backend(d["backend_source"], directory / "backend")
    revisions, dirty = {}, {}
    for role in ("backend", "frontend"):
        revisions[role], dirty[role] = source_revision(d[role + "_source"])
    manifest = {
        "release": directory.name,
        "mode": d["mode"],
        "revisions": revisions,
        "dirty": dirty,
    }
    if d["mode"] == "docker":
        manifest["image"] = build_image(store, directory, revisions)
        manifest["celery_autoscale"] = True
    else:
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
            env=build_environment(),
        )
    compile_frontend(store, directory, manifest.get("image"))
    return save_build(store, directory, manifest)


def require_image(image):
    try:
        execute(["docker", "image", "inspect", "--format", "{{.Id}}", image], capture=True)
    except ValueError as error:
        raise ValueError(
            f"无法使用已有运行镜像 {image}。请恢复该镜像，或运行 ./setup.sh --docker build。"
        ) from error


def frontend_deploy_tool(store, relative):
    path = Path(store.doc["deployment"]["frontend_source"]) / relative
    if not path.is_file():
        raise ValueError(f"前端源码目录缺少 {relative}，请先更新前端源码仓库。")
    return path


def build_frontend_plugins(store):
    d = store.doc["deployment"]
    image = None
    if d["mode"] == "docker":
        ensure_docker()
        release = read(store.var / "built.yaml")
        if release.get("mode") != "docker" or not release.get("image"):
            raise ValueError("请先进行一次 Docker 构建，准备可复用的前端构建镜像。")
        image = release["image"]
        require_image(image)
        ensure_networks(d)
    data = Path(d["frontend_data"]).resolve()
    with lock(data / ".plugin-release.lock"):
        update_sources(store, ("frontend",))
        stage_tool = frontend_deploy_tool(store, "deploy/plugin_stage.py")
        with tempfile.TemporaryDirectory(prefix=".plugin-build.", dir=data) as stage:
            compile_frontend(store, Path(stage), image, plugins_only=True)
            execute([sys.executable, stage_tool, Path(stage) / "plugins", data])


def hot_update_frontend(store):
    d = store.doc["deployment"]
    data = Path(d["frontend_data"]).resolve()
    with lock(data / ".plugin-release.lock"):
        inbox = data / "plugin-inbox"
        inbox.mkdir(exist_ok=True)
        print(f"待发布目录：{inbox}", flush=True)
        script = frontend_deploy_tool(store, "tools/plugins/publish.mjs")
        if d["mode"] == "docker":
            active = read(store.var / "active.yaml")
            if active.get("mode") != "docker" or not active.get("release"):
                raise ValueError("请先按当前部署方式启动服务。")
            release_id = active["release"]
            container = deployment_container(store)
            if not container or not (container.get("State") or {}).get("Running"):
                raise ValueError("请先启动此部署的容器。")
            if not any(
                item.get("Destination") == "/data/frontend"
                and Path(item.get("Source", "")).resolve() == data
                for item in container.get("Mounts", [])
            ):
                raise ValueError("运行中的容器使用其他前端数据目录，请核对保存的配置。")
            with script.open("rb") as program:
                execute(
                    [
                        "docker",
                        "exec",
                        "-i",
                        "--workdir",
                        "/workspace/frontend",
                        container["Id"],
                        "node",
                        "--import",
                        "tsx",
                        "--input-type=module",
                        "-",
                        "/data/frontend",
                    ],
                    stdin=program,
                )
        else:
            layout = read(store.runtime / "runtime.yaml")
            frontend = Path(layout.get("frontend_root", ""))
            if not (frontend / ".next/BUILD_ID").is_file() or not layout.get("node"):
                raise ValueError("请先通过向导启动前端，生成运行布局。")
            if Path(layout.get("frontend_data", "")).resolve() != data:
                raise ValueError("运行布局使用其他前端数据目录，请核对保存的配置。")
            release_id = frontend.parent.name
            execute(
                [layout["node"], "--import", "tsx", script, str(data)],
                cwd=frontend,
            )
        write(data / ".plugin-host.yaml", {"release": release_id})


def rebuild_frontend(store):
    d = store.doc["deployment"]
    if d["mode"] != "docker":
        raise ValueError("重新编译前端复用 Docker 镜像，请使用 Docker 部署模式。")
    previous = read(store.var / "built.yaml")
    image = previous.get("image")
    if previous.get("mode") != "docker" or not isinstance(image, str) or not image:
        raise ValueError("没有可复用的 Docker 构建，请先运行 ./setup.sh --docker build。")
    previous_id = previous.get("release")
    if not isinstance(previous_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", previous_id
    ):
        raise ValueError("已有 Docker 构建的版本记录无效，请先执行完整构建。")
    backend = store.var / "releases" / previous_id / "backend"
    if not backend.is_dir():
        raise ValueError(f"后端快照已缺失：{backend}。请先执行完整构建。")
    ensure_docker()
    require_image(image)
    ensure_networks(d)
    update_sources(store, ("frontend",))

    directory = release_directory(store)
    shutil.copytree(backend, directory / "backend")
    revision, dirty = source_revision(d["frontend_source"])
    manifest = {
        **previous,
        "release": directory.name,
        "revisions": {**previous.get("revisions", {}), "frontend": revision},
        "dirty": {**previous.get("dirty", {}), "frontend": dirty},
    }
    print(f"[build] 使用已有镜像 {image} 编译前端宿主与插件。", flush=True)
    compile_frontend(store, directory, image)
    save_build(store, directory, manifest)
    print("可选择“启动已构建版本”，或运行 ./setup.sh --docker start 应用本次编译。", flush=True)


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
        *PROCESS_KEYS,
        "API_LOG_LEVEL",
        "WORKER_LOG_LEVEL",
        "SCHEDULER_LOG_LEVEL",
        "FRONTEND_WORKERS",
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


def deployment_container(store):
    existing = inspect_container(store.doc["deployment"]["container"])
    if existing is not None and (existing["Config"].get("Labels") or {}).get(
        "org.everunfold.deployment"
    ) != str(store.path):
        raise ValueError("同名容器不属于此部署，请修改容器名称。")
    return existing


def stop(store):
    if store.doc["deployment"]["mode"] == "native":
        execute(["systemctl", "--user", "stop", unit_name(store)])
    elif (existing := deployment_container(store)) is not None:
        # Operate on the verified ID so a replacement using the same name cannot
        # bypass the ownership check between inspect and stop/remove.
        execute(["docker", "stop", "--time", "40", existing["Id"]])
        execute(["docker", "rm", existing["Id"]])


def trigger_sync(store):
    if store.doc["deployment"]["mode"] == "docker":
        container = deployment_container(store)
        state = container.get("State") if container else None
        if container is None or not isinstance(state, dict) or state.get("Running") is not True:
            raise ValueError("请先启动此部署的容器，再触发同步。")
        execute(
            [
                "docker",
                "exec",
                container["Id"],
                "python",
                "/opt/everunfold/deploy/runtime.py",
                "sync",
            ]
        )
        return
    layout = store.runtime / "runtime.yaml"
    if not layout.is_file():
        raise ValueError("请先构建并启动此部署，生成运行配置。")
    execute([sys.executable, ROOT / "deploy/runtime.py", "sync", "--layout", layout])


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


def restart_container(store):
    if store.doc["deployment"]["mode"] != "docker":
        raise ValueError("重启容器仅适用于 Docker 部署。")
    existing = deployment_container(store)
    if existing is None:
        raise ValueError("此部署没有可重启的容器，请先运行 ./setup.sh --docker start。")
    container = existing["Id"]
    execute(["docker", "restart", "--time", "45", container])
    wait_until_ready(store, container)


def start(store, release, *, initialize=False, foreground=False):
    d = store.doc["deployment"]
    directory = store.var / "releases" / release["release"]
    if release.get("mode") != d["mode"]:
        raise ValueError("部署方式已改变，请重新构建。")
    if not (directory / "frontend/.next/BUILD_ID").is_file():
        raise ValueError("缺少前端编译产物，请先构建。")
    workers = os.environ.get(
        "FRONTEND_WORKERS", store.doc["frontend"]["environment"].get("FRONTEND_WORKERS", "1")
    )
    if not re.fullmatch(r"[1-9]|[12][0-9]|3[0-2]", workers):
        raise ValueError("FRONTEND_WORKERS 必须是 1–32 之间的整数。")
    if workers != "1" and not (directory / "frontend/tools/frontend-cluster.ts").is_file():
        raise ValueError("当前前端产物不支持多个运行 worker；请用新版前端源码重新编译。")
    if store.doc["backend"]["environment"].get("EVERUNFOLD_METRICS_TOKEN") and (
        not (directory / "backend/app/entrypoints/serve_api.py").is_file()
        or not (directory / "frontend/tools/frontend-metrics.ts").is_file()
    ):
        raise ValueError("当前产物缺少监控模块；请更新前后端源码并完整构建后启动。")
    capacity = capacity_arguments(
        store.doc["backend"]["environment"]
        | {key: os.environ[key] for key in PROCESS_KEYS if key in os.environ}
    )
    if "--autoscale" in capacity and d["mode"] == "docker" and not release.get("celery_autoscale"):
        raise ValueError("当前部署镜像不支持 Celery 自动伸缩；请更新部署仓库后完整构建一次。")
    container = None
    if d["mode"] == "docker":
        ensure_docker()
        require_image(release["image"])
        ensure_networks(d)
    elif not foreground:
        ensure_native_service()
    for role in ("backend", "frontend"):
        Path(d[role + "_data"]).mkdir(parents=True, exist_ok=True)
    frontend_data = Path(d["frontend_data"])
    with lock(frontend_data / ".plugin-release.lock"):
        # Stop before re-pairing. A failed prepare leaves the previous release on disk.
        if not foreground:
            stop(store)
        store.render()
        layout_for(store, release)
        runtime_action(store, release, "prepare-init" if initialize else "prepare")
        store.import_runtime()
        store.save()
        marker = frontend_data / ".plugin-host.yaml"
        plugins = frontend_data / "plugins"
        if read(marker).get("release") != release["release"] or not plugins.is_dir():
            publish_plugins(directory / "plugins", plugins)
            marker.unlink(missing_ok=True)
    if foreground:
        runtime_action(store, release, "serve")
        return
    if d["mode"] == "docker":
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
            host = d.get(role + "_bind", d.get("bind", "127.0.0.1"))
            if host:
                host = f"[{host}]" if ":" in host else host
                mapping = f"{host}:{d[role + '_port']}:{store.doc[role]['environment'][key]}"
                args += ["-p", mapping]
                print(f"[start] {role} 宿主映射 {mapping}", flush=True)
        container = docker_create(d, [*args, release["image"], "serve"])
        execute(["docker", "start", container])
    else:
        install_unit(store)
    write(store.var / "active.yaml", release)
    wait_until_ready(store, container)


def wait_until_ready(store, container=None):
    print("[start] 服务已启动，等待两端就绪…", flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if container is not None:
            result = subprocess.run(
                [
                    "docker",
                    "exec",
                    container,
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
