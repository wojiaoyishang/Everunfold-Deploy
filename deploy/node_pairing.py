"""Issue credentials for one external frontend through the running backend release."""

import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from config_validation import identifier, private_receiver_url, public_url, token, uuid_value
from operations import deployment_container
from runtime import environment
from storage import Store, lock, read, write

MARKER = "EVERUNFOLD_NODE_CONNECTION_BASE64="


def validate_receiver(receiver, allow_private_http):
    valid = private_receiver_url if allow_private_http else public_url
    if not valid(receiver) or re.search(r"[\x00-\x1f\x7f]", receiver):
        raise ValueError("请输入后端 Worker 可访问的 HTTPS 接收器根地址，或显式允许可信私网 HTTP。")


def backend_target(document):
    app, env = document["application"], document["environment"]
    return (
        env.get("DATABASE_URL") or app.get("database", {}).get("url"),
        env.get("BACKEND_BASE_URL") or app.get("BACKEND_BASE_URL"),
    )


def export_runner(store):
    if store.doc["deployment"]["mode"] == "native":
        layout = read(store.runtime / "runtime.yaml")
        if (
            layout.get("backend_config") != str(store.path_for("backend"))
            or not Path(layout.get("python", "")).is_file()
        ):
            raise ValueError("请先构建并启动本部署的后端。")

        def run(config_path, args):
            selected = {**layout, "backend_config": str(config_path)}
            return [layout["python"], *args], {
                "cwd": layout["backend_root"],
                "env": environment(selected, "backend"),
            }

        return run

    container = deployment_container(store)
    state = container.get("State") if container else None
    if not isinstance(state, dict) or state.get("Running") is not True:
        raise ValueError("请先启动此部署的容器。")

    def run(config_path, args):
        return [
            "docker",
            "exec",
            "-w",
            "/workspace/backend",
            "-e",
            f"CONFIG_FILE=/config/backend/{config_path.name}",
            container["Id"],
            "/opt/backend/venv/bin/python",
            *args,
        ], {}

    return run


@contextmanager
def export_config(path, document, *, temporary):
    if not temporary:
        yield path
        return
    descriptor, name = tempfile.mkstemp(prefix=".node-export-", suffix=".yaml", dir=path.parent)
    os.close(descriptor)
    target = Path(name)
    try:
        write(target, document)
        yield target
    finally:
        target.unlink(missing_ok=True)


def decode_bundle(encoded, node):
    """Check the backend's portable result before presenting it as a credential."""
    try:
        if not encoded or len(encoded) > 87384:
            raise ValueError
        raw = base64.b64decode(encoded, validate=True)
        bundle = json.loads(raw.decode("utf-8"))
        inbound, outbound = (bundle["tokens"][direction] for direction in ("inbound", "outbound"))
        if (
            len(raw) > 65536
            or bundle["format"] != "everunfold-node-connection/v1"
            or bundle["node_id"] != node
            or not public_url(bundle["backend_public_url"])
            or not uuid_value(bundle["source_epoch"])
            or not all(
                re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", item["name"])
                and len(item["value"]) <= 4096
                and token(item["value"])
                for item in (inbound, outbound)
            )
            or any(inbound[key] == outbound[key] for key in ("name", "value"))
        ):
            raise ValueError
    except (KeyError, ValueError, TypeError, RecursionError):
        raise ValueError("后端未返回有效的前端配对凭据。") from None
    return raw


def export_node(path, node, receiver="", allow_private_http=False):
    node, receiver = node.strip(), receiver.strip()
    if not identifier(node):
        raise ValueError("请输入有效的前端节点 ID。")
    if receiver:
        validate_receiver(receiver, allow_private_http)
        receiver = receiver.rstrip("/")
    elif allow_private_http:
        raise ValueError("允许私网 HTTP 时请同时填写接收器地址。")

    path = Path(path).resolve()
    with lock(path.parent / "var" / "pairing-export.lock"):
        store = Store(path)
        if not store.original:
            raise ValueError("请先保存并启动统一部署。")
        backend = store.doc["backend"]
        meta = backend["assistant"]
        deployed_path = store.path_for("backend")
        deployed = read(deployed_path)
        if not deployed:
            raise ValueError("请先构建并启动本部署的后端。")
        deployed_meta = deployed.get("assistant", {})
        local_nodes = (meta.get("node"), deployed_meta.get("node"))
        if any(node.casefold() == local.casefold() for local in local_nodes if local):
            raise ValueError("请使用独立节点 ID。当前部署的本机前端密钥可在向导查看。")
        if backend_target(deployed) != backend_target(backend):
            raise ValueError("请先启动已构建版本，应用后端数据库地址或发布 URL 的修改。")
        runner = export_runner(store)

        pairing = meta.get("external_pairings", {}).get(node)
        new_pairing = pairing is None and bool(receiver)
        if new_pairing:
            if node in (backend["application"].get("frontends") or {}):
                raise ValueError("此节点已在后端声明，请留空接收器地址后重新导出。")
            suffix = hashlib.sha256(node.encode()).hexdigest()[:16].upper()
            pairing = {
                "node": node,
                "receiver": receiver,
                "allow_private_http": allow_private_http,
                "inbound_env": f"NEXTJS_TO_FASTAPI_NODE_{suffix}_TOKEN",
                "outbound_env": f"FASTAPI_TO_NEXTJS_NODE_{suffix}_TOKEN",
            }
        args = ["deploy/node_bundle.py", "--preserve-binding"]
        if pairing is None:
            args.extend(("--node", node))
        else:
            if receiver and (receiver, allow_private_http) != (
                pairing["receiver"],
                pairing["allow_private_http"],
            ):
                raise ValueError(
                    "此节点已有配对，请留空接收器地址重新导出；地址可在后端节点控制台修改。"
                )
            names = [pairing[key] for key in ("inbound_env", "outbound_env")]
            variables = backend["environment"]
            if new_pairing:
                if any(name in variables for name in names):
                    raise ValueError("配对令牌引用已被占用，请选择其他节点 ID。")
                variables.update((name, secrets.token_urlsafe(32)) for name in names)
            if not all(token(variables.get(name, "")) for name in names):
                raise ValueError("此节点的后端配对令牌缺失或无效。")
            deployed["assistant"] = pairing
            deployed["environment"].update((name, variables[name]) for name in names)

        with export_config(deployed_path, deployed, temporary=pairing is not None) as config_path:
            command, options = runner(config_path, args)
            # Save before touching the registry so interrupted exports can be retried
            # with the same credentials even if the database commit already happened.
            if new_pairing:
                meta.setdefault("external_pairings", {})[node] = pairing
                store.save()
            try:
                result = subprocess.run(
                    command, **options, capture_output=True, text=True, timeout=45
                )
            except subprocess.TimeoutExpired:
                raise ValueError("等待后端导出配对超时，请使用同一节点 ID 重试。") from None
        if result.returncode:
            conflict = "EVERUNFOLD_NODE_BINDING_CONFLICT" in result.stderr.splitlines()
            upgrade = "unrecognized arguments: --preserve-binding" in result.stderr
            if new_pairing and (conflict or upgrade):
                del meta["external_pairings"][node]
                for name in names:
                    del variables[name]
                store.save()
            if conflict:
                raise ValueError("此节点 ID 已被其他连接使用，请选择独立的节点 ID。")
            if upgrade:
                raise ValueError("请更新后端源码并构建启动一次，再导出配对。")
            raise ValueError("后端未能导出配对，请检查节点 ID、后端版本和数据库迁移。")
        matches = [
            line[len(MARKER) :] for line in result.stdout.splitlines() if line.startswith(MARKER)
        ]
        if len(matches) != 1:
            raise ValueError("后端未返回有效的前端配对凭据。")
        raw = decode_bundle(matches[0], node)
        local_credentials = {
            document["environment"].get(metadata.get(key))
            for document, metadata in ((deployed, deployed_meta), (backend, meta))
            for key in ("inbound_env", "outbound_env")
        }
        if any(
            credential["value"] in local_credentials
            for credential in json.loads(raw)["tokens"].values()
        ):
            raise ValueError("此节点与本机前端共用密钥，请为外部前端设置独立配对。")
        return matches[0]


def write_bundle(path, encoded, node):
    raw = decode_bundle(encoded, node)
    path = Path(path).expanduser()
    descriptor, name = tempfile.mkstemp(prefix=".node-bundle-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw + b"\n")
        os.link(temporary, path)
    except FileExistsError:
        raise ValueError("导出文件已存在，请另选路径。") from None
    finally:
        temporary.unlink(missing_ok=True)
