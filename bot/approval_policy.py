from __future__ import annotations

DEPRECATED_APPROVAL_POLICY_MAP = {
    "on-failure": "on-request",
}

USER_SELECTABLE_APPROVAL_POLICIES = frozenset(
    {
        "untrusted",
        "on-request",
        "never",
    }
)

SUPPORTED_APPROVAL_POLICIES = frozenset(
    set(USER_SELECTABLE_APPROVAL_POLICIES) | set(DEPRECATED_APPROVAL_POLICY_MAP)
)


def normalize_approval_policy(policy: str, *, fallback: str = "on-request") -> str:
    normalized = str(policy or "").strip().lower()
    if not normalized:
        return fallback
    return DEPRECATED_APPROVAL_POLICY_MAP.get(normalized, normalized)
