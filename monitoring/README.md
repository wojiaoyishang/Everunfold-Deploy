# Everunfold 指标和日志接入

Prometheus 可从后端聚合目标和前端独立目标拉取指标。应用将日志输出到 stdout/stderr，外部采集器可从 Docker 容器或 systemd journal 读取。

## 指标

1. 更新后端、前端与统一部署源码。在后端独立或统一 setup 的「监控」中启用 Prometheus 指标。填写后端实际直连的监控来源 IP/CIDR，并复制采集令牌到 Prometheus 主机的私有文件。让 `/metrics` 仅在监控网或带 TLS 的私有反代上可达。经反代访问时，白名单识别后端看到的直接来源，反代也应限制真实采集来源。
2. 统一部署会自动填写回环前端地址和双方共享的节点指标令牌。分开部署时，在后端向导填写私有前端地址，再在前端向导填写相同的「前端内部指标令牌」。可单独填写前端内部指标的来源 IP/CIDR，单端前端留空时沿用接收器的来源白名单。统一部署保留回环来源。
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

### 前端独立采集

在每个前端的 setup 启用「前端独立 Prometheus 指标」，为该节点配置独立令牌与来源 IP/CIDR。使用统一向导时在同一个「监控」分组配置。首次升级重新编译并启动前端。之后 Prometheus 直接访问前端 `/metrics`，利用同一份 Node.js worker 指标，无需联系 FastAPI。后端聚合的配置和接口保持原样。

每个前端创建自己的采集任务和令牌文件。面板可以按 `job` 或 `node` 区分前端，统计同一前端时选用一个采集来源。

~~~yaml
scrape_configs:
  - job_name: everunfold-frontend-a
    scheme: https
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/frontend-a.token
    static_configs:
      - targets: ["frontend-a-monitor.example.internal:443"]
        labels:
          node: frontend-a
~~~

指标白名单可填 IPv4、IPv6 或 CIDR，多个用逗号分隔。Docker 向导也接受已连接的网络名，例如 `1panel-network`，并保存由 Docker 查得的当前网段。网段改变后重新填写网络名。[Docker 网络检查](https://docs.docker.com/reference/cli/docker/network/inspect/) · [Nginx 地址访问规则](https://nginx.org/en/docs/http/ngx_http_access_module.html)

## Celery Exporter（可选）

任务成功/失败、运行耗时和实际子进程数使用独立的 Celery exporter。它应在应用和 Redis Broker 均可达的 Docker 网络上运行，9808 不发布到宿主。

~~~bash
cd monitoring
docker compose -f compose.collectors.yaml --profile celery up -d celery-exporter
~~~

复制 `.env.example` 为 `monitoring/.env`。填写 `CE_BROKER_URL`（与后端的 `CELERY_BROKER_URL` 一致）、`CELERY_KEY_PREFIX`（项目 Redis 前缀加 `celery:`）、`CELERY_QUEUE_NAME`（项目 Redis 前缀加 `settings`）和可选的 `EVERUNFOLD_MONITOR_NETWORK`。例如前缀是 `everunfold:` 时，填写 `everunfold:celery:` 和 `everunfold:settings`。在部署向导填 `http://celery-exporter:9808`。后端启用 Celery 任务事件，每约 30 秒后台读取 exporter。Broker 的完整积压使用应用自己的指标，包含 Redis 的全部优先级队列。

## 应用日志

统一部署和前后端独立部署的「日志」分组都提供「结构化日志」。默认开启，应用每行输出一个 JSON 对象，包含 `time`、`level`、`component`、`event`、`message` 和对应事件的字段。选择「关闭（纯文本）」后，同一批应用事件以可读文本输出。配置保存在 `environment.EVERUNFOLD_LOG_FORMAT`，值为 `json` 或 `text`。已有部署首次使用此选项时先用新版前后端源码构建，之后保存并重启即可切换。

外部 Alloy 可读取 Docker 日志或原生部署的 systemd journal，并写入外部 Loki。统一容器内可按 `component` 区分 `nodejs`、`fastapi`、`celery` 和 `scheduler`。采集端可用 `org.everunfold.deployment` 容器标签发现统一部署，用实际的 unit 名筛选 journal。

如果采集端为日志设置了 `app="everunfold"`，可在 Grafana Explore 中查询：

~~~logql
{app="everunfold"} | json | component="nodejs" | event="sync"
~~~

查看本地日志时也可将输出传给 `python deploy/log_view.py`，它会把 JSON 转成易读的行并保留普通文本。

### 请求来源

在后台进入「设置 → 真实 IP 来源配置」，选择首选来源和回退策略，填写 Node.js、FastAPI 两端反向代理实际使用的直连 IP 或 CIDR。初始规则以直连地址为准。使用代理请求头时，代理应覆盖单值头，并正常追加代理链。「检测当前连接」分别显示两端收到的地址、请求头、选择结果和生效版本。前端节点要先配对并启用才能获取规则和参与诊断。

访问日志 `event="http_access"` 中，`peer_ip` 是服务器看到的直连地址，`client_ip` 和 `client_ip_source` 表示规则采用的地址和来源。`x_real_ip`、`x_forwarded_for`、`forwarded`（仅 `for`）、`cf_connecting_ip`、`true_client_ip` 保存收到的有效地址值或无效标记，`x_forwarded_proto` 记录有效的 HTTP/HTTPS 协议值。IP 字段保留在日志正文里，供查询时解析。服务间同步和 Prometheus 的访问白名单继续按各自的直连地址验证。

~~~logql
{app="everunfold"} | json | event="http_access" | client_ip="203.0.113.42"
~~~

## 后端插件指标

后端类插件可选实现异步 metrics(context)，返回至多 50 个 app.core.monitor_metrics.PluginMetric。支持 gauge、counter 和声明式键值标签。名称自动放在 everunfold_plugin_<插件 ID>_ 命名空间内。仅已部署且启用的插件会进入后台采集，各插件超时或出错会单独跳过。证书指标可由反代或以后接入的提供方负责。
