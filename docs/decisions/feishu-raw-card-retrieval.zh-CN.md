# 飞书原卡查询、JSON 2.0 终态卡与转发读取决策

文档角色：中文规范源。英文同步副本：`docs/decisions/feishu-raw-card-retrieval.md`。

另见：

- `docs/decisions/feishu-card-text-projection.zh-CN.md`：当前 best-effort 文本投影边界
- `docs/architecture/focus-design.zh-CN.md`：当前架构与模块边界
- `docs/contracts/feishu-thread-lifecycle.zh-CN.md`：执行卡与终态收口生命周期
- `docs/doc-index.zh-CN.md`：文档索引

## 1. 问题陈述

用户希望同时满足两个目标：

- 终态卡在飞书里正确显示分级标题、列表、引用、代码、链接等结构
- 终态卡被直接发送、直接转发、或合并转发后，FOCUS 仍能尽可能高保真地读取其内容，而不是退化成纯文本猜测

围绕这个目标，之前的讨论里出现过两个过度简化的判断：

- “JSON 1.0 更适合保真读取，JSON 2.0 只能解决显示”
- “只要收到 merge_forward，就等于直接拿到完整原卡 JSON”

这两种说法都不准确。

按飞书当前官方文档：

- 卡片消息默认返回的是“接收消息结构”，不是发送时的原始卡片 JSON
- 但 `message/get` 与 `message/list` 在带 `card_msg_content_type=user_card_content` 时，可以返回发送时的原始卡片 JSON
- 这条能力同时覆盖卡片 JSON 1.0 与 2.0
- `merge_forward` 外层消息内容固定为 `Merged and Forwarded Message`
- 对 `merge_forward`，应先展开子消息，再对子消息逐条做后续查询

因此，真正需要的不是继续围绕“1.0 还是 2.0”二分争论，而是定义：

- 哪些场景应优先走“按 `message_id` 查询原卡 JSON”
- 哪些场景只能继续走 best-effort 投影
- 如何在重启、转发、跨会话、旧实例残缺日志等情况下，判断本项目到底收到了什么

## 2. 决策摘要

本仓库关于终态卡显示与读取的决策如下：

1. 终态卡使用 **JSON 2.0**。
2. “高保真读取”不再依赖默认事件体或默认历史列表结构，而应优先依赖：
   - 目标消息的 `message_id`
   - `message/get` 或 `message/list`
   - `card_msg_content_type=user_card_content`
3. 读取架构采用三段式：
   - 可按 `message_id` 精确查询：走原卡读取
   - `merge_forward`：先展开子消息，再尽量走原卡读取
   - 其余情况：best-effort 投影
4. `merge_forward` 不是“完整原卡 JSON 本体”，只是“进入子消息展开链路的入口”。
5. 普通转发不承诺保留原始源消息 ID，但若转发后的新消息本身仍为 `interactive`，则仍可能通过这条新消息的 `message_id` 读取其完整卡片 JSON。
6. `/last text` 保留为兜底能力，不再被视为唯一权威路径。
7. 本决策不新增 `/text` 功能；“直接读取转发卡片本身”是主路径。
8. 为支持重启后验证，仓库已提供受严格布尔开关控制的“原始接收观测”能力，
   可明确记录：
   - 原始事件体里收到的 `msg_type`
   - 外层消息 `message_id`
   - 对 `merge_forward` 展开后拿到的子消息 `message_id`
   - 是否拿到了原卡 JSON
   - 最终是走了原卡读取还是投影回退

## 3. 为什么是 JSON 2.0 + 原卡查询

### 3.1 JSON 1.0 的主要问题在显示层

当前项目的终态卡正文路径里，飞书客户端对 JSON 1.0 / markdown 子集的分级标题支持较弱。

这带来两个直接后果：

- `#` / `##` 等层级在用户端显示不理想
- 为适配显示，发送侧不得不做显式 sanitize，从而引入额外的信息折叠

因此，继续坚持 JSON 1.0 的主要收益并不是“天然更保真”，而只是：

- 当前项目已有的 best-effort 文本投影路径对它更熟悉
- 默认历史结构里它更容易被投影成可用文本

这不是长期设计优势。

### 3.2 JSON 2.0 的主要收益在显示与结构表达

JSON 2.0 的强项是：

- 更适合终态结构化表达
- 更有希望正确显示标题层级、列表、引用、代码、链接
- 更适合把“展示正文”和“可机器读取结构”统一到同一份卡片合同中

因此，终态卡主显示路径应优先升级到 JSON 2.0。

### 3.3 读回保真与否，关键不在 1.0/2.0，而在是否查询原卡

如果只靠：

- 接收事件体
- 默认 `message/list`
- 当前 `project_interactive_card_text(...)`

那么不论 1.0 还是 2.0，本质上都还是在吃飞书的“默认回传结构”，属于投影路径，不属于高保真读取。

只有在带 `card_msg_content_type=user_card_content` 时，读取链路才升级为“原卡 JSON 读取”。

这时：

- 1.0 与 2.0 都可以走高保真
- 2.0 也不再天然弱于 1.0

所以真正的分界不是卡片版本，而是：

- 有没有可用的 `message_id`
- 有没有走原卡查询

## 4. 正式术语

### 4.1 默认投影读取

指不额外要求原卡格式，只消费：

- 接收事件里的默认 `content`
- 或 `message/list` / `message/get` 默认返回的卡片结构

再通过本项目的投影逻辑抽取文本。

这是 best-effort，不承诺完整保真。

### 4.2 原卡读取

指对目标消息使用：

- `message/get`
- 或 `message/list`

并显式传入：

- `card_msg_content_type=user_card_content`

从而取得发送时的原始卡片 JSON。

### 4.3 高保真读取

本决策里的“高保真”定义为：

- 终态正文可恢复
- 标题层级可恢复
- 列表、引用、代码、链接等结构可恢复
- 使用的是卡片结构本身，而不是纯文本猜测

这里不要求逐字符还原发送前原始 Markdown 字符串。

### 4.4 普通转发

指飞书“转发消息”生成的一条新消息。

它有自己的 `message_id`。文档没有承诺保留原始源消息 ID。

### 4.5 合并转发 `merge_forward`

指飞书的合并转发消息类型。

它的外层消息内容固定为：

- `Merged and Forwarded Message`

后续应通过查询接口拿到其中的子消息，再对子消息分别处理。

## 5. 官方合同边界

### 5.1 `message/get` 与 `message/list`

飞书官方文档当前明确写明：

- 不传 `card_msg_content_type` 时：
  - 返回默认卡片结构
  - 不支持返回发送时的原始卡片 JSON
- 传入 `user_card_content` 时：
  - 返回发送时的原始卡片 JSON
  - 同时覆盖卡片 1.0 与 2.0

因此，“JSON 2.0 无法被原样读回”的旧判断应视为失效。

### 5.2 `merge_forward`

飞书官方文档当前明确写明：

- 合并转发生成的新消息内容固定为 `Merged and Forwarded Message`
- 其中的子消息可以通过“获取指定消息的内容”接口获取
- 对 `merge_forward` 调 `message/get` 时，返回的 `items` 中会包含：
  - 1 条合并转发外层消息
  - N 条子消息
- 子消息对象有 `message_id`
- 合并转发场景会返回 `upper_message_id`

但文档没有明确承诺：

- 子消息 `message_id` 一定等于“最初源消息的 message_id”
- 所有类型消息在合并转发后都绝不丢信息

因此，本项目只能正式宣称：

- `merge_forward` 提供“子消息可继续查询”的官方路径
- 不能宣称“merge_forward = 绝不丢信息”

### 5.3 普通转发

飞书“转发消息”文档说明的是：

- 调用该接口会生成一条新的消息
- 新消息有自己的 `message_id`
- 新消息类型可以是 `interactive`

但文档没有承诺：

- 会返回原始源消息 ID
- 会附带某种统一的“源消息引用元数据”

因此，普通转发的正式边界是：

- 可能丢失原始源消息 ID
- 但如果转发后的新消息本身仍是 `interactive`，则仍可能通过“新消息自己的 `message_id`”读取到其完整卡片 JSON

## 6. 读取架构决策

### 6.1 总体原则

读取路径不再按“1.0 / 2.0”分支，而按“文本来源权威性与读取保真度”分支：

1. 本地 terminal result store 命中：权威终态文本
2. 可按 `message_id` 查询当前消息的原卡 JSON：raw-card projection
3. 其他情况：payload / best-effort projection

### 6.2 普通 `interactive` 消息

当收到一条普通 `interactive` 消息时：

1. 优先对当前这条消息自己的 `message_id` 调 `message/get`，并设置
   `card_msg_content_type=user_card_content`
2. 若能拿到原卡 JSON，则按本项目卡片协议做 raw-card projection
3. 对新版终态卡，只有 `terminal_result_id` 能在本机器人实例本地 terminal result store
   中命中且 checksum 匹配时，store 正文才是权威文本
4. 对 store miss 的新版终态卡、没有 `result_id` 的历史终态卡、以及其他交互卡片，
   原卡 JSON 只能提供非权威投影
5. 若原卡读取失败，则回退到事件 payload / 默认结构的 best-effort 投影

这里要特别注意：

- 不需要先知道“原始源消息 ID”
- 当前消息自己的 `message_id` 就足够成为高保真读取入口
- 高保真读取不等于权威文本；权威文本只来自本地 store 命中

### 6.3 `merge_forward`

当收到 `merge_forward` 时：

1. 不把外层固定文案当成内容本体
2. 用外层 `message_id` 调 `message/get`
3. 获取其中的子消息列表
4. 对每条子消息分别处理：
   - 若是 `interactive`，再按其 `message_id` 查询原卡 JSON
   - 若是 `text` / `post` 等，走现有文本路径
5. 对本项目终态卡候选子消息，仍按同一套三档合同处理

所以：

- `merge_forward` 不是原卡 JSON
- `merge_forward` 是进入“子消息展开 + 原卡读取”的入口

### 6.4 其他情况

当既没有：

- 普通 `interactive` 的可用原卡读取
- 也没有 `merge_forward` 的子消息展开结果

就只能回退到：

- 当前 payload / best-effort 投影
- 或 `/last text`

## 7. 终态卡协议

### 7.1 单份权威正文

当前 JSON 2.0 terminal result card 的正式合同是：

- 只保留单份权威正文
- 不再为“怕丢语义”额外放一份相同正文的隐藏副本

原因：

- 如果原卡查询能力可用，卡片正文可作为高保真 projection 输入；是否权威仍取决于
  本地 terminal result store 是否命中
- 双份正文只是在默认投影链路受限时的补偿手段

### 7.2 结构化正文块

终态卡使用一个稳定、可定位的正文块位，用于：

- 用户显示
- 机器读取

正式要求：

- 终态正文必须位于一个固定的 rich text / content block
- 解析器只认这一个块位
- 标题、列表、引用、代码、链接等结构都从该块位恢复

### 7.3 结构摘要的角色

该兼容层已经移除。

当前终态卡协议只保留：

- 标题与模板合同
- `final_reply_text` 正文
- 隐藏 marker
- 新版卡片正文元素上的 `fc_tr_<result_id>_<checksum>` 引用

因此当前行为变成：

- 原卡查询成功且 `result_id` 可从本地 terminal result store 恢复时：以
  store 正文为权威结果
- 原卡查询成功但 store miss 时：卡片正文只作为 degraded projection 回退
- 原卡查询失败时：只剩 best-effort 投影，不再依赖结构摘要修复标题层级

## 8. 当前实现状态

当前仓库已完成本决策的主路径：

- terminal result card 使用 JSON 2.0，正文位稳定，并带有
  `fc_tr_<result_id>_<checksum>` 引用
- 普通 `interactive` 消息有 `message_id` 时，优先请求
  `card_msg_content_type=user_card_content`
- 原卡查询成功后，先用本地 terminal result store 判断是否有权威正文；
  store miss 时明确降级为卡片投影
- `merge_forward` 先展开子消息，再对其中的 `interactive` 子消息
  逐条请求原卡
- 原卡查询失败仍保留 payload / 历史结构的 best-effort 投影
- `/last text` 继续作为本机器人实例的导出与兜底入口，不取代上述读取栈

## 9. 实现边界

### 9.1 原卡与默认查询共用一个飞书适配边界

消息查询能力显式接受 `card_msg_content_type`。原卡路径传入
`user_card_content`，普通历史或默认投影路径则不传。两者不会通过隐式默认值
互相替换。

### 9.2 读取决策按权威性降级

当前读取顺序是：

1. 本地 terminal result store 命中：权威终态文本
2. 按 `message_id` 取得原卡：原卡投影
3. 事件 payload 或默认历史结构：best-effort 投影

这个顺序不因卡片是直收、普通转发还是 `merge_forward` 子消息而改变。

### 9.3 跨实例权威性有意受限

terminal result store 是本机器人实例的本地事实源。来自其他机器人、其他实例或
历史环境的卡片，即使能取得完整原卡，也只能作为非权威投影。实现不会因为
卡片 marker 形似本项目协议就自动提升为权威正文。

## 10. 观测与调试设计

结构化 ingress 日志已实现，但它是显式调试面，不是常态业务日志。
这套证据链用于区分飞书原始事件形态、原卡查询结果与最终投影路径。

### 10.1 当前记录的事实

开启调试面后，当前实现会记录：

- ingress 事件的 `msg_type`、`message_id`、`chat_id`、`thread_id`、
  `parent_id`、`root_id` 与有界的原始 `content`
- `merge_forward` 展开是否成功、item 数量与子消息 ID
- `interactive` 子消息是否进入 `raw_card_from_merge_forward_child`
- 原卡查询是否成功，以及成功时的 schema 和标题摘要
- 最终解析走 `raw_card_direct` 还是 `best_effort_projection`，以及文本是否权威

日志不是另一份卡片或 terminal result 事实源；它只记录读取决策所用证据。

### 10.2 结构化日志事件

当前事件名可单独 grep：

- `card_ingress_event`
- `card_ingress_merge_forward_expansion`
- `card_ingress_raw_card_fetch`
- `card_ingress_resolution`

实现还可以输出 `card_ingress_merge_forward_child`。它用于标记某个
`interactive` 子消息的原卡替换路径。

### 10.3 调试开关合同

`debug_raw_card_ingress` 的合同是：

- 默认为 `false`
- 只接受 YAML boolean `true` / `false`；字符串 `"true"` / `"false"` 都是配置错误
- 只有显式设为 `true` 时才记录上述 ingress 事件
- 关闭时不会因原卡查询成功或失败而写入这组 INFO 日志

### 10.4 边界

这套日志包含消息和会话标识、标题摘要、错误以及有界的原始内容，
因此不得默认开启。它能帮助区分“飞书只给了投影”、“子消息未展开”、
“原卡查询失败”和“项目主动降级”，但不能证明飞书跨所有转发形态的保真保证。

## 11. 手工验证顺序

### 11.1 第一轮：普通卡片直收

目标：

- 自己发送一张 JSON 2.0 terminal result card
- 确认对该消息自己的 `message_id` 能原卡读取

成功标准：

- 读到 `schema`
- 读到原卡正文块
- 不依赖当前投影逻辑也能恢复终态内容

### 11.2 第二轮：普通转发

目标：

- 把该卡片直接转发给机器人
- 观察收到的是：
  - `interactive`
  - 还是退化成 `text`

成功标准：

- 若仍是 `interactive`，可直接用这条转发后消息自己的 `message_id` 原卡读取
- 若退化成 `text`，明确记录为普通转发不适合作为主路径

### 11.3 第三轮：合并转发

目标：

- 把该卡片以 `merge_forward` 方式转发给机器人
- 确认外层消息展开后能拿到子消息

成功标准：

- `message/get` 返回 `1 + N` 条 `items`
- 子消息里存在 `interactive`
- 可对子消息继续做原卡读取

### 11.4 第四轮：回退验证

目标：

- 模拟原卡读取失败
- 确认当前 best-effort 投影和 `/last text` 仍可工作

## 12. 当前产品结论

本仓库当前的正式产品行为是：

- 显示优先使用 JSON 2.0
- 高保真读取优先使用“按 `message_id` 查询原卡 JSON”
- `merge_forward` 作为子消息展开入口，而不是完整内容本体
- 普通转发是否可靠，取决于转发后是否仍保留可查询的 `interactive` 新消息
- `/last text` 是兜底，不是唯一权威路径
- 转发语义仍受飞书真实事件形态限制；结构化调试日志用于实测取证，
  不把未经官方合同保证的转发形态写成产品承诺

## 13. 维护规则

如果仓库改变下列任一事实，必须同时审阅本文与
`docs/decisions/feishu-card-text-projection.zh-CN.md`：

- terminal result card 发送格式
- 原卡查询策略
- `merge_forward` 子消息展开行为
- `/last text` 读取语义
- ingress 调试开关的默认值、类型或日志字段
