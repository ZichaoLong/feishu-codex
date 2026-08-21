# Focus Web UI 与 kimi-web 复用决策

文档角色：中文规范源。英文同步副本：`docs/decisions/focus-web-ui-and-kimi-web-reuse.md`。

> 状态：已采纳。Focus Web 的初始源码来自 kimi-web，但导入后的产品、视觉、功能与
> 架构由 Focus 自己拥有。kimi-web 不再是持续对齐或合并目标。

## 1. 问题与决策

Focus 需要一套适合桌面与移动浏览器、能可靠展示 Markdown、代码、数学公式、图表、diff
与结构化工具的前端。重新实现整套成熟展示资产没有产品收益，因此初始实现复用了 kimi-web
已经验证过的 Vue shell、响应式布局、富文本 renderer、设计系统和结构化展示组件。

这项决定只授予源码复用与派生维护边界，不把 kimi-web 的产品或 backend 语义带入 Focus：

- kap-server transport、Kimi session/provider 语义、产品品牌与 equal-writer queue 不复用；
- 浏览器只连接 Focus-owned Gateway，并只消费 Focus-owned DTO 与 projection event；
- Gateway、adapter、projection、state owner 与 mutation admission 都遵守 Focus 的正式合同；
- 导入后的代码可按 Focus 的 ownership、可维护性和产品需要自由修改、重命名、拆分或替换。

因此，本决策不是当前 Web 功能清单，也不定义运行时行为。当前产品与协议事实只由第 5 节
链接的正式合同持有。

## 2. Focus-owned adapter 与 projection

`web/src/focus/` 是浏览器侧 Focus ownership layer。它负责 Focus transport、wire decode、
projection、browser-local state owner 与 mutation coordination。Python Gateway 和对应 runtime
owner 负责把 app-server 事实投影成 Focus wire。浏览器不直接依赖完整 Codex schema，也不直连
app-server。

Kimi-derived component 可以渲染只读 projection 并调用 typed callback，但不能取得 transport、
thread lifecycle、settings、pending interaction、mutation recovery 或 runtime authority。组件目录和
文件名可以随 Focus 架构演进；来源身份不要求保留 Kimi 的模块边界。

Focus 不建立 kap-server compatibility facade。协议差异在 Focus-owned adapter/projection 边界
明确解释，不能用仿造旧 API 的方式把两套 backend 语义长期并存。

## 3. 源码复用边界

初始导入保留了 kimi-web 的浏览器 shell、移动端布局、Markdown、KaTeX、Mermaid、Shiki、diff
和结构化工具展示等实现资产。保留某个导入组件不等于 Focus 承诺它原先对应的后端 API 或产品
能力；没有正式 Focus contract 的入口必须保持不可用。

Focus-owned 演进不要求：

- 追踪新的 Kimi commit 或 UI 变化；
- 缩小 Focus 与 Kimi 的源码差异；
- 为未来 merge 保留旧目录、API 或 abstraction；
- 自动吸收 Kimi 后续功能。

若未来希望再次导入较新的 Kimi 源码，必须另作明确决策并审阅实际变更；它不是日常开发、构建
或发布门禁。

## 4. Provenance、license 与发布义务

来源事实固定为：

- 上游仓库：[`MoonshotAI/kimi-code`](https://github.com/MoonshotAI/kimi-code)；
- 初始导入 commit：
  [`c497af60e6cd20aab05e590f98a28fb15dd3491d`](https://github.com/MoonshotAI/kimi-code/commit/c497af60e6cd20aab05e590f98a28fb15dd3491d)；
- 详细导入记录与维护流程：[`web/UPSTREAM.md`](../../web/UPSTREAM.md)；
- 逐文件来源清单：[`web/provenance/kimi-web-files.json`](../../web/provenance/kimi-web-files.json)；
- 保留的 MIT 许可：[`web/licenses/kimi-web-MIT.txt`](../../web/licenses/kimi-web-MIT.txt)；
- 发布 notices：[`web/THIRD_PARTY_NOTICES.md`](../../web/THIRD_PARTY_NOTICES.md)。

修改已登记的 Kimi-derived 文件时，必须按 `web/UPSTREAM.md` 更新并审阅其本地修改摘要。
`focus_owned_files` 明确没有 Kimi 对应物的源码；新增或重新分类文件必须显式审阅，不能由脚本
猜测。构建与发布必须继续携带适用的 MIT、字体、图标和依赖 notices。

provenance 核验可以读取开发者显式提供的本地 kimi-code checkout，但运行、安装、普通构建和
发布不得依赖该 checkout。同步摘要不表示与新 Kimi 版本同步。

## 5. 当前行为的正式入口

| 问题 | 当前事实源 |
| --- | --- |
| 总体分层、Gateway 与应用事务 owner | [`focus-design.zh-CN.md`](../architecture/focus-design.zh-CN.md) |
| Feishu、Web、`focus` / `fcodex` 共享 backend 的拓扑 | [`focus-shared-backend-runtime.zh-CN.md`](../architecture/focus-shared-backend-runtime.zh-CN.md) |
| Web endpoint、event、DTO、历史与工具详情投影 | [`focus-web-wire.zh-CN.md`](../contracts/focus-web-wire.zh-CN.md) |
| existing-thread prompt 的单次 effect 与 F5/unknown 恢复 | [`focus-web-prompt-mutation-recovery.zh-CN.md`](../contracts/focus-web-prompt-mutation-recovery.zh-CN.md) |
| Web 与飞书设置的事实源和生效边界 | [`runtime-settings-fact-sources.zh-CN.md`](../contracts/runtime-settings-fact-sources.zh-CN.md) |
| ordinary realtime input、exclusive action、steer 与 interrupt | [`root-operation-owner.zh-CN.md`](../contracts/root-operation-owner.zh-CN.md) |
| `thread/start` / `thread/resume` 的本地提交 | [`thread-create-local-commit.zh-CN.md`](../contracts/thread-create-local-commit.zh-CN.md)、[`thread-resume-local-commit.zh-CN.md`](../contracts/thread-resume-local-commit.zh-CN.md) |
| approval、question 与 MCP request 的多前端生命周期 | [`server-request-lifecycle.zh-CN.md`](../contracts/server-request-lifecycle.zh-CN.md) |
| subagent 与 parent-history Tasks 边界 | [`subagent-observation-and-recovery.zh-CN.md`](../contracts/subagent-observation-and-recovery.zh-CN.md) |
| trusted-proxy 外部访问 | [`focus-web-external-access.zh-CN.md`](./focus-web-external-access.zh-CN.md) |

本决策只解释源码来源和派生 ownership。上表合同变化时，不应把旧行为复制回来补充本文。
