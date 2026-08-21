# `system.yaml` admission contract

Document role: synchronized English peer. Canonical Chinese: `docs/contracts/system-config.zh-CN.md`.

## Purpose

`system.yaml` stores the application identity used by one Focus instance to
connect to Feishu, together with instance-level administrator, trigger
identity, network, and group-history recovery settings. These fields sit at a
safety-sensitive admission boundary. Treating `"false"` as a boolean,
expanding a scalar as a string list, or silently skipping malformed list items
can change who may trigger the bot, which sensitive payloads are logged, or how
long the service waits.

This document defines one explicit, fail-closed boundary for that static
instance configuration. It does not define `codex.yaml`, persisted Feishu
binding state, active main-turn leases, or upstream
`~/.codex/config.toml`.

The current `focusd` process is a combined Feishu service and optional Web
Gateway; there is no separate Web-only daemon admission mode. Consequently,
`app_id` and `app_secret` remain required when starting `focusd` even if an
operator intends to use only its Web Gateway. They are not prerequisites for
the installation script to create the managed Python environment.

## Authoritative source and projections

`bot.system_config.SystemConfig` is the sole authoritative source for the
accepted-key inventory, defaults, types, ranges, and normalization rules of
`system.yaml`.

- `config/system.yaml.example` is the source-install projection, while
  `bot/install_template_data/system.yaml.example` is the packaged-install
  projection. Tests require their contents to be identical and each documented
  key inventory to equal the schema inventory.
- The daemon parses the complete document into `SystemConfig` before creating
  `CodexBot`.
- `FeishuBot` consumes values parsed by that schema; it no longer performs its
  own `str()`, `float()`, or `int()` conversions or silently filters list items.
- Runtime administration also validates the complete file before reading the
  Feishu HTTP timeout. It does not continue by replacing an invalid value with
  a default.

Adding a setting requires changing the `SystemConfig` field/parser, both
example projections, this contract, and the schema tests together. Adding a
consumer-side `dict.get()` does not create a supported setting.

## Formal fields

| Field | Type and range | Default semantics |
| --- | --- | --- |
| `app_id` | Nonempty string after trimming outer whitespace | Required in the file; no runtime default |
| `app_secret` | Nonempty string after trimming outer whitespace | Required in the file; no runtime default |
| `request_timeout_seconds` | Finite number strictly greater than `0` | `5.0` seconds |
| `feishu_ws_proxy` | Closed `env` / `disabled` enum, case-normalized | `env` |
| `admin_open_ids` | List of unique, nonempty strings | Empty list |
| `bot_open_id` | String; empty means not configured | Empty string |
| `trigger_open_ids` | List of unique, nonempty strings | Empty list |
| `group_history_fetch_limit` | Integer greater than or equal to `0` | `50`; `0` disables recovery |
| `group_history_fetch_lookback_seconds` | Integer greater than or equal to `0` | `86400`; `0` disables recovery |
| `debug_raw_card_ingress` | YAML boolean | `false` |

`debug_raw_card_ingress` records raw card callbacks that may contain user input
and identity data. It is for short-lived diagnosis only, so its default must
remain off.

## Admission rules

- The document is a string-keyed mapping. Duplicate mapping keys at any depth,
  unknown top-level keys, and explicit nulls are rejected; duplicate keys
  never use last-value-wins semantics.
- Strings, booleans, integers, numbers, and string lists are distinct types.
  Numeric strings are not converted, and booleans are not interpreted as
  Python integer subclasses.
- Numbers must be finite and satisfy the table above. `NaN`, infinities,
  non-positive timeouts, and negative history parameters are rejected.
- Open-ID fields must be YAML lists whose items are unique and nonempty after
  trimming. Scalars, nulls, non-string items, empty items, and duplicates are
  rejected rather than silently discarded.
- Invalid configuration stops the daemon, runtime administration, and config
  write paths with a field-specific diagnostic. A consumer must not mask it
  with a default or consume only the apparently valid subset.

## Controlled `/init` update boundary

`/init <token>` retains a controlled update of the raw YAML document so it does
not materialize every default into the file. This is not a schema bypass:

1. The command validates the complete current document and reads existing
   `admin_open_ids` / `bot_open_id` from the typed projection.
2. It updates only those two fields and preserves every other validated field.
3. `save_system_config` validates the complete updated document again before
   the atomic write.
4. On either validation failure, it neither writes the file nor updates the
   in-process administrator or bot identity.

Consequently, `/init` cannot preserve and rewrite unknown keys, malformed
lists, or missing credentials as a side effect.

## Relationship to other fact sources

`system.yaml` is an instance-level static admission fact. It owns the Feishu
application identity, static administrator/trigger identities, network policy,
and group-history recovery parameters. Dynamic facts such as group activation
and mode, chat bindings, thread/runtime leases, and live writer ownership remain
owned by their stores and runtime contracts. Schema validation does not merge
these layers, and a discovered bot Open ID does not automatically become a
configuration fact. It becomes the next-start static fact only after an
explicit `/init` write succeeds.

## Compatibility consequence

Configuration that previously depended on permissive conversion or silent
filtering now fails at admission. This includes misspelled or unknown keys,
quoted booleans/numbers, scalar Open-ID fields, lists containing nulls,
numbers, empty or duplicate items, non-positive timeouts, negative history
parameters, and missing or empty application credentials. Repair the document
using the spellings and YAML types shown in `system.yaml.example`; retaining
the old interpretations would preserve the identity and safety ambiguity this
contract removes.
