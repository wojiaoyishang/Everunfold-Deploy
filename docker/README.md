# Docker 部署教程

从仓库根目录运行 `./setup.sh --docker`，不要在 `docker/` 内运行向导。
使用 Docker Engine 与 BuildKit；无需 Compose 插件。

## 构建与运行

“构建并启动”依次完成源码更新、后端代码快照、统一运行镜像构建、前端宿主与插件编译、数据库检查、自动配对和容器启动。
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

```bash
docker restart everunfold       # 仅重启现有进程，不编译、不做 DDL
./setup.sh --docker deploy      # 更新代码，构建并重新创建容器
./setup.sh --docker build       # 只构建，保留正在运行的版本
./setup.sh --docker migrate     # 先备份数据库；停止本实例后显式执行迁移
./setup.sh --docker start       # 启动最后成功构建的版本
./setup.sh --docker stop        # 停止并删除本实例容器，保留数据与产物
./setup.sh logs
```

升级流程：先 `build`；若版本需要迁移，备份数据库后 `migrate`；再 `start`。
“初始化数据库并构建启动”只初始化确认空的数据库；已有非空库不会被清空。
迁移失败或启动准备失败不会自动回滚数据库，日志会指出下一步。

每次 build 会分别构建宿主和插件，将插件写入候选目录，全部成功才发布；内容哈希文件增量复制、manifest 最后原子替换。
已有独立插件目录不会被自动删除；移除插件时先停用对应功能，再处理它的数据目录。
npm 缓存位于前端数据目录的 `npm-cache/`，重建无需从零下载。旧版本产物和缓存不自动清理，确认不再需要后由部署者清理。

API worker、Celery 池/并发及日志等级从各自 YAML 的 `environment` 读取，容器创建时也允许相应环境变量覆盖：

```bash
API_WORKERS=2 WORKER_POOL=prefork WORKER_CONCURRENCY=2 ./setup.sh --docker start
```

前端编译并行度在向导“前端编译 worker 数”设置，并通过 `FRONTEND_BUILD_WORKERS` 传给编译容器。
前端运行一个 Node 服务；Scheduler 始终一个，不使用 Celery Beat。

## 排错

- 检测只读地验证数据库/Redis，不测试发布域名；Docker 检测在选定网络中执行。
- 启动后会等待两端 `/health/ready`，最多 120 秒。超时会明确显示“服务已启动但未就绪”，可查看 `./setup.sh logs`，不会假报成功。
- 如果 Git 拒绝更新，进入提示的源码目录自行处理；脚本不自动合并或丢弃改动。
- 向导可以查看/复制字段和结果；构建阶段恢复普通终端，npm 安装和编译日志实时输出。
- 改动运行配置后用 `start` 重新创建容器；直接 `docker restart` 不会重新解析网络网关，也不应用新的宿主挂载或映射设置。
