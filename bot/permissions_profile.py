from __future__ import annotations

BUILTIN_PERMISSION_PROFILE_READ_ONLY = ":read-only"
BUILTIN_PERMISSION_PROFILE_WORKSPACE = ":workspace"
BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS = ":danger-full-access"

BUILTIN_PERMISSION_PROFILE_IDS = frozenset(
    {
        BUILTIN_PERMISSION_PROFILE_READ_ONLY,
        BUILTIN_PERMISSION_PROFILE_WORKSPACE,
        BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
    }
)

PERMISSION_PROFILE_CHOICES: dict[str, dict[str, str]] = {
    "read-only": {
        "profile_id": BUILTIN_PERMISSION_PROFILE_READ_ONLY,
        "label": "Read Only",
        "description": "只读当前工作区；更安全，改文件前通常需要先调整审批或权限基线。",
    },
    "workspace": {
        "profile_id": BUILTIN_PERMISSION_PROFILE_WORKSPACE,
        "label": "Workspace",
        "description": "可读写当前工作区；工作区外写入仍受限。",
    },
    "danger-full-access": {
        "profile_id": BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
        "label": "Danger Full Access",
        "description": "可编辑工作区外文件并直接联网，风险最高。",
    },
}

LEGACY_SANDBOX_TO_PERMISSION_PROFILE_ID = {
    "read-only": BUILTIN_PERMISSION_PROFILE_READ_ONLY,
    "workspace-write": BUILTIN_PERMISSION_PROFILE_WORKSPACE,
    "danger-full-access": BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
}

PERMISSION_PROFILE_ID_TO_LEGACY_SANDBOX = {
    profile_id: sandbox for sandbox, profile_id in LEGACY_SANDBOX_TO_PERMISSION_PROFILE_ID.items()
}


def normalize_permissions_profile_id(
    value: str,
    *,
    fallback: str = BUILTIN_PERMISSION_PROFILE_DANGER_FULL_ACCESS,
) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return fallback
    if normalized in PERMISSION_PROFILE_CHOICES:
        return PERMISSION_PROFILE_CHOICES[normalized]["profile_id"]
    if normalized in BUILTIN_PERMISSION_PROFILE_IDS:
        return normalized
    if normalized in LEGACY_SANDBOX_TO_PERMISSION_PROFILE_ID:
        return LEGACY_SANDBOX_TO_PERMISSION_PROFILE_ID[normalized]
    choices = ", ".join(sorted(BUILTIN_PERMISSION_PROFILE_IDS))
    raise ValueError(
        f"unsupported permissions profile {value!r}; expected one of: {choices}"
    )


def permissions_profile_choice_key(profile_id: str) -> str:
    normalized = normalize_permissions_profile_id(profile_id)
    for key, config in PERMISSION_PROFILE_CHOICES.items():
        if config["profile_id"] == normalized:
            return key
    return ""


def permissions_profile_label(profile_id: str) -> str:
    key = permissions_profile_choice_key(profile_id)
    if key:
        return PERMISSION_PROFILE_CHOICES[key]["label"]
    normalized = str(profile_id or "").strip()
    return normalized or "Default"
