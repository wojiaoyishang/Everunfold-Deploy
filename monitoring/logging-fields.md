# JSON 日志字段

结构化日志默认以 JSON Lines 输出。应用写入 stdout/stderr，外部 Alloy 可按行读取，Loki 可用 LogQL 的 `| json` 解析。字段值为 `null` 或缺省时，表示该事件没有对应数据。

## 通用字段

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `time` | string | UTC 事件时间，使用 ISO 8601。 |
| `level` | string | `debug`、`info`、`warning`、`error`，Python 另支持 `critical`。 |
| `component` | string | `fastapi`、`nodejs`、`celery` 或 `scheduler`。 |
| `event` | string | 事件类型，例如 `http_access`、`task_received`、`task_finished`、`access`、`sync` 或 `log`。 |
| `message` | string | 人可读的事件内容。 |
| `pid` | number | 产生这条日志的进程 ID。 |
| `logger` | string | Python logger 名称，例如 `celery.app.trace`。 |
| `symbol` | string | 可选业务标记，例如 `content.publish`。最长 64 个字符，以字母或数字开头，后续可用字母、数字、`_`、`.`、`:`、`-`。 |
| `error_type`, `frames` | string, string[] | Python 异常类和末尾最多 12 个栈帧，栈帧仅包含文件名、行号和函数名。 |

## HTTP 请求

以下字段用于 `event="http_access"`。FastAPI 的访问记录受 `API_LOG_LEVEL` 控制。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `request_id` | string | 当前服务生成的 32 位十六进制请求 ID。各服务独立生成。 |
| `request_url` | string 或 null | URL 路径，例如 `/v1/account/profile`，最长 2048 个字符。查询串、片段、主机和认证信息均省略。 |
| `route` | string 或 null | FastAPI 匹配的路由模板，例如 `/v1/admin/users/{user_id}`。适合汇总请求。 |
| `method` | string | 请求方法，例如 `GET`、`POST`。 |
| `status` | number 或 null | HTTP 状态码。响应头发出前连接中断时为 `null`。 |
| `outcome` | string | `completed` 表示响应发完，`aborted` 表示中断，`error` 表示 FastAPI 处理时抛出异常。HTTP 4xx、5xx 可同时是 `completed`。 |
| `duration_ms` | number | 从接收请求到响应结束或中断的毫秒数，包含响应流传输时间。 |
| `http_version` | string | HTTP 协议版本，例如 `1.1`。 |
| `user_agent` | string 或 null | 请求提供的浏览器或客户端标识，最多 512 个字符，去除控制字符。 |
| `node_id` | string | 出日志的前端节点 ID。 |
| `peer_ip`, `client_ip` | string 或 null | 直连地址和按可信代理策略选出的客户端地址。 |
| `client_ip_source`, `trusted_proxy`, `ip_policy_revision` | string 或 null, boolean, number 或 null | 客户端地址来源、代理是否受信和当前策略版本。 |
| `x_real_ip`, `x_forwarded_for`, `forwarded`, `cf_connecting_ip`, `true_client_ip` | (string 或 null)[] | 收到的代理地址列表，`null` 表示某项无效。 |
| `x_forwarded_proto` | string 或 null | 收到的有效 `http` 或 `https` 协议提示。 |

## Celery Worker

成功、接收和常规重试记录随 `WORKER_LOG_LEVEL=INFO` 输出。失败和拒绝沿用 Celery 的原有日志级别。任务参数、返回值和异常正文在结构化任务事件中省略。任务执行期间调用 Python logger 也会带上当前任务上下文。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `task_id`, `task_name` | string | Celery 任务 ID 和注册名，例如 `everunfold.settings.deliver`。 |
| `task_state` | string | `task_received` 为 `RECEIVED`。`task_finished` 可能为 `SUCCESS`、`FAILURE`、`RETRY`、`REJECTED` 或 `IGNORED`。 |
| `root_id`, `parent_id` | string | 执行上下文提供的任务链根 ID 和父任务 ID。 |
| `retries` | number | 当前执行前已经重试的次数。 |
| `queue` | string | 执行上下文提供的投递路由键。 |
| `worker_hostname` | string | 执行上下文提供的 Celery Worker 名称。 |
| `duration_ms` | number | Celery 成功记录提供的执行时长，单位毫秒。 |

## 代码中写日志

FastAPI、Worker 和 Scheduler 使用 Python 标准库的 `logging.getLogger(__name__)`。部署入口会统一设置格式，可使用 `debug`、`info`、`warning`、`error`、`exception` 和 `critical`。业务标记通过 `extra` 传入。

```python
import logging

logger = logging.getLogger(__name__)
logger.info("Article publication completed", extra={"symbol": "content.publish"})
```

前端服务端代码可从 `src/server/runtime-log.ts` 导入 `runtimeLogger`。可使用 `debug`、`info`、`warn` 和 `error`。前端的 `warn` 会写成 `level="warning"`。

```ts
import { runtimeLogger } from "@/server/runtime-log";

runtimeLogger.info("published", "Article publication completed", {
  symbol: "content.publish",
});
```

## 实测输出

以下四行来自隔离环境的实际输出。两条请求分别访问 FastAPI 的健康检查和 Node.js 的登录页，测试请求都带有查询参数。后两行来自同一个 Celery prefork 任务，一条是执行完成日志，一条是业务代码带 `symbol` 的日志。

```jsonl
{"time":"2026-11-19T13:02:46.929460+00:00","component":"fastapi","level":"info","event":"http_access","logger":"uvicorn.error.everunfold.http","pid":1110067,"message":"GET → 200","request_id":"8f2bb4aa66c4455a901dac6b412e1011","request_url":"/health/live","route":"/health/live","http_version":"1.1","user_agent":"Everunfold-Monitor-Smoke/1.0","method":"GET","status":200,"outcome":"completed","duration_ms":120.465,"peer_ip":"127.0.0.1","client_ip":"127.0.0.1","client_ip_source":"peer","trusted_proxy":false,"ip_policy_revision":0,"x_real_ip":[],"x_forwarded_for":[],"forwarded":[],"cf_connecting_ip":[],"true_client_ip":[],"x_forwarded_proto":null}
{"request_id":"cab5dba55572414183313fd12521b459","request_url":"/login","http_version":"1.1","user_agent":"Everunfold-Monitor-Smoke/1.0","node_id":"log-check","method":"GET","status":200,"outcome":"completed","duration_ms":4896.594,"peer_ip":"127.0.0.1","client_ip":"127.0.0.1","client_ip_source":"peer","trusted_proxy":false,"ip_policy_revision":null,"x_real_ip":[],"x_forwarded_for":[],"forwarded":[],"cf_connecting_ip":[],"true_client_ip":[],"x_forwarded_proto":null,"pid":1110179,"time":"2026-11-19T13:02:51.828Z","level":"info","component":"nodejs","event":"http_access","message":"GET → 200"}
{"time":"2026-11-19T13:08:35.469262+00:00","component":"celery","level":"info","event":"task_finished","logger":"celery.app.trace","pid":1113536,"message":"Celery task success","task_id":"427922c5-dfc0-4525-bf59-b13e09b7535b","task_name":"everunfold.test.publish","root_id":"427922c5-dfc0-4525-bf59-b13e09b7535b","worker_hostname":"monitor-prefork@localhost","retries":0,"queue":"logging-fixture","duration_ms":0.852,"task_state":"SUCCESS"}
{"time":"2026-11-19T13:08:35.468559+00:00","component":"celery","level":"info","event":"log","logger":"everunfold.example","pid":1113536,"message":"Article publication completed","task_id":"427922c5-dfc0-4525-bf59-b13e09b7535b","task_name":"everunfold.test.publish","root_id":"427922c5-dfc0-4525-bf59-b13e09b7535b","worker_hostname":"monitor-prefork@localhost","retries":0,"queue":"logging-fixture","symbol":"content.publish"}
```

## Loki 查询

以下查询假设 Alloy 添加了 `app="everunfold"` 标签。按业务使用一组稳定的 `symbol`，URL、请求 ID、User-Agent、IP 和任务 ID 在查询时从正文解析。

```logql
{app="everunfold"} | json | event="http_access" | status >= 500
{app="everunfold"} | json | event="http_access" | duration_ms > 500
{app="everunfold"} | json | component="celery" | task_state=~"FAILURE|RETRY"
{app="everunfold"} | json | symbol="content.publish"
```
