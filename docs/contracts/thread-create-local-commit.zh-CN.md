# Thread Create 与本地提交合同

文档角色：中文规范源。英文同步副本：`docs/contracts/thread-create-local-commit.md`。

## 范围

Codex app-server 持有 `thread/start` 和创建出的 thread。Focus 只持有 typed success response
在本地引起的后果：

1. 为 response 中的 thread 保留 machine runtime lease；
2. 记录 response-side effective settings fact；
3. 把返回的 thread 提交给发起请求的 surface；
4. 对 Web，把首轮 turn 的准备和提交当作后续独立操作。

这条边界不是 durable distributed transaction。它不会让 `thread/start` 具备幂等性，也不持有 thread inventory。

## 上游基线

Codex TUI 发送 typed `thread/start`，收到有效 success response 后切换本地 session，否则展示错误。上游没有
frontend durable create journal，也没有 global mutation quarantine。若 thread 已创建但 response 丢失，仍可通过
`thread/list`、`thread/read` 或 `thread/resume` 发现。

Focus 对齐这一行为。一次 create 结果未知，不是“某个既有 thread 不可安全 mutation”的证据。

## 持久 history mode 边界

通过 canonical `CodexAppServerAdapter.create_thread()` 新建的 Focus thread 是持久 thread。Web 与飞书
共用这条 adapter / `ThreadCreateTransaction` 路径；adapter 显式发送 `historyMode: paginated`，并要求
typed success 中的 upstream `Thread.historyMode` 仍精确为 `paginated`。缺字段、未来值或返回 `legacy`
都不是成功，不得自动删除该字段重试。Focus 不解析或重写 rollout，也不把这项 response fact 另行持久化。

该选择只作用于升级后新建的 Web/飞书 thread。既有 `legacy` thread 不迁移，仍可按已有生命周期打开和
使用；只在需要 paginated store 的检查能力上明确 unavailable。`ThreadSummary.history_mode=None` 只用于
没有 upstream Thread DTO 的 Focus-owned provisional/temporary summary；任何真实 upstream Thread DTO
都必须严格解码为 `legacy` 或 `paginated`。

fcodex 的 `thread/start` 由 upstream TUI 经本地 proxy 发送，不调用 canonical adapter create。Focus 只做
既有 cwd 注入与准入/结算，不添加、删除或覆盖其 `historyMode`；是否请求 paginated 以及上游自己的有限
fallback 继续由固定版本 TUI 持有。

## 同栈 create

Web 和飞书在串行 RuntimeLoop 上调用 `ThreadCreateTransaction.create_and_commit_thread()`，顺序为：

```text
typed thread/start response
  -> 验证非空 thread id
  -> 验证 persisted historyMode 精确为 paginated
  -> 取得 machine runtime lease
  -> 记录 effective-settings fact
  -> 执行一次 surface-local commit callback
```

callback 是唯一与 surface 相关的本地提交边界：

- 飞书提交 exact binding transition；
- Web 在首轮准备前验证并返回 exact created thread identity；后续 document projection 是
  best-effort presentation，不创建临时 main-turn owner。

这里没有 owner descriptor、write-ahead attempt、durable phase ledger、lease provenance journal、terminal
tombstone，也不跨 restart replay callback。

## fcodex 外部 transport

fcodex 通过自己的 websocket 发送 `thread/start`。转发前，Focus 为当前 backend generation 签发一个 opaque
`ExternalThreadCreateAttempt`。有效 typed success 必须恰好消费该 capability 一次，并把返回的 direct-root
identity 提交给 `FcodexParticipantRuntimeRegistry`。

该 capability 只在进程内有效。disconnect 或 backend replacement 会使其失效；复制、重放、已消费或旧代际
capability 不能结算其他请求。这是 create 边界唯一额外的多前端规则。

error 或 unknown response 会消费这个 exact request 并报告给 fcodex client。Focus 不会只因为 create effect
可能未知就 quarantine proxy connection，也不会阻止下一次显式 create。

## 结果分类

### 已知无 effect

若 transport 证明 bytes 未发送，返回原始 typed error，不应用任何本地后果。后续 retry 是用户新的显式操作。

### 结果未知

若 request 可能已发送，或名义 success 缺少有效 thread identity，Focus 报告
`ThreadCreateOutcomeUnknown`，且绝不自动重发该意图。surface 应提示用户先检查全局 thread list，再决定是否
重新创建。

未知只限于这个 exact request：不创建 durable record，不阻止任何已知 thread、surface、lifecycle command、
backend replacement。

### success response 后本地失败

若有效 success response 之后，runtime lease、effective-settings projection 或 surface-local callback 失败，Focus
报告 `ThreadCreateLocalCommitFailed`，并携带已知 `thread_id` 与失败 stage。该 thread 仍可从 inventory 找到并
重新打开。

Focus 不会根据异常猜测部分执行的 callback 是否已提交，因此既不 replay callback，也不围绕它建立 recovery
quarantine。其他 thread 保持可用。

### 成功

surface 收到 `CommittedThreadCreate(response, local_result)`。对 Web，这会提交新 thread identity，首轮
ordinary prompt 随后直接使用上游 `turn/start` 的 start-or-steer 语义，不读取、取得、激活或释放共享
`InteractionLease`。首轮结果未知时仍不自动 replay；presentation 与首轮结果不会改变 create identity。
当 Focus 已知 created thread id、但首轮 `turn/start` 返回 `turn_submission_unknown` 时，Web 必须先在当前
Tab 的 `sessionStorage` 持久保存一条 browser-local possibly-sent 记录，才可收口原 Composer 并尝试打开该
thread。该记录的 user payload 只保存原文本和“原提交是否带附件”，另含 stable client、created thread、cwd、
local operation/key 与固定空/零 schema 元数据；它不补造 mutation id、client message id 或服务端 recovery
identity。已提交到 thread scope 的 attachment receipt 必须在保存前删除。用户可从任意当前 UI scope 显式放弃
这条 browser-local record；只有打开该 created thread 后，才可把纯文本 handoff 为一条具有新 identity 的未发送
消息。这两条本地动作均不调用
服务端 unknown-resolution API。若本地记录不能持久保存，Web 不得声称已收口原 Composer。
此时 Web 保留原 draft scope、不自动导航，并在错误中暴露 known created thread id，避免 scope 切换吞掉仍由
Composer 持有的 payload；用户可从全局 thread list 手动打开该 thread 核对。
首轮明确失败时，Focus 尝试把 exact attachment set 恢复为 pending；`thread_created_turn_not_started`
通过 `attachment_disposition=restored|reupload_required` 报告实际结果。只有 `restored` 表示原 attachment id
可直接重试；store 写失败不得伪装成已恢复，用户应在已创建 thread 中移除旧 attachment 并重新上传。

## Reset 与可观测性

connection invalidation 和 backend reset 会撤销旧 fcodex create capability。不存在需要清除、恢复或暴露到
service status 的 create ledger。machine process proof 与 runtime lease 仍由既有 runtime authority 持有。

operational warning 可以报告已知 thread id 与本地失败 stage；它只是诊断，不是 mutation admission authority。

## 回归要求

测试必须锁定：

- typed success 的顺序与一次本地 callback；
- Web/飞书共用的 canonical create 显式请求并验证 `historyMode=paginated`；
- history mode 不支持或 response 为 legacy 时不自动 retry；
- fcodex `thread/start` payload 的 `historyMode` 原样透传；
- known-no-effect 与 unknown transport 的分类；
- unknown 后不自动 retry；
- unknown 或 local failure 后不建立 thread/global mutation quarantine；
- Web 首轮 ordinary prompt 不创建、读取或改写 foreign blank/active main-turn lease；known failure 精确报告
  attachment restore/reupload disposition；known-thread unknown 在打开 created thread 前持久锁定纯文本，
  不复用附件、不补造服务端 identity、也不自动 replay；
- fcodex capability 一次性消费与 backend generation 失效；
- fcodex 本地 ACK retry 不能 replay create 或 callback；
- Web 与飞书在 thread id 已知时把它暴露给用户。
