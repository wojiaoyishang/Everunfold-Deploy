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

## 重启、更新和迁移

| 向导操作 | 命令 | 适用情况 |
| --- | --- | --- |
| 重新编译前端 | `./setup.sh --docker rebuild-frontend` | 前端或前端插件源码更新，复用最后一次成功构建的镜像和后端快照 |
| 启动已构建版本 | `./setup.sh --docker start` | 应用新产物或新的挂载、端口及网络配置 |
| 重启容器 | `./setup.sh --docker restart` | 使用原容器和当前产物重新启动，等待前后端就绪 |
| 保存并构建 | `./setup.sh --docker build` | 准备完整版本，运行中的服务继续使用原产物 |
| 构建并启动 | `./setup.sh --docker deploy` | 完整构建并应用 |

前端更新用 `rebuild-frontend`，成功后再用 `start` 切换产物。开启自动拉取时只更新前端源码，复用上次构建记录中的镜像和后端快照。向导会先保存当前设置。编译在临时容器中执行，前端宿主与插件都成功后才登记新版本。新产物的源码版本以 `var/releases/<版本>/release.yaml` 为准，复用镜像的标签记录的是首次完整构建。若原镜像或快照已清理，先执行一次完整 `build`。

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

API worker、Celery 池/并发及日志等级从各自 YAML 的 `environment` 读取，容器创建时也允许相应环境变量覆盖：

```bash
API_WORKERS=2 WORKER_POOL=prefork WORKER_CONCURRENCY=2 ./setup.sh --docker start
```

两种构建操作共用向导中的「前端编译 worker 数」和「Node 编译内存 MiB」。编译容器分别从 `FRONTEND_BUILD_WORKERS` 与 `NODE_OPTIONS` 读取这两个设置。
前端运行一个 Node 服务；Scheduler 始终一个，不使用 Celery Beat。

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
