# 飞书卡片文本投影与对等解析边界

英文原文：`docs/decisions/feishu-card-text-projection.md`

另见：

- `docs/architecture/feishu-codex-design.zh-CN.md`：当前架构与模块边界
- `docs/contracts/feishu-thread-lifecycle.zh-CN.md`：执行卡生命周期与终态收口
- `docs/decisions/feishu-attachment-ingress.zh-CN.md`：附件入口与本地暂存边界

## 1. 问题陈述

用户希望在飞书里接收和转发卡片消息时，`feishu-codex` 至少能稳定拿到真正有用的文本语义，尤其是：

- `feishu-codex` 自己发送的终态执行结果
- 其他普通消息卡片里对人可见、对 Codex 有意义的文本

但当前前提是：

- 飞书侧没有被本仓库视为“完整、稳定、可直接依赖”的卡片 AST 合同
- 本仓库当前 execution card 首先是用户可见 UI，不是交换格式
- 当前执行卡里的 `process log`、`reply segments`、`final reply` 还没有在发送侧被收紧为三份正式合同

如果继续沿着“收到 `interactive` 后尽量猜一段文本”的路径扩展，会带来：

- 行为边界模糊
- 失败时难以定位
- 卡片 UI 结构和交换语义耦合
- 第三方复杂卡片被误判成“已支持”

因此，这里需要定义一条更窄、更稳定的卡片文本边界。

## 2. 决策摘要

本仓库对飞书卡片文本处理的设计决策是：

1. 本仓库只承诺**卡片文本投影**，不承诺卡片 UI / 动作 / 状态的完整对等解析。
2. `feishu-codex` 自身运行中的 live execution card 仍以人类可读 UI 为主，不直接作为强合同的 round-trip 载体。
3. 只有 turn 终态结果进入强合同，并且必须存在一份权威的 `final_reply_text` 表示。
4. 发送侧可以继续保留：
   - `process_log`
   - `reply_segments`
   但这两者属于 display-only 信息，不属于强合同的交换语义。
5. 终态强合同的常态载体应是卡片，而不是额外的大段普通文本。
6. 接收侧强合同只解析 `final_reply_text`。
7. 接收侧对 `process_log`、`reply_segments`、普通外部卡片文本做 best-effort 提取；提取失败不影响主流程。
8. 对审批卡、表单卡、动态卡等强状态 / 强动作耦合卡片，不承诺支持。
9. 如果终态回复无法无损落入可接受的卡片预算，才允许降级为普通文本；不应继续发送“部分可见、但无法可靠 round-trip”的终态卡。

## 3. 为什么要这样收紧

这条边界的核心目的是把“给人看”和“给另一个智能体消费”拆开。

拆开后的责任如下：

- execution card
  - 面向人类阅读
  - 可包含过程日志、工作痕迹、阶段性回复
- `final_reply_text`
  - 面向接收侧强合同
  - 必须是完整、明确、可直接交给 Codex 的最终文本语义
- 外部普通卡片文本提取
  - 只提供 best-effort 文本补充
  - 不假装恢复原始卡片的按钮、表单或状态语义

这样做的好处是：

- 合同更清晰：真正需要 round-trip 的只有终态结果
- fail-closed 更自然：拿不到 `final_reply_text` 就明确降级，而不是猜
- 维护成本更低：不需要实现通用卡片 AST 解释器
- 更符合本仓库的设计倾向：显式合同、单一路径、明确边界

## 4. 正式术语

### 4.1 `process_log`

`process_log` 指面向人类回溯的过程日志，包括但不限于：

- 命令执行
- 命令输出片段
- 文件修改摘要
- MCP / web / image 等工具调用痕迹
- 运行中附加说明

它对应当前 execution transcript 里的 `process_blocks` / `process_text()` 语义层。

### 4.2 `reply_segments`

`reply_segments` 指 assistant 在执行过程中产生的一系列阶段性文本段，包括但不限于：

- “我正在查看 ...”
- “我现在准备修改 ...”
- “还需要注意 ...”
- 中途阶段性总结

它对应当前 execution transcript 里的 `reply_segments` 语义层。

### 4.3 `final_reply_text`

`final_reply_text` 指 turn 终态时，应该被另一个智能体稳定消费的最终文本结果。

这里的关键要求是：

- 它应优先对应目标 turn 里**最后一个文本型 `agentMessage`**
- 它不是“猜测最后一个可见 segment”
- 也不是“把回复面板所有段落随便拼一遍”
- 它必须是发送侧明确给出的权威结果表示

### 4.4 `terminal execution card`

`terminal execution card` 指同一张 execution card 在 turn 结束后的终态形态。

它仍然可以继续承担：

- `process_log`
- `reply_segments`
- 终态视觉收口

如果该卡片里额外包含专门的 `final_reply_text` 区块，那么它也可以成为强合同载体。

### 4.5 `terminal result card`

`terminal result card` 指一张**专门设计为终态结果载体**的独立卡片。

它与 `terminal execution card` 的区别是：

- `terminal execution card` 延续现有执行卡的生命周期
- `terminal result card` 只承担终态结果表达，不承担运行中执行 UI

## 5. 发送侧合同

### 5.1 live execution card 继续保留回溯体验

在 turn 运行过程中，发送侧可以继续维护当前 execution card 体验：

- 单独展示 `process_log`
- 单独展示 `reply_segments`
- 运行中可带取消按钮

这些内容主要服务人类用户，不要求接收侧严格对等解析。

### 5.2 终态结果必须提供权威 `final_reply_text`

当 turn 结束时，发送侧必须额外提供一份权威 `final_reply_text` 表示。

这里允许两种常态正式形态：

1. `terminal execution card`
   - 现有 execution card 在终态 patch 后携带专门的 `final_reply_text` 语义区块
   - 同一张卡既保留 execution UI，又承担终态结果强合同
2. `terminal result card`
   - 单独发送一张专门的终态结果卡
   - execution card 可以继续只承担 `process_log` / `reply_segments` 等 display-only 内容
   - 终态结果卡上的 `final_reply_text` 区块承担强合同

只有在上述两种卡片载体都无法无损承载结果时，才允许使用降级形态：

- `terminal result text`
  - 直接发送普通文本消息
  - 文本内容就是权威 `final_reply_text`
  - 这是一种溢出 / 失败兜底，不是常态推荐路径

对当前第一阶段 rollout，推荐把发送侧行为进一步收紧为：

- 终态权威结果优先走单独的 `terminal result card`
- 当前卡片标题合同固定为：
  - execution card：`Codex 执行过程`
  - terminal result card：`Codex`
- `terminal result card` 发送成功后，如果终态 snapshot 能明确定位最后一个文本型 `agentMessage`，则旧 execution card 的 reply 面板应去掉这最后一段
- 旧 execution card 只继续保留 `process_log` 与更早的过程性 `reply_segments`
- 如果剔除后旧 execution card 已经没有任何过程日志或过程性回复可展示，则应直接删除旧 execution card，而不是保留空壳卡片
- 如果只能回退到本地 transcript，或者终态结果载体发送失败，则不要剔除 execution card 里的最终回复

### 5.3 终态超长时优先发文本，不发“部分终态卡”

如果 `final_reply_text` 无法在卡片预算内完整表达：

- 必须降级为普通文本
- 不应继续发送只包含部分最终结果的终态卡来承担强合同

原因：

- 文本天然更适合作为完整结果载体
- 这能避免“卡片显示了一部分，但接收侧误以为拿到了完整终态结果”

### 5.4 `process_log` 与 `reply_segments` 保持 display-only

即使终态结果里继续保留：

- 过程日志面板
- 回复分段面板

它们也只属于 display-only 信息。

更具体地说：

- 一旦权威终态结果已经通过 `terminal result card` 或降级文本成功送达，execution card 里的 `reply_segments` 应尽量只保留过程性分段
- 如果终态 snapshot 能区分“最后一段最终答案”和“更早的阶段性回复”，则应把最后那段最终答案从 execution card 中剔除
- 如果当前只能依赖本地 transcript，无法可靠区分最后一段，则宁可保留 execution card 原文，也不要冒“把唯一可见结果删掉”的风险

本合同明确不要求：

- 接收侧完整恢复这些面板
- 接收侧按原顺序重建 UI
- 接收侧把这些信息视作强语义输入

## 6. 接收侧合同

### 6.1 强合同：只解析 `final_reply_text`

接收侧的正式成功条件只有一个：

- 稳定拿到权威 `final_reply_text`

收到后，应把它当作卡片消息的主文本结果交给 Codex。

当前第一阶段强合同对 `terminal result card` 的识别条件进一步固定为：

- header 标题为 `Codex`
- header template 为 `green`
- 卡片内至少存在一个 markdown 区块，其内容末尾携带一段不可见 marker

接收侧对这个 markdown 区块的解释是：

- 用户可见部分，就是权威 `final_reply_text`
- 不可见 marker 只用于声明“这是一张权威 terminal result card”

也就是说，接收侧强合同不再依赖任何额外的说明性提示文案，也不把用户可见提示文案本身当作合同。

### 6.2 best-effort：`process_log` 与 `reply_segments`

如果接收侧还能稳定提取到：

- `process_log`
- `reply_segments`

可以把这些信息作为补充上下文附带给 Codex。

但这些提取属于 best-effort：

- 提不到不报错
- 提错风险高的情况下宁可放弃
- 不纳入 fail-closed 判定

### 6.3 外部普通卡片：只提取有效文本

对于其他普通消息卡片，接收侧只做有限文本提取。

设计目标不是恢复原卡片，而是尽量提取对 Codex 有意义的有效文本，例如：

- 标题
- 普通文本
- 明显可见的 markdown / plain_text 内容
- 简单说明段落

这些文本提取的主要价值是：

- 让 Codex 继续利用自己的理解能力
- 避免本仓库自己维护复杂的卡片语义解释器

### 6.4 外部复杂卡片：明确不承诺

下列类型不进入正式支持合同：

- 审批卡
- 交互表单卡
- 动态数据驱动卡
- 强按钮语义卡
- 高度依赖后端状态的业务卡

对这些卡片：

- 不承诺可读
- 不承诺可 round-trip
- 不承诺能恢复其真实业务语义

## 7. 哪些场景属于 round-trip，哪些不属于

### 7.1 属于强合同 round-trip 的场景

- `feishu-codex` 自己发送的终态结果，且存在权威 `final_reply_text`
- 权威结果由下列任一载体明确给出：
  - `terminal execution card` 中的专用结果区块
  - `terminal result card` 中的专用结果区块
  - 仅在超长降级场景下，由 `terminal result text` 给出

### 7.2 不属于强合同 round-trip 的场景

- 运行中的 execution card
- 只靠当前回复面板去猜“最后一段就是最终回复”
- 只靠颜色、按钮、折叠状态推断语义
- 审批 / 表单 / 动态卡片

## 8. 架构边界

实现上应把卡片文本处理视为单独边界，而不是继续散落在各处特判里。

理想边界是：

- 发送侧
  - 明确生成 `final_reply_text`
  - 优先选择合适的终态卡片载体
  - 仅在卡片无法无损承载时，降级为 plain text
- 接收侧
  - 优先识别和提取权威 `final_reply_text`
  - 对 display-only 内容做 best-effort 补充
  - 对普通外部卡片做有限文本提取

不应继续依赖：

- “收到 `interactive` 就尽量拼一段文本”
- “把 execution card 当前可见文案当作天然交换格式”

## 9. 明确不做的事

在没有稳定完整卡片 AST 之前，本仓库不应默认实现：

- 通用卡片 UI 复原
- 按钮动作语义恢复
- 表单字段状态恢复
- 动态卡片数据重放
- 审批上下文恢复
- 任意第三方卡片的完整对等解析

## 10. 验证口径

后续如果实现该能力，至少要验证：

1. 终态短回复：
   - 发送侧能通过终态卡片给出权威 `final_reply_text`
   - 接收侧稳定拿到完整结果
2. 终态超长回复：
   - 发送侧直接走普通文本
   - 接收侧仍能稳定拿到完整结果
3. 运行中 execution card：
   - 人类可继续看到 `process_log` 和 `reply_segments`
   - 接收侧不把它误判成强合同终态结果
4. 外部普通卡片：
   - 可提取明显有效文本
   - 提取失败时不影响主流程
5. 外部复杂卡片：
   - 明确落到 unsupported / ignored，而不是假装已支持

## 11. 建议实现方案

本节不是新增合同，而是推荐的落地顺序与实现形状。

### 11.1 推荐先做“最小可靠闭环”

建议第一阶段优先做下面这条最小可靠闭环：

1. 发送侧继续保留当前 live execution card：
   - `process_log`
   - `reply_segments`
   - 运行中按钮
2. turn 终态时，额外发送一张**权威 `terminal result card`**，其中包含专门的 `final_reply_text` 区块
3. 接收侧强合同优先消费这张终态结果卡
4. 如果终态结果载体发送成功，且 snapshot 能区分最后一段最终答案，则回写旧 execution card，移除这最后一段
5. 只有当终态结果卡也无法无损承载结果时，才降级为普通文本
6. 普通卡片文本提取作为独立的 best-effort 能力后续补上

这样做的原因是：

- 不需要先把当前 execution card 改造成严格交换格式
- execution card 和强合同结果卡职责分离，边界最清楚
- 仍然保持飞书里的可视化体验，不需要常态额外发一大段普通文本

如果后续确认“同一张终态 execution card 携带权威结果区块”的模板也足够稳定，再把 `terminal execution card` 作为备选方案评估；它不属于当前第一阶段 rollout 目标。

### 11.2 发送侧建议

发送侧建议把“人类可读 UI”和“权威终态结果载体”拆成两条明确路径。

推荐顺序：

1. 保持当前 live execution card 更新机制不变
2. turn 终态时，从权威 turn 数据构造 `final_reply_text`
3. 如果 `final_reply_text` 非空：
   - 优先发送 `terminal result card`
   - 让该卡片成为强合同载体
   - 如果引用回复发送失败，再回退到直接发送一张终态结果卡
4. 如果终态结果载体发送成功，且终态 snapshot 能明确定位最后一个文本型 `agentMessage`：
   - 回写旧 execution card
   - 只保留更早的 `reply_segments`
   - 去掉最后那段最终答案
5. execution card 继续保留：
   - 终态视觉收口
   - 过程日志
   - 回复分段
6. 如果终态结果载体发送失败，或只能回退到本地 transcript：
   - 不要删除 execution card 里的最终回复
   - 以 fail-closed 为先，避免结果丢失
7. 只有在卡片预算不足时，才降级为普通文本

这里最重要的实现建议是：

- **不要从“当前卡片里显示了什么”反推 `final_reply_text`**
- `final_reply_text` 应从 turn 终态时的权威数据源单独生成

### 11.3 `final_reply_text` 的建议来源

如果上游没有单独提供“final answer”字段，建议按下面顺序取值：

1. 终态 thread snapshot 中目标 turn 的最后一个文本型 `agentMessage`
2. 同一 turn 中更早出现的文本型 `agentMessage` 继续保留在 execution card 的 `reply_segments`，仅作为 display-only 的阶段性回复
3. 只有在 snapshot 不可得或数据残缺时，才回退到本地 transcript 的归并结果

这里的关键点是：

- 优先依赖终态 snapshot / turn items
- 不优先依赖 live card 的显示内容
- 不把“最后一个可见 reply segment”当作天然可靠来源
- 一旦 later reconcile 拿到和此前不同的权威 `final_reply_text`，发送侧应再次发出更正后的终态结果载体，而不能只修 execution card

当前代码里的现有能力表明，这条路径是可行的：

- `snapshot_reply()` 已能从 thread snapshot 中读取 turn items、完整 reply 文本和最后一个文本型 `agentMessage`
- `ExecutionTranscript` 已有本地 reply / process 两条通道

但后续实现时，建议把“终态权威文本”升级成显式字段，而不是继续隐含在 `reply_segments` 语义里。

### 11.4 接收侧建议

接收侧建议拆成两个明确阶段：

1. 强合同阶段
   - 只识别并消费发送侧的权威 `final_reply_text`
   - 推荐优先消费 `terminal result card`
   - 如果未来采用同卡方案，则消费 `terminal execution card` 里的专用结果区块
   - 仅在溢出降级时才消费对应的普通文本消息
2. best-effort 阶段
   - 再去解析普通 `interactive` 卡片里的可见文本
   - 解析到多少算多少

这意味着接收侧不应把下面这些路径混在一起：

- 自家终态结果识别
- 普通外部卡片文本提取
- 复杂卡片语义恢复

建议优先级是：

1. 先把自家终态结果打通
2. 再补普通外部卡片的有效文本提取
3. 不进入复杂卡片解析

### 11.5 普通外部卡片的建议提取范围

对外部普通卡片，建议只提取低歧义、明显可见的文本：

- 标题
- 普通文本
- `plain_text`
- `markdown`
- 简单说明段落

建议显式放弃：

- 按钮动作语义
- 表单值
- 审批状态机含义
- 动态卡片数据绑定语义

如果某张卡片只能提取到少量文本，也没有问题：

- 这部分文本本来就是 best-effort
- 主要价值是继续让 Codex 自己理解上下文

### 11.6 建议的模块落点

为了避免继续把逻辑散落在 `_extract_text()` 一类传输层方法里，建议新增一个独立边界模块，例如：

- `bot/card_text_projection.py`

这个边界建议承载两类职责：

1. 发送侧终态结果投影
   - 输入：turn snapshot / runtime transcript
   - 输出：`final_reply_text`、终态卡片载体选择、可选 display-only 补充信息
2. 接收侧卡片文本提取
   - 输入：飞书 `interactive` 消息内容
   - 输出：强合同文本或 best-effort 文本

而现有模块建议保持职责清晰：

- `bot/runtime_card_publisher.py`
  - 继续负责 execution card 的渲染与发送
- `bot/execution_output_controller.py`
  - 增加终态结果卡 / 终态文本兜底的发送编排
- `bot/feishu_bot.py`
  - 只做消息类型分发，不继续承担复杂卡片语义判断

### 11.7 建议的 rollout 顺序

建议按以下顺序 rollout：

1. sender-only：
   - turn 终态额外发送权威 `terminal result card`
   - 先不改外部卡片解析
2. self-consumption：
   - 接收侧优先识别并消费这张终态结果卡
   - 跑通自家结果 round-trip
3. ordinary-card best-effort：
   - 引入普通外部卡片文本提取
   - 但不纳入强合同
4. optional terminal-execution-card：
   - 只有在确认需要、且同卡终态模板足够稳定时，再考虑让 `terminal execution card` 也直接携带可解析的强合同区块
5. overflow fallback：
   - 仅在卡片预算不足时，降级为纯文本

### 11.8 明确不建议的实现方式

不建议：

- 直接把当前 execution card 当成交换格式
- 通过 UI 排版去猜哪一段是最终回复
- 把普通外部卡片与自家终态结果放进同一套解析规则
- 把纯文本兜底误用成常态主路径

当前阶段更应该优先保证：

- 终态结果可靠
- 普通卡片 best-effort
- 复杂卡片 fail-closed
