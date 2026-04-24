---
title: Feishu Get History API (working note)
status: temporary reference
source_url: https://open.feishu.cn/document/server-docs/im-v1/message/list
---

# Feishu Get History API

> Working note: this is a temporary implementation-time summary of the Feishu
> history-message API.
> Repository facts and formal contracts should live under `docs/contracts/`,
> `docs/decisions/`, or `docs/architecture/`.

## Official Source

- API doc: `GET https://open.feishu.cn/open-apis/im/v1/messages`
- Source page: `https://open.feishu.cn/document/server-docs/im-v1/message/list`

## Preconditions

- bot ability must be enabled for the app
- when querying group history, the bot must already be in that group

## Request Shape

### Headers

- `Authorization: Bearer <tenant_access_token | user_access_token>`

### Query Parameters

- `container_id_type`
  - `chat`: p2p or group chat
  - `thread`: topic thread
- `container_id`
  - chat id for `chat`
  - thread id for `thread`
- `start_time`, `end_time`
  - second-level timestamps
  - not supported for `thread` containers
- `sort_type`
  - `ByCreateTimeAsc`
  - `ByCreateTimeDesc`
- `page_size`
  - default `20`
  - valid range `1..50`
- `page_token`
  - pagination token from the previous response
- `card_msg_content_type`
  - only affects card-message payload shape
  - does not affect other message types

## Repository-Relevant Notes

- for ordinary chat-group topics, `container_id_type=chat` only returns the
  thread root message; use `container_id_type=thread` to traverse thread replies
- group-history access needs additional permissions beyond the baseline p2p scope
- `page_token` pagination must keep the same `sort_type` as the first request
- deleted or recalled messages may still appear in history with deletion markers
- thread visibility can fail with "thread is invisible to the operator" if
  upstream visibility rules block the current operator

## Response Shape

Typical top-level fields:

- `code`
- `msg`
- `data.has_more`
- `data.page_token`
- `data.items`

Typical per-message fields:

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

## Common Error Cases

- `230002`: bot is not in the target group
- `230006`: bot ability is not enabled
- `230027`: missing required permission
- `230073`: thread is invisible to the current operator
- `231203`: chat type does not support history fetch

## Usage in This Repository

When this API affects repository behavior, prefer recording only the
repository-relevant contract, for example:

- which Feishu-side history replay behavior is assumed
- which identity / permission mode is required
- which visibility or pagination constraints matter to implementation

Do not keep large copied API excerpts here long-term.
