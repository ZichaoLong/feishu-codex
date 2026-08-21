# Python 依赖锁与安装可复现性决策

文档角色：中文规范源。英文同步副本：`docs/decisions/python-dependency-locking.md`。

## 1. 要解决的问题

Focus 同时有三类 Python 依赖：项目运行依赖、安装时使用的 build tooling，以及只在开发和 CI
中使用的工具。过去的 `requirements-dev.txt` 又抄了一份运行依赖，安装器则单独写死
`setuptools<81` 与 `wheel`。这种形状存在两个问题：

- 同一个直接依赖在 `pyproject.toml`、开发 requirements 和安装器中可能独立漂移；
- 只有直接依赖范围，没有提交 transitive resolution，CI 与实际安装可能在不同时间解析出不同版本。

本决策不改变项目只能通过 `bash install.sh` / `./install.ps1` 安装或修复的纪律，也不允许开发者
在当前 Python / Conda 环境执行 `pip install .` 或 `pip install -e .`。

## 2. 事实源与生成投影

每类依赖只有一个直接声明事实源，lock 文件只是可重新生成、必须提交的解析投影：

| 层 | 事实源 | 职责 |
| --- | --- | --- |
| 项目运行依赖 | `pyproject.toml` 的 `project.dependencies` | 定义 Focus 支持的直接运行依赖范围；其他输入文件不得再抄一份 |
| build tooling | `requirements-build.in` | 定义受管虚拟环境中构建 Focus 所需的 `setuptools` / `wheel` 范围 |
| 开发与 CI 工具 | `requirements-dev.in` | 只定义 pytest、Ruff 及其平台补充，不重复运行依赖 |
| 安装解析 | `requirements.lock` | 由运行依赖与 build tooling 共同生成的 Python 3.11+ universal 版本投影 |
| 开发解析 | `requirements-dev.lock` | 由运行依赖、build tooling 与开发工具共同生成的 Python 3.11+ universal 版本投影 |

`requirements-dev.txt` 因此被删除。修改直接运行依赖时只改 `pyproject.toml`；修改 build 或开发工具时
只改对应 `.in` 文件。

## 3. 生成与升级合同

lock 只能通过以下仓库入口生成：

```bash
bash scripts/lock-python-dependencies.sh
```

生成器固定使用 `uv 0.8.14`、Python 3.11 resolution lower bound 与 `--universal`。默认模式把已有
lock 中的 pins 作为解析偏好：它用于重放当前 resolution、吸收直接约束的必要变化，并供 CI 检查
生成结果是否与提交内容一致；它不会主动把所有包升级到最新版本。

全量依赖升级必须显式执行：

```bash
bash scripts/lock-python-dependencies.sh --upgrade
```

`--upgrade` 会忽略已有 output pins。升级后的两个 lock 必须作为一个整体审查，不能只接受测试通过而
忽略 transitive diff。脚本拒绝其他参数，并把稳定的仓库入口写入生成文件头，避免默认模式与升级模式
仅因命令行头不同而产生伪漂移。

`uv` 只是生成工具，不是 Focus 的运行依赖。开发机缺少精确版本时，脚本会在写 lock 前失败；CI 负责
安装同一精确版本并执行默认模式。

## 4. 安装与 CI 边界

`install.py` 不再从 checkout 构建或安装源码。它先按
[安装制品交付合同](../contracts/install-artifact-delivery.zh-CN.md) 解析 remote channel 或本地 artifact，
并在修改 service 或受管 `.venv` 前完整验证 bundle、其中的 Focus wheel 与 `requirements.lock`。进入受管事务后它：

1. 按需建立带 pip 的 CPython 3.11+ `.venv`；
2. 以 bundle 内 lock 作为 constraint，对同一个已验证 wheel 执行 `--force-reinstall`；pip 从 wheel metadata
   读取 Focus 运行依赖，并在该 lock 下解析和安装；
3. 以隔离模式调用已安装的管理入口刷新 wrapper 与 service，使 checkout 不能 shadow 刚安装的 package。

Focus wheel 只在 bundle build 阶段生成。制品构建器使用一次性临时 build / egg-info root，并要求 wheel 中完整
`bot/` 路径和字节与 setuptools source manifest 精确一致；checkout 中被忽略的 `build/` 与 `*.egg-info/`
不参与该产物。clean-build child 关闭 setuptools user config，并把 build、bdist 与 egg-info 的 path-bearing
staging 目录固定到同一临时 root；它不删除或改写 checkout-local cache。该 child 还固定使用 ZIP 时间戳下限
（`SOURCE_DATE_EPOCH=315532800`）作为 Focus-owned 规范化值，而不继承调用者时钟或可复现策略；这只改变
archive metadata，payload authority 仍是 source manifest 中的路径与字节。安装器只消费 bundle 中已经验证的
wheel，不保留第二条源码安装路径。

公开安装入口的 bootstrap Python 必须是带 `venv` / `ensurepip` 的 CPython 3.11+。调用者的系统
Python / Conda 环境不需要预装 pip package、Focus 运行依赖、`setuptools` 或 `wheel`；安装器在机器级
FOCUS data root 的固定 `.venv` 中引导 pip 并安装这些内容。复用该环境前，安装器会探测其中的解释器；
若它是旧版 Python 或非 CPython，就用本次选定的 CPython 3.11+ 自动重建，而不是把不兼容拖到 pip 阶段并
误报成 index 故障。Unix 入口会 probe 候选解释器的实现与版本，
也接受 `FOCUS_INSTALL_PYTHON=/path/to/python` 显式指定。若 `venv` 创建阶段的 ensurepip subprocess
失败，安装器会提示 Debian/Ubuntu 常见的 `python3-venv` / versioned venv package；权限、磁盘空间等
文件系统错误保持原始异常，不伪装成缺少 ensurepip。

`bot/public_command_contract.py` 是四个稳定公开入口（`focus`、`focusd`、`focusctl`、`fcodex`）的命令名与
Python module catalog。bootstrap 只从这里生成 wrapper，contract test 要求 `pyproject.toml` 的
console-script 投影与它精确一致。

这仍可能是联网安装路径：remote channel 先访问 GitHub；即使使用不访问 GitHub 的 `--artifact`，pip 仍可能
需要访问其默认或用户显式配置的 package index。安装器不会在失败后静默追加
另一个 index；index 选择属于 package supply-chain authority，跨到另一来源可能违反私有仓库边界，或为
同一版本选择不同 artifact。用户应修复已配置的来源、网络、证书或本地缓存后重试。bundle 对 Focus wheel、
dependency lock 与整个 ZIP 有 SHA-256，但没有内置第三方 wheelhouse 或第三方 artifact hash，因此当前不承诺
完整离线安装。更完整的来源、proxy 与验证边界由
[安装制品交付合同](../contracts/install-artifact-delivery.zh-CN.md) 持有。

Python package 安装完成后，bootstrap 还会刷新操作系统 service 定义。Linux 会执行
`systemctl --user daemon-reload`，因此要求可用的 user manager/bus；macOS 写入 launchd definition；
Windows 通过 Task Scheduler 注册固定名称的任务。这些是安装环境能力，不是 Python package 依赖。

安装、卸载与 purge 共用一份机器级互斥锁。首次安装没有运行实例；后续 repair/upgrade 会先检查所有已知实例：
任一实例存在 active/submission turn、pending approval/input、非 idle loaded thread，或运行状态无法可靠核验时，
在修改 `.venv`、wrapper 或 service definition 前拒绝，且不提供 force。运行但 idle 的实例会先关闭新 ingress 并
停止；成功后只恢复这些原 running 实例，原 stopped 实例与 autostart 设置不变。

单独执行一次 `service/status` 再停服不能满足这个合同：两步之间仍可能进入新 prompt。运行进程因此由
`ServiceRuntimeLifecycle` 持有一个仅存在于当前进程的 offline-maintenance admission：它先原子关闭 external
ingress，再复用 backend-reset preview 做最终 idle 核验。核验失败或另一实例拒绝时恢复 ingress；成功后只等待
service manager 停服。这个标志不是 durable 安装状态，也不复制 loaded-thread、pending request 或 backend
status；进程退出就是它的最终清理边界。

这不是热升级，也不建立多代虚拟环境或回滚状态机。固定 `.venv` repair 失败时明确返回非零，已停止实例保持
stopped；操作者修复 index、网络、证书、权限、磁盘或缓存后重跑同一脚本。这个失败边界避免运行进程继续消费
正在改写的环境，同时保留一条简单、可重复的 repair 路径。

`uninstall` 与 `purge` 复用同一事务，但成功后不恢复 service。前者删除 service/wrapper/completion 与受管 `.venv`，
保留 matching marker 证明的 config 和其他 data；后者删除两个 matching managed roots。现有 data root 没有 matching
marker 时必须先 repair，卸载器不会猜测目录 ownership。Windows 的自删除 helper 使用同一 lock owner 的 handoff
barrier：父进程仍持主锁时，helper 先取得 barrier 并返回 matching armed proof，父进程随后才释放主锁。父命令只输出
helper PID 与 result path；最终是否删除成功以 helper 写出的逐 target result 为准。

Focus Web 的 production assets 是 ignored 生成物，不再提交到 Git。bundle 构建前必须先生成它们；经过
payload 核验后，它们只作为 Focus wheel 的 package data 交付。最终用户安装和运行 Web UI 不需要 Node.js。
Node.js 只属于 Web 开发/重建工具链，或用户自己选择 npm 版 Codex
CLI 时该上游分发的运行前置。用户也可安装上游 standalone 原生 Codex binary，此路径不要求 Node.js；
Focus 只消费已有 Codex CLI，不替用户安装它。

constraint 本身不会触发包安装，因此构建环境仍必须从 lock 显式安装 build tooling；安装时的运行依赖由 wheel
metadata 投影 `pyproject.toml`，而不是复制到第二份 requirements 输入。这保持了 `pyproject.toml` 对运行依赖的
单一事实源，同时让实际安装消费提交的版本集合。

CI 的 Python 主任务先用默认模式重生成两个 lock，再要求 `git diff` 为空，随后从
`requirements-dev.lock` 安装测试环境。macOS / Windows 合同任务也消费同一个 universal dev lock，
但不重复生成它。需要证明可安装制品时，CI 另行生成 Web production assets，再构建并验证 bundle；普通验证构建
不等于发布。

## 5. 可靠性边界

当前 lock 提供的是“包名、版本、环境 marker 与 dependency graph 的可审查固定”，不是完整的软件供应链
不可变保证：

- lock 未包含第三方 artifact hash；相同版本仍可能按平台取得不同 wheel；
- universal resolution 表示从 Python 3.11 起的 marker/版本解，不证明每个平台的每个 artifact 永远可取；
- package index、TLS、证书和 artifact 可用性仍由 pip / uv 与配置的 index 信任边界负责；
- `pyproject.toml`、`.in` 和 lock 的一致性由生成器与 CI 检查，不能仅靠手工查看包名集合证明。

如果未来要求 artifact 级可重复或离线安装，应单独设计 hashes、wheelhouse、签名与平台矩阵；不能把当前
版本 lock 描述成已经提供了这些能力。

## 6. 维护流程

依赖变更应遵循一条路径：

1. 只修改所属层的直接声明事实源；
2. 普通约束调整运行默认生成命令；计划性全量升级才使用 `--upgrade`；
3. 审查两个 lock 的新增、移除、版本、marker 与来源注释；
4. 运行安装合同、全量 Python 测试和文档检查；
5. 不手工修补 lock 中的单个 transitive pin，也不重新引入重复运行依赖清单。
