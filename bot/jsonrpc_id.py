"""Type-preserving keys for JSON-RPC request identifiers.

JSON-RPC treats the numeric identifier ``1`` and the string identifier
``"1"`` as different values.  Internal maps which track a server request must
therefore never use ``str(id)`` as their key.  The resulting string is an
opaque transport-safe token as well: it can be carried through a Web route or
a Feishu card without having to reconstruct the original JSON value.
"""

from __future__ import annotations

import base64
from typing import Any


def jsonrpc_id_key(value: Any) -> str:
    """Return a stable, type-preserving key for one non-null JSON-RPC id.

    The app-server adapter currently exposes ``int | str`` ids, while proxy
    code also accepts a JSON numeric ``float`` defensively.  ``bool`` is
    intentionally rejected even though Python makes it an ``int`` subclass:
    JSON booleans are not JSON-RPC numeric ids.
    """

    if isinstance(value, str):
        if not value:
            raise ValueError("JSON-RPC id must not be empty")
        # The key is also used as an opaque card/URL token.  Base64url keeps
        # arbitrary JSON text (including whitespace, slash, and Unicode)
        # safe across those transports without trimming or reparsing it.
        encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
        return f"string:{encoded}"
    if isinstance(value, bool) or value is None:
        raise ValueError("JSON-RPC id must be a non-empty string or number")
    if isinstance(value, int):
        return f"integer:{value}"
    if isinstance(value, float):
        return f"number:{value!r}"
    raise ValueError("JSON-RPC id must be a non-empty string or number")


def optional_jsonrpc_id_key(value: Any) -> str:
    """Return an empty key for a missing/malformed notification id.

    Notifications are untrusted protocol payloads.  They should be ignored
    when no valid id is present rather than accidentally matching the string
    representation of a Python value such as ``None``.
    """

    try:
        return jsonrpc_id_key(value)
    except ValueError:
        return ""
