"""Shared monitoring fields and a private Loki proxy include."""

import re
import secrets
from ipaddress import ip_network
from urllib.parse import urlsplit

METRICS = "EVERUNFOLD_METRICS_"
LOKI = "EVERUNFOLD_LOKI_"
LOCAL = "127.0.0.1,::1"


def networks(value, *, optional=False):
    if "%" in value:
        raise ValueError("IP/CIDR 不接受网卡作用域。")
    items = [ip_network(item.strip(), strict=False) for item in value.split(",") if item.strip()]
    if (not items and not optional) or any(item.prefixlen == 0 for item in items):
        raise ValueError("请填写有效的受限 IP/CIDR 列表。")
    return list(dict.fromkeys(str(item) for item in items))


def valid_peers(value, optional=False):
    try:
        networks(value, optional=optional)
        return True
    except ValueError:
        return False


def valid_url(value):
    if not value:
        return True
    try:
        url = urlsplit(value)
        return (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and url.username is None
            and not url.query
            and not url.fragment
            and url.path in {"", "/"}
            and (url.port is None or 1 <= url.port <= 65535)
        )
    except ValueError:
        return False


def add_monitoring_fields(add, value, env, *, combined=False):
    add(
        "metrics_enabled",
        "Prometheus 指标",
        "1" if env.get(METRICS + "TOKEN") else "0",
        choices=(("0", "关闭"), ("1", "启用")),
        group="监控",
    )
    group = {"group": "监控", "active": lambda: value("metrics_enabled") == "1"}
    add(
        "metrics_peers",
        "指标拉取来源 IP/CIDR",
        env.get(METRICS + "ALLOWED_PEERS", LOCAL),
        valid=valid_peers,
        hint="填写后端实际直连来源，多个用逗号分隔。",
        **group,
    )
    for key, label, name in (
        ("metrics_token", "Prometheus 采集令牌", "TOKEN"),
        ("metrics_node_token", "前端内部指标令牌", "NODE_TOKEN"),
    ):
        add(
            key,
            label,
            env.get(METRICS + name) or secrets.token_urlsafe(32),
            secret=True,
            valid=lambda text: bool(re.fullmatch(r"[!-~]{32,256}", text)),
            generate=lambda: secrets.token_urlsafe(32),
            **group,
        )
    if not combined:
        add(
            "metrics_frontend",
            "私有前端根地址",
            env.get(METRICS + "FRONTEND_URL", ""),
            valid=valid_url,
            hint="留空不采集前端。启用时在前端填写同一个内部指标令牌。",
            **group,
        )
    add(
        "metrics_celery",
        "私有 Celery Exporter 根地址",
        env.get(METRICS + "CELERY_URL", ""),
        valid=valid_url,
        hint="可留空。仍采集 Broker 队列深度和 Scheduler 指标。",
        **group,
    )
    for action, label in (("query", "Loki 查询来源 IP/CIDR"), ("push", "Alloy 写入来源 IP/CIDR")):
        add(
            "loki_" + action,
            label,
            env.get(LOKI + action.upper() + "_ALLOWED_PEERS", LOCAL),
            valid=lambda text: valid_peers(text, optional=True),
            group="监控",
            hint="填写 Loki 反代的直连来源。多个用逗号分隔，留空拒绝全部。",
        )


def monitor_errors(value):
    if value("metrics_enabled") == "1" and value("metrics_token") == value("metrics_node_token"):
        return ["Prometheus 采集令牌与前端内部指标令牌必须不同。"]
    return []


def monitor_environment(value, *, frontend=None):
    enabled = value("metrics_enabled") == "1"
    return {
        METRICS + "TOKEN": value("metrics_token") if enabled else "",
        METRICS + "ALLOWED_PEERS": value("metrics_peers"),
        METRICS + "NODE_TOKEN": value("metrics_node_token") if enabled else "",
        METRICS + "FRONTEND_URL": (
            (frontend if frontend is not None else value("metrics_frontend")) if enabled else ""
        ),
        METRICS + "CELERY_URL": value("metrics_celery") if enabled else "",
        LOKI + "QUERY_ALLOWED_PEERS": value("loki_query"),
        LOKI + "PUSH_ALLOWED_PEERS": value("loki_push"),
    }


def loki_proxy_include(env):
    def access(action):
        addresses = networks(env.get(LOKI + action + "_ALLOWED_PEERS", LOCAL), optional=True)
        return (
            "    satisfy all;\n"
            + "".join(f"    allow {address};\n" for address in addresses)
            + "    deny all;\n"
        )

    return (
        "# Include in a dedicated Loki server block. Define upstream everunfold_loki.\n"
        "location = /loki/api/v1/push {\n"
        + access("PUSH")
        + "    limit_except POST { deny all; }\n"
        "    proxy_pass http://everunfold_loki;\n"
        "}\n"
        "location ~ ^/(?:ready|loki/api/v1/(?:query|query_range|labels|series|"
        "label/[^/]+/values|index/(?:stats|volume|volume_range)|status/buildinfo|"
        "detected_fields|detected_labels|patterns))$ {\n"
        + access("QUERY")
        + "    limit_except GET POST { deny all; }\n"
        "    proxy_pass http://everunfold_loki;\n"
        "}\n"
        "location / { return 404; }\n"
    )
