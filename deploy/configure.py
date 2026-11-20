"""Combined frontend/backend form using the existing Everunfold terminal UI."""

import asyncio
import copy
import hashlib
import ipaddress
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from config_mirrors import add_mirror_fields
from config_model import Configuration, Field
from config_validation import (
    db_url,
    identifier,
    peer_list,
    positive,
    public_url,
    redis_url,
    token,
    valid_port,
)
from frontend_monitoring import (
    add_direct_metrics_fields,
    direct_metrics_environment,
    direct_metrics_errors,
)
from monitoring_access import (
    LOCAL,
    METRICS,
    NODE_PEERS,
    add_peer_field,
    networks,
)
from monitoring_setup import (
    add_monitoring_fields,
    monitor_environment,
    monitor_errors,
)
from network_inputs import edit_fields, migrate_unified
from storage import read
from worker_capacity import add_capacity_fields, capacity_environment, capacity_errors


def generated():
    return secrets.token_urlsafe(32)


def imported(store, role, source):
    candidate = read(Path(source) / "config.yaml")
    if candidate:
        if candidate.get("component") != role or candidate.get("version") != 1:
            raise ValueError(f"{role} 源码目录的 config.yaml 格式无效。")
        store.doc[role] = candidate
        store.doc["deployment"]["imported_" + role + "_source"] = str(Path(source).resolve())
    return bool(candidate)


def defaults(store, mode=None):
    d = store.doc["deployment"]
    for key, value in {
        "mode": "docker",
        "auto_pull": "1",
        "container": "everunfold",
        "network": "everunfold-network",
        "extra_networks": "",
        "host_gateway": "1",
        "tls_proxy_network": "",
        "backend_port": "3434",
        "frontend_port": "3000",
        "build_workers": "2",
        "build_memory": "3072",
        "uid": str(os.getuid()),
        "gid": str(os.getgid()),
    }.items():
        d.setdefault(key, value)
    if mode:
        d["mode"] = mode
    for role in ("backend", "frontend"):
        d.setdefault(role + "_bind", d.get("bind", "127.0.0.1"))
        d.setdefault(
            role + "_repo",
            f"https://github.com/wojiaoyishang/Everunfold-{role.title()}.git",
        )
        d.setdefault(role + "_source", str(store.root.parent / ("Everunfold-" + role.title())))
        d.setdefault(role + "_data", str(store.root / "data" / role))
        if not store.original and not store.doc[role]["application"]:
            imported(store, role, d[role + "_source"])


def configure(store, mode=None):
    defaults(store, mode)
    original = copy.deepcopy(store.doc)
    doc = store.doc
    d = doc["deployment"]
    b, f = doc["backend"], doc["frontend"]
    ba, be, fa, fe = (
        b["application"],
        b["environment"],
        f["application"],
        f["environment"],
    )
    fields = []
    by_key = {}
    setters = []
    native = d["mode"] == "native"
    if not native:
        migrate_unified(d)
    if be.get("BACKEND_BASE_URL"):
        ba["BACKEND_BASE_URL"] = be["BACKEND_BASE_URL"]
    if fe.get("PUBLIC_SITE_URL"):
        fa["PUBLIC_SITE_URL"] = fe["PUBLIC_SITE_URL"]

    def add(key, label, value="", **options):
        item = Field(key, label, str(value), **options)
        fields.append(item)
        by_key[key] = item
        return item

    def value(key):
        return by_key[key].value

    def bind(target, name, key, label, default="", **options):
        item = add(key, label, target.get(name, default), **options)
        setters.append(lambda: target.update({name: value(key)}))
        return item

    def dep(key, label, **options):
        return bind(d, key, key, label, group="部署", **options)

    dep(
        "mode",
        "部署方式（docker / native）",
        valid=lambda s: s in {"docker", "native"},
        hint="docker 为单容器；native 直接部署为用户级 systemd 服务。",
    )
    dep(
        "auto_pull",
        "构建前自动拉取 Git（1/0）",
        valid=lambda s: s in {"1", "0"},
        hint="空目录 clone；已有仓库 pull --ff-only。任何错误停止，不覆盖本地改动。重启不拉取。",
    )
    for role, title in (("backend", "后端"), ("frontend", "前端")):
        dep(
            role + "_repo",
            title + "远程仓库",
            valid=lambda s: bool(s) and not s.startswith("-"),
        )
        for suffix, label in (("source", "源码保存位置"), ("data", "持久数据位置")):
            dep(
                role + "_" + suffix,
                title + label,
                valid=lambda s: s.startswith("/") and not any(c in s for c in ",\n\r"),
                resolve=lambda s: str(Path(s).expanduser().resolve()) if s else "",
                hint="填写宿主机绝对路径；更换数据路径不会自动搬迁旧数据。",
            )

    def docker_active():
        return value("mode") == "docker"

    mirror_mapping = add_mirror_fields(add, value, d, "deploy", docker_active)
    setters.append(lambda: d.update(mirror_mapping()))

    for key, label in (("container", "容器名称"), ("network", "主网络")):
        dep(
            key,
            label,
            valid=lambda s: bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", s)),
            active=docker_active,
        )
    dep(
        "extra_networks",
        "额外网络（逗号分隔）",
        valid=lambda s: all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", n.strip())
            for n in s.split(",")
            if n.strip()
        ),
        active=docker_active,
        hint="例如 1panel-network；须已存在，启动前逐个连接。",
    )
    dep(
        "host_gateway",
        "添加 host.docker.internal（1/0）",
        valid=lambda s: s in {"0", "1"},
        active=docker_active,
    )
    for role, title in (("backend", "后端"), ("frontend", "前端")):
        item = add(
            role + "_bind",
            title + "宿主映射绑定 IP",
            d[role + "_bind"],
            valid=lambda s: not s or valid_ip(s),
            active=docker_active,
            empty="不映射",
            group="部署",
            hint="填写宿主 IP 或网桥名，留空关闭映射。",
        )
        setters.append(lambda role=role, item=item: d.update({item.key: literal(role + "_bind")}))
        dep(
            role + "_port",
            title + "宿主映射端口",
            valid=valid_port,
            active=lambda role=role: docker_active() and bool(value(role + "_bind")),
        )
    for key, label in (("uid", "容器运行 UID"), ("gid", "容器运行 GID")):
        dep(
            key,
            label,
            valid=lambda s: s.isdecimal(),
            active=docker_active,
            hint="默认当前用户。挂载目录须允许此 UID/GID 读写。",
        )
    for key, label in (
        ("build_workers", "前端编译 worker 数"),
        ("build_memory", "Node 编译内存 MiB"),
    ):
        dep(key, label, valid=positive)
    bind(
        ba,
        "BACKEND_BASE_URL",
        "api",
        "后端发布 URL",
        "http://127.0.0.1:8001" if native else "http://127.0.0.1:3434",
        valid=public_url,
        group="后端",
        hint="用于公开资源链接；独立于监听 IP 和端口。",
    )
    bind(
        fa,
        "PUBLIC_SITE_URL",
        "site",
        "前端发布 URL",
        "http://127.0.0.1:3000",
        valid=public_url,
        group="前端",
        hint="仅前端配置；后端通过已认证的节点同步获知此地址。",
    )
    bind(
        fe,
        "FRONTEND_WORKERS",
        "frontend_workers",
        "前端 Node.js 运行 worker 数",
        "1",
        valid=lambda s: bool(re.fullmatch(r"[1-9]|[12][0-9]|3[0-2]", s)),
        group="前端",
        hint="默认 1，范围 1–32。共用一个端口，增加会占用更多内存；与编译 worker 数分开。",
    )
    bind(
        ba.setdefault("project", {}),
        "key",
        "project",
        "项目标识",
        "everunfold",
        valid=identifier,
        group="后端",
    )
    bind(
        be,
        "DATABASE_URL",
        "database",
        "PostgreSQL URL",
        ba.get("database", {}).get("url", ""),
        valid=db_url,
        group="数据库",
        hint="点击分项填写。URL 回显；密码请避免在共享终端展示。",
    )
    bind(
        be,
        "REDIS_URL",
        "redis",
        "运行 Redis URL",
        ba.get("redis", {}).get("url", "redis://127.0.0.1:6379/0"),
        valid=redis_url,
        group="数据库",
    )
    bind(
        be,
        "CELERY_BROKER_URL",
        "broker",
        "Celery Broker URL",
        ba.get("celery", {}).get("broker_url", ""),
        valid=lambda s: not s or redis_url(s),
        group="数据库",
        empty="沿用 Redis 的 DB 1",
    )
    bind(
        be,
        "CELERY_RESULT_BACKEND_URL",
        "result",
        "Celery 结果 Redis URL",
        ba.get("celery", {}).get("result_backend_url", ""),
        valid=lambda s: not s or redis_url(s),
        group="数据库",
        empty="沿用 Redis 的 DB 2",
    )
    bind(
        be,
        "EVERUNFOLD_AUTO_INIT_EMPTY_DB",
        "auto_init_empty_db",
        "空库自动初始化（1/0）",
        "1",
        valid=lambda s: s in {"0", "1"},
        group="数据库",
    )
    bind(
        ba.setdefault("redis", {}),
        "namespace_scope",
        "namespace",
        "Redis 命名空间",
        "production",
        valid=identifier,
        group="数据库",
    )
    auth = ba.setdefault("auth", {})
    for name, default, label in (
        (
            "application_master_key_env",
            "EVERUNFOLD_API_APPLICATION_MASTER_KEY",
            "应用主密钥",
        ),
        ("totp_master_key_env", "EVERUNFOLD_TOTP_MASTER_KEY", "TOTP 主密钥"),
    ):
        reference = auth.setdefault(name, default)
        # Existing imports with missing encryption keys must be repaired, never regenerated.
        prior = bool(original["backend"]["application"])
        initial = "" if prior else generated()
        bind(
            be,
            reference,
            reference,
            label,
            initial,
            valid=lambda s: bool(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", s)),
            secret=True,
            group="密钥",
            generate=None if prior else generated,
            resolve=(lambda s: s) if prior else (lambda s: s or generated()),
            hint="已有站点必须保留原值；新站点留空自动生成。",
        )
    bind(
        be,
        "EVERUNFOLD_ADMIN_PASSWORD",
        "admin",
        "初始管理员密码",
        generated(),
        secret=True,
        valid=lambda s: 12 <= len(s) <= 1024,
        generate=generated,
        resolve=lambda s: s or generated(),
        group="密钥",
        hint="只用于初始化新库，不重置已有账号。",
    )
    meta = b.setdefault("assistant", {})
    bind(
        meta,
        "node",
        "node",
        "前端节点 ID",
        fa.get("FRONTEND_NODE_ID", "web-main"),
        valid=identifier,
        group="自动配对",
    )
    for key, name, default, label in (
        (
            "inbound",
            "inbound_env",
            "NEXTJS_TO_FASTAPI_WEB_MAIN_TOKEN",
            "前端 → 后端令牌",
        ),
        (
            "outbound",
            "outbound_env",
            "FASTAPI_TO_NEXTJS_WEB_MAIN_TOKEN",
            "后端 → 前端令牌",
        ),
    ):
        reference = meta.setdefault(name, default)
        bind(
            be,
            reference,
            key,
            label,
            generated(),
            valid=token,
            secret=True,
            generate=generated,
            resolve=lambda s: s or generated(),
            group="自动配对",
            hint="统一写入本机两端。给其他前端配对时，请使用独立节点导出。已有部署沿用原值。",
        )
    internal_auth = ba.setdefault("internal_auth", {})
    add(
        "backend_peers",
        "外部前端调用后端的来源 IP/CIDR",
        ", ".join(internal_auth.get("allowed_peer_ips") or ["127.0.0.1", "::1"]),
        valid=peer_list,
        group="自动配对",
        hint="填写后端实际看到的直连地址，逗号分隔。统一容器内的回环地址会自动保留。",
    )
    setters.append(
        lambda: internal_auth.update(
            allowed_peer_ips=[
                ip.strip() for ip in literal("backend_peers").split(",") if ip.strip()
            ]
        )
    )
    front_backend = fa.setdefault("backend", {})
    profiles = f.get("connection_profiles")
    if profiles:
        current = profiles["profiles"].get(profiles["active"], {})
        front_backend = current.setdefault("backend", front_backend)
    add(
        "frontend_peers",
        "后端调用前端接收器的来源 IP/CIDR",
        ", ".join(front_backend.get("allowed_peer_ips") or ["127.0.0.1", "::1"]),
        valid=peer_list,
        group="自动配对",
        hint="填写前端实际看到的直连地址。统一容器内的回环地址会自动保留。",
    )
    setters.append(
        lambda: front_backend.update(
            allowed_peer_ips=[
                ip.strip() for ip in literal("frontend_peers").split(",") if ip.strip()
            ]
        )
    )
    for role, config, env, title in (
        ("backend", ba, be, "后端"),
        ("frontend", fa, fe, "前端"),
    ):
        prefix = "API" if role == "backend" else "FRONTEND"
        bind(
            env,
            prefix + "_HOST",
            role + "_host",
            title + "绑定 IP",
            "127.0.0.1" if native else "0.0.0.0",
            valid=lambda s: s in {"127.0.0.1", "0.0.0.0"},
            group="进程",
            active=lambda: not docker_active(),
            hint="原生统一部署通过 127.0.0.1 互通。",
        )
        setters.append(
            lambda env=env, prefix=prefix: (
                env.update({prefix + "_HOST": "0.0.0.0"}) if docker_active() else None
            )
        )
        bind(
            env,
            prefix + "_PORT",
            role + "_listen",
            title + "监听端口",
            "8001" if role == "backend" else "3000",
            valid=valid_port,
            group="进程",
        )
        monitor = config.setdefault("nodes_monitor", {})
        flag = add(
            role + "_monitor",
            title + "节点控制台（1/0）",
            "1" if monitor.get("enabled", True) else "0",
            valid=lambda s: s in {"0", "1"},
            group="节点控制台",
        )
        setters.append(lambda flag=flag, monitor=monitor: monitor.update(enabled=flag.value == "1"))
        password_name = (
            "EVERUNFOLD_SETUP_" + ("FRONTEND_" if role == "frontend" else "") + "NODES_PASSWORD"
        )
        old_password = env.get(password_name, "")
        old_hash = monitor.get("password_hash", "")
        item = bind(
            env,
            password_name,
            role + "_password",
            title + "控制台密码",
            "" if old_hash else generated(),
            secret=True,
            valid=lambda s, old_hash=old_hash: 12 <= len(s) <= 1024 or (not s and bool(old_hash)),
            generate=generated,
            resolve=lambda s, old_hash=old_hash: s or ("" if old_hash else generated()),
            group="节点控制台",
        )

        password_state = {"value": old_password, "hash": old_hash}

        def password_hash(
            role=role,
            monitor=monitor,
            item=item,
            password_state=password_state,
        ):
            password = item.value
            if password and (not password_state["hash"] or password != password_state["value"]):
                if role == "backend":
                    monitor["password_hash"] = PasswordHasher().hash(password)
                else:
                    salt = secrets.token_bytes(16)
                    digest = hashlib.scrypt(
                        password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32
                    )
                    monitor["password_hash"] = f"scrypt${salt.hex()}${digest.hex()}"
                password_state.update(value=password, hash=monitor["password_hash"])
            monitor.pop("origin", None)

        setters.append(password_hash)
        secret_name = monitor.setdefault(
            "session_secret_env",
            "EVERUNFOLD_" + ("FRONTEND_" if role == "frontend" else "") + "NODES_SESSION_SECRET",
        )
        bind(
            env,
            secret_name,
            role + "_session",
            title + "控制台会话密钥",
            generated(),
            secret=True,
            valid=lambda s: bool(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", s)),
            generate=generated,
            resolve=lambda s: s or generated(),
            group="节点控制台",
        )
        proxies = add(
            role + "_proxies",
            title + "反代来源 IP",
            ",".join(monitor.get("trusted_tls_proxy_ips", [])),
            valid=lambda s: all(valid_ip(v.strip()) for v in s.split(",") if v.strip()),
            group="节点控制台",
            hint="逗号分隔；Docker 宿主反代也可按网络名自动解析网关。",
        )
        setters.append(
            lambda proxies=proxies, monitor=monitor: monitor.update(
                trusted_tls_proxy_ips=[
                    v.strip() for v in literal(proxies.key).split(",") if v.strip()
                ]
            )
        )
    frontend_console = fa["nodes_monitor"]
    add(
        "console_peers",
        "允许访问前端节点控制台的来源 IP/CIDR",
        ", ".join(frontend_console.get("allowed_peer_ips", [])),
        valid=peer_list,
        group="节点控制台",
        empty="沿用现有访问规则",
        hint="按前端实际看到的直连来源校验。",
    )

    def save_frontend_console_peers():
        peers = [ip.strip() for ip in literal("console_peers").split(",") if ip.strip()]
        if peers:
            frontend_console["allowed_peer_ips"] = peers
        else:
            frontend_console.pop("allowed_peer_ips", None)

    setters.append(save_frontend_console_peers)
    bind(be, "API_WORKERS", "workers", "API worker 数", "1", valid=positive, group="进程")
    add_capacity_fields(add, value, be)
    add_monitoring_fields(add, value, be, combined=True)
    add_direct_metrics_fields(add, value, fe)
    add_peer_field(
        add,
        "frontend_metrics_peers",
        "前端内部指标来源 IP/CIDR",
        fe.get(NODE_PEERS, LOCAL),
        optional=True,
        active=lambda: value("metrics_enabled") == "1",
        hint="统一容器内的后端采集保留回环来源。仅在需要时填写额外采集网段。",
    )
    add(
        "log_format",
        "结构化日志",
        be.get("EVERUNFOLD_LOG_FORMAT", fe.get("EVERUNFOLD_LOG_FORMAT", "json")),
        choices=(("json", "开启（JSON）"), ("text", "关闭（纯文本）")),
        group="日志",
        hint="默认每行输出一个 JSON 对象，便于外部采集。重新启动后生效。",
    )
    for name, label, default in (
        ("API_LOG_LEVEL", "API 日志等级", "INFO"),
        ("WORKER_LOG_LEVEL", "Worker 日志等级", "WARNING"),
        ("SCHEDULER_LOG_LEVEL", "Scheduler 日志等级", "INFO"),
    ):
        bind(
            be,
            name,
            name,
            label,
            default,
            valid=lambda s: s in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
            resolve=str.upper,
            group="日志",
        )

    def errors():
        issues = []
        paths = [
            Path(value(r + "_" + k)).resolve()
            for r in ("backend", "frontend")
            for k in ("source", "data")
        ]
        for index, path in enumerate(paths):
            if (
                path == Path("/")
                or path == store.root
                or path.is_relative_to(store.var)
                or store.var.is_relative_to(path)
            ):
                issues.append("源码和数据目录不可使用根目录、部署仓库或 var 目录。")
            for other in paths[:index]:
                if path.is_relative_to(other) or other.is_relative_to(path):
                    issues.append("前后端源码和数据目录须相互独立，不能嵌套。")
        if value("backend_listen") == value("frontend_listen"):
            issues.append("前后端是两个进程，监听端口不能相同。")
        if docker_active():
            hosts = [value(role + "_bind") for role in ("backend", "frontend")]
            ports = [value(role + "_port") for role in ("backend", "frontend")]
            if (
                all(host and valid_ip(host) for host in hosts)
                and all(valid_port(port) for port in ports)
                and int(ports[0]) == int(ports[1])
            ):
                a, b = map(ipaddress.ip_address, hosts)
                if a == b or (a.version == b.version and (a.is_unspecified or b.is_unspecified)):
                    issues.append("两端宿主映射的绑定 IP 和端口冲突。")
            if value("network") in {"host", "none"}:
                issues.append("请选择网桥网络，不支持 host / none。")
        if value("inbound") == value("outbound"):
            issues.append("双向令牌不能相同。")
        if any(
            value("node").casefold() == node.casefold()
            for node in meta.get("external_pairings", {})
        ):
            issues.append("本机前端节点 ID 已用于外部配对，请选择不同的 ID。")
        return (
            issues
            + capacity_errors(value)
            + monitor_errors(value)
            + direct_metrics_errors(value, value("metrics_token"), value("metrics_node_token"))
        )

    address_types = {
        "backend_peers": "source",
        "console_peers": "source",
        "frontend_peers": "source",
        "backend_proxies": "gateway",
        "frontend_proxies": "gateway",
        "backend_bind": "single",
        "frontend_bind": "single",
        "metrics_peers": "source",
        "frontend_metrics_peers": "source",
        "frontend_direct_peers": "source",
    }
    literal, address_mapping = edit_fields(by_key, d, address_types, docker_active)

    def apply():
        for setter in setters:
            setter()
        be.update(capacity_environment(value))
        d.update(address_mapping())
        be.update(monitor_environment(literal, frontend="http://127.0.0.1:" + fe["FRONTEND_PORT"]))
        fe[METRICS + "NODE_TOKEN"] = be[METRICS + "NODE_TOKEN"]
        fe[NODE_PEERS] = ",".join(networks(f"{LOCAL},{literal('frontend_metrics_peers')}"))
        fe.update(direct_metrics_environment(literal))
        be["EVERUNFOLD_LOG_FORMAT"] = fe["EVERUNFOLD_LOG_FORMAT"] = value("log_format")
        for name, index in (("CELERY_BROKER_URL", 1), ("CELERY_RESULT_BACKEND_URL", 2)):
            if not be[name]:
                be[name] = urlsplit(be["REDIS_URL"])._replace(path=f"/{index}").geturl()
        normalize(store)

    def save():
        if not store.original:
            for role in ("backend", "frontend"):
                source = Path(value(role + "_source"))
                if (source / "config.yaml").is_file() and d.get(
                    "imported_" + role + "_source"
                ) != str(source.resolve()):
                    raise ValueError(
                        "所选源码目录已有配置，请先点“导入原配置”以沿用原密钥，然后核对并保存。"
                    )
        apply()
        store.save()

    def import_existing():
        found = []
        for role in ("backend", "frontend"):
            source = value(role + "_source")
            candidate = read(Path(source) / "config.yaml")
            if candidate:
                if candidate.get("component") != role or candidate.get("version") != 1:
                    raise ValueError(f"{role} 原配置格式无效。")
                found.append((role, source, candidate))
        if not found:
            raise ValueError("两个源码目录均未找到 config.yaml。请先修改源码保存位置。")
        for key in d:
            if key in by_key:
                d[key] = value(key)
        for role, source, candidate in found:
            store.doc[role] = candidate
            d["imported_" + role + "_source"] = str(Path(source).resolve())
        store.save()

    async def probe():
        from operations import probe_document

        apply()
        return await probe_document(store)

    async def export(node, receiver, allow_private_http):
        from node_pairing import export_node

        return await asyncio.to_thread(export_node, store.path, node, receiver, allow_private_http)

    return Configuration(
        "Everunfold · 前后端统一部署",
        fields,
        save=save,
        check=errors,
        probe=probe,
        export_node=export,
        native=native,
        backend=True,
        location=str(store.path),
        notice="同一容器或同一主机内自动配对。数据库与 Redis 由外部提供。",
        import_existing=import_existing,
    )


def valid_ip(value):
    try:
        ipaddress.ip_address(value)
        return "%" not in value
    except ValueError:
        return False


def normalize(store):
    """Managed local pairing and data paths; preserve other connections and settings."""
    doc, d = store.doc, store.doc["deployment"]
    b, f = doc["backend"], doc["frontend"]
    ba, be, fa, fe = (
        b["application"],
        b["environment"],
        f["application"],
        f["environment"],
    )
    meta = b["assistant"]
    receiver = "http://127.0.0.1:" + fe["FRONTEND_PORT"]
    meta.update(receiver=receiver, allow_private_http=False)
    peers = ba.setdefault("internal_auth", {}).setdefault("allowed_peer_ips", [])
    ba["internal_auth"]["allowed_peer_ips"] = list(dict.fromkeys([*peers, "127.0.0.1", "::1"]))
    ba.setdefault("cors", {})["use_frontend_registry"] = True
    for role, document in (("backend", b), ("frontend", f)):
        data = "/data/" + role if d["mode"] == "docker" else d[role + "_data"]
        app, env = document["application"], document["environment"]
        env["EVERUNFOLD_DATA_DIR"] = data
        env.pop("CONFIG_FILE", None)
        for name in ("EVERUNFOLD_LOKI_QUERY_ALLOWED_PEERS", "EVERUNFOLD_LOKI_PUSH_ALLOWED_PEERS"):
            env.pop(name, None)
        if role == "backend":
            app.setdefault("media", {})["uploads_path"] = data + "/uploads"
            env.pop("BACKEND_BASE_URL", None)
        else:
            app.setdefault("settings", {})["directory"] = data + "/settings"
            app.setdefault("cache", {})["directory"] = data + "/public-snapshots"
            env.pop("PUBLIC_SITE_URL", None)
            env["PLUGINS_DIRECTORY"] = data + "/plugins"
    local = {
        "BACKEND_BASE_URL": ba["BACKEND_BASE_URL"],
        "FRONTEND_NODE_ID": meta["node"],
        "backend": {
            "base_url": "http://127.0.0.1:" + be["API_PORT"],
            "allowed_peer_ips": list(
                dict.fromkeys(
                    [
                        *(
                            f.get("connection_profiles", {})
                            .get("profiles", {})
                            .get(f.get("connection_profiles", {}).get("active"), {})
                            .get("backend", fa.get("backend", {}))
                            .get("allowed_peer_ips", [])
                        ),
                        "127.0.0.1",
                        "::1",
                    ]
                )
            ),
            "nextjs_to_fastapi_token_env": meta["inbound_env"],
            "fastapi_to_nextjs_token_env": meta["outbound_env"],
        },
    }
    profiles = f.get("connection_profiles")
    if profiles:
        active = profiles["profiles"][profiles["active"]]
        # Existing browser-selected remote profiles stay selected; local profile is updated.
        matches = [
            p for p in profiles["profiles"].values() if p.get("FRONTEND_NODE_ID") == meta["node"]
        ]
        if not matches:
            raise ValueError("前端配置含连接列表，但找不到待配对节点；请先在节点页选用正确节点。")
        for target in matches:
            target.update(copy.deepcopy(local))
        if active not in matches:
            raise ValueError("当前前端选中了其他节点，请先在节点页切回此部署节点后再部署。")
        fa.pop("backend_node_file", None)
    else:
        fa.update(local)
        fa.pop("backend_node_file", None)
    for ref in (meta["inbound_env"], meta["outbound_env"]):
        fe[ref] = be[ref]
    for name in ("FRONTEND_NODE_ID", "BACKEND_BASE_URL"):
        fe.pop(name, None)
