# 多实例支持实施计划（草案）

> 状态：已按本文方向完成第一轮实现，后续仍需继续收正式合同文档
>
> 说明：本文档先作为实现前计划，放在 `docs/_work/`。在设计确认并落地后，相关结论应再分别下沉到 `docs/architecture/`、`docs/contracts/`、`docs/decisions/` 与 README。

## 1. 背景与目标

当前 `feishu-codex` 默认围绕单个 Feishu app / 单个实例运行。
新的现实需求是：

- 同一台机器上的同一位本地操作者
- 同时管理多个 Feishu 企业 / 多个 bot app
- 不希望为了“多企业”强制拆成多套完全独立的本地 Codex 用户空间
- 仍希望：
  - 架构清晰
  - 维护简单
  - 行为无歧义
  - 默认路径尽量顺手

本轮设计的核心结论是：

- **`CODEX_HOME` 默认共享**：它代表本地操作者自己的 Codex 用户空间
- **Feishu 实例运行时按实例隔离**：每个实例独立持有配置、数据、service owner、control plane、app-server backend
- **同一 thread 不能被多个 backend 同时写入**：共享持久化 thread namespace，不共享 live backend namespace

## 2. 设计结论

### 2.1 共享什么

下列层面按“本地操作者”共享：

- `CODEX_HOME`
- 上游 `config.toml`
- 上游 auth / history / sessions / skills / model cache
- 裸 `codex` 产生的持久化 thread 元数据与 rollout

共享这些状态的原因是：

- 本地真实操作者通常是同一个人
- 裸 `codex` 与 `fcodex` 的线程发现最好互通
- 不希望因为多企业就复制多套 profile / history / skills / auth
- 这些状态描述的是“本地用户如何使用 Codex”，不是“哪个 Feishu 企业在运行”

### 2.2 隔离什么

下列层面按 Feishu 实例隔离：

- `FC_CONFIG_DIR`
- `FC_DATA_DIR`
- `system.yaml`
- `init.token`
- `codex.yaml`
- chat binding store
- group chat store
- profile state store（实例级的新 thread seed profile 状态；不承载 machine-global 的 thread-wise resume profile）
- service instance lease
- control plane socket
- managed app-server runtime discovery
- Feishu runtime attached/released snapshot
- app-server backend 进程本身

隔离这些状态的原因是：

- 它们描述的是“某个 Feishu bot 实例如何运行”
- 它们直接影响 owner、binding、ACL、群聊上下文与运维控制面
- 如果继续共享，会退化成“单状态空间多机器人”，复杂度和风险都过高

### 2.3 实例可见范围（admission）

共享 `CODEX_HOME` 只表示：

- 多个实例都能在机器级看见同一批 persisted thread

它**不表示**：

- 某个 thread 会自动对所有 Feishu 实例开放
- 任意企业里的普通用户都可以直接 `/resume` 任意共享 thread

因此本轮建议再引入一层**实例可见范围（admission）**：

- 每个实例维护自己的 admitted thread 集合
- 一个 thread 只有被管理员显式导入到某实例后，才进入该实例的 Feishu 可见范围
- Feishu `/session`、Feishu `/resume`、实例内聊天续写，只面向：
  - 当前实例 admitted 的 thread
  - 当前实例已经存在 binding 的 thread
- `fcodex` 作为本地 Codex 使用入口，仍可保留更强的全局发现能力

当前实现再补一条实际取舍：

- **default 实例保留原单实例全局可见行为**
- **只有命名实例（如 `corp-a`、`corp-b`）才默认要求显式 admission**

这样做的原因是：

- 单实例默认路径继续保持顺手
- 多实例扩展时，新增实例才需要显式导入共享 thread
- 管理员心智是“默认实例像以前一样工作；额外企业实例按 admission 收紧”

这样做的原因是：

- 共享 `CODEX_HOME` 解决的是“本地操作者统一使用 Codex”
- admission 解决的是“哪个 Feishu 实例允许暴露/使用哪个 thread”
- 二者不是同一个边界

建议新增每实例本地 store，用于记录 admitted thread 集合。
它应落在实例自己的 `FC_DATA_DIR` 下，而不是落在共享 `CODEX_HOME` 下。

### 2.4 backend 结论

本轮多实例设计中：

- **不同实例，不共享同一个 live app-server backend**
- **每个实例各自管理自己的 backend**
- **`fcodex` 连接的是某一个实例 backend，而不是一个全局共享 backend**

这里必须严格区分两种“共享”：

1. **共享 `CODEX_HOME`**
   - 表示共享持久化 thread namespace 和本地用户态
2. **共享 app-server backend**
   - 表示共享 live thread 内存态、订阅、交互请求、turn 生命周期

本轮只接受第 1 种共享，不接受第 2 种跨实例共享。

### 2.5 安全规则

所有实例统一遵守一条核心规则：

- **一个 thread 在任一时刻只能通过一个 backend 写入**

这条规则是现有 shared-backend 安全模型的直接延伸：

- 实例内：Feishu 与 `fcodex` 可以通过同一个实例 backend 安全共享 live thread
- 实例间：不能让两个实例 backend 同时 live attach 同一 thread
- 裸 `codex` 若自行使用 isolated backend 写同一 thread，仍然不在安全支持路径内

## 3. 目标运行时模型

### 3.1 一实例一运行时空间

目标模型：

- `instance A`
  - `FC_CONFIG_DIR_A`
  - `FC_DATA_DIR_A`
  - `service owner A`
  - `control plane A`
  - `app-server backend A`
- `instance B`
  - `FC_CONFIG_DIR_B`
  - `FC_DATA_DIR_B`
  - `service owner B`
  - `control plane B`
  - `app-server backend B`
- 所有实例共享同一个 `CODEX_HOME`

### 3.2 实例内共享，实例间隔离

在本轮设计里，“shared backend”今后的精确定义应是：

- **实例内共享 backend**
- 不是“全系统所有实例共用一个 backend”

因此：

- 某个实例内的所有飞书会话共享该实例的 backend
- 连接到该实例的 `fcodex` 共享该实例的 backend
- 不同实例之间不共享 live backend

## 4. 新增基础设施

如果共享 `CODEX_HOME`，多个实例都会看见同一批 persisted thread。
因此必须新增跨实例协调层；否则“线程可见性共享”会演变成“线程 live ownership 冲突”。

### 4.1 Global Instance Registry

新增一个机器级全局实例注册表。

建议职责：

- 记录当前有哪些实例正在运行
- 记录每个实例的：
  - `instance_name`
  - `FC_CONFIG_DIR`
  - `FC_DATA_DIR`
  - control endpoint
  - backend URL
  - owner pid / started_at
- 为 `feishu-codexctl` 和 `fcodex` 提供“先找实例，再决定连谁”的统一发现面

建议边界：

- 它只描述实例清单与连接入口
- 它不直接承担 thread owner 逻辑

### 4.2 Global Thread Runtime Lease

新增一个机器级 thread runtime lease 注册层。

建议职责：

- 记录某个 `thread_id` 当前是否已被某实例 backend live attach
- 记录该 attach 归属到哪个实例
- 防止两个实例同时把同一个 thread 恢复成两份 live runtime
- 为“空闲时自动流转、执行中明确拒绝”的跨实例工作流提供统一事实源

建议记录字段：

- `thread_id`
- `owner_instance`
- `owner_service_token`
- `backend_url`
- `attached_at`
- `lease_state`（如 `attached` / `released` / `stale`）

建议边界：

- 它只负责“哪个实例当前持有这个 thread 的 live backend runtime”
- 它不负责飞书 chat 级 binding、群 ACL、审批 owner、interaction owner
- interaction owner 仍然是实例内运行态事实，不升级成全局概念

建议先采用这条流转规则：

- 若 thread 当前未被任何实例 live attach：
  - 第一个命中的 admitted 实例可正常获取 runtime lease 并启动 turn
- 若 thread 当前被实例 A live attach，但没有 running turn、没有待处理审批/补充输入：
  - 实例 B 上的下一条 prompt 可触发**自动流转**
  - 具体表现为：B 请求 A 释放 runtime，A 成功释放后，B 获取 lease 并接管 backend
- 若 thread 当前在实例 A 上仍有 running turn 或待处理交互：
  - 实例 B 上的 prompt 必须 pure reject
  - 初始版本不做隐式排队，也不偷偷强抢
  - 后续如确有必要，再单独设计显式管理员 takeover 命令

## 5. 命令面目标形状

## 5.1 `feishu-codex`

`feishu-codex` 是实例级 service 管理入口。

目标形状：

- `feishu-codex --instance <name> start|stop|restart|status|log|run|config`
- 也可提供：
  - `feishu-codex instance list`
  - `feishu-codex instance create <name>`
  - `feishu-codex instance remove <name>`

建议：

- `systemd --user` 改为 template service，例如 `feishu-codex@<instance>.service`
- 外层命令可以保留自动实例解析，但 service 管理面本质上仍是实例级

## 5.2 `feishu-codexctl`

`feishu-codexctl` 仍应是实例级管理面，不应退化成“全局神控台”。

原因：

- 它管理的是某个运行中的 Feishu service
- binding / thread release / runtime status 都依附于某个实例 backend
- 即使允许自动推断，底层合同也应指向单个实例

目标形状：

- `feishu-codexctl --instance <name> service status`
- `feishu-codexctl --instance <name> binding list`
- `feishu-codexctl --instance <name> thread status --thread-id ...`

## 5.3 `fcodex`

`fcodex` 保持“Codex 使用入口”的定位，但要支持多实例自动路由。

目标原则：

- 常用路径尽量不强迫用户显式写 `--instance`
- 架构上保留 `--instance` 作为消歧参数
- 歧义时 fail-closed，而不是猜

建议目标形状：

- `fcodex [--instance <name>]`
- `fcodex [--instance <name>] <prompt>`
- `fcodex [--instance <name>] resume <thread_id>`
- `fcodex [--instance <name>] resume <thread_id|thread_name>`

自动路由建议顺序：

1. 若显式给了 `--instance`，直接使用
2. 若目标 thread 当前被唯一实例 live attach，则自动路由到该实例
3. 若当前只有一个运行中的实例，则自动路由到该实例
4. 若本地配置存在默认实例，则使用默认实例
5. 否则报歧义错误，要求显式指定实例

## 6. 实施阶段

### Phase 1：实例目录与 service 模板

目标：先把实例级运行时边界搭起来。

工作项：

- 定义实例目录布局
- 引入实例名校验与 layout resolver
- 把 `FC_CONFIG_DIR` / `FC_DATA_DIR` / systemd service name 统一走同一个 resolver
- 安装脚本改为安装 template service
- 为每实例预留 thread admission store
- 保留 `CODEX_HOME` 共享，不在这一阶段拆 home

阶段目标：

- 多实例能独立启动/停止
- 实例内数据互不污染
- 还不要求 `fcodex` 实现完整自动路由

### Phase 2：实例 admission + Global Instance Registry

目标：先收紧“哪个实例允许暴露哪个 thread”，再让本地命令能发现“当前有哪些实例在运行”。

工作项：

- 新增每实例 thread admission store
- 明确 Feishu `/session`、Feishu `/resume` 的实例内可见范围规则
- 新增本地管理入口，用于导入 / 撤销某实例对某 thread 的 admission
- 新增 registry store
- service 启动/停止时注册与注销
- `feishu-codexctl` 支持枚举实例或按实例连接 control plane
- `fcodex` 支持根据 registry 自动发现候选实例

阶段目标：

- 共享 `CODEX_HOME` 不再等于“所有实例对所有 thread 默认可见”
- 本地命令不再只能依赖单个 `FC_DATA_DIR`
- 能明确知道当前系统里有哪些 feishu-codex 实例处于运行状态

### Phase 3：Global Thread Runtime Lease

目标：防止跨实例双 backend 写同一 thread。

工作项：

- 新增 thread runtime lease store
- thread attach / resume / release 时显式写 lease
- service 停止、异常退出、实例重启时清理或恢复 lease
- 歧义路径改为 fail-closed

阶段目标：

- 同一时刻，一个 thread 最多只被一个实例 backend live attach
- 多实例共享 `CODEX_HOME` 时，live runtime 仍然保持单 owner

### Phase 4：`fcodex` 自动路由与实例消歧

目标：让本地使用体验自然，但合同仍清晰。

工作项：

- `fcodex` 支持 `--instance`
- 引入自动路由顺序
- 对裸 `codex` 可发现的 thread 提供“可发现但不承诺 live 安全”的提示语义
- 在歧义时直接报错，不偷偷猜实例

阶段目标：

- 常见使用无需频繁输入 `--instance`
- 复杂场景下仍有显式可控的选择器

### Phase 5：文档、README 与回归测试

目标：把“可实现”收成“可维护的正式合同”。

工作项：

- 把确认后的结论下沉到：
  - `docs/architecture/`
  - `docs/contracts/`
  - `docs/decisions/`
  - `README.md`
- 增加实例相关测试：
  - 同实例双启动 fail-fast
  - 不同实例并行运行
  - 全局 registry 注册/恢复
  - thread runtime lease 接管/释放
  - `fcodex` 自动路由 / 歧义报错

## 7. 明确接受的限制

本轮方案应明确接受这些限制，而不是试图模糊处理：

- 裸 `codex` 使用 isolated backend 与 Feishu 同时写同一 thread，不在安全支持路径内
- `fcodex` 可以发现裸 `codex` 生成的 thread，但不能自动把裸 `codex` 的 live backend 纳入当前 owner 模型
- 同一 thread 允许被多个实例看见，但不允许被多个实例 backend 同时 live attach
- 跨企业共享某个 thread 是管理员/操作者层面的显式决策，不应因为共享 `CODEX_HOME` 就默默自动发生
- 即使多个企业实例都能看见同一 thread，是否允许某个 chat 继续写入，仍取决于该实例内的 ACL / mode / owner 规则

## 8. 推荐审视点

在开始实现前，建议重点确认以下问题：

1. 是否接受“共享 `CODEX_HOME`，但 backend 按实例隔离”作为正式方向
2. 是否接受 `fcodex` 保留 `--instance`，但默认尽量自动路由
3. 是否接受 `feishu-codexctl` 本质上仍是实例级管理面
4. 是否接受必须新增全局 `instance registry` 与 `thread runtime lease`
5. 是否接受把“裸 `codex` isolated backend 并发写同一 thread”继续定义为文档教育边界，而不是试图技术封死
