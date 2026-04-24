---
title: 飞书获取历史消息 API（工作笔记）
status: 临时参考
source_url: https://open.feishu.cn/document/server-docs/im-v1/message/list
---

# 飞书获取历史消息 API

> 工作笔记：这是对飞书历史消息 API 的临时实现期摘要。
> 仓库事实与正式契约应下沉到 `docs/contracts/`、`docs/decisions/` 或
> `docs/architecture/`。

## 官方来源

- API：`GET https://open.feishu.cn/open-apis/im/v1/messages`
- 文档页：`https://open.feishu.cn/document/server-docs/im-v1/message/list`

## 前提条件

- 应用需要开启机器人能力
- 查询群历史消息时，机器人必须已经在目标群里

## 请求形状

### 请求头

- `Authorization: Bearer <tenant_access_token | user_access_token>`

### 查询参数

- `container_id_type`
  - `chat`：单聊或群聊
  - `thread`：话题线程
- `container_id`
  - `chat` 对应 chat id
  - `thread` 对应 thread id
- `start_time`、`end_time`
  - 秒级时间戳
  - 对 `thread` 容器暂不支持
- `sort_type`
  - `ByCreateTimeAsc`
  - `ByCreateTimeDesc`
- `page_size`
  - 默认 `20`
  - 合法范围 `1..50`
- `page_token`
  - 上一页返回的分页 token
- `card_msg_content_type`
  - 只影响卡片消息的返回格式
  - 不影响其他消息类型

## 与本仓库更相关的点

- 对普通对话群里的话题，`container_id_type=chat` 只能拿到话题根消息；
  如果要遍历话题回复，应使用 `container_id_type=thread`
- 群历史消息通常需要比基础单聊场景更高的权限组合
- 使用 `page_token` 翻页时，`sort_type` 必须与首次请求保持一致
- 已删除或已撤回的消息仍可能出现在历史结果里，但会带删除标记
- 如果上游可见性规则不允许当前操作者查看某个话题，会出现
  “thread is invisible to the operator” 一类错误

## 响应形状

常见顶层字段：

- `code`
- `msg`
- `data.has_more`
- `data.page_token`
- `data.items`

常见消息字段：

- `message_id`
- `root_id`
- `parent_id`
- `thread_id`
- `msg_type`
- `create_time`
- `update_time`
- `deleted`
- `updated`
- `chat_id`
- `sender`
- `body.content`
- `mentions`
- `upper_message_id`

## 常见错误场景

- `230002`：机器人不在目标群里
- `230006`：机器人能力未启用
- `230027`：缺少必要权限
- `230073`：当前操作者对该 thread 不可见
- `231203`：当前群类型不支持历史消息获取

## 在本仓库里的使用方式

当此 API 会影响仓库行为时，应优先记录“与仓库实现直接相关”的合同，例如：

- 飞书侧历史回捞行为的假设是什么
- 需要哪种身份 / 权限模式
- 哪些可见性或分页约束会影响实现

不要长期在这里保留大段复制的官方 API 摘录。
