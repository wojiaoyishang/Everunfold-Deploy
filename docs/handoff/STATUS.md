# 交接状态

更新时间：2026-11-16。

## 已完成

- 新建统一部署仓库实现，前后端业务仓库无修改。
- 根 setup.sh 彩色 TUI，合并两端配置、自动生成并允许编辑密钥、总览/顺序编辑/单项编辑/复制/保存成功提示。
- 两端源码目录、远程 URL、自动拉取开关和各自持久目录；clone / pull --ff-only，Git 错误停止并交给用户解决。
- Docker 单常驻容器，原生用户级 systemd 或前台 supervisor；API/Worker/Scheduler/Node 同进程管理入口。
- 后端代码快照、前端宿主及插件独立构建、npm verbose/timing 日志和持久下载缓存；重启不编译。
- 保留原密钥的单端配置导入；统一根配置与可写运行配置三方合并。
- 数据库/Redis 检测、空库初始化、显式迁移、自动登记/启用节点和传递来源 UUID；修复跨端凭据手动传递流程的薄弱点。
- 主网加额外网、host.docker.internal、按网络网关信任宿主反代、后台启动就绪等待。
- 根 README 按 Docker/不用 Docker 分开，docker/README.md 和 deploy/README.md 提供完整教程。
- 修复构建成功后旧容器检查出现 `KeyError: 'Config'`：停止旧容器和查询临时容器退出码均使用 `docker container inspect`，避免通用查询匹配同名网络/卷；容器详情校验失败、daemon/权限错误显示明确提示，仅明确不存在的容器可以跳过。
- 停止/删除使用检查过所有权的完整容器 ID，避免同名容器被替换后的竞态；成功构建后用 `start` 继续，不拉取源码或再次执行 npm。

## 验证

- 本地 Node 22.15.1/npm 10.9.2 执行真实宿主生产构建及 6 个插件构建成功。没有修改两个来源仓库。
- 临时 PostgreSQL 17、Redis：真实初始化空库、创建管理员、注册插件、登记并启用 web-main、前端获得来源 UUID，两端 /health/ready 通过。
- 同一组 API/Worker/Scheduler/Node 完整停止后重启，两个 ready 再次通过，未重新构建；没有 inventory_token_missing。
- 再次初始化跳过已有库，保留账号和数据。
- TUI 132×44 / 86×28 截图已在内置浏览器检查；源码/数据目录编辑弹窗、颜色、保存成功和留在页面符合预期。
- 常规 pytest：45 通过、1 个外部服务集成用例默认跳过。前次完整部署集成用例另行启用实测通过，本次仅修改 Docker CLI 检查逻辑，未重复执行应用构建与服务集成。Ruff 检查通过。
- 本次新增回归覆盖同名网桥且无容器、明确不存在、daemon/权限错误、异常 JSON/缺少 Config、无效标签/ID、拒绝操作其他部署的容器、按已校验 ID 停止/删除，以及 start 复用构建产物。Docker 返回通过测试替身复现，本地仍没有可用 daemon，未声称实机容器验收通过。
- pytest 覆盖实际 Git clone/快进更新/脏树拒绝/分叉拒绝、配置冲突/多次保存/浏览器回写/自定义目录旧配置导入、Docker 多网顺序/退出码/容器所有权、挂载、进程退出清理、真实后端配置加载。

## 限制与验收待项

- 当前编码环境没有 Docker Engine/daemon，尚未完成真实 Docker 镜像构建和容器运行验收；已验证命令生成、网络附加次序和退出码处理。
- 当前环境没有可用 systemd 用户会话；真实多进程通过前台 supervisor 验收，用户级 service 安装启动需在目标 Linux 会话验收。
- 不连接用户生产服务、不变更线上容器。本轮只对隔离临时数据运行测试。
- 第一次上线前由用户推送新仓库，然后在目标服务器依 Docker/原生教程执行。

## 可选集成测试

准备一个通过原生 build 生成的测试目录，设置以下变量再运行 `uv run pytest tests/test_live_runtime.py -q -s`：

- EVERUNFOLD_TEST_BUILD_ROOT：包含 Deploy/config.yaml 和 Deploy/var/releases 的隔离测试根目录。
- EVERUNFOLD_TEST_PG_BIN / EVERUNFOLD_TEST_PG_SHARE：PostgreSQL 二进制与 share 路径。
- EVERUNFOLD_TEST_REDIS：redis-server 绝对路径。
- PATH 包含 Node 22；动态库通过 LD_LIBRARY_PATH 配置（仅在解包工具测试时需要）。

测试会改写这个显式提供的测试配置，只能传临时测试实例。每轮 PostgreSQL/Redis 均创建在 pytest 临时目录并在结束时关闭。
