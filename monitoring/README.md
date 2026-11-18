# Everunfold 指标和日志接入

监控服务器运行现有 Prometheus 和 Grafana，可再运行 Loki。应用服务器上的 Alloy 读取原有 stdout/stderr，然后发给 Loki。Prometheus 只需增加一个 Everunfold 采集目标。

## 指标

1. 更新后端、前端与统一部署源码。在后端独立或统一 setup 的「监控」中启用 Prometheus 指标。填写后端实际直连的监控来源 IP/CIDR，并复制采集令牌到 Prometheus 主机的私有文件。让 `/metrics` 仅在监控网或带 TLS 的私有反代上可达。经反代访问时，白名单识别后端看到的直接来源，反代也应限制真实采集来源。
2. 统一部署会自动填写回环前端地址和双方共享的节点指标令牌。分开部署时，在后端向导填写私有前端地址，再在前端 `config.yaml` 的 `environment` 配置相同的 `EVERUNFOLD_METRICS_NODE_TOKEN`。前端内部指标沿用接收器的来源 IP 白名单。后端的采集令牌与节点令牌不同。
3. 保存后首次选择「构建并启动」。应用新的 Python/Node 依赖需要构建一次。
4. 在 Prometheus 增加如下配置。targets 填写 Prometheus 容器能访问的地址，令牌文件由 Prometheus 进程读取。

~~~yaml
scrape_configs:
  - job_name: everunfold
    scrape_interval: 30s
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/everunfold.token
    static_configs:
      - targets: ["everunfold-monitor.internal:3434"]
~~~

面板可使用 everunfold_api_http_requests_total、everunfold_node_http_requests_total 及各自延迟直方图、everunfold_scheduler_up、everunfold_scheduler_healthy、everunfold_scheduler_last_successful_scan_timestamp_seconds、everunfold_scheduler_oldest_due_timestamp_seconds、everunfold_celery_broker_queued_messages、everunfold_celery_workers_online、everunfold_sync_revision_gap 和 everunfold_sync_failed。采集状态用 everunfold_metrics_source_up。若某来源不可用，同一响应仍可读取其他来源和 FastAPI 本机指标。带时间戳的指标用 time() 减去时间戳计算时长。

## Celery Exporter（可选）

任务成功/失败、运行耗时和实际子进程数使用独立的 Celery exporter。它应在应用和 Redis Broker 均可达的 Docker 网络上运行，9808 不发布到宿主。

~~~bash
cd monitoring
docker compose -f compose.collectors.yaml --profile celery up -d celery-exporter
~~~

复制 `.env.example` 为 `monitoring/.env`。填写 `CE_BROKER_URL`（与后端的 `CELERY_BROKER_URL` 一致）、`CELERY_KEY_PREFIX`（项目 Redis 前缀加 `celery:`）、`CELERY_QUEUE_NAME`（项目 Redis 前缀加 `settings`）和可选的 `EVERUNFOLD_MONITOR_NETWORK`。例如前缀是 `everunfold:` 时，填写 `everunfold:celery:` 和 `everunfold:settings`。在部署向导填 `http://celery-exporter:9808`。后端启用 Celery 任务事件，每约 30 秒后台读取 exporter。Broker 的完整积压使用应用自己的指标，包含 Redis 的全部优先级队列。

## Loki 日志和来源限制

1. 在监控服务器进入 monitoring，运行 docker compose -f compose.loki.yaml up -d。Loki 使用独立数据卷，模板默认保留 14 天，端口只绑定宿主回环。
2. 在「监控」分组分别填写「Loki 查询来源 IP/CIDR」（Grafana 的实际来源）和「Alloy 写入来源 IP/CIDR」（应用主机的实际来源）。保存会在配置目录下生成 var/monitoring/loki-access.conf。留空会拒绝该方向所有请求。
3. 把生成的文件装到监控服务器的 Nginx/OpenResty。为 Loki 使用独立站点，绑定监控网并配置 HTTPS。在 http 上下文声明 upstream everunfold_loki { server 127.0.0.1:3100; }，在该站点 server 块 include 生成的文件，并配置 auth_basic 与只读凭据文件。若反代也在 Docker 中，upstream 填它实际可达的 Loki 私有地址。改白名单后重新应用 include 并重载反代。查询与写入规则匹配直连来源，Loki 的删除和配置等管理路由不在允许列表。
4. 在应用服务器的 monitoring/.env 设置 LOKI_PUSH_URL 为私有 HTTPS 地址加 /loki/api/v1/push，LOKI_USER、LOKI_PASSWORD 为反代写入凭据，以及 EVERUNFOLD_ENVIRONMENT。启动 Alloy。

~~~bash
docker compose -f compose.collectors.yaml up -d alloy
~~~

Docker 采集用本机 Docker socket，并通过 Everunfold 容器标签筛选运行日志。Alloy 的数据卷保留读取位置。原生部署使用 alloy-journal.alloy，设置 EVERUNFOLD_SYSTEMD_UNIT 为实际用户服务名，并给 Alloy 读取该 journal 的权限。Grafana 的 Loki 数据源填写反代根地址和查询凭据。

四个应用组件的日志归为一行 JSON，保留 time、level、component、event、message 及事件字段。Grafana Explore 可以用下面的表达式显示便于阅读的行。未带 JSON 字段的工具/框架日志也会保留原文。

~~~logql
{app="everunfold"} | json | line_format "{{if .message}}{{.time}} · {{.level}} · {{.component}} · {{.message}}{{else}}{{ __line__ }}{{end}}"
~~~

只查应用同步时可用 {app="everunfold",component="nodejs"} | json | event="sync"。容器日志也可以管道传给 python deploy/log_view.py，便于在终端查看。日志索引只加 app、env、component 这类低基数字段，错误码和任务相关信息留在正文。

## 后端插件指标

后端类插件可选实现异步 metrics(context)，返回至多 50 个 app.core.monitor_metrics.PluginMetric。支持 gauge、counter 和声明式键值标签。名称自动放在 everunfold_plugin_<插件 ID>_ 命名空间内。仅已部署且启用的插件会进入后台采集，各插件超时或出错会单独跳过。证书指标可由反代或以后接入的提供方负责。
