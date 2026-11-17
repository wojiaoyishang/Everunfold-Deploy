"""Public entry point, always launched from root setup.sh."""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

from configure import configure, defaults, imported
from operations import (
    build,
    docker_probe,
    ensure_native_service,
    execute,
    layout_for,
    probe_document,
    rebuild_frontend,
    restart_container,
    runtime_action,
    start,
    stop,
    trigger_sync,
    unit_name,
    update_sources,
)
from storage import Store, lock, read


def perform(store, action):
    if action == "sync":
        # The foreground supervisor holds the operation lock for its lifetime.
        trigger_sync(store)
        return
    d = store.doc["deployment"]
    with lock(store.var / "operation.lock"):
        if action == "pull":
            update_sources(store)
            return
        if action == "check":
            results = asyncio.run(probe_document(store))
            for item in results:
                print(f"{item.name}: {item.state} — {item.detail}")
            if any(item.state == "error" for item in results):
                raise ValueError("检测未通过，请修正上方问题。")
            return
        if action == "rebuild-frontend":
            rebuild_frontend(store)
            return
        if action == "restart":
            restart_container(store)
            return
        if action == "stop":
            stop(store)
            return
        if action == "logs":
            if d["mode"] == "docker":
                execute(["docker", "logs", "--tail", "100", "--follow", d["container"]])
            else:
                execute(["journalctl", "--user", "-u", unit_name(store), "-n", "100", "-f"])
            return
        if action in {"build", "deploy", "init-deploy"}:
            if d["mode"] == "native" and action != "build":
                ensure_native_service()
            release = build(store)
            if action == "build":
                return
        else:
            release = read(store.var / "built.yaml")
            if not release:
                raise ValueError("请先在向导中选择“保存并构建”，或运行 ./setup.sh build。")
        if action in {"init-db", "migrate"}:
            if action == "migrate":
                stop(store)
            for role in ("backend", "frontend"):
                Path(d[role + "_data"]).mkdir(parents=True, exist_ok=True)
            store.render()
            layout_for(store, release)
            runtime_action(store, release, "init" if action == "init-db" else "migrate")
            store.import_runtime()
            store.save()
            print("操作完成；运行 ./setup.sh start 启动服务。")
            return
        start(
            store,
            release,
            initialize=action == "init-deploy",
            foreground=action == "run",
        )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "_probe":
        docker_probe(Path(sys.argv[2]))
        return
    parser = argparse.ArgumentParser(description="Everunfold 前后端统一部署")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--docker", action="store_true", help="Docker 单容器部署")
    modes.add_argument("--native", action="store_true", help="不使用 Docker")
    parser.add_argument(
        "action",
        nargs="?",
        default="configure",
        choices=(
            "configure",
            "import",
            "pull",
            "build",
            "rebuild-frontend",
            "deploy",
            "init-deploy",
            "start",
            "restart",
            "run",
            "stop",
            "logs",
            "check",
            "sync",
            "init-db",
            "migrate",
        ),
    )
    parser.add_argument("--backend-source", type=Path, help="import 时的后端源码目录")
    parser.add_argument("--frontend-source", type=Path, help="import 时的前端源码目录")
    args = parser.parse_args()
    mode = "docker" if args.docker else "native" if args.native else None
    store = Store()
    defaults(store, mode)
    if args.action == "import":
        if store.path.exists():
            raise ValueError(
                "统一配置已存在，拒绝覆盖。请在向导中编辑；如需重新导入，先备份并移走统一配置及 var/runtime。"
            )
        for role in ("backend", "frontend"):
            source = getattr(args, role + "_source")
            if source:
                store.doc["deployment"][role + "_source"] = str(source.expanduser().resolve())
            if imported(store, role, store.doc["deployment"][role + "_source"]):
                print(f"已读取 {role} 原有配置和密钥。")
        store.save()
        print(f"导入成功：{store.path}。请运行 ./setup.sh 核对目录并保存。")
        return
    if args.backend_source or args.frontend_source:
        parser.error("源码参数仅用于 import；其他操作请在向导中设置保存位置。")
    if args.action != "configure":
        if not store.path.exists():
            raise ValueError("请先运行 ./setup.sh 完成配置并保存。")
        if args.action not in {"logs", "stop", "pull", "sync", "restart"}:
            form = configure(store, mode)
            errors = form.errors()
            if errors:
                raise ValueError("\n".join(errors))
            # Explicit builds apply unified settings; start uses current runtime/browser edits.
            if args.action in {"build", "deploy", "init-deploy"}:
                form.save()
        perform(store, args.action)
        return
    if not sys.stdin.isatty():
        raise ValueError("交互向导需要终端；自动化可使用 build / deploy / check 等子命令。")
    from config_ui import run

    actions = {
        20: "build",
        21: "deploy",
        22: "init-deploy",
        23: "rebuild-frontend",
        24: "restart",
        25: "start",
        30: "init-db",
        31: "migrate",
        32: "sync",
        40: "pull",
    }
    notice = ""
    form = None
    while True:
        if form is None:
            form = configure(store, mode)
        if notice:
            form.notice = notice
            notice = ""
        result = run(form)
        if result == 33:
            store = Store()
            mode = None
            notice = "原配置导入成功；请核对数据目录、发布 URL 和凭据，再保存或部署。"
            form = None
            continue
        if result not in actions:
            return
        action = actions[result]
        print(f"\nEverunfold · {action} · {store.path}", flush=True)
        try:
            target = store
            if action in {"sync", "restart"}:
                if not store.path.is_file():
                    raise ValueError("请先保存部署配置。")
                target = Store(store.path)
                defaults(target, mode)
            perform(target, action)
        except (OSError, ValueError) as error:
            print(f"\033[31m操作未完成：{error}\033[0m", file=sys.stderr)
        input("\n按回车返回配置向导…")
        if action in {"sync", "restart"}:
            continue
        store = Store()
        mode = None
        form = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit("操作已中断。") from None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Everunfold：{error}") from None
