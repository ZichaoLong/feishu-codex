"""CLI projection for the explicit service backend-reset command."""

from __future__ import annotations

import pathlib

from bot.backend_reset.contract import (
    BackendResetResultContractError,
    decode_backend_reset_result,
)
from bot.service_control_plane import (
    ServiceControlOutcomeUnknownError,
    control_request,
)


def reset_service_backend(
    data_dir: pathlib.Path,
    *,
    force: bool,
    instance_name: str,
) -> int:
    """Execute one reset control transaction and render its typed result."""

    if type(force) is not bool:
        raise TypeError("backend reset force must be an exact bool")
    normalized_instance_name = str(instance_name or "").strip()
    if not normalized_instance_name:
        raise ValueError("backend reset requires the resolved instance name")
    raw_result = control_request(
        data_dir,
        "service/reset-backend",
        {"force": force},
        timeout_seconds=30.0,
    )
    try:
        result = decode_backend_reset_result(
            raw_result,
            expected_force=force,
        )
    except BackendResetResultContractError as exc:
        raise ServiceControlOutcomeUnknownError(
            "backend 可能已重置，但返回结果无法验证"
            f"（{exc}）。请先运行 focusctl --instance {normalized_instance_name} "
            "service status 检查同一目标实例；不要立即重试。"
        ) from exc

    print("backend reset: ok")
    print(f"force: {'yes' if result.force else 'no'}")
    print(f"app server: {result.app_server_url}")
    print(
        "detached bindings: "
        f"{', '.join(result.detached_binding_ids) or '（无）'}"
    )
    print(
        "interrupted bindings: "
        f"{', '.join(result.interrupted_binding_ids) or '（无）'}"
    )
    print(f"retired old-epoch requests: {result.retired_request_count}")
    print(
        "cleared transient runtime leases: "
        f"{', '.join(result.purged_thread_ids) or '（无）'}"
    )
    for warning in result.projection_warnings:
        print(f"projection warning: {warning}")
    print("next:")
    print("  - attach this instance: focusctl service attach")
    print("  - attach one thread: focusctl thread attach --thread-id <thread_id>")
    print("  - attach one binding: focusctl binding attach <binding_id>")
    return 0
