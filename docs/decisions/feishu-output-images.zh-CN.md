# 飞书出站图片结果边界

文档角色：中文规范源。英文同步副本：`docs/decisions/feishu-output-images.md`。

另见：

- `docs/architecture/focus-design.zh-CN.md`：当前架构与仓库边界
- `docs/contracts/feishu-thread-lifecycle.zh-CN.md`：执行卡与终态结果载体的生命周期规则
- `docs/decisions/feishu-card-text-projection.zh-CN.md`：当前权威终态文本合同
- `docs/decisions/feishu-attachment-ingress.zh-CN.md`：反方向能力，即飞书附件进入 Codex 的边界

## 1. 问题陈述

用户希望 FOCUS 在飞书侧回复时，能同时给出文本与图片。

这里至少有两类完全不同的出站场景：

1. Codex 在 turn 中显式产出了“图片生成”结果。
2. Codex 通过其他路径让某张图片文件出现在本地，例如 shell 下载、网页抓取、MCP 工具、或手工创建。

本仓库现在已经有清晰的终态文本合同，但还没有对应的“出站图片合同”。

如果不先收紧边界，“支持图片回复”会变得很模糊：

- 图片是否应该塞进运行中的执行卡
- 是否应该作为单独的飞书图片消息发送
- 是否所有本地图片文件都该自动发回飞书
- 服务重启 / reconcile 后是否要重发
- 是否要让其他飞书机器人稳定消费这些出站图片

本文就是为此收紧范围。

## 2. 当前事实

### 2.1 当前仓库行为

当前仓库的基线是：

1. 飞书到 Codex 的图片入口已经存在。
   - 飞书 `image` 附件会被下载并暂存到本地。
   - 它既会作为带路径的文本上下文进入 Codex，也会额外作为 `localImage` turn 输入项进入 Codex。
2. Codex 到飞书的图片出站能力现在分成两条路径：
   - 一等能力：显式 `send-image`
   - 附加收益：上游 `imageGeneration`
3. 当前用户可见的 turn 结果路径仍然是纯文本：
   - 运行中的内容进 execution card
   - 权威终态文本进 terminal result card，或回退为普通文本
4. `imageGeneration` 不是唯一出站图片路径；但它仍保留为兼容上游的附加图片来源。

因此，当前真正需要收紧的，不再是“有没有图片能力”，而是：

- 哪条能力是一等合同
- 哪条能力只是兼容上游的附加收益
- 两条路径各自作用在哪个状态层

### 2.2 上游 Codex 事实

上游 Codex 已经提供了一条很有价值的一等图片生成形状：

- turn item 里可能出现 `imageGeneration`
- 该 item 带有：
  - `result`
  - 可选的 `savedPath`
- 上游 core 会尝试把生成出来的图片字节落盘；成功时会写出 `savedPath`
- app-server 的 thread snapshot 也会保留这个 `savedPath`

这里有三个重要推论：

1. `result` 当前确实通常就是图片生成结果的 base64 负载，但本仓库不应把它直接重新定义成自己的通用图片解码合同。
2. `savedPath` 不是对每一个 `imageGeneration` item 都保证存在。
   - 落盘可能失败
   - 负载可能非法
3. 如果要做可靠的出站图片能力，不能只依赖瞬时 notification 流；
   后续还要能从 thread snapshot 中重新读到带 `savedPath` 的 `imageGeneration` item。

### 2.3 飞书平台事实

飞书已经提供了实现可靠“追加式图片投递”所需的基础原语：

- 上传图片资源并获得 `image_key`
- 发送 `msg_type=image` 的图片消息
- 对某条已有消息进行图片引用回复

官方文档：

- 上传图片：
  `https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/image/create`
- 发送消息：
  `https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create`
- 回复消息：
  `https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/reply`

这比把图片强行塞进当前 execution card 的 patch 循环更适合本仓库。

## 3. 为什么现有机制不够

本仓库当前其实已经有两层比较清晰的输出结构：

- execution card
  - 面向人类
  - 可 patch
  - 承担过程日志与阶段性回复 UI
- terminal result carrier
  - 承担权威终态文本合同
  - 负责 `final_reply_text` 的稳定表达

但这两层都不能自然地解决出站图片：

1. execution card 当前是 patch 驱动的文本 UI，不是通用多媒体 transcript 载体。
2. 当前权威终态合同本身就是以文本为中心设计的。
3. 仓库里还没有“某个 turn 的哪些图片已经发给飞书了”的持久化账本。
4. 仓库里也没有对图片结果在 watchdog、reconcile、重启恢复下的正式规则。

所以，图片支持不能被理解成“只要让当前卡片支持图片就行”。那会让文本合同和恢复路径重新变得模糊。

## 4. 第一阶段决策

本仓库当前的出站图片合同应收紧为如下形状。

### 4.1 继续把文本作为唯一权威 round-trip 合同

`final_reply_text` 继续是飞书消息 round-trip、以及下游机器人稳定消费时的唯一权威终态结果合同。

图片只是附加的人类可见产物，不替代 `final_reply_text`，也不进入当前这套强文本投影合同。

### 4.2 一等能力是显式 `send-image`

当前一等能力是显式 `send-image`：

- 正式入口：`focusctl image send --path <file> [--thread-id <id> | --thread-name <name>]`
- 若省略 thread selector，可回落到环境变量 `CODEX_THREAD_ID`
- 作用对象：目标 thread 当前所有 `attached` 的 Feishu bindings
- 投递形状：上传一次，然后以独立飞书 `image` 消息 fanout

这意味着：

- 支持：
  - Codex 明确决定“这张本地图片就是要发到飞书”
  - 在 `focus` / `fcodex`、裸 `codex`、或其他本地入口里，通过同一条控制面动作把图片送到飞书
- 不支持：
  - 自动扫描工作区并猜哪些图片应该发
  - 把图片硬塞进 execution card patch 流

这里的关键不是“图片文件来自哪里”，而是“是否有明确的出站动作”。

### 4.3 `imageGeneration` 仍保留为兼容上游的附加收益

上游 Codex 显式发出的 `imageGeneration` item 仍值得支持，但它不再是本仓库的主合同。

原因是：

- 它依赖上游是否暴露该工具
- 它依赖认证 / provider / model 能力
- 它还依赖模型在该 turn 里是否真的调用这条工具

因此它适合继续作为 bonus path，而不是本仓库唯一、或主要的图片能力。

这意味着：

- 支持：
  - Codex 通过上游图片生成能力产生的图片
- 但这条路径不是“任意本地图片文件”的自动发图语义来源

### 4.4 `imageGeneration` 路径仍以 `savedPath` 作为规范投递输入

第一阶段应把 `savedPath` 视为规范的图片投递输入。

建议规则：

1. 如果某个 `imageGeneration` item 带有非空 `savedPath`，且文件在本地仍存在：
   - 该图片可以进入飞书投递路径
2. 如果 `savedPath` 缺失：
   - 第一阶段不做 best-effort 投递
   - 仍可保留 execution card 里的过程提示，但不要在这里再发明一个自己的 fallback decode 路径

这样职责会更清楚：

- 上游负责图片生成结果的解码与落盘
- 本仓库负责把已经物化到本地的图片工件送到飞书

### 4.5 投递形状：先文本，再 0..N 条图片消息

推荐的飞书侧投递顺序是：

1. 继续发当前已有的权威终态文本载体
2. 然后为同一 turn 追加发送 0..N 条飞书图片回复

这样可以同时获得：

- 稳定的文本语义
- 简单的图片传输
- 不需要重做当前 execution card 的合同

### 4.6 execution card 继续保持文本中心

execution card 可以继续显示 `图片生成` 之类的过程提示，但第一阶段不应把它升级成权威图片载体。

原因：

- 当前 execution card 是 patch 驱动的
- 图片投递天然更适合 append-only
- 如果把终态权威文本与可变图片 UI 混到一张卡里，会明显增加 reconcile、去重和长执行 patch 的复杂度

### 4.7 两条路径的恢复与去重边界不同

`imageGeneration` 补发路径必须能抵抗恢复路径带来的重复投递。

因此实现时应维护持久化的 turn 级投递状态，例如至少按如下维度记账：

- instance
- chat binding
- thread id
- turn id
- image item id

这份状态必须阻止以下场景的重复发图：

- late reconcile
- watchdog recovery
- service restart
- 同一 turn 被重复做 terminal processing

它可以允许一个 fast path：

- 当 `item/completed(imageGeneration)` notification 到来，且其中已经带有可用 `savedPath` 时，立即尝试发图

但 notification 流不能成为唯一依据。恢复路径仍应能通过重新读取 terminal thread snapshot，把还没送达的图片工件补发一次，且只补发一次。

相比之下，显式 `send-image` 是一次明确的人工 / agent 控制面动作：

- 不自动重放
- 不自动 reconcile
- 不承诺自动去重
- 若 fanout 半途失败，允许出现 partial delivery，但 CLI 必须明确报告

## 5. 明确非目标

第一阶段出站图片能力不解决以下问题：

1. “把任意本地图片文件自动发回飞书”
2. “在运行中的 execution card 里持续 patch 出一整套图片画廊”
3. “把飞书出站图片也纳入当前强文本投影合同”
4. “保证另一个飞书机器人也能稳定把这些图片消息当作机器可读输入消费”

最后这一点尤其重要，因为本仓库当前的飞书触发与文本入口模型，本来就是以文本为中心设计的，并没有定义第二套针对 bot-authored 图片回复的强合同。

## 6. 对两类常见用户期待的影响

### 6.1 “Codex 自己生成了一张图”

这类能力适合作为 bonus path 支持，但前提是：

- 上游确实发出了显式 `imageGeneration` item
- 且该 item 有可用的 `savedPath`

### 6.2 “Codex 下载或创建了一张本地图片文件”

这类场景现在可以被一等支持，但前提不是“工作区里有张图”，而是：

- Codex 或操作者显式执行 `send-image`

换句话说，本仓库支持的是“显式发送这张图”，不是“自动发现并发送这张图”。

因此，自动扫描并发送任意本地图片，仍然是明确非目标。

## 7. 当前结论与后续建议

当前已经确定的正式方向是：

1. 保持现有文本终态合同不变
2. 把显式 `send-image` 作为一等图片能力
3. 把 `imageGeneration` 保留为兼容上游的附加收益
4. `imageGeneration` 只投递带 `savedPath` 的结果，并维持 turn 级去重 / 恢复账本
5. 显式 `send-image` 保持 thread-scoped、fail-closed、独立图片消息 fanout 的简单形状

当前仓库已补上一份薄 skill 示例：

- `.agents/skills/feishu-send-image/`

同时，`focusctl skill install` 会把当前受管 skills 一并安装到当前目录
`.agents/skills/` 下，其中包括：

- `feishu-send-image`
- `feishu-scheduled-prompts`

因此它是目录作用域的：

- 在 `~` 下执行时，home 下的 Codex 线程都能发现
- 在某个仓库目录下执行时，只对该仓库生效

它只负责提示 Codex 在需要时调用：

- `focusctl image send --path ...`

而不把核心图片投递逻辑搬进 skill。

后续仍建议继续观察上游 `imageGeneration` / `savedPath` / backend auth 能力的稳定性，但不要把它升级成本仓库主合同。
