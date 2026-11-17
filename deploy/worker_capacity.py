"""Celery capacity choices and the matching launch arguments."""

import re
from collections.abc import Callable, Mapping

POOLS = (("solo", "单进程（solo）"), ("prefork", "多进程（prefork）"))
STRATEGIES = (("fixed", "固定并发"), ("auto", "按任务量自动伸缩"))
AUTOSCALE_MAX = 128
PROCESS_KEYS = ("WORKER_POOL", "WORKER_CONCURRENCY", "WORKER_AUTOSCALE")


def positive(value: str) -> bool:
    return bool(re.fullmatch(r"[1-9][0-9]*", value))


def valid_bound(value: str) -> bool:
    return len(value) <= 3 and positive(value) and int(value) <= AUTOSCALE_MAX


def capacity_arguments(environment: Mapping[str, str]) -> list[str]:
    pool = environment.get("WORKER_POOL", "solo")
    if pool not in {"solo", "prefork"}:
        raise ValueError("Celery 执行池须选 solo 或 prefork。")
    autoscale = environment.get("WORKER_AUTOSCALE", "")
    if autoscale:
        if pool != "prefork":
            raise ValueError("Celery 自动伸缩要求使用 prefork 执行池。")
        maximum, _, minimum = autoscale.partition(",")
        if not (valid_bound(maximum) and valid_bound(minimum) and int(minimum) <= int(maximum)):
            raise ValueError("WORKER_AUTOSCALE 格式为 最大,最小，须满足 1 ≤ 最小 ≤ 最大 ≤ 128。")
        return ["--pool", pool, "--autoscale", autoscale]
    concurrency = environment.get("WORKER_CONCURRENCY", "1")
    if not positive(concurrency) or (pool == "solo" and concurrency != "1"):
        raise ValueError("Celery 固定并发须为正整数；solo 仅支持并发 1。")
    return ["--pool", pool, "--concurrency", concurrency]


def add_capacity_fields(
    add: Callable[..., object],
    value: Callable[[str], str],
    environment: Mapping[str, str],
) -> None:
    autoscale = environment.get("WORKER_AUTOSCALE", "")
    maximum, _, minimum = autoscale.partition(",") if autoscale else ("4", ",", "1")

    def parallel() -> bool:
        return value("pool") == "prefork"

    def automatic() -> bool:
        return parallel() and value("worker_strategy") == "auto"

    add(
        "pool", "Celery 执行池", environment.get("WORKER_POOL", "solo"), choices=POOLS, group="进程"
    )
    add(
        "worker_strategy",
        "Celery 并发策略",
        "auto" if autoscale else "fixed",
        choices=STRATEGIES,
        active=parallel,
        group="进程",
    )
    add(
        "concurrency",
        "Celery 固定并发数",
        environment.get("WORKER_CONCURRENCY", "1"),
        valid=positive,
        active=lambda: parallel() and not automatic(),
        group="进程",
    )
    for key, label, bound in (
        ("worker_min", "Celery 最小并发", minimum),
        ("worker_max", "Celery 最大并发", maximum),
    ):
        add(key, label, bound, valid=valid_bound, active=automatic, group="进程")


def capacity_environment(value: Callable[[str], str]) -> dict[str, str]:
    pool = value("pool")
    automatic = pool == "prefork" and value("worker_strategy") == "auto"
    return {
        "WORKER_POOL": pool,
        "WORKER_CONCURRENCY": value("concurrency") if pool == "prefork" else "1",
        "WORKER_AUTOSCALE": f"{value('worker_max')},{value('worker_min')}" if automatic else "",
    }


def capacity_errors(value: Callable[[str], str]) -> list[str]:
    if (
        value("pool") == "prefork"
        and value("worker_strategy") == "auto"
        and valid_bound(value("worker_min"))
        and valid_bound(value("worker_max"))
        and int(value("worker_min")) > int(value("worker_max"))
    ):
        return ["Celery 最小并发不能超过最大并发。"]
    return []
