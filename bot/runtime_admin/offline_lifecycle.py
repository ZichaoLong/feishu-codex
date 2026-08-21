"""Offline and cross-instance lifecycle owner used by ``focusctl``."""

from __future__ import annotations

import pathlib
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from bot.binding_identity import format_binding_id
from bot.codex_protocol.client import CodexRpcError
from bot.instance_resolution import CliInstanceTarget
from bot.service_control_plane import ServiceControlOutcomeUnknownError
from bot.stores.chat_binding_store import ChatBindingStore
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_feishu_interaction_holder,
)
from bot.stores.instance_registry_store import InstanceRegistryEntry
from bot.stores.service_instance_lease import ServiceInstanceMaintenanceLease
from bot.thread_resolution import resolve_resume_target_by_name


@dataclass(frozen=True, slots=True)
class RuntimeAdminOfflineLifecyclePorts:
    resolve_target_instance: Callable[..., CliInstanceTarget]
    request: Callable[..., Any]
    attached_endpoint_adapter: Callable[..., tuple[Any, Any, str]]
    lifecycle_control_timeout_seconds: Callable[..., float]
    lease_owner_instance: Callable[[str], str]
    list_running_instances: Callable[[], list[InstanceRegistryEntry]]
    list_known_instance_names: Callable[[], list[str]]
    resolve_instance_paths: Callable[[str], Any]


@dataclass(frozen=True, slots=True)
class RuntimeAdminOfflineLifecycleReceipt:
    result: dict[str, Any]
    cleanup_results: tuple[dict[str, Any], ...] = ()
    cleanup_failures: tuple[dict[str, str], ...] = ()


class RuntimeAdminOfflineLifecycle:
    """Own one lifecycle mutation and its cross-instance local settlement."""

    def __init__(self, ports: RuntimeAdminOfflineLifecyclePorts) -> None:
        self._ports = ports

    def resolve_archive_name(
        self,
        target: CliInstanceTarget,
        thread_name: str,
    ) -> str:
        try:
            adapter, cfg, _app_server_url = (
                self._ports.attached_endpoint_adapter(
                    target.data_dir,
                    running_entry=target.running_entry,
                )
            )
            try:
                thread = resolve_resume_target_by_name(
                    adapter,
                    name=thread_name,
                    limit=cfg.thread_list_query_limit,
                )
            finally:
                adapter.stop()
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(
                "按名称解析 thread 失败；archive mutation 尚未发送，可安全重试："
                f"{exc}"
            ) from exc
        resolved_thread_id = str(thread.thread_id or "").strip()
        if not resolved_thread_id:
            raise ValueError(
                "按名称解析 thread 返回了空 thread_id；archive mutation 尚未发送。"
            )
        return resolved_thread_id

    def resolve_archive_targets(
        self,
        thread_ids: list[str],
        *,
        thread_name: str = "",
        explicit_instance: str = "",
    ) -> list[tuple[CliInstanceTarget, dict[str, str]]]:
        if thread_ids:
            if explicit_instance:
                target = self._ports.resolve_target_instance(explicit_instance)
                return [
                    (target, {"thread_id": thread_id})
                    for thread_id in thread_ids
                ]
            targets = []
            for thread_id in thread_ids:
                preferred_instance = self._ports.lease_owner_instance(thread_id)
                targets.append(
                    (
                        self._ports.resolve_target_instance(
                            None,
                            preferred_running_instance=preferred_instance,
                        ),
                        {"thread_id": thread_id},
                    )
                )
            return targets
        if explicit_instance:
            target = self._ports.resolve_target_instance(explicit_instance)
            thread_id = self.resolve_archive_name(target, thread_name)
            return [(target, {"thread_id": thread_id})]
        bootstrap_target = self._ports.resolve_target_instance(None)
        resolved_thread_id = self.resolve_archive_name(
            bootstrap_target,
            thread_name,
        )
        target_params = {"thread_id": resolved_thread_id}
        owner_instance = self._ports.lease_owner_instance(resolved_thread_id)
        if owner_instance:
            target = self._ports.resolve_target_instance(
                None,
                preferred_running_instance=owner_instance,
            )
            return [(target, target_params)]
        return [(bootstrap_target, target_params)]

    @staticmethod
    def _is_thread_unreadable_for_stale_cleanup_error(
        exc: Exception,
    ) -> bool:
        if not isinstance(exc, CodexRpcError):
            return False
        message = str(exc.error.get("message", "") or "").strip().lower()
        return (
            message.startswith("no rollout found for thread id ")
            or message.startswith("thread not found:")
            or message.startswith("thread not loaded:")
        )

    def _resolve_stale_binding_query_target(
        self,
    ) -> tuple[str, pathlib.Path, InstanceRegistryEntry]:
        running_instances = self._ports.list_running_instances()
        if not running_instances:
            raise ValueError(
                "binding clear-stale 需要至少一个运行中的实例，"
                "以便通过 app-server 验证 thread 是否仍存在。"
            )
        selected = sorted(
            running_instances,
            key=lambda item: (
                0
                if str(item.instance_name or "").strip().lower() == "default"
                else 1,
                item.instance_name,
            ),
        )[0]
        return (
            selected.instance_name,
            pathlib.Path(selected.data_dir),
            selected,
        )

    def build_thread_presence_checker(
        self,
        data_dir: pathlib.Path,
        *,
        running_entry: InstanceRegistryEntry,
    ):
        adapter, _cfg, _app_server_url = (
            self._ports.attached_endpoint_adapter(
                pathlib.Path(data_dir),
                running_entry=running_entry,
            )
        )
        cache: dict[str, tuple[str, str]] = {}

        def _check(thread_id: str) -> tuple[str, str]:
            normalized_thread_id = str(thread_id or "").strip()
            if not normalized_thread_id:
                return "skip", "empty_thread_id"
            cached = cache.get(normalized_thread_id)
            if cached is not None:
                return cached
            try:
                adapter.read_thread(normalized_thread_id, include_turns=False)
            except Exception as exc:
                if self._is_thread_unreadable_for_stale_cleanup_error(exc):
                    result = ("stale", str(exc) or "thread not found")
                else:
                    result = ("unknown", str(exc) or type(exc).__name__)
            else:
                result = ("present", "")
            cache[normalized_thread_id] = result
            return result

        return adapter, _check

    @staticmethod
    def _release_offline_binding_interaction_lease(
        interaction_leases: InteractionLeaseStore,
        *,
        binding: tuple[str, str],
        thread_id: str,
    ) -> None:
        holder = make_feishu_interaction_holder(
            binding[0],
            binding[1],
            owner_pid=0,
        )
        interaction_leases.release(thread_id, holder)
        remaining = interaction_leases.load(thread_id)
        if remaining is not None and remaining.holder.same_holder(holder):
            raise RuntimeError(
                "interaction lease 仍由已清理 binding 持有: "
                f"{format_binding_id(binding)}"
            )

    def clear_stale_bindings_from_store(
        self,
        data_dir: pathlib.Path,
        thread_presence_check,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        maintenance = (
            nullcontext()
            if dry_run
            else ServiceInstanceMaintenanceLease(pathlib.Path(data_dir))
        )
        with maintenance:
            store = ChatBindingStore(pathlib.Path(data_dir))
            interaction_leases = InteractionLeaseStore(pathlib.Path(data_dir))
            clear_bindings: list[tuple[tuple[str, str], str]] = []
            retained_binding_ids: list[str] = []
            skipped_binding_ids: list[str] = []
            unknown_threads: dict[str, str] = {}
            stale_thread_ids: set[str] = set()
            records = sorted(
                store.load_all().items(),
                key=lambda item: format_binding_id(item[0]),
            )
            for binding, state in records:
                binding_id = format_binding_id(binding)
                thread_id = str(
                    state.get("current_thread_id", "") or ""
                ).strip()
                if not thread_id:
                    skipped_binding_ids.append(binding_id)
                    continue
                status, reason = thread_presence_check(thread_id)
                if status == "stale":
                    clear_bindings.append((binding, thread_id))
                    stale_thread_ids.add(thread_id)
                    continue
                if status == "unknown":
                    unknown_threads.setdefault(thread_id, reason)
                retained_binding_ids.append(binding_id)

            if not dry_run:
                for binding, thread_id in clear_bindings:
                    self._release_offline_binding_interaction_lease(
                        interaction_leases,
                        binding=binding,
                        thread_id=thread_id,
                    )
                    store.clear(binding)
        cleared_binding_ids = [
            format_binding_id(binding)
            for binding, _thread_id in clear_bindings
        ]
        return {
            "cleared_binding_ids": (
                [] if dry_run else cleared_binding_ids
            ),
            "would_clear_binding_ids": (
                cleared_binding_ids if dry_run else []
            ),
            "stale_thread_ids": sorted(stale_thread_ids),
            "unknown_threads": [
                {"thread_id": thread_id, "reason": reason}
                for thread_id, reason in sorted(unknown_threads.items())
            ],
            "retained_binding_ids": retained_binding_ids,
            "skipped_binding_ids": skipped_binding_ids,
            "dry_run": bool(dry_run),
        }

    def _cleanup_stale_bindings_in_running_instance(
        self,
        data_dir: pathlib.Path,
        *,
        dry_run: bool,
    ) -> dict[str, Any]:
        result = self._ports.request(
            pathlib.Path(data_dir),
            "binding/clear-stale",
            {"dry_run": bool(dry_run)},
        )
        return dict(result)

    def clear_stale_bindings(
        self,
        *,
        explicit_instance: str = "",
        dry_run: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        normalized_explicit_instance = str(explicit_instance or "").strip()
        cleanup_results: list[dict[str, Any]] = []
        cleanup_failures: list[dict[str, str]] = []

        if normalized_explicit_instance:
            target = self._ports.resolve_target_instance(
                normalized_explicit_instance
            )
            if target.running_entry is not None:
                try:
                    result = self._cleanup_stale_bindings_in_running_instance(
                        target.data_dir,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    cleanup_failures.append(
                        {
                            "instance_name": target.instance_name,
                            "mode": "control-plane",
                            "reason": str(exc),
                        }
                    )
                else:
                    cleanup_results.append(
                        {
                            "instance_name": target.instance_name,
                            "mode": "control-plane",
                            **result,
                        }
                    )
                return cleanup_results, cleanup_failures

            query_name, query_dir, query_entry = (
                self._resolve_stale_binding_query_target()
            )
            adapter, thread_presence_check = (
                self.build_thread_presence_checker(
                    query_dir,
                    running_entry=query_entry,
                )
            )
            try:
                try:
                    result = self.clear_stale_bindings_from_store(
                        target.data_dir,
                        thread_presence_check,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    cleanup_failures.append(
                        {
                            "instance_name": target.instance_name,
                            "mode": "local-store",
                            "reason": str(exc),
                        }
                    )
                else:
                    cleanup_results.append(
                        {
                            "instance_name": target.instance_name,
                            "mode": "local-store",
                            "query_instance_name": query_name,
                            **result,
                        }
                    )
            finally:
                adapter.stop()
            return cleanup_results, cleanup_failures

        running_entries = self._ports.list_running_instances()
        if not running_entries:
            raise ValueError(
                "binding clear-stale 需要至少一个运行中的实例，"
                "以便通过 app-server 验证 thread 是否仍存在。"
            )
        running_instance_names = {
            str(entry.instance_name or "").strip().lower()
            for entry in running_entries
        }
        stopped_instance_names = [
            str(instance_name or "").strip().lower()
            for instance_name in self._ports.list_known_instance_names()
            if str(instance_name or "").strip().lower()
            and str(instance_name or "").strip().lower()
            not in running_instance_names
        ]
        adapter = None
        thread_presence_check = None
        query_instance_name = ""
        if stopped_instance_names:
            query_instance_name, query_dir, query_entry = (
                self._resolve_stale_binding_query_target()
            )
            adapter, thread_presence_check = (
                self.build_thread_presence_checker(
                    query_dir,
                    running_entry=query_entry,
                )
            )
        for entry in running_entries:
            instance_name = str(entry.instance_name or "").strip().lower()
            try:
                result = self._cleanup_stale_bindings_in_running_instance(
                    pathlib.Path(entry.data_dir),
                    dry_run=dry_run,
                )
            except Exception as exc:
                cleanup_failures.append(
                    {
                        "instance_name": instance_name,
                        "mode": "control-plane",
                        "reason": str(exc),
                    }
                )
            else:
                cleanup_results.append(
                    {
                        "instance_name": instance_name,
                        "mode": "control-plane",
                        **result,
                    }
                )

        try:
            for instance_name in stopped_instance_names:
                if thread_presence_check is None:
                    raise RuntimeError(
                        "stale binding thread presence checker was not initialized"
                    )
                paths = self._ports.resolve_instance_paths(instance_name)
                try:
                    result = self.clear_stale_bindings_from_store(
                        paths.data_dir,
                        thread_presence_check,
                        dry_run=dry_run,
                    )
                except Exception as exc:
                    cleanup_failures.append(
                        {
                            "instance_name": instance_name,
                            "mode": "local-store",
                            "reason": str(exc),
                        }
                    )
                    continue
                cleanup_results.append(
                    {
                        "instance_name": instance_name,
                        "mode": "local-store",
                        "query_instance_name": query_instance_name,
                        **result,
                    }
                )
        finally:
            if adapter is not None:
                adapter.stop()
        return cleanup_results, cleanup_failures

    @staticmethod
    def _same_path(
        left: pathlib.Path | str,
        right: pathlib.Path | str,
    ) -> bool:
        left_path = pathlib.Path(left).expanduser().resolve(strict=False)
        right_path = pathlib.Path(right).expanduser().resolve(strict=False)
        return left_path == right_path

    def clear_archived_thread_bindings_from_store(
        self,
        data_dir: pathlib.Path,
        thread_id: str,
        *,
        dry_run: bool = False,
    ) -> list[str]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return []
        maintenance = (
            nullcontext()
            if dry_run
            else ServiceInstanceMaintenanceLease(pathlib.Path(data_dir))
        )
        with maintenance:
            store = ChatBindingStore(pathlib.Path(data_dir))
            interaction_leases = InteractionLeaseStore(pathlib.Path(data_dir))
            cleared_binding_ids: list[str] = []
            records = sorted(
                store.load_all().items(),
                key=lambda item: format_binding_id(item[0]),
            )
            for binding, state in records:
                owner_thread_id = str(
                    state.get("current_thread_id", "") or ""
                ).strip()
                if owner_thread_id != normalized_thread_id:
                    continue
                if not dry_run:
                    self._release_offline_binding_interaction_lease(
                        interaction_leases,
                        binding=binding,
                        thread_id=normalized_thread_id,
                    )
                    store.clear(binding)
                cleared_binding_ids.append(format_binding_id(binding))
            return cleared_binding_ids

    def _cleanup_archived_bindings_in_running_instance(
        self,
        data_dir: pathlib.Path,
        thread_id: str,
        *,
        dry_run: bool,
    ) -> list[str]:
        result = self._ports.request(
            pathlib.Path(data_dir),
            "thread/clear-archived-bindings",
            {"thread_id": thread_id, "dry_run": bool(dry_run)},
        )
        return list(
            result.get("would_clear_binding_ids")
            or result.get("cleared_binding_ids")
            or []
        )

    def cleanup_archived_thread_bindings_in_scope(
        self,
        thread_id: str,
        *,
        explicit_instance: str = "",
        exclude_instance_name: str = "",
        exclude_data_dir: pathlib.Path | None = None,
        dry_run: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            return [], []

        normalized_exclude_instance = str(
            exclude_instance_name or ""
        ).strip().lower()
        normalized_exclude_data_dir = (
            pathlib.Path(exclude_data_dir)
            if exclude_data_dir is not None
            else None
        )
        normalized_explicit_instance = str(explicit_instance or "").strip()
        cleanup_results: list[dict[str, Any]] = []
        cleanup_failures: list[dict[str, str]] = []
        running_instance_names: set[str] = set()

        if normalized_explicit_instance:
            target = self._ports.resolve_target_instance(
                normalized_explicit_instance
            )
            try:
                if target.running_entry is not None:
                    cleared_binding_ids = (
                        self._cleanup_archived_bindings_in_running_instance(
                            target.data_dir,
                            normalized_thread_id,
                            dry_run=dry_run,
                        )
                    )
                    mode = "control-plane"
                else:
                    cleared_binding_ids = (
                        self.clear_archived_thread_bindings_from_store(
                            target.data_dir,
                            normalized_thread_id,
                            dry_run=dry_run,
                        )
                    )
                    mode = "local-store"
            except Exception as exc:
                return [], [
                    {
                        "instance_name": target.instance_name,
                        "mode": (
                            "control-plane"
                            if target.running_entry is not None
                            else "local-store"
                        ),
                        "reason": str(exc),
                    }
                ]
            return [
                {
                    "instance_name": target.instance_name,
                    "mode": mode,
                    "cleared_binding_ids": cleared_binding_ids,
                }
            ], []

        for entry in self._ports.list_running_instances():
            instance_name = str(entry.instance_name or "").strip().lower()
            running_instance_names.add(instance_name)
            entry_data_dir = pathlib.Path(entry.data_dir)
            if instance_name == normalized_exclude_instance:
                continue
            if (
                normalized_exclude_data_dir is not None
                and self._same_path(
                    entry_data_dir,
                    normalized_exclude_data_dir,
                )
            ):
                continue
            try:
                cleared_binding_ids = (
                    self._cleanup_archived_bindings_in_running_instance(
                        entry_data_dir,
                        normalized_thread_id,
                        dry_run=dry_run,
                    )
                )
            except Exception as exc:
                cleanup_failures.append(
                    {
                        "instance_name": instance_name,
                        "mode": "control-plane",
                        "reason": str(exc),
                    }
                )
                continue
            cleanup_results.append(
                {
                    "instance_name": instance_name,
                    "mode": "control-plane",
                    "cleared_binding_ids": cleared_binding_ids,
                }
            )

        for instance_name in self._ports.list_known_instance_names():
            normalized_instance_name = str(
                instance_name or ""
            ).strip().lower()
            if (
                not normalized_instance_name
                or normalized_instance_name == normalized_exclude_instance
                or normalized_instance_name in running_instance_names
            ):
                continue
            paths = self._ports.resolve_instance_paths(normalized_instance_name)
            if (
                normalized_exclude_data_dir is not None
                and self._same_path(
                    paths.data_dir,
                    normalized_exclude_data_dir,
                )
            ):
                continue
            try:
                cleared_binding_ids = (
                    self.clear_archived_thread_bindings_from_store(
                        paths.data_dir,
                        normalized_thread_id,
                        dry_run=dry_run,
                    )
                )
            except Exception as exc:
                cleanup_failures.append(
                    {
                        "instance_name": normalized_instance_name,
                        "mode": "local-store",
                        "reason": str(exc),
                    }
                )
                continue
            cleanup_results.append(
                {
                    "instance_name": normalized_instance_name,
                    "mode": "local-store",
                    "cleared_binding_ids": cleared_binding_ids,
                }
            )
        return cleanup_results, cleanup_failures

    def cleanup_archived_thread_bindings_in_other_instances(
        self,
        thread_id: str,
        *,
        target_instance_name: str,
        target_data_dir: pathlib.Path,
        dry_run: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        return self.cleanup_archived_thread_bindings_in_scope(
            thread_id,
            exclude_instance_name=target_instance_name,
            exclude_data_dir=target_data_dir,
            dry_run=dry_run,
        )

    def resolve_archived_thread_listing_target(
        self,
        explicit_instance: str = "",
    ) -> tuple[str, pathlib.Path, InstanceRegistryEntry]:
        normalized_explicit_instance = str(explicit_instance or "").strip()
        if normalized_explicit_instance:
            target = self._ports.resolve_target_instance(
                normalized_explicit_instance
            )
            if target.running_entry is None:
                raise ValueError(
                    "thread clear-archived-bindings --all 需要目标实例正在运行，"
                    "以便查询上游 archived thread 列表；"
                    "若已知 thread id，请改用 --thread-id。"
                )
            return target.instance_name, target.data_dir, target.running_entry

        running_instances = self._ports.list_running_instances()
        if not running_instances:
            raise ValueError(
                "thread clear-archived-bindings --all 需要至少一个运行中的实例，"
                "以便查询上游 archived thread 列表；"
                "若已知 thread id，请改用 --thread-id。"
            )
        selected = sorted(
            running_instances,
            key=lambda item: (
                0
                if str(item.instance_name or "").strip().lower() == "default"
                else 1,
                item.instance_name,
            ),
        )[0]
        return (
            selected.instance_name,
            pathlib.Path(selected.data_dir),
            selected,
        )

    def list_archived_thread_ids_from_running_instance(
        self,
        data_dir: pathlib.Path,
        *,
        running_entry: InstanceRegistryEntry,
        page_size: int = 100,
    ) -> list[str]:
        adapter, _cfg, _app_server_url = (
            self._ports.attached_endpoint_adapter(
                pathlib.Path(data_dir),
                running_entry=running_entry,
            )
        )
        seen_thread_ids: set[str] = set()
        archived_thread_ids: list[str] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        try:
            while True:
                page, cursor = adapter.list_threads(
                    limit=page_size,
                    cursor=cursor,
                    sort_key="updated_at",
                    model_providers=[],
                    archived=True,
                )
                for thread in page:
                    thread_id = str(thread.thread_id or "").strip()
                    if not thread_id or thread_id in seen_thread_ids:
                        continue
                    seen_thread_ids.add(thread_id)
                    archived_thread_ids.append(thread_id)
                if not cursor:
                    break
                if cursor in seen_cursors:
                    raise RuntimeError(
                        "thread/list archived pagination returned "
                        f"a repeated cursor: {cursor}"
                    )
                seen_cursors.add(cursor)
        finally:
            adapter.stop()
        return archived_thread_ids

    @staticmethod
    def validate_lifecycle_control_result(
        result: Any,
        *,
        action: str,
        expected_thread_id: str = "",
    ) -> dict[str, Any]:
        def _invalid(reason: str) -> None:
            raise ServiceControlOutcomeUnknownError(
                f"控制面已处理 `{action}`，但返回了畸形 lifecycle result："
                f"{reason}"
            )

        if not isinstance(result, dict):
            _invalid("result 不是对象")
        valid_actions = {
            "thread/archive",
            "thread/unarchive",
            "thread/delete",
        }
        if action not in valid_actions:
            raise ValueError(f"未知 lifecycle action：{action}")
        outcome = result.get("upstream_outcome")
        if outcome not in {"success", "error", "unknown"}:
            _invalid("缺少有效 upstream_outcome")
        focus_cleanup = result.get("focus_cleanup")
        if focus_cleanup not in {"complete", "incomplete", "skipped"}:
            _invalid("缺少有效 focus_cleanup")
        thread_id = result.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id.strip():
            _invalid("缺少有效 thread_id")
        expected = str(expected_thread_id or "").strip()
        if expected and thread_id.strip() != expected:
            _invalid(
                f"thread_id 不匹配：expected={expected}, "
                f"actual={thread_id.strip()}"
            )
        if not isinstance(result.get("thread_title"), str):
            _invalid("thread_title 不是字符串")
        if not isinstance(result.get("working_dir"), str):
            _invalid("working_dir 不是字符串")
        cleared_binding_ids = result.get("cleared_binding_ids")
        if not isinstance(cleared_binding_ids, list) or not all(
            isinstance(item, str) for item in cleared_binding_ids
        ):
            _invalid("cleared_binding_ids 必须为 list[str]")
        cleanup_errors = result.get("cleanup_errors")
        if not isinstance(cleanup_errors, list) or not all(
            isinstance(item, str) for item in cleanup_errors
        ):
            _invalid("cleanup_errors 必须为 list[str]")
        if outcome in {"error", "unknown"} and focus_cleanup != "skipped":
            _invalid(
                f"upstream_outcome={outcome} 时 focus_cleanup 必须为 skipped"
            )
        if outcome == "success":
            if action == "thread/unarchive" and focus_cleanup != "skipped":
                _invalid(
                    "thread/unarchive 成功时 focus_cleanup 必须为 skipped"
                )
            if action in {"thread/archive", "thread/delete"} and (
                focus_cleanup not in {"complete", "incomplete"}
            ):
                _invalid(
                    f"{action} 成功时 focus_cleanup 必须为 complete 或 incomplete"
                )
        if focus_cleanup == "complete" and cleanup_errors:
            _invalid("focus_cleanup=complete 时 cleanup_errors 必须为空")
        if focus_cleanup == "incomplete" and not cleanup_errors:
            _invalid("focus_cleanup=incomplete 时 cleanup_errors 不能为空")
        if focus_cleanup == "skipped" and (
            cleared_binding_ids or cleanup_errors
        ):
            _invalid(
                "focus_cleanup=skipped 时不能包含 binding cleanup 结果"
            )
        return result

    def archive_thread(
        self,
        data_dir: pathlib.Path,
        thread_id: str,
        *,
        instance_name: str = "",
        running_entry: InstanceRegistryEntry | None = None,
    ) -> RuntimeAdminOfflineLifecycleReceipt:
        normalized_thread_id = str(thread_id or "").strip()
        if not normalized_thread_id:
            raise ValueError("thread archive mutation 只接受已解析的 thread_id。")
        result = self.validate_lifecycle_control_result(
            self._ports.request(
                data_dir,
                "thread/archive",
                {"thread_id": normalized_thread_id},
                timeout_seconds=(
                    self._ports.lifecycle_control_timeout_seconds(
                        data_dir,
                        operation="archive",
                        running_entry=running_entry,
                    )
                ),
            ),
            action="thread/archive",
            expected_thread_id=normalized_thread_id,
        )
        cleanup_results: list[dict[str, Any]] = []
        cleanup_failures: list[dict[str, str]] = []
        if result.get("upstream_outcome") == "success":
            cleanup_results, cleanup_failures = (
                self.cleanup_archived_thread_bindings_in_other_instances(
                    str(result["thread_id"]),
                    target_instance_name=instance_name,
                    target_data_dir=data_dir,
                )
            )
        return RuntimeAdminOfflineLifecycleReceipt(
            result=result,
            cleanup_results=tuple(cleanup_results),
            cleanup_failures=tuple(cleanup_failures),
        )

    def _thread_binding_locations(
        self,
        thread_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        normalized_thread_id = str(thread_id or "").strip()
        locations: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        running_instance_names: set[str] = set()
        for entry in self._ports.list_running_instances():
            instance_name = str(entry.instance_name or "").strip().lower()
            running_instance_names.add(instance_name)
            try:
                result = self._ports.request(
                    pathlib.Path(entry.data_dir),
                    "thread/local-bindings",
                    {"thread_id": normalized_thread_id},
                )
            except Exception as exc:
                failures.append(
                    {
                        "instance_name": instance_name,
                        "mode": "control-plane",
                        "reason": str(exc),
                    }
                )
                continue
            locations.append(
                {
                    "instance_name": instance_name,
                    "mode": "control-plane",
                    "binding_ids": list(result.get("binding_ids") or []),
                    "running_binding_ids": list(
                        result.get("running_binding_ids") or []
                    ),
                    "pending_binding_ids": list(
                        result.get("pending_binding_ids") or []
                    ),
                }
            )

        for instance_name in self._ports.list_known_instance_names():
            normalized_instance_name = str(
                instance_name or ""
            ).strip().lower()
            if (
                not normalized_instance_name
                or normalized_instance_name in running_instance_names
            ):
                continue
            paths = self._ports.resolve_instance_paths(normalized_instance_name)
            try:
                with ServiceInstanceMaintenanceLease(paths.data_dir):
                    binding_ids = [
                        format_binding_id(binding)
                        for binding, state in sorted(
                            ChatBindingStore(paths.data_dir).load_all().items(),
                            key=lambda item: format_binding_id(item[0]),
                        )
                        if str(
                            state.get("current_thread_id", "") or ""
                        ).strip()
                        == normalized_thread_id
                    ]
            except Exception as exc:
                failures.append(
                    {
                        "instance_name": normalized_instance_name,
                        "mode": "local-store",
                        "reason": str(exc),
                    }
                )
                continue
            locations.append(
                {
                    "instance_name": normalized_instance_name,
                    "mode": "local-store",
                    "binding_ids": binding_ids,
                    "running_binding_ids": [],
                    "pending_binding_ids": [],
                }
            )
        return locations, failures

    def _validate_unarchive_binding_preflight(self, thread_id: str) -> None:
        locations, failures = self._thread_binding_locations(thread_id)
        if failures:
            details = "; ".join(
                f"{item['instance_name']} ({item['mode']}): {item['reason']}"
                for item in failures
            )
            raise ValueError(
                "无法完整检查本机 Focus bindings，拒绝 unarchive："
                f"{details}"
            )
        residual = [
            (item["instance_name"], binding_id)
            for item in locations
            for binding_id in item.get("binding_ids") or []
        ]
        if residual:
            details = ", ".join(
                f"{instance}:{binding_id}"
                for instance, binding_id in residual
            )
            raise ValueError(
                "仍有本地 binding 指向该 archived thread；"
                "请先执行 `focusctl thread clear-archived-bindings "
                "--thread-id <id>`："
                + details
            )

    def _validate_delete_binding_preflight(self, thread_id: str) -> None:
        locations, failures = self._thread_binding_locations(thread_id)
        if failures:
            details = "; ".join(
                f"{item['instance_name']} ({item['mode']}): {item['reason']}"
                for item in failures
            )
            raise ValueError(
                "无法完整检查本机 Focus runtime，拒绝 delete："
                f"{details}"
            )
        running = [
            (item["instance_name"], binding_id)
            for item in locations
            for binding_id in item.get("running_binding_ids") or []
        ]
        pending = [
            (item["instance_name"], binding_id)
            for item in locations
            for binding_id in item.get("pending_binding_ids") or []
        ]
        if running or pending:
            details = [
                f"running {instance}:{binding_id}"
                for instance, binding_id in running
            ]
            details.extend(
                f"pending {instance}:{binding_id}"
                for instance, binding_id in pending
            )
            raise ValueError(
                "该 thread 仍有已知 Focus 活动，拒绝 delete："
                + ", ".join(details)
            )

    def unarchive_thread(
        self,
        data_dir: pathlib.Path,
        thread_id: str,
        *,
        running_entry: InstanceRegistryEntry | None = None,
    ) -> RuntimeAdminOfflineLifecycleReceipt:
        self._validate_unarchive_binding_preflight(thread_id)
        result = self.validate_lifecycle_control_result(
            self._ports.request(
                data_dir,
                "thread/unarchive",
                {"thread_id": thread_id},
                timeout_seconds=(
                    self._ports.lifecycle_control_timeout_seconds(
                        data_dir,
                        operation="unarchive",
                        running_entry=running_entry,
                    )
                ),
            ),
            action="thread/unarchive",
            expected_thread_id=thread_id,
        )
        return RuntimeAdminOfflineLifecycleReceipt(result=result)

    def delete_thread(
        self,
        data_dir: pathlib.Path,
        thread_id: str,
        *,
        instance_name: str,
        confirm: Callable[[str], bool],
        running_entry: InstanceRegistryEntry | None = None,
    ) -> RuntimeAdminOfflineLifecycleReceipt | None:
        self._validate_delete_binding_preflight(thread_id)
        if not confirm(thread_id):
            return None
        result = self.validate_lifecycle_control_result(
            self._ports.request(
                data_dir,
                "thread/delete",
                {"thread_id": thread_id},
                timeout_seconds=(
                    self._ports.lifecycle_control_timeout_seconds(
                        data_dir,
                        operation="delete",
                        running_entry=running_entry,
                    )
                ),
            ),
            action="thread/delete",
            expected_thread_id=thread_id,
        )
        cleanup_results: list[dict[str, Any]] = []
        cleanup_failures: list[dict[str, str]] = []
        if result.get("upstream_outcome") == "success":
            cleanup_results, cleanup_failures = (
                self.cleanup_archived_thread_bindings_in_other_instances(
                    str(result.get("thread_id") or thread_id),
                    target_instance_name=instance_name,
                    target_data_dir=data_dir,
                )
            )
        return RuntimeAdminOfflineLifecycleReceipt(
            result=result,
            cleanup_results=tuple(cleanup_results),
            cleanup_failures=tuple(cleanup_failures),
        )
