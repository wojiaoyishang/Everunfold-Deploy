"""Keep Docker network selections in deployment metadata and resolve them at startup."""

import ipaddress
import re

from monitoring_access import resolve_gateways, resolve_sources

PREFIX = "EVERUNFOLD_NETWORK_INPUT_"
MIGRATED = PREFIX + "VERSION"
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
METRIC_FIELDS = {
    "metrics_peers": "EVERUNFOLD_METRICS_ALLOWED_PEERS",
    "frontend_metrics_peers": "EVERUNFOLD_METRICS_NODE_ALLOWED_PEERS",
    "frontend_direct_peers": "EVERUNFOLD_FRONTEND_METRICS_ALLOWED_PEERS",
}
ADDRESS_FIELDS = {
    "peers": "source",
    "tls_peers": "gateway",
    "host": "single",
    "publish_host": "single",
    **dict.fromkeys(METRIC_FIELDS, "source"),
}


def entries(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def network_name(value):
    return value not in {"host", "none", "default"} and bool(NAME.fullmatch(value))


def split_input(value, kind, docker):
    literals, names = [], []
    parts = entries(value)
    if kind == "single" and len(parts) != 1:
        raise ValueError("请填写一个 IP 地址或 Docker 网络名称。")
    for part in parts:
        try:
            if "%" in part:
                raise ValueError
            if kind == "source" and "/" in part:
                ipaddress.ip_network(part, strict=False)
            else:
                ipaddress.ip_address(part)
        except ValueError:
            if not docker or not network_name(part):
                raise ValueError("请填写有效的 IP 地址、网段或 Docker 网络名称。") from None
            names.append(part)
        else:
            literals.append(part)
    return ",".join(literals), ",".join(dict.fromkeys(names))


def edit_fields(by_key, saved, fields, docker):
    for key, kind in fields.items():
        field = by_key[key]
        literal_valid = field.valid
        literal_resolve = field.resolve
        fallback = field.value
        names = saved.get(PREFIX + key.upper(), "")
        if docker() and names:
            field.value = names if kind == "single" else ",".join(filter(None, (fallback, names)))

        def valid(value, kind=kind, literal_valid=literal_valid):
            if kind == "single" and not value.strip():
                return literal_valid(value)
            try:
                literals, names = split_input(value, kind, docker())
            except ValueError:
                return False
            if not names:
                return literal_valid(literals)
            return not literals or literal_valid(literals)

        def resolve(text, kind=kind, literal_resolve=literal_resolve):
            if kind == "single" and not text.strip():
                return literal_resolve(text)
            literals, names = split_input(text, kind, docker())
            return ",".join(entries(text)) if names else literal_resolve(literals)

        field.valid = valid
        field.resolve = resolve
        field.hint += (
            " Docker 模式可填网络名，启动时解析为"
            + {"source": "子网。", "gateway": "网桥网关。", "single": "对应 IP。"}[kind]
        )

    def literal(key):
        value = by_key[key].value
        if key not in fields or not docker() or not value.strip():
            return value
        return split_input(value, fields[key], True)[0]

    def mapping():
        if not docker():
            return {}
        return {
            PREFIX + key.upper(): split_input(by_key[key].value, kind, True)[1]
            if by_key[key].value.strip()
            else ""
            for key, kind in fields.items()
        }

    return literal, mapping


def inspectors(snapshot):
    def get(name):
        if name not in snapshot:
            raise ValueError(f"请将 Docker 网络 {name} 加入主网络或额外网络。")
        return snapshot[name]

    def subnets(name):
        values = get(name)["IPAM"]["Config"] or []
        result = ",".join(item["Subnet"] for item in values if item.get("Subnet"))
        if not result:
            raise ValueError(f"Docker 网络 {name} 没有有效子网。")
        return result

    def gateways(name):
        data = get(name)
        values = data["IPAM"]["Config"] or []
        return ",".join(
            item["Gateway"] for item in values if data["Driver"] == "bridge" and item.get("Gateway")
        )

    return subnets, gateways


def migrate_unified(settings):
    name = settings.pop("tls_proxy_network", "")
    if name:
        for role in ("BACKEND", "FRONTEND"):
            key = PREFIX + role + "_PROXIES"
            settings[key] = ",".join(dict.fromkeys([*entries(settings.get(key, "")), name]))


def unified_addresses(document, snapshot):
    subnets, gateways = inspectors(snapshot)
    settings = document["deployment"]
    roles = ("backend", "frontend")
    overrides = {role: {} for role in roles}
    binds = {}
    for role in roles:
        prefix = PREFIX + role.upper()
        selected = settings.get(prefix + "_BIND", "")
        binds[role] = (
            resolve_gateways(selected, gateways, single=True)
            if selected
            else settings.get(role + "_bind", settings.get("bind", "127.0.0.1"))
        )
        peer_names = settings.get(prefix + "_PEERS", "")
        if peer_names:
            overrides[role]["EVERUNFOLD_INTERNAL_PEER_CIDRS"] = ",".join(
                resolve_sources(peer_names, subnets)
            )
        proxy_names = settings.get(prefix + "_PROXIES", "")
        if proxy_names:
            overrides[role]["EVERUNFOLD_NODES_TLS_PROXY_IPS"] = resolve_gateways(
                proxy_names, gateways
            )
    for key, variable in METRIC_FIELDS.items():
        source_names = settings.get(PREFIX + key.upper(), "")
        if source_names:
            role = "backend" if key == "metrics_peers" else "frontend"
            current = document[role]["environment"].get(variable, "")
            overrides[role][variable] = ",".join(
                resolve_sources(",".join(filter(None, (current, source_names))), subnets)
            )
    console_names = settings.get(PREFIX + "CONSOLE_PEERS", "")
    if console_names:
        overrides["frontend"]["EVERUNFOLD_FRONTEND_NODES_ALLOWED_PEERS"] = ",".join(
            resolve_sources(console_names, subnets)
        )
    return binds, overrides
