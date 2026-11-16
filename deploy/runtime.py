"""Single supervisor for API, worker, scheduler and frontend; no build at restart."""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from storage import read

PROCESS_OVERRIDES = {
    "API_WORKERS",
    "WORKER_POOL",
    "WORKER_CONCURRENCY",
    "API_LOG_LEVEL",
    "WORKER_LOG_LEVEL",
    "SCHEDULER_LOG_LEVEL",
}


def environment(layout, role):
    document = read(layout[role + "_config"])
    private = document["environment"]
    # CONFIG_FILE is intentionally different for each process. Do not inject the
    # frontend YAML environment into os.environ: its loader preserves hot edits.
    names = set(private) | {
        "CONFIG_FILE",
        "EVERUNFOLD_CONFIG_LOADED",
        "PYTHONPATH",
        "EVERUNFOLD_PROJECT_ROOT",
        "BACKEND_BASE_URL",
        "PUBLIC_SITE_URL",
        "FRONTEND_NODE_ID",
        "EVERUNFOLD_DATA_DIR",
        "PLUGINS_DIRECTORY",
    }
    env = {k: v for k, v in os.environ.items() if k not in names}
    if role == "backend":
        env.update(private)
    env.update({k: os.environ[k] for k in PROCESS_OVERRIDES if k in os.environ})
    env.update(
        CONFIG_FILE=layout[role + "_config"],
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=layout["backend_root"],
        EVERUNFOLD_DATA_DIR=layout[role + "_data"],
        EVERUNFOLD_PROJECT_ROOT=layout[role + "_root"],
        NEXT_TELEMETRY_DISABLED="1",
    )
    if role == "frontend":
        env.update(
            NODE_ENV="production",
            PLUGINS_DIRECTORY=layout["frontend_data"] + "/plugins",
        )
    if layout.get("tls_proxy_ips"):
        env["EVERUNFOLD_NODES_TLS_PROXY_IPS"] = layout["tls_proxy_ips"]
    return env


def run_backend(layout, arguments):
    subprocess.run(
        [layout["python"], *arguments],
        cwd=layout["backend_root"],
        env=environment(layout, "backend"),
        check=True,
    )


def prepare(layout, initialize=False):
    run_backend(
        layout,
        ["deploy/runtime_check.py", "--initialize" if initialize else "--prepare"],
    )
    run_backend(layout, [str(Path(__file__).with_name("pair.py")), layout["frontend_config"]])


def register_backend_plugins(layout):
    print("[plugins] 同步当前后端版本的插件清单……", flush=True)
    run_backend(layout, ["-m", "tools.register_backend_plugins", "--apply"])


def trigger_sync(layout):
    if not (Path(layout["backend_root"]) / "tools/trigger_frontend_sync.py").is_file():
        raise ValueError("当前后端版本缺少触发同步命令，请先更新并部署新版后端。")
    run_backend(layout, ["-m", "tools.trigger_frontend_sync"])


def commands(layout):
    env = environment(layout, "backend")
    python = layout["python"]
    return {
        "api": (
            "backend",
            [
                python,
                "-m",
                "uvicorn",
                "app.entrypoints.api:app",
                "--host",
                env.get("API_HOST", "0.0.0.0"),
                "--port",
                env.get("API_PORT", "8001"),
                "--workers",
                env.get("API_WORKERS", "1"),
                "--log-level",
                env.get("API_LOG_LEVEL", "INFO").lower(),
                "--no-proxy-headers",
            ],
        ),
        "worker": (
            "backend",
            [
                python,
                "-m",
                "celery",
                "-A",
                "app.entrypoints.worker:celery_app",
                "worker",
                "--pool",
                env.get("WORKER_POOL", "solo"),
                "--concurrency",
                env.get("WORKER_CONCURRENCY", "1"),
                "--loglevel",
                env.get("WORKER_LOG_LEVEL", "WARNING"),
            ],
        ),
        "scheduler": ("backend", [python, "-m", "app.entrypoints.scheduler"]),
        "frontend": (
            "frontend",
            [
                layout.get("node", "node"),
                "--import",
                "tsx",
                "tools/plugins/run.mts",
                "start",
            ],
        ),
    }


def health(layout, frontend=True):
    for role in ("backend", "frontend") if frontend else ("backend",):
        document = read(layout[role + "_config"])
        port = document["environment"]["API_PORT" if role == "backend" else "FRONTEND_PORT"]
        target = f"http://127.0.0.1:{port}/health/ready"
        with urllib.request.urlopen(target, timeout=4) as response:
            if response.status != 200:
                raise ValueError(role + " not ready")


@contextmanager
def runtime_lock(layout):
    # Preparing a different release must not change the catalog under a running API.
    lock_file = Path(layout["backend_data"]) / ".runtime.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with lock_file.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("此数据目录已有实例运行或正在启动，请先停止该实例。") from None
        yield


def serve(layout):
    with runtime_lock(layout):
        return supervise(layout)


def supervise(layout):
    processes = {}
    stopping = False
    status = 0

    def stop(*_):
        nonlocal stopping
        stopping = True

    previous_handlers = {s: signal.signal(s, stop) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for name, (role, command) in commands(layout).items():
            if stopping:
                break
            print(
                f"[Everunfold] 启动 {name}；CONFIG_FILE={layout[role + '_config']}",
                flush=True,
            )
            processes[name] = subprocess.Popen(
                command,
                cwd=layout[role + "_root"],
                env=environment(layout, role),
                start_new_session=True,
            )
        while not stopping:
            for name, process in processes.items():
                if process.poll() is not None:
                    status = process.returncode or 1
                    print(
                        f"[Everunfold] {name} 意外退出（{process.returncode}）；停止其余进程。",
                        flush=True,
                    )
                    stopping = True
                    break
            time.sleep(0.2)
    finally:
        for process in processes.values():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 35
        for process in processes.values():
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        # Kill whole groups even if a parent died before its children.
        for process in processes.values():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        for s, handler in previous_handlers.items():
            signal.signal(s, handler)
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("serve", "prepare", "prepare-init", "init", "check", "health", "migrate", "sync"),
    )
    parser.add_argument(
        "--layout", default=os.environ.get("EVERUNFOLD_RUNTIME", "/config/runtime.yaml")
    )
    args = parser.parse_args()
    layout = read(args.layout)
    if args.action in {"prepare", "prepare-init"}:
        with runtime_lock(layout):
            prepare(layout, initialize=args.action == "prepare-init")
            register_backend_plugins(layout)
    elif args.action == "init":
        prepare(layout, initialize=True)
    elif args.action == "serve":
        return serve(layout)
    elif args.action == "check":
        run_backend(layout, ["deploy/runtime_check.py"])
    elif args.action == "health":
        health(layout)
    elif args.action == "sync":
        trigger_sync(layout)
    else:
        run_backend(layout, ["-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"])
        register_backend_plugins(layout)
        run_backend(layout, ["deploy/runtime_check.py"])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as error:
        sys.exit(error.returncode)
    except (ValueError, OSError) as error:
        print(f"[Everunfold] {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
