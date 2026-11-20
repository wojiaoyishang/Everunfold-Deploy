# Docker 部署教程

从仓库根目录运行 `./setup.sh --docker`，不要在 `docker/` 内运行向导。
使用 Docker Engine 与 BuildKit；无需 Compose 插件。

## 构建与运行

“构建并启动”依次完成源码更新、后端代码快照、统一运行镜像构建、前端宿主与插件编译、数据库检查、自动配对、后端插件清单同步和容器启动。
只有构建成功后才会停止并替换本部署拥有的旧容器。无关的同名容器会触发错误，不会被删除。

镜像名为 `everunfold:<版本号>`，含 Everunfold 标识和两端源码 commit 标签。
运行镜像自带 Python 3.12、Node.js 22 和锁定的后端依赖。源码与前端产物通过挂载提供。
编译容器和数据库准备容器执行完即删除，最终仅有一个应用容器常驻。

| 宿主内容 | 容器位置 | 权限与用途 |
| --- | --- | --- |
| `var/releases/<版本>/backend` | `/workspace/backend` | 只读后端代码快照 |
| `var/releases/<版本>/frontend` | `/workspace/frontend` | 已编译的 Next.js 产物，可写框架缓存 |
| `var/runtime` | `/config` | 可写配置目录，允许浏览器原子保存 |
| 向导选择的后端数据目录 | `/data/backend` | 上传媒体和后端持久文件 |
| 向导选择的前端数据目录 | `/data/frontend` | settings、公开快照、插件、npm 下载缓存 |

源码拉取目录只负责保存、更新 Git 工作树；实际运行使用构建时生成的稳定快照。
向导设置的 UID/GID 默认为运行向导的用户。更改为其他 UID 时，须先确保挂载目录对该 UID 可读写。
前端配置挂载的是**目录**，不能改成单个只读文件绑定，否则节点页无法原子保存。

## 端口与反向代理

默认前端监听 `0.0.0.0:3000`，后端监听 `0.0.0.0:8001`；前端网页和内部接收器共用 3000 一个端口。
宿主机默认发布 `127.0.0.1:3000 → 3000` 和 `127.0.0.1:3434 → 8001`。
两端公开域名可以由宿主机 Nginx/1Panel 分别反代到这两个宿主端口。

例如向导填写：

| 配置项 | 示例 |
| --- | --- |
| 后端发布 URL | `https://api.example.com` |
| 前端发布 URL | `https://blog.example.com` |
| 后端宿主映射绑定 IP / 端口 | `127.0.0.1` / `3434` |
| 前端宿主映射绑定 IP / 端口 | `127.0.0.1` / `3000` |
| 主网络 | `everunfold-network` |
| 额外网络 | `1panel-network` |
| 宿主反代所在 Docker 网络 | `1panel-network` |
| 添加 host.docker.internal | `1` |
| 后端持久目录 | `/opt/everunfold/Everunfold-Data/backend` |
| 前端持久目录 | `/opt/everunfold/Everunfold-Data/frontend` |

主自定义网络不存在时会创建；额外网络必须已经存在。
内置 `bridge` 与 `1panel-network` 也能组合：创建容器后分别 `docker network connect`，不会同时向 `docker run` 传冲突的网络模式。
选定宿主反代网络后，每次由 setup 启动都会读取其当前网关，加入两端节点控制台的 HTTPS 代理信任 IP。
容器内运行的反代应填写实际直连代理 IP；不要把所有网络来源都当成 HTTPS 反代。

数据库/Redis 若在同一自定义网络，可填写它们的容器 DNS 名与容器内部端口。
若服务在宿主机，启用 host.docker.internal 后使用该名称和宿主服务端口；服务仍须监听可被网桥访问的接口。
两端内部通信固定走当前容器回环地址，不经过对外发布 URL 或反代。

## 接入外部前端

1. 在向导中配置「后端宿主映射绑定 IP」和「后端宿主映射端口」。例如绑定 `0.0.0.0:3434`，由宿主的 IPv4 接口转到容器 `8001`；也可只绑定一个指定的宿主 IP。经宿主反代时可保留 `127.0.0.1:3434`，让外部节点走 HTTPS。前端宿主映射独立；任一端绑定 IP 留空会关闭对应的宿主映射。旧配置的 `deployment.bind` 会作为两端各自的初始值。端口修改后选择「启动已构建版本」重新创建容器。
2. 在「外部前端调用后端的来源 IP/CIDR」中加入后端实际看到的对端地址。外部前端使用可达的宿主 IP 加映射端口，或使用反代的 HTTPS 根地址作为「后端内部连接 URL」。后端若经反代接收请求，应填写直接连接到后端的代理地址；应用不会从转发头取得白名单来源。限制外部端口的防火墙范围，跨机器发送令牌时使用 HTTPS 或受控私网。
3. 使用向导「导出外部前端配对」，给新前端填写独立节点 ID 和后端 Worker 可达的接收器根地址。配对可复制为 Base64，也可通过命令行导出 JSON。详见 [查看通讯密钥和接入其他前端](../README.md#查看通讯密钥和接入其他前端)。
4. 选择「启动已构建版本」后，在后端 `/_nodes` 登记并启用该节点。前端的「后端调用接收器的来源 IP/CIDR」应允许 Worker 的直连来源。后端收到同步请求后会核对来源与令牌；同步来访和投递结果可在 `/_nodes` 查看。

## 容器软件源

在 Docker 向导的「容器软件源 → 镜像源方案」选择官方源（默认）、清华 TUNA、中科大 USTC 或自定义。

APT 主源和安全更新源用于构建运行镜像，npm Registry 用于编译前端主体和插件。PyPI 索引用于构建后端和连接检测镜像。

清华和中科大方案使用各自的 Debian、Debian Security 和 PyPI 镜像，npm 使用 npmmirror。自定义方案可按项填入 HTTP/HTTPS 源地址。PyPI 地址须指向 Simple API，留空使用该工具的官方源。安装继续按仓库锁文件校验版本与哈希。

可单独填写 Node.js、Python 和 uv 基础镜像引用，例如 `registry.example.com/mirror/python:3.12-slim-bookworm`。使用私有仓库时先用 Docker 登录，并选择与默认镜像同版本、同发行版的副本。

保存后运行 `./setup.sh --docker build`，再运行 `./setup.sh --docker start` 应用新产物。

只改 npm Registry 时可直接执行 `./setup.sh --docker rebuild-frontend`，随后执行 `start`。单独编译插件也会使用新的 npm Registry。首次升级此功能时需先完整构建支持镜像源的运行镜像。

## 重启、更新和迁移

| 向导操作 | 命令 | 适用情况 |
| --- | --- | --- |
| 重新编译前端 | `./setup.sh --docker rebuild-frontend` | 前端主体更新，复用最后一次成功构建的镜像和后端快照 |
| 编译前端插件 | `./setup.sh --docker build-plugins` | 仅编译前端插件源码，产物进入前端数据目录的 `plugin-inbox/` |
| 热更新前端 | `./setup.sh --docker hot-update-frontend` | 将 `plugin-inbox/` 中的插件包发布到运行中的前端 |
| 启动已构建版本 | `./setup.sh --docker start` | 应用新产物或新的挂载、端口及网络配置 |
| 重启容器 | `./setup.sh --docker restart` | 使用原容器和当前产物重新启动，等待前后端就绪 |
| 保存并构建 | `./setup.sh --docker build` | 准备完整版本，运行中的服务继续使用原产物 |
| 构建并启动 | `./setup.sh --docker deploy` | 完整构建并应用 |

前端主体更新用 `rebuild-frontend`，成功后再用 `start` 切换产物。开启自动拉取时只更新前端源码，复用上次构建记录中的镜像和后端快照。向导会先保存当前设置。编译在临时容器中执行，前端宿主与插件都成功后才登记新版本。新产物的源码版本以 `var/releases/<版本>/release.yaml` 为准，复用镜像的标签记录的是首次完整构建。若原镜像或快照已清理，先执行一次完整 `build`。

只更新前端插件时，确认所选前端源码仓库已经包含最新部署工具。插件源码放在该仓库的 `src/plugins/<目录名>/`，执行 `build-plugins` 后再执行 `hot-update-frontend`。收到受信任的预编译包时，直接解压到前端数据目录，以 `plugin-inbox/<目录名>/manifest.json` 为入口，再执行 `hot-update-frontend`。热更新校验全部待发布包并公布新清单，无需编译或重启。启停仍由站点设置控制。

`restart` 只重启通过归属校验的现有容器，使用现有镜像、挂载、端口和配置。向导里未保存的修改会保留。若之前用 `stop` 删除了容器，使用 `start` 从构建产物启动。

后端代码、依赖或部署运行镜像更新时用完整 `build`。若版本要求迁移，备份后用 `./setup.sh --docker migrate` 停止本实例并显式升级，再运行 `start`。

```bash
./setup.sh --docker sync        # 触发已配对节点的同步
./setup.sh --docker stop        # 停止并删除本实例容器，保留数据与产物
./setup.sh --docker logs        # 查看运行日志
```

“初始化数据库并构建启动”只初始化确认空的数据库；已有非空库不会被清空。
迁移失败或启动准备失败不会自动回滚数据库，日志会指出下一步。
“触发同步”使用当前运行容器的后端配置和版本。更新部署程序和后端后首次使用时，需先构建并启动支持此操作的版本。

完整构建和前端单独编译都会分别构建宿主和插件，将插件写入候选目录，全部成功才发布；内容哈希文件增量复制、manifest 最后原子替换。
已有独立插件目录不会被自动删除；移除插件时先停用对应功能，再处理它的数据目录。
npm 缓存位于前端数据目录的 `npm-cache/`，重建无需从零下载。旧版本产物和缓存不自动清理，确认不再需要后由部署者清理。

API worker、Celery 池/并发、前端运行 worker 及日志等级从各自 YAML 的 `environment` 读取。向导的「前端 Node.js 运行 worker 数」对应 `frontend.environment.FRONTEND_WORKERS`，默认 `"1"`，范围 1–32。前端运行 worker 共用一个监听端口，每个进程会增加内存占用。

```bash
API_WORKERS=2 WORKER_POOL=prefork WORKER_CONCURRENCY=2 FRONTEND_WORKERS=2 ./setup.sh --docker start
```

两种构建操作共用向导中的「前端编译 worker 数」和「Node 编译内存 MiB」。编译容器分别从 `FRONTEND_BUILD_WORKERS` 与 `NODE_OPTIONS` 读取这两个设置。
首次启用多个前端运行 worker 时，若现有产物不支持，先更新两仓库并选「重新编译前端」，再选「启动已构建版本」。此后调整数量只需保存并启动已构建版本。上面的环境变量覆盖由新版统一镜像支持。Scheduler 始终只有一个进程，不使用 Celery Beat。

「进程」分组里的 Celery 执行池和并发策略均从列表选择。选「多进程（prefork）」和「按任务量自动伸缩」后，填写最小和最大并发（建议先用 1 和 4）。统一 YAML 中对应 `backend.environment.WORKER_AUTOSCALE: "4,1"`，顺序为最大、最小。任务少时缩回下限；留空则按 `WORKER_CONCURRENCY` 固定运行。旧配置保持固定模式。首次启用需要用新版部署仓库完整构建一次镜像；此后调整范围只需保存并「启动已构建版本」。

## 排错

- 若旧版脚本在构建成功后出现 `KeyError: 'Config'`，原因可能是通用 `docker inspect` 匹配到了同名网络或卷。新版已限定为容器查询；更新部署脚本后直接执行 `./setup.sh --docker start`，复用 `var/built.yaml` 指向的成功产物，无需重新构建。
- 若后端插件返回 HTTP 503 / 业务码 `50389`「插件部署尚未同步」，用**当前运行的容器**登记现有清单，完成后无需重启。在宿主机执行（替换非默认容器名）：

  ```bash
  docker exec -e EVERUNFOLD_DATA_DIR=/data/backend -w /workspace/backend everunfold \
    /opt/backend/venv/bin/python -m tools.register_backend_plugins \
    --config /config/backend/config.yaml --apply
  ```

  这会同步插件清单并排队投递设置，无需迁移、重新导入数据或重编译。验证术语时重新打开加密文章并解锁。之后用更新的部署脚本构建并启动，新镜像会自动完成同步。

- 检测只读地验证数据库/Redis，不测试发布域名；Docker 检测在选定网络中执行。
- 启动后会等待两端 `/health/ready`，最多 120 秒。超时会明确显示“服务已启动但未就绪”，可查看 `./setup.sh logs`，不会假报成功。
- 如果 Git 拒绝更新，进入提示的源码目录自行处理；脚本不自动合并或丢弃改动。
- 向导可以查看/复制字段和结果；构建阶段恢复普通终端，npm 安装和编译日志实时输出。
- 改动运行配置后用 `start` 重新创建容器；直接 `docker restart` 不会重新解析网络网关，也不应用新的宿主挂载或映射设置。
