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

以下字段用于 `event="http_access"`。前后端的 `message` 都按 `GET /路径 → 200` 显示，`request_url` 可单独查询。FastAPI 的访问记录受 `API_LOG_LEVEL` 控制。

部署入口关闭 Uvicorn 的原生访问日志，每个进入 FastAPI 的 HTTP 请求由应用中间件记录一次。若用 Everunfold 的格式化器显式开启原生访问日志，`logger="uvicorn.access"` 的记录也会输出方法、过滤后的 URL 路径、HTTP 版本和状态。请求耗时、请求 ID 和 User-Agent 来自应用中间件。

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

## 业务事件

启用 `core.business-observability` 后输出 `event="business_event"`。
HTTP 业务事件和同一请求的访问日志共用一次来源解析。后台「真实 IP 来源配置」中的可信代理、
地址来源与回退同时作用于两条记录。只有对应账号名可从请求或有效二次验证挑战取得时，登录失败才填入尝试用户名。
密码、验证码、Cookie、完整邮件地址和正文不写入业务日志。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `business_action` | string | `login`、`comment_submit`、`comment_moderate`、`notification`，或发布、页面、账号动作。 |
| `business_result` | string | `success`、`failure`、`challenge`（待二次验证）、`suppressed`（跳过）、`unknown`（结果未知）。 |
| `business_stage` | string | 可选阶段，例如 `password`、`second_factor`、`manual`、`scheduled`、`delivery`、`security_mail`。评论审核可为 `approve`、`reject` 等动作。 |
| `business_state` | string | 可选结果状态，例如评论 `pending` / `approved`，发布 `first` / `update` / `republish`。 |
| `business_reason`, `business_error_code` | string, number | 归类后的失败原因及可用的稳定业务错误码。 |
| `status`, `request_id` | number, string | FastAPI 的 HTTP 状态码及应用访问日志里的同一个请求 ID。 |
| `client_ip`, `client_ip_source`, `peer_ip` | string 或 null | 按后台规则取出的客户端 IP、实际采用的来源和直连地址。未获信任且配置不回退时客户端 IP 为 null。 |
| `trusted_proxy`, `ip_policy_revision` | boolean, number 或 null | 直连是否属于可信代理，以及本次使用的后台规则修订号。 |
| `user_agent`, `method`, `request_url`, `route` | string 或 null | 浏览器或客户端标识、HTTP 方法、去掉查询串的路径及匹配的路由模板。 |
| `duration_ms` | number | HTTP 请求处理时间，和对应访问日志一致。 |
| `business_attempted_username` | string | 仅登录失败可用。用户名去掉首尾空格并转小写，邮箱登录仅记录掩码和域名，如 `***@example.test`。 |
| `business_resource_kind`, `resource_id` | string, string | 可选业务对象类型与 ID。通知的 `resource_id` 为投递或安全邮件 ID。 |
| `business_channel`, `business_notification_type` | string, string | `email`、`in-app` 或插件渠道，以及通知定义 ID。 |
| `business_attempt`, `business_affected_count` | number | 可选投递尝试次数和批量评论审核影响条数。 |
| `business_recipient_domain`, `business_mail_profile_id` | string | 邮件收件域名和发送配置 ID。发送配置或收件信息缺失时省略对应值。 |
| `business_delivery_duration_ms` | number | 外部渠道投递处理的毫秒数，包括通知投递前检查。直接发送账号安全邮件时统计 SMTP 调用耗时。 |
| `business_role_changed`, `business_account_enabled` | boolean | 角色发生变更，或账号启停后的状态。 |
| `symbol` | string | `business.<action>.<result>`，例如 `business.comment_submit.success`。 |

SMTP 返回接受时记邮件成功。结果未知和跳过各自计数，邮件失败率只以已知成功与失败为分母。
站内通知在数据库入箱后记成功。提交评论时待审核算提交成功，审核操作另外统计。
账号安全邮件包含邮箱验证、注册验证、密码找回及密码重置完成通知。仅建立排期时记录排期动作，
定时发布的执行结果由 Worker 另行记录。

```logql
{job="docker", service_name=~"(?i).*everunfold.*"}
  | json | event="business_event" | business_channel="email" | business_result="failure"

{job="docker", service_name=~"(?i).*everunfold.*"}
  | json | symbol="business.login.failure" | client_ip="203.0.113.42"
```

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

以下日志来自隔离环境的实际输出。前三行分别是 FastAPI 应用、Node.js 和显式开启的 Uvicorn 原生访问日志，测试请求都访问 `/health/live` 并携带查询参数。后两行来自同一个 Celery prefork 任务。

```jsonl
{"time":"2026-11-19T13:45:10.897331+00:00","component":"fastapi","level":"info","event":"http_access","logger":"uvicorn.error.everunfold.http","pid":1127420,"message":"GET /health/live → 200","request_id":"4f2f9504c09741eda4a24b9b8c0b2d0f","request_url":"/health/live","route":"/health/live","http_version":"1.1","user_agent":"Everunfold-Log-Check/1.0","method":"GET","status":200,"outcome":"completed","duration_ms":424.192,"peer_ip":"127.0.0.1","client_ip":"127.0.0.1","client_ip_source":"peer","trusted_proxy":false,"ip_policy_revision":null,"x_real_ip":[],"x_forwarded_for":[],"forwarded":[],"cf_connecting_ip":[],"true_client_ip":[],"x_forwarded_proto":null}
{"request_id":"f28ed6ecbf6b4b629fe45af61dbae782","request_url":"/health/live","http_version":"1.1","user_agent":"Everunfold-Log-Check/1.0","node_id":"log-check","method":"GET","status":200,"outcome":"completed","duration_ms":1.417,"peer_ip":"127.0.0.1","client_ip":"127.0.0.1","client_ip_source":"peer","trusted_proxy":false,"ip_policy_revision":null,"x_real_ip":[],"x_forwarded_for":[],"forwarded":[],"cf_connecting_ip":[],"true_client_ip":[],"x_forwarded_proto":null,"pid":1127391,"time":"2026-11-19T13:45:06.194Z","level":"info","component":"nodejs","event":"http_access","message":"GET /health/live → 200"}
{"time":"2026-11-19T13:45:11.714462+00:00","component":"fastapi","level":"info","event":"http_access","logger":"uvicorn.access","pid":1127523,"message":"GET /health/live → 200","method":"GET","request_url":"/health/live","http_version":"1.1","status":200}
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
