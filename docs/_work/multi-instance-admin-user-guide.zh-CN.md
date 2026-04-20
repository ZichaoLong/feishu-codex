# 多实例模式下的管理员与用户使用方式（草案）

> 状态：待审视草案
>
> 说明：本文描述的是目标使用方式，不表示当前代码已全部支持。待设计确认并实现后，再把稳定部分下沉到正式合同与 README。

## 1. 角色划分

本文只区分两类角色：

- **管理员 / 本地操作者**
  - 在本机安装、维护和运行 `feishu-codex`
  - 创建和管理多个 Feishu 实例
  - 决定某个企业实例是否接入某个 thread
- **普通飞书用户**
  - 只在自己所在企业 / 群聊 / 私聊中与对应 bot 交互
  - 不直接接触本地 service 管理细节

这里有一条重要前提：

- 多企业的真实本地操作者通常是同一个人
- 因此本地 `CODEX_HOME` 共享是自然的
- 但 Feishu 运行时与权限面仍按实例隔离

## 2. 管理员的心智模型

管理员需要记住的不是“多套独立本地 Codex”，而是：

- 我有一套共享的本地 Codex 用户空间
- 我同时运营多个 Feishu 实例
- 每个实例有自己的：
  - app 凭证
  - service
  - control plane
  - backend
  - binding / group / ACL 状态
- 这些实例都可能看见同一批 persisted thread
- 但同一时刻，只允许一个实例 backend live attach 某个 thread

## 3. 管理员的目标操作方式

### 3.1 创建实例

管理员为每个企业创建一个实例。

典型目标形态：

- `corp-a`
- `corp-b`

每个实例独立维护：

- `system.yaml`
- `codex.yaml`
- `init.token`
- `FC_DATA_DIR` 下的本地运行态

共享：

- `CODEX_HOME`

### 3.2 启动与停止实例

管理员按实例启动服务。

目标命令面示例：

```bash
feishu-codex --instance corp-a start
feishu-codex --instance corp-b start
feishu-codex --instance corp-a status
feishu-codex --instance corp-b log
```

管理员需要理解：

- `corp-a` 与 `corp-b` 是两条独立的 Feishu service
- 它们各自拥有自己的 backend
- 停掉某个实例，只影响该实例

### 3.3 管理实例内运行态

管理员使用 `feishu-codexctl` 管理某个实例。

目标命令面示例：

```bash
feishu-codexctl --instance corp-a service status
feishu-codexctl --instance corp-a binding list
feishu-codexctl --instance corp-a thread status --thread-id <id>
```

管理员需要理解：

- `feishu-codexctl` 的对象是“某个运行中的 Feishu service”
- 它不是一个脱离实例的全局线程神控台

### 3.4 跨企业复用 thread 的原则

管理员可以决定不同企业实例是否使用同一个 persisted thread。

但应遵守这些原则：

- 这应当是显式决策，而不是隐式串用
- 即使多个实例都能看见同一个 thread，也不表示它们可以同时写
- 某个 thread 当前若已被实例 A live attach，实例 B 只能：
  - 观察
  - 等待
  - 显式接管（若后续设计提供）
  - 或在被拒绝后不做写入

更直白地说：

- **看见同一个 thread，可以接受**
- **同时把它变成两个 live backend runtime，不可以接受**

### 3.5 管理员对普通用户的预期说明

管理员可以对普通用户给出简单说明：

- 在自己所在企业/群里正常使用 bot 即可
- 如果 thread 正在被别的地方执行，系统可能提示当前不可写或需等待
- 如果要在本地继续同一个 live thread，请让管理员或本地操作者使用 `fcodex`
- 不要把裸 `codex` 当作与飞书安全共享 live thread 的默认入口

## 4. 普通飞书用户的目标使用方式

普通用户的目标心智应尽量简单：

- 我只和当前企业里的这个 bot 交互
- 这个 bot 是否能写当前 thread，取决于当前 chat 的 ACL / mode / owner 状态
- 如果系统提示当前 thread 正忙、被占用、或本实例当前不能写，就不要反复并发触发

普通用户不需要理解：

- `CODEX_HOME`
- app-server backend
- control plane
- runtime lease

## 5. 本地操作者使用 `fcodex` 的目标方式

### 5.1 默认原则

`fcodex` 是安全共享 live thread 的本地入口。

正式建议保持不变：

- 如果希望本地与 Feishu 继续同一个 live thread，请使用 `fcodex`
- 不要把裸 `codex` isolated backend 当成共享 live thread 的默认路径

### 5.2 多实例下的目标体验

多实例下，`fcodex` 的目标体验是：

- 常见情况下自动选到正确实例
- 复杂或歧义情况下要求显式指定实例

目标命令面示例：

```bash
fcodex
fcodex resume <thread_id>
fcodex /resume <thread_name>
fcodex --instance corp-a
fcodex --instance corp-b resume <thread_id>
```

### 5.3 自动路由的用户心智

本地操作者可以这样理解：

- 如果系统已经知道这个 thread 当前归哪个实例 live attach，`fcodex` 就直接连过去
- 如果当前只有一个运行中的实例，`fcodex` 就直接用它
- 如果有多个实例而且看不出该进哪一个，就会要求我显式指定实例

用户不应期望：

- `fcodex` 在歧义时替自己“猜”实例
- `fcodex` 自动把裸 `codex` 的 isolated backend 纳入共享 owner 模型

## 6. 裸 `codex` 的建议使用方式

裸 `codex` 仍然可用，但其角色要说清楚：

- 它是上游原生命令面
- 它可以生成和恢复 persisted thread
- 这些 thread 因为共享 `CODEX_HOME`，后续可以被 `fcodex` 发现
- 但裸 `codex` 自己开的 isolated backend，不在 `feishu-codex` 的安全共享 live thread 合同内

因此建议：

- 把裸 `codex` 视为“本地独立使用 Codex”的入口
- 把 `fcodex` 视为“需要和 Feishu 安全共享 live thread”时的入口

## 7. 推荐给管理员的默认操作习惯

建议形成下面这套默认习惯：

1. 每个企业单独建一个实例
2. 每个实例独立配置自己的 `system.yaml`
3. 日常 service 管理一律走 `feishu-codex --instance ...`
4. 日常本地线程管理一律走 `feishu-codexctl --instance ...`
5. 需要与飞书继续同一个 live thread 时，一律走 `fcodex`
6. 不把裸 `codex` concurrent write 当成受支持路径
7. 如果一个 thread 有跨企业复用需求，由管理员显式决策，而不是让普通用户无感串线

## 8. 推荐给普通用户的简单说明

可以把最终用户说明压缩成下面几条：

- 正常在当前企业 / 群里使用 bot
- 如果系统提示 thread 正在执行、等待审批或当前不可写，请等待或联系管理员
- 不要假设不同群/不同企业里的 bot 会自动共享可写上下文
- 如需本地继续同一 live thread，由管理员或本地操作者使用 `fcodex`

## 9. 需要你最后拍板的点

在实现前，建议重点确认下面这些使用侧取舍：

1. 是否接受 `fcodex` 把“多实例”默认隐藏起来，只在歧义时暴露 `--instance`
2. 是否接受 `feishu-codexctl` 继续保持实例级，不做无实例的全局线程管理面
3. 是否接受跨企业复用同一 thread 只作为管理员显式决策，不作为普通用户默认路径
4. 是否接受继续把“裸 `codex` isolated backend 并发写同一 thread”归为文档教育边界
5. 是否接受普通用户在跨实例占用场景下看到明确拒绝/等待提示，而不是系统偷偷排队或自动猜测接管
