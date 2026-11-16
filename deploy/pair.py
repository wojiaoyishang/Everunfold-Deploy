"""Run with the backend interpreter after initialization, before starting services."""

import asyncio
import os
from pathlib import Path
from uuid import UUID

from storage import read, write


class PairingError(ValueError):
    pass


def selected_connection(doc):
    if "connection_profiles" in doc:
        profiles = doc["connection_profiles"]
        return profiles["profiles"][profiles["active"]]
    return doc["application"]


def validate_connection(doc, node, target, backend_env, epoch):
    selected = selected_connection(doc)
    if selected.get("FRONTEND_NODE_ID") != node:
        raise PairingError("前端当前连接不是此部署节点，请先在节点页切回正确连接。")
    for key, reference in (
        ("nextjs_to_fastapi_token_env", target.nextjs_to_fastapi_token_env),
        ("fastapi_to_nextjs_token_env", target.fastapi_to_nextjs_token_env),
    ):
        name = selected.get("backend", {}).get(key)
        if not name or doc["environment"].get(name) != backend_env.get(reference):
            raise PairingError("两端节点令牌不一致，请在统一向导中核对配对并保存。")
    old_epoch = (
        selected.get("source_epoch")
        if "connection_profiles" in doc
        else selected.get("settings", {}).get("source_epoch")
    )
    if old_epoch and old_epoch != epoch:
        raise PairingError(
            "数据库来源 UUID 与前端已有配置不同；请核对是否连接了错误数据库，勿覆盖原有快照。"
        )


async def pair_locked(frontend_path):
    from app.core.config import load_settings
    from app.core.database import Database
    from app.modules.settings.node_registry import (
        NodeRegistration,
        registry_directory,
        registry_record,
    )
    from app.modules.settings.nodes_schemas import NodeChange
    from app.plugins.nodes_monitor.management import manage_node
    from sqlalchemy import text
    from tools.import_frontend_nodes import run as import_nodes

    settings = load_settings()
    pairing = settings.setup_frontend
    if not pairing:
        raise PairingError("后端缺少 setup 前端配对信息。")
    node, target = pairing
    database = Database(settings.database.url.get_secret_value())
    try:
        async with database.session() as session:
            epoch = str(
                UUID(
                    str(
                        await session.scalar(
                            text("SELECT source_epoch FROM site_settings WHERE id = 1")
                        )
                    )
                )
            )
            record = await registry_record(session)
        doc = read(frontend_path)
        backend_env = {**read(os.environ["CONFIG_FILE"])["environment"], **os.environ}
        validate_connection(doc, node, target, backend_env, epoch)
        if record is None:
            await import_nodes(False)
        async with database.session() as session:
            directory = registry_directory(await registry_record(session))
            previous = directory.entries.get(node)
            # Connection changes use the same audited service/locks as /_nodes.
            prior_binding = directory.bindings.get(node)
            fields = {
                "settings_receiver_url",
                "allow_private_http",
                "nextjs_to_fastapi_token_env",
                "fastapi_to_nextjs_token_env",
            }
            changed = prior_binding is None or prior_binding.model_dump(
                include=fields
            ) != target.model_dump(include=fields)
            if previous and previous.enabled and changed:
                await manage_node(
                    session,
                    settings,
                    NodeChange(
                        action="edit",
                        node_id=node,
                        expected_revision=directory.revision,
                        registration=NodeRegistration(enabled=False),
                    ),
                )
        await import_nodes(True, pairing)
        async with database.session() as session:
            directory = registry_directory(await registry_record(session))
            previous = directory.entries.get(node)
            if previous is None or not previous.enabled:
                await manage_node(
                    session,
                    settings,
                    NodeChange(
                        action="add" if previous is None else "edit",
                        node_id=node,
                        expected_revision=directory.revision,
                        registration=NodeRegistration(enabled=True),
                    ),
                )
        if "connection_profiles" in doc:
            selected_connection(doc)["source_epoch"] = epoch
        else:
            doc["application"].setdefault("settings", {})["source_epoch"] = epoch
        write(frontend_path, doc)
        print(f"节点 {node} 已登记、启用并同步来源 UUID；双向令牌已配对。", flush=True)
    finally:
        await database.close()


async def pair(frontend_path):
    path = Path(frontend_path)
    guard = path.with_name(path.name + ".switch.lock")
    try:
        descriptor = os.open(guard, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise PairingError("前端配置正在保存，请稍后重试。") from None
    try:
        await pair_locked(path)
    finally:
        os.close(descriptor)
        guard.unlink()


if __name__ == "__main__":
    import sys

    try:
        asyncio.run(pair(sys.argv[1]))
    except PairingError as error:
        raise SystemExit(str(error)) from None
    except Exception as error:
        # Exceptions from ORM/URL validation can contain credentials. Runtime checks
        # already provide sanitized dependency errors; don't echo raw SQL parameters.
        raise SystemExit(
            f"自动配对失败（{type(error).__name__}），请检查节点配置与数据库权限。"
        ) from None
