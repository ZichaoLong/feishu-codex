# 仓库导航与变更锥纪律

文档角色：中文规范源。英文同步副本：`docs/architecture/development-navigation.md`。

本文规定日常诊断、review、功能实现、行为变更与重构时如何限制读取范围、
识别事实源、扩展变更锥并闭合导航影响。功能或行为变更必须在改变事实的同一
transaction 中对齐相关合同、代码、测试、guard 与导航影响。本文不定义产品
行为，也不授予修改权限、commit 预算或 campaign 例外。

## 1. 两套纪律的边界

仓库导航纪律适用于每个开发任务，回答“先读什么、何时多读一跳、改完要同步
哪些派生入口、应验证到哪里”。大型变更 campaign 纪律只在广泛变更时追加，
回答“目标与非目标是什么、允许多少提交、何时停止或请示”。两者组合使用，
但不得互相复制状态或规则。

需要用一句话启用完整流程时，使用 `使用 $develop-focus 完成：<任务>`。这个 skill
只动态路由到当前路径适用的 `AGENTS.md`、本文和固定导航工具：`AGENTS.md` 拥有
汇报、授权、campaign、请示与停止纪律，本文拥有导航和变更锥纪律。Skill 不复制
规则，也不扩大用户任务授予的权限或范围。

## 2. 事实角色与优先级

| 内容 | 权威来源 | 导航中的角色 |
| --- | --- | --- |
| 规范行为、架构意图与边界 | `docs/contracts/`、`docs/architecture/`、`docs/decisions/` 中相关正式文档 | 只链接，不转述 |
| 可变 runtime fact 与外部 effect authority | production code 中唯一 owner | 只定位 owner symbol |
| 配置值与 schema | canonical 配置入口及其校验 | 只定位入口或 owner |
| 回归与静态证据 | tests 与 guards | 只组成验证锥 |
| 能力入口、owner、合同和验证锥的定位 | `scripts/focus_capabilities.json` | 人工复核的派生索引 |
| agent 操作入口 | `develop-focus`、`navigate-focus-development` skills 与适用的 `AGENTS.md` | 只路由到当前权威来源和固定工具 |
| 临时 campaign 状态 | 当前 `docs/_work/` ledger | 不进入长期导航 |

capability catalog 是人工复核、非完备、可丢弃重建的派生索引；`owner` ref 不会
创造 ownership，只指向已经由正式文档与 production implementation 证明的 owner。
它的价值是缩小首轮读取范围，而不是声明仓库的完整形状。catalog 与代码或正式文档
冲突时，catalog 立即视为 stale，不能覆盖任何权威来源。代码与正式合同冲突则是
合同缺口，不能由导航层裁决。测试只提供
证据，不成为第二份行为合同；capability 缺失也不证明对应能力不存在。

## 3. Code-first 导航 invariant

“代码即导航”是指 production 结构是实现位置、依赖方向和 runtime authority 的首要
地图，不是让代码取代正式合同。理想情况下，即使删除 capability catalog，开发者
仍能沿 public entry、明确命名的 owner、直接依赖和对应测试定位变更；正式合同仍
独立拥有规范行为与架构意图。

代码和测试应持续满足以下可发现性约束：

- package、module、symbol 以 capability 或 owner 词汇命名；一个 mutable fact 或
  external effect 只有一个明显的 production owner；
- public entry 与跨边界依赖显式、直接且有方向，避免用宽泛 re-export、动态注册、
  compatibility alias、`common.py` 或 `utils.py` 隐藏真正 authority；
- owner API、类型与正式合同使用同一词汇；module docstring 只说明局部角色并链接
  正式合同，不复制行为、默认值或可变事实；
- focused tests 按 capability 或 owner 形成可预测拓扑，跨 owner invariant 由明确
  sentinel 或 direction guard 保护；
- 只在 ownership、change locality 或依赖方向确实更清晰时分包或拆文件；不得为了
  指标拆散一个必须共同维护 invariant 的 owner。

Catalog 与 skills 只能加速第一跳，不能成为理解代码所必需的旁路。如果稳定任务
必须依赖 catalog 中的叙述才能找到 owner、call chain 或测试，应把它视为代码结构
或正式合同的可发现性缺口，在有边界的 transaction/campaign 中修正，而不是扩充
第二份架构描述。

## 4. 读取锥纪律

1. 先读当前路径适用的 `AGENTS.md`，再用 `focus_nav.py list` / `show` 查询最接近
   的 reviewed capability。命中结果只是读取假设，不是 ownership 证明。
2. 首轮只读命中项返回的 entry、owner、contract、focused-test、sentinel 与 guard
   引用，先确定本任务涉及的事实类型、authority 和外部 effect。
3. 只有出现以下证据时才扩展一跳：调用或 import 跨出当前边界；同一 mutable
   fact 或 effect 被另一 owner 读写；正式合同指向上位合同；失败测试或 guard
   指向相邻模块；修改影响一个已知 public entry 或直接 consumer。
4. Python 边界优先用 `focus_nav.py module` 实时查看直接 imports/importers。
   其他语言或非 import 关系使用精确 symbol、route、event、config key 或测试名做
   窄 `rg`。每次只沿已出现的证据扩展一跳，不按目录批量预读相邻文件。
5. catalog 无命中时，从用户词汇、public entry、正式合同或失败测试开始窄搜索；
   不猜 owner，不伪造 capability ref。只有人工完成 owner、合同与验证锥复核后才
   登记新的 capability。
6. `docs/_work/` 仅供明确处于 active governance 的任务读取；普通开发不得把
   历史 ledger 当当前架构或行为证据。

当已有证据足以解释目标行为、定位唯一修改 authority 并选择回归锥时，停止扩大
读取范围。目录大小或“也许相关”本身不是扩锥证据。

## 5. 修改与导航影响闭环

编辑前，明确本次 change cone 中的正式合同、production owner、public entry、
直接 consumer、focused tests、sentinels 与方向 guard。修改应留在这个锥内；
只有第 4 节的证据触发条件成立时才扩大。

出现以下任一变化时，必须在同一个 transaction 中复核并更新或删除受影响的
capability refs：

- entry 或 owner 的路径、symbol、职责或 effect authority 改变；
- 正式合同路径、heading、优先级或行为边界改变；
- focused-test、sentinel 或测试 package 拓扑改变；
- import 方向、直接 consumer 或适用 guard 改变；
- capability 被新增、合并、拆分或删除。

如果 catalog 中没有受影响 capability，可以明确结束 navigation-impact 复核而不
新增条目。catalog 不以全仓覆盖为目标，只登记已经人工复核且对重复开发有明确
价值的稳定入口。

每笔 transaction 在编辑前建立初始锥，并在提交前人工复核本笔显式 changed
paths。以下实时反查可以辅助这次复核：

```bash
python scripts/focus_nav.py paths <repo-relative-path>... [--locations]
```

工具不读取 Git，也不从路径猜 capability 或 semantic owner。它只返回当前 catalog
中精确命中的 capability、role 与 ref；pytest `::node` ref 按其基础文件匹配，
`--locations` 从当前源码解析 symbol、heading section 或 test target 的行范围。任何
selector 无法唯一定位、或目录级 test target 没有精确行范围时，`--locations` 整次
查询 fail closed。这个命令是派生索引助手，不是 transaction 的完成 authority；
`unmapped` 只表示没有 catalog ref 命中，不能证明没有语义影响，也不能替代本节
人工 navigation-impact 复核。若完整 catalog 因无关 stale ref 无法载入，从正式
合同、代码与 tests 完成人工复核；不得据此修改权威来源来适配 catalog，也不得把
无关修复自动扩入当前 scope。

路径迁移必须原子删除旧 ref。除非正式外部合同明确要求兼容，不保留旧导航路径、
shim、宽泛 re-export 或新旧双入口。行为修复、owner extraction 与 package move
仍按各自 transaction 边界处理；导航投影随实际改变它的那笔 transaction 更新。

## 6. Stale 索引的 fail-closed 处理

路径或 symbol 仍存在，只能证明引用可解析，不能证明 owner、合同或回归锥仍然
正确。出现引用缺失、代码证据与分类冲突、owner 已迁移、测试不再覆盖目标行为，
或一个 capability 指向多条含糊路径时：

1. 停止把该 catalog 项用作证据；
2. 从正式文档、production code 和 tests 窄范围重建 change cone；
3. 在本次 transaction 中修正或删除 stale refs；
4. 重新运行 catalog 校验和受影响验证锥。

不得以 stale catalog 的结论反向修改代码或合同来“适配导航”。

## 7. 长期导航允许与禁止承载的内容

capability catalog 只允许 reviewed entry、owner、contract、focused-test、sentinel
与 guard refs。skill 只保留调用工具和路由到本文所需的步骤。

长期导航不得保存：

- 行为叙述、invariant、架构理由或产品默认值；
- branch、HEAD、计数、digest、manifest、marker 或 campaign evidence；
- 完整或传递 import/call/test graph；
- 任意 shell 命令、可变环境事实或 `docs/_work/` 引用；
- 未经 owner、合同与验证锥复核的推测性条目。

直接 Python imports/importers、changed-path 反查和 ref locations 必须从当前源码与
显式输入实时计算，不得持久化 graph、反向索引或行号。catalog 的严格 schema 与
引用校验负责阻止不允许的形状，但语义正确性仍由 change-impact review 负责。

## 8. 验证锥与完成条件

`focus_verify.py <capability>` 是 reviewed 最小验证锥，不是 transaction 或 campaign
退出门禁的替代品。实际变更还必须加入本次修改产生的直接 consumer 回归、失败
证据指向的测试、适用静态 guard、合同/文档检查，以及变更风险要求的更宽门禁。

一笔开发 transaction 只有同时满足以下条件才完成：

- 权威合同、代码与测试对本次语义保持一致；
- 所有受影响 capability 的 navigation-impact 已更新、删除或确认无命中；
- 旧路径与 stale refs 归零；
- focused verification 与本 transaction 所需的更宽门禁均为绿色。
