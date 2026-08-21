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
        normalized = str(fallback or "").strip().lower()
    normalized = DEPRECATED_APPROVAL_POLICY_MAP.get(normalized, normalized)
    if normalized not in USER_SELECTABLE_APPROVAL_POLICIES:
        choices = ", ".join(sorted(SUPPORTED_APPROVAL_POLICIES))
        raise ValueError(
            f"unsupported approval policy {policy!r}; expected one of: {choices}"
        )
    return normalized
