# Everunfold Deploy

[指标与日志接入](monitoring/README.md)。

一个配置向导，部署 Everunfold 前端、API、Celery Worker 和 Scheduler。
Docker 模式只保留 **一个常驻应用容器**，PostgreSQL 和 Redis 使用外部服务。
前后端源码仓库仍分别维护，本仓库负责配置、拉取、构建、配对和启动。
「日志 → 结构化日志」默认开启，应用按行输出 JSON，供外部日志系统采集。

在仓库根目录运行：

```bash
./setup.sh
```

首先显示所有配置，可按 `1` 顺序编辑、`2` 修改选中项、`3` 退出。
密钥自动生成但可查看、复制和修改；已有应用主密钥必须沿用。
“仅保存”会显示成功和配置绝对路径，继续留在页面。
“构建并启动”执行完成后按回车返回向导。

## 一、Docker 从零构建

Linux 主机准备 Git、curl 和可正常运行的 [Docker Engine](https://docs.docker.com/engine/install/)。
无需宿主机安装 Node.js；向导所用的 Python 3.12 由 uv 管理。
没有 uv 时，`setup.sh` 会询问是否从官方源安装到本仓库 `var/tools`，不修改系统 Python。

```bash
git clone https://github.com/wojiaoyishang/Everunfold-Deploy.git
cd Everunfold-Deploy
./setup.sh --docker
```

在统一向导中设置：

1. 自动拉取源码是否开启、前后端仓库 URL、各自的源码保存目录。
2. 后端与前端各自的持久数据目录。
3. PostgreSQL、Redis；Broker 和结果 Redis 留空时沿用 Redis 主机的 DB 1、DB 2。
4. 两端发布 URL、监听端口、节点控制台密码、容器网络，以及各自的宿主映射绑定 IP 和端口。

点击“检测连接”会在所选 Docker 网络内验证 PostgreSQL 认证/查询、迁移版本和 Redis PING。
首次会构建一个小型检测镜像。检测本身不初始化数据库。

新站点选择“初始化数据库并构建启动”；已有站点选择“构建并启动”。
普通启动默认也会初始化**确认没有用户对象的空库**；可关闭此选项。
已有数据库需要迁移时会停止并提示，使用“升级迁移”显式处理。

```bash
./setup.sh --docker rebuild-frontend  # 复用已有镜像，只编译前端和前端插件
./setup.sh --docker start             # 应用最后一次成功构建
./setup.sh --docker restart           # 重启已创建的容器
./setup.sh --docker build             # 完整构建，暂不切换运行版本
./setup.sh --docker deploy            # 完整构建后启动
./setup.sh sync                 # 触发已配对节点的同步
./setup.sh logs                 # 查看合并后的前后端日志
```

日常前端更新在向导选择「重新编译前端」，成功后选择「启动已构建版本」。后者复用镜像并挂载新产物。只想重启当前服务时，选择「重启容器」，容器会继续使用当前挂载的版本。

详细的端口、挂载、1Panel 网络和升级步骤见 [Docker 教程](docker/README.md)。
其他服务器上的前端也能连接此后端，详见[接入外部前端](docker/README.md#接入外部前端)。

## 二、不用 Docker

Linux 主机安装 Git、curl、rsync、Node.js **22.15.1 或更新的 22.x**、npm **10.9.2 或更新的 10.x**。
uv/Python 3.12 由 `setup.sh` 同样管理。后台部署需要可用的 systemd 用户会话。

```bash
git clone https://github.com/wojiaoyishang/Everunfold-Deploy.git
cd Everunfold-Deploy
./setup.sh --native
```

填写相同的数据库、发布 URL 和数据目录。源码在独立目录保存，编译和依赖安装在版本目录中执行。
点击“构建并启动”会安装并启用 `everunfold-<实例标识>.service` 用户服务，SSH 断开后仍由 systemd 管理。

没有 systemd 用户会话，或者只想在前台查看启动过程：

```bash
./setup.sh --native build
./setup.sh --native run
```

`run` 在前台启动全部进程，Ctrl+C 统一停止。`start` 使用后台用户服务，不会重新编译。
开机常驻与日志说明见 [直接部署教程](deploy/README.md)。

## 手动触发同步

在向导选择“触发同步”，或执行 `./setup.sh sync`。操作使用运行配置和后端数据库凭据，登记当前后端插件清单，并让 Worker 向已配对节点重发公开内容、站点设置和缓存策略。重试次数已用完的当前投递也会重新排队。
Docker 模式在本部署的运行容器内执行。原生前台模式可在另一个终端触发。表单中尚未保存的修改会保留在向导中。“已排队”表示后端已接受请求，实际投递结果可在节点状态或部署日志查看。

## 沿用已经配置好的前后端

新部署首次打开时，会读取默认源码目录中已有的 `config.yaml`，保留应用主密钥、数据库连接和节点凭据。
源码不在默认的同级目录时，先在向导修改源码路径，点“导入原配置”，重新检查总览。
此操作沿用原密钥和设置，不要求先填写新的数据库配置。也可以先执行一次性命令导入：

```bash
./setup.sh import \
  --backend-source /opt/everunfold/Everunfold-Backend \
  --frontend-source /opt/everunfold/Everunfold-Frontend
./setup.sh
```

导入不会改写两个源码仓库的配置，也不会复制持久数据。
在向导中将两端数据路径指向原有的数据目录。统一配置已经存在时，`import` 拒绝覆盖。
新实例启用前，停掉原来的两个应用容器/进程，避免重复的 Scheduler、Worker 或端口占用。

## 配置和更新规则

- 默认统一配置为**本仓库根目录 `config.yaml`**；可用 `CONFIG_FILE=/绝对路径/config.yaml ./setup.sh` 指定。
- 每个部署实例使用独立目录；同目录的 `var/` 和默认数据目录属于一个实例。根配置丢失时须恢复备份，不会根据旧运行目录重新生成密钥。
- 配置按 `deployment`、`backend`、`frontend` 分区。两端运行文件生成到 `var/runtime/backend/config.yaml`、`var/runtime/frontend/config.yaml`，各进程的 `CONFIG_FILE` 分别明确指向它们。
- 浏览器节点页修改运行配置后，下一次打开/保存向导会合并回来。同一字段同时被修改时显示冲突，拒绝静默覆盖。
- 发布 URL 用于浏览器入口与资源链接；同机内部调用自动使用 `127.0.0.1` 加实际监听端口。双向令牌和来源 UUID 自动配对，不用手动传 Base64。
- 后端不添加“前端公开地址”字段，前端自行上报发布 URL；部署会登记并启用向导里选定的前端节点。
- 自动拉取开启时，空目录执行 `git clone`，已有仓库执行 **`git pull --ff-only`**。本地改动、非 Git 目录、origin 不匹配、分叉或其他 Git 错误均停止，提示用户自行处理。不会执行 push、reset 或 stash。
- 自动拉取关闭时，使用所选目录的现有代码，包括本地修改。版本记录会标记仓库是否有未提交改动。
- 每次成功构建生成独立版本，构建失败保留当前运行版本。启动前准备失败会保持停止状态并输出原因，旧产物仍保留。
- 由向导或 `setup.sh start` 启动时，检查数据库和节点配对后同步后端插件清单。已有库无需为插件清单单独执行迁移；普通的容器或 systemd 重启不执行这一步。
- 重启不拉取、不安装依赖、不编译。npm 下载缓存持久保存；完整构建仍用 `npm ci` 核验锁文件并输出详细日志。

配置、备份和 `var/runtime` 含私有密钥，文件使用 `0600`，已被 Git 忽略。
请一起备份统一配置、`var/runtime`、两端数据目录和外部数据库；不要更换已有站点的应用/TOTP 主密钥。

开发与交接见 [docs/README.md](docs/README.md)。
