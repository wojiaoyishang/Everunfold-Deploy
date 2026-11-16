# 不用 Docker 的部署教程

入口仍是仓库根目录 `./setup.sh --native`。统一配置和 Docker 使用同一种结构。

## 准备运行环境

需要 Linux、Git、curl、rsync、Node.js 22（>=22.15.1）、npm 10（>=10.9.2）。
Node 版本要求来自前端锁定项目，勿直接切换其他大版本。
[uv 官方安装方法](https://docs.astral.sh/uv/getting-started/installation/)也可手动使用；没有 uv 时 setup 可按提示安装到本仓库。
Python 3.12 和各版本的虚拟环境由 uv 创建，不修改系统 Python。

PostgreSQL 和 Redis 先由用户准备好。向导负责验证凭据、检查迁移，并按选项初始化空库。

```bash
./setup.sh --native
```

设置两端源码和持久目录、数据库、Redis、公开 URL。原生模式忽略 Docker 网络、UID/GID 和宿主端口映射。
默认进程绑定 `127.0.0.1`，请将两端发布 URL 配置为各自实际的 HTTPS 域名，再由反代连接监听端口。

## 后台运行

“构建并启动”需要 `systemctl --user` 可用。向导会生成：

```text
~/.config/systemd/user/everunfold-<实例标识>.service
```

实例标识由统一配置绝对路径确定。日志统一写入 journal，Worker 默认 WARNING，避免常规任务刷屏。
用户服务要在注销后和开机时持续运行，可由系统管理员执行：

```bash
sudo loginctl enable-linger 部署用户名
```

之后以该部署用户重新登录，再运行向导。没有用户会话时，setup 会提示改用前台 `run`，不会宣称服务已经启动。

```bash
./setup.sh --native build
./setup.sh --native start
./setup.sh logs
./setup.sh stop
```

## 前台运行

```bash
./setup.sh --native build
./setup.sh --native run
```

`run` 检查依赖、配对并同步后端插件清单后，启动 API、Worker、Scheduler 和 Node。Ctrl+C / SIGTERM 统一停止。
任一进程意外退出时，其余进程也停止，避免只剩前端或重复 Scheduler 的半运行状态。
不使用 nohup 隐藏启动错误；需要后台常驻时使用上面的 systemd 用户服务。

## 配置、数据与更新

后端运行代码在 `var/releases/<版本>/backend`，依赖环境为其 `.venv`。
前端已编译产物在同版本 `frontend`，不会在每次启动时重新执行 npm。
前后端各自 `CONFIG_FILE` 指向 `var/runtime/<组件>/config.yaml`，媒体、快照和插件指向向导中各自的数据目录。

更新使用 `./setup.sh --native deploy`；有迁移时先 `build`，备份数据库，再 `migrate` 和 `start`。
自动拉取关闭时使用工作树现状；开启时仅允许干净仓库快进更新。
更改配置后运行 `start` 即可重新启动，无需重新编译。若改了依赖、代码或部署方式，先 `build`。

`API_WORKERS`、`WORKER_POOL`、`WORKER_CONCURRENCY` 和日志等级可以在向导中编辑，保存到 `backend.environment`。
前台 `run` 也读取进程环境覆盖。systemd 服务请通过向导或该用户服务的 Environment 覆盖设置，不依赖交互 shell 导出的变量。

## 代码检查

```bash
uv sync --locked
uv run --locked ruff check deploy tests
uv run --locked pytest -q
```

真实运行验收为可选的 `tests/test_live_runtime.py`：使用临时 PostgreSQL、Redis 和已构建的前端产物，验证空库初始化、自动配对、两端 ready、旧插件指纹恢复和幂等初始化。
仅测试显式提供的临时实例，不连接用户生产服务。所需环境变量见该文件和 [交接状态](../docs/handoff/STATUS.md)。
