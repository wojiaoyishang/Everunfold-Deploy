"""Shared Prometheus monitoring fields."""

import re
import secrets
from urllib.parse import urlsplit

from monitoring_access import LOCAL, METRICS, add_peer_field


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

    def enabled():
        return value("metrics_enabled") == "1"

    group = {"group": "监控", "active": enabled}
    add_peer_field(
        add,
        "metrics_peers",
        "指标拉取来源 IP/CIDR",
        env.get(METRICS + "ALLOWED_PEERS", LOCAL),
        hint="填写后端实际直连来源，多个用逗号分隔。仍须采集令牌。",
        active=enabled,
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
    }
