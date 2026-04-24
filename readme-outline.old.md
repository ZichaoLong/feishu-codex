说明

# 前置条件
保留

# 安装配置

## 安装

## 飞书配置
创建应用，机器人，权限，事件与回调

## 本地配置
/init 设置，管理员（飞书侧哪些用户可以操作设置项，其他用户只能使用和对话交互或查看）
system.yaml,codex.yaml,init.token,api key env 等

## 多实例配置

## 安装后会发生什么

## 启用和使用

# 使用

## 单聊
发送消息自动发起会话
查看status
/help导航

## 群聊
### 工作模式
### ACL

## 进阶使用
- 多目录、项目飞书操控：开启多个群聊会话，每个会话里只有用户本人和机器人，不同会话绑定不同项目
- 飞书侧与本地共同操作同一个 thread。安全机制：多订阅 + 单交互租约，可以飞书与本地 fcodex 协作，谁发起当前 turn，谁就获得当前的交互租约，任何交互请求不会路由到其他会话或 fcodex TUI，turn 结束时（我印象中主要判定依据是 app server 的 turn/completed 事件，另有几种保底 thread 状态查询判断）释放 interaction owner，并把 turn 终态结果推送会推送至每个前端。不建议跨实例操作同一个 thread（有ThreadRuntimeLease保护，但当前不推荐强行使用）。
- 裸 codex 内部的 /resume 不会跨 provider 搜索，飞书、feishu-codexctl可跨 provider 搜索 thread。fcodex resume 可跨 provider 恢复。【注】：把 fcodex slash命令去掉/符号，纯做wrapper，与 codex 保持最一致的使用方式，去掉原来的 fcodex /resume 不能再接受其他参数的限制。fcodex /session, fcodex /rm, fcodex /profile, fcodex /help 等移动到 feishu-codexctl下更好？
- 本地管理命令
- 飞书及本地的 debug 能力

## 注意事项
- 如何切换 profile/provider 及其生效机制
    - 上游事实
        - app server 内 thread（codex 内部术语，即一个 codex 会话）处于 load 状态时，无法热更改 provider，unload 状态下可调用公开接口传入 provider，再 resume thread，让新 provider 生效
        - app server 内一个 thread 如果还有订阅者，则不会 unload，如果所有订阅者取消订阅，则会 unload
        - codex TUI 内部也无法在不退出的情况下更改 provider，最多可以改模型
    - 使用时如何生效
        - 裸 codex TUI 总是启动独立 app server，TUI 进程订阅相应 thread。裸 codex 切换 provider 的方式是：退出当前 codex，重新 codex resume xxx -p xxx
        - 飞书多会话绑定 feishu-codex 实例进程，该进程订阅 app server 内线程；fcodex 打开后也订阅 app server 中的线程。feishu-codex 与 fcodex 共享 app server 是它们可共同写入而不导致上下文错乱的基础，但也导致切换 profile/provider 需要比裸 codex 多几步操作
            - 飞书侧，unsubscribe。【注】：把原来的 release-feishu-runtime 改成 unsubscribe，飞书侧提供此功能，本地 feishu-codexctl 也提供此功能。
            - 本地 fcodex 退出（自动退订 thread）
            - 飞书侧切换 profile/provider，发送消息；或本地 fcodex resume xxx -p xxx。【注】：把 profile/provider 设置做成 thread wise（应允许持久化，类似于当前的bindings持久化，方便重启服务仍然生效）而非 feishu-codex 全局共享的，更好？
- 如何设置 sandbox/approval 及其生效机制
    - 上游事实
        - codex TUI 内部可更改，feishu-codex 也可通过公开接口输入 sandbox/approval 设置，下一次 turn 生效
        - permissions 是 sandbox/approval 这两个设置旋钮的打包套餐版
    - 使用时如何生效
        - 裸 codex TUI：内部设置，下次生效
        - 飞书侧与本地 fcodex：各自持有各自的 sandbox/approval 设置，谁拿到 interaction owner，按照谁的设置进行 turn。【注】：当前似乎并不是这么实现，看看是否可以实现为 binding wise 的形状，并允许持久化，方便重启进程时恢复飞书侧设置，fcodex 里使用全局 $CODEX_HOME/config.toml 里的默认设置即可。

## 避坑速记
- /new 会创建新线程，取消旧绑定，但 app server 中可能尚未特化此线程，发起消息后真正特化
- /rm 实际时 Codex archive，从常规列表里隐藏，不是硬删除
- fcodex 打开后，连接共享 app server，本项目 proxy 层根据 interaction owner 持有情况决定是否路由 app server 发起的交互请求，其他前端 TUI 行为遵从上游设计

更进一步，查看 docs/...
