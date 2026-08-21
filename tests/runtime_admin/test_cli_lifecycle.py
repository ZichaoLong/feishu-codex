import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from bot.adapters.base import ThreadSummary
from bot.codex_config import CodexConfig
from bot.instance_resolution import CliInstanceTarget
from bot.runtime_admin.cli import (
    _archive_thread,
    _archive_threads,
    _clear_archived_thread_bindings,
    _clear_stale_bindings,
    _confirm_delete_thread,
    _delete_thread,
    _offline_lifecycle,
    _resolve_thread_archive_target,
    _resolve_thread_archive_targets,
    _unarchive_thread,
    _unarchive_threads,
)
from bot.runtime_admin.cli_inputs import build_runtime_admin_parser
from bot.service_control_plane import (
    ServiceControlError,
    ServiceControlOutcomeUnknownError,
)
from bot.stores.instance_registry_store import InstanceRegistryEntry


def _resolve_thread_archive_name(*args, **kwargs):
    return _offline_lifecycle().resolve_archive_name(*args, **kwargs)


class RuntimeAdminCliLifecycleTests(unittest.TestCase):
    def test_thread_archive_rejects_mixing_thread_id_and_thread_name(self) -> None:
        parser = build_runtime_admin_parser()

        args = parser.parse_args(["thread", "archive", "--thread-id", "thread-1", "--thread-name", "demo"])

        with self.assertRaisesRegex(ValueError, "不能同时提供"):
            _resolve_thread_archive_targets(args)

    def test_archive_thread_cleans_other_instances_after_archive(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": ["p2p:ou_user:chat-1"],
            "cleanup_errors": [],
        }
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        calls: list[tuple[Path, str, dict[str, object]]] = []

        def _fake_request(
            data_dir: Path,
            method: str,
            params: dict[str, object],
            *,
            timeout_seconds: float = 3.0,
        ):
            del timeout_seconds
            calls.append((data_dir, method, params))
            if method == "thread/archive":
                return snapshot
            self.assertEqual(method, "thread/clear-archived-bindings")
            self.assertEqual(params, {"thread_id": "thread-1", "dry_run": False})
            return {"thread_id": "thread-1", "cleared_binding_ids": ["p2p:ou_other:chat-2"]}

        with patch("bot.runtime_admin.cli._request", side_effect=_fake_request):
            with patch("bot.runtime_admin.cli.list_running_instances", return_value=[explorer_entry]):
                with patch("bot.runtime_admin.cli.list_known_instance_names", return_value=["default", "explorer"]):
                    with redirect_stdout(stdout):
                        result = _archive_thread(
                            Path("/tmp/default-data"),
                            {"thread_id": "thread-1"},
                            instance_name="default",
                        )

        self.assertEqual(result, 0)
        self.assertEqual(
            [(str(data_dir), method) for data_dir, method, _params in calls],
            [
                ("/tmp/default-data", "thread/archive"),
                ("/tmp/explorer-data", "thread/clear-archived-bindings"),
            ],
        )
        rendered = stdout.getvalue()
        self.assertIn("instance: default", rendered)
        self.assertIn("cleared bindings in this instance: p2p:ou_user:chat-1", rendered)
        self.assertIn("explorer (control-plane): p2p:ou_other:chat-2", rendered)
        self.assertIn("其他可达运行实例与已知非运行实例", rendered)

    def test_archive_thread_reports_cleanup_failure(self) -> None:
        stdout = io.StringIO()
        snapshot = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin.cli._request", return_value=snapshot):
            with patch(
                "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.cleanup_archived_thread_bindings_in_other_instances",
                return_value=(
                    [],
                    [{"instance_name": "explorer", "mode": "control-plane", "reason": "down"}],
                ),
            ):
                with redirect_stdout(stdout):
                    result = _archive_thread(
                        Path("/tmp/instance-data"),
                        {"thread_id": "thread-1"},
                        instance_name="explorer",
                    )

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("instance: explorer", rendered)
        self.assertIn("cleanup warnings:", rendered)
        self.assertIn("explorer (control-plane): down", rendered)
        self.assertIn("已尝试清理其他可达运行实例", rendered)
        self.assertNotIn("已同时清理其他可达运行实例", rendered)

    def test_archive_thread_rejects_unresolved_name_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "只接受已解析的 thread_id"):
            _archive_thread(
                Path("/tmp/default-data"),
                {"thread_name": "demo"},
                instance_name="default",
            )

    def test_archive_thread_treats_malformed_lifecycle_result_as_unknown(self) -> None:
        stdout = io.StringIO()
        with patch("bot.runtime_admin.cli._request", return_value={}):
            with redirect_stdout(stdout):
                result = _archive_thread(
                    Path("/tmp/default-data"),
                    {"thread_id": "thread-1"},
                    instance_name="default",
                )

        self.assertEqual(result, 3)
        self.assertIn("upstream outcome: unknown", stdout.getvalue())
        self.assertIn("畸形 lifecycle result", stdout.getvalue())

    def test_archive_thread_rejects_non_string_cleanup_items_as_unknown(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": [1],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin.cli._request", return_value=result_payload):
            with redirect_stdout(stdout):
                result = _archive_thread(
                    Path("/tmp/default-data"),
                    {"thread_id": "thread-1"},
                    instance_name="default",
                )

        self.assertEqual(result, 3)
        self.assertIn("upstream outcome: unknown", stdout.getvalue())

    def test_archive_thread_rejects_invalid_outcome_cleanup_combination(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "skipped",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch("bot.runtime_admin.cli._request", return_value=result_payload):
            with redirect_stdout(stdout):
                result = _archive_thread(
                    Path("/tmp/default-data"),
                    {"thread_id": "thread-1"},
                    instance_name="default",
                )

        self.assertEqual(result, 3)
        self.assertIn("focus_cleanup", stdout.getvalue())

    def test_clear_archived_thread_bindings_public_command_prints_dry_run(self) -> None:
        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.cleanup_archived_thread_bindings_in_scope",
            return_value=(
                [
                    {
                        "instance_name": "explorer",
                        "mode": "local-store",
                        "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                    }
                ],
                [],
            ),
        ) as mock_cleanup:
            with redirect_stdout(stdout):
                result = _clear_archived_thread_bindings("thread-1", dry_run=True)

        self.assertEqual(result, 0)
        self.assertEqual(mock_cleanup.call_args.kwargs["explicit_instance"], "")
        self.assertTrue(mock_cleanup.call_args.kwargs["dry_run"])
        rendered = stdout.getvalue()
        self.assertIn("thread: thread-1", rendered)
        self.assertIn("scope: all known instances", rendered)
        self.assertIn("mode: dry-run", rendered)
        self.assertIn("would clear bindings:", rendered)
        self.assertIn("explorer (local-store): p2p:ou_user:chat-1", rendered)

    def test_clear_archived_thread_bindings_all_requires_running_instance_to_query(self) -> None:
        with patch("bot.runtime_admin.cli.list_running_instances", return_value=[]):
            with self.assertRaisesRegex(ValueError, "至少一个运行中的实例"):
                _clear_archived_thread_bindings(all_archived=True)

    def test_clear_archived_thread_bindings_all_rejects_stopped_explicit_instance(self) -> None:
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
            with self.assertRaisesRegex(ValueError, "目标实例正在运行"):
                _clear_archived_thread_bindings(all_archived=True, explicit_instance="explorer")

    def test_clear_archived_thread_bindings_all_cleans_each_archived_thread(self) -> None:
        stdout = io.StringIO()
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )

        def _fake_cleanup(thread_id: str, **kwargs):
            self.assertEqual(kwargs["explicit_instance"], "")
            self.assertTrue(kwargs["dry_run"])
            if thread_id == "thread-2":
                return [], []
            return [
                {
                    "instance_name": "explorer",
                    "mode": "local-store",
                    "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                }
            ], []

        with patch(
            "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.resolve_archived_thread_listing_target",
            return_value=("explorer", Path("/tmp/explorer-data"), explorer_entry),
        ) as mock_resolve:
            with patch(
                "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.list_archived_thread_ids_from_running_instance",
                return_value=["thread-2", "thread-1"],
            ) as mock_list_archived:
                with patch(
                    "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.cleanup_archived_thread_bindings_in_scope",
                    side_effect=_fake_cleanup,
                ) as mock_cleanup:
                    with redirect_stdout(stdout):
                        result = _clear_archived_thread_bindings(all_archived=True, dry_run=True)

        self.assertEqual(result, 0)
        mock_resolve.assert_called_once_with("")
        self.assertEqual(mock_list_archived.call_args.kwargs["running_entry"], explorer_entry)
        self.assertEqual([call.args[0] for call in mock_cleanup.call_args_list], ["thread-2", "thread-1"])
        rendered = stdout.getvalue()
        self.assertIn("archived query instance: explorer", rendered)
        self.assertIn("archived threads: 2", rendered)
        self.assertIn("scope: all known instances", rendered)
        self.assertIn("thread: thread-1", rendered)
        self.assertIn("would clear bindings:", rendered)
        self.assertIn(
            "summary: archived_threads=2 threads_with_bindings=1 would_clear_bindings=1 cleanup_failed=0",
            rendered,
        )

    def test_clear_stale_bindings_explicit_running_instance_uses_control_plane(self) -> None:
        stdout = io.StringIO()
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="explorer",
            data_dir=Path("/tmp/explorer-data"),
            running_entry=explorer_entry,
        )

        def _fake_request(data_dir: Path, method: str, params: dict[str, object]):
            self.assertEqual(data_dir, Path("/tmp/explorer-data"))
            self.assertEqual(method, "binding/clear-stale")
            self.assertEqual(params, {"dry_run": True})
            return {
                "would_clear_binding_ids": ["p2p:ou_user:chat-stale"],
                "stale_thread_ids": ["thread-stale"],
                "unknown_threads": [],
                "dry_run": True,
            }

        with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
            with patch("bot.runtime_admin.cli._request", side_effect=_fake_request):
                with redirect_stdout(stdout):
                    result = _clear_stale_bindings(explicit_instance="explorer", dry_run=True)

        self.assertEqual(result, 0)
        rendered = stdout.getvalue()
        self.assertIn("scope: explorer", rendered)
        self.assertIn("mode: dry-run", rendered)
        self.assertIn("explorer (control-plane)", rendered)
        self.assertIn("would clear stale bindings: p2p:ou_user:chat-stale", rendered)

    def test_clear_stale_bindings_returns_nonzero_when_threads_are_unknown(self) -> None:
        explorer_entry = InstanceRegistryEntry(
            instance_name="explorer",
            owner_pid=123,
            service_token="svc-token",
            control_endpoint="tcp://127.0.0.1:32002",
            app_server_url="ws://127.0.0.1:9002",
            config_dir="/tmp/explorer-config",
            data_dir="/tmp/explorer-data",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="explorer",
            data_dir=Path("/tmp/explorer-data"),
            running_entry=explorer_entry,
        )

        with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
            with patch(
                "bot.runtime_admin.cli._request",
                return_value={
                    "cleared_binding_ids": [],
                    "stale_thread_ids": [],
                    "unknown_threads": [{"thread_id": "thread-unknown", "reason": "lookup_error"}],
                },
            ):
                with redirect_stdout(io.StringIO()):
                    result = _clear_stale_bindings(explicit_instance="explorer")

        self.assertEqual(result, 1)

    def test_archive_threads_batches_partial_failures(self) -> None:
        stdout = io.StringIO()
        target_a = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))
        target_b = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))

        def _fake_request(
            data_dir: Path,
            method: str,
            params: dict[str, str],
            *,
            timeout_seconds: float = 3.0,
        ):
            del timeout_seconds
            self.assertEqual(method, "thread/archive")
            if params["thread_id"] == "thread-2":
                raise ServiceControlError("busy")
            return {
                "thread_id": params["thread_id"],
                "thread_title": "demo",
                "working_dir": str(data_dir),
                "upstream_outcome": "success",
                "focus_cleanup": "complete",
                "cleared_binding_ids": ["p2p:ou_user:chat-1"],
                "cleanup_errors": [],
            }

        with patch("bot.runtime_admin.cli._lease_owner_instance", side_effect=["explorer", "default"]):
            with patch("bot.runtime_admin.cli._resolve_target_instance", side_effect=[target_a, target_b]):
                with patch("bot.runtime_admin.cli._request", side_effect=_fake_request):
                    with patch(
                        "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.cleanup_archived_thread_bindings_in_other_instances",
                        return_value=([], []),
                    ):
                        with redirect_stdout(stdout):
                            result = _archive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("batch archive: total=2", rendered)
        self.assertIn("[1/2] thread: thread-1", rendered)
        self.assertIn("[2/2] thread: thread-2", rendered)
        self.assertIn("instance: explorer", rendered)
        self.assertIn("instance: default", rendered)
        self.assertIn("status: archived", rendered)
        self.assertIn("status: failed", rendered)
        self.assertIn("summary: archived=1 failed=1", rendered)

    def test_archive_threads_stops_immediately_on_unknown_outcome(self) -> None:
        stdout = io.StringIO()
        target = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))

        with patch("bot.runtime_admin.cli._lease_owner_instance", return_value="default"):
            with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
                with patch(
                    "bot.runtime_admin.cli._request",
                    side_effect=ServiceControlOutcomeUnknownError("response lost"),
                ) as mock_request:
                    with redirect_stdout(stdout):
                        result = _archive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 3)
        self.assertEqual(mock_request.call_count, 1)
        self.assertIn("status: unknown", stdout.getvalue())
        self.assertIn("batch 已停止", stdout.getvalue())

    def test_unarchive_thread_success_does_not_create_binding(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "skipped",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch(
            "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle._validate_unarchive_binding_preflight"
        ) as mock_preflight:
            with patch("bot.runtime_admin.cli._request", return_value=result_payload) as mock_request:
                with redirect_stdout(stdout):
                    result = _unarchive_thread(
                        Path("/tmp/default-data"),
                        "thread-1",
                        instance_name="default",
                    )

        self.assertEqual(result, 0)
        mock_preflight.assert_called_once_with("thread-1")
        self.assertEqual(mock_request.call_args.args[:3], (
            Path("/tmp/default-data"),
            "thread/unarchive",
            {"thread_id": "thread-1"},
        ))
        rendered = stdout.getvalue()
        self.assertIn("恢复为未归档状态并回到常规列表", rendered)
        self.assertIn("当前仍未加载，也未创建 binding", rendered)
        self.assertIn("focus resume thread-1", rendered)
        self.assertIn("/resume thread-1", rendered)

    def test_unarchive_threads_runs_each_target_and_reports_summary(self) -> None:
        stdout = io.StringIO()
        running_entry = InstanceRegistryEntry(
            instance_name="default",
            owner_pid=1234,
            service_token="token-default",
            control_endpoint="tcp://127.0.0.1:9000",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="/tmp/config-default",
            data_dir="/tmp/data-default",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="default",
            data_dir=Path("/tmp/data-default"),
            running_entry=running_entry,
        )

        with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
            with patch("bot.runtime_admin.cli._unarchive_thread", return_value=0) as mock_unarchive:
                with redirect_stdout(stdout):
                    result = _unarchive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 0)
        self.assertEqual(mock_unarchive.call_count, 2)
        self.assertEqual(
            [item.args[1] for item in mock_unarchive.call_args_list],
            ["thread-1", "thread-2"],
        )
        self.assertIn("summary: unarchived=2 failed=0", stdout.getvalue())

    def test_unarchive_threads_continues_after_failure_but_stops_on_unknown(self) -> None:
        stdout = io.StringIO()
        running_entry = InstanceRegistryEntry(
            instance_name="default",
            owner_pid=1234,
            service_token="token-default",
            control_endpoint="tcp://127.0.0.1:9000",
            app_server_url="ws://127.0.0.1:8765",
            config_dir="/tmp/config-default",
            data_dir="/tmp/data-default",
            started_at=1.0,
            updated_at=1.0,
        )
        target = CliInstanceTarget(
            instance_name="default",
            data_dir=Path("/tmp/data-default"),
            running_entry=running_entry,
        )

        with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
            with patch(
                "bot.runtime_admin.cli._unarchive_thread",
                side_effect=[ValueError("blocked"), 0, 3, 0],
            ) as mock_unarchive:
                with redirect_stdout(stdout):
                    result = _unarchive_threads(
                        ["thread-1", "thread-2", "thread-3", "thread-4"]
                    )

        self.assertEqual(result, 3)
        self.assertEqual(mock_unarchive.call_count, 3)
        rendered = stdout.getvalue()
        self.assertIn("reason: blocked", rendered)
        self.assertIn("summary: unarchived=1 failed=1 unknown=1", rendered)
        self.assertIn("batch 已停止", rendered)

    def test_unarchive_thread_treats_malformed_lifecycle_result_as_unknown(self) -> None:
        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle._validate_unarchive_binding_preflight"
        ):
            with patch("bot.runtime_admin.cli._request", return_value={}):
                with redirect_stdout(stdout):
                    result = _unarchive_thread(
                        Path("/tmp/default-data"),
                        "thread-1",
                        instance_name="default",
                    )

        self.assertEqual(result, 3)
        self.assertIn("upstream outcome: unknown", stdout.getvalue())
        self.assertIn("畸形 lifecycle result", stdout.getvalue())

    def test_delete_thread_success_uses_root_only_contract(self) -> None:
        stdout = io.StringIO()
        result_payload = {
            "thread_id": "thread-1",
            "thread_title": "demo",
            "working_dir": "/tmp/project",
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleared_binding_ids": [],
            "cleanup_errors": [],
        }
        with patch(
            "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle._validate_delete_binding_preflight"
        ) as mock_preflight:
            with patch("bot.runtime_admin.cli._request", return_value=result_payload):
                with patch(
                    "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.cleanup_archived_thread_bindings_in_other_instances",
                    return_value=([], []),
                ):
                    with redirect_stdout(stdout):
                        result = _delete_thread(
                            Path("/tmp/default-data"),
                            "thread-1",
                            instance_name="default",
                            force=True,
                        )

        self.assertEqual(result, 0)
        mock_preflight.assert_called_once_with("thread-1")
        self.assertIn("可能同时删除 spawned descendants", stdout.getvalue())
        self.assertIn("binding clear-stale --dry-run", stdout.getvalue())

    def test_delete_thread_treats_malformed_lifecycle_result_as_unknown(self) -> None:
        stdout = io.StringIO()
        with patch(
            "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle._validate_delete_binding_preflight"
        ):
            with patch("bot.runtime_admin.cli._request", return_value={}):
                with patch(
                    "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.cleanup_archived_thread_bindings_in_other_instances"
                ) as mock_cleanup:
                    with redirect_stdout(stdout):
                        result = _delete_thread(
                            Path("/tmp/default-data"),
                            "thread-1",
                            instance_name="default",
                            force=True,
                        )

        self.assertEqual(result, 3)
        mock_cleanup.assert_not_called()
        self.assertIn("upstream outcome: unknown", stdout.getvalue())
        self.assertIn("畸形 lifecycle result", stdout.getvalue())

    def test_delete_confirmation_requires_force_when_noninteractive(self) -> None:
        with patch.object(sys.stdin, "isatty", return_value=False):
            with self.assertRaisesRegex(ValueError, "必须显式提供 `--force`"):
                _confirm_delete_thread("thread-1", force=False)

    def test_delete_force_still_prints_focus_safety_scope(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            confirmed = _confirm_delete_thread("thread-1", force=True)

        self.assertTrue(confirmed)
        self.assertIn("仅协调本机已知 Focus/fcodex runtime", stdout.getvalue())

    def test_archive_threads_continues_after_target_resolution_failure(self) -> None:
        stdout = io.StringIO()
        target_b = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))

        with patch("bot.runtime_admin.cli._lease_owner_instance", side_effect=["explorer", "default"]):
            with patch(
                "bot.runtime_admin.cli._resolve_target_instance",
                side_effect=[ValueError("ambiguous instance"), target_b],
            ):
                with patch(
                    "bot.runtime_admin.cli._request",
                    return_value={
                        "thread_id": "thread-2",
                        "thread_title": "demo",
                        "working_dir": "/tmp/default-data",
                        "upstream_outcome": "success",
                        "focus_cleanup": "complete",
                        "cleared_binding_ids": [],
                        "cleanup_errors": [],
                    },
                ):
                    with patch(
                        "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.cleanup_archived_thread_bindings_in_other_instances",
                        return_value=([], []),
                    ):
                        with redirect_stdout(stdout):
                            result = _archive_threads(["thread-1", "thread-2"])

        self.assertEqual(result, 1)
        rendered = stdout.getvalue()
        self.assertIn("ambiguous instance", rendered)
        self.assertIn("status: failed", rendered)
        self.assertIn("status: archived", rendered)
        self.assertIn("summary: archived=1 failed=1", rendered)

    def test_resolve_thread_archive_target_prefers_live_runtime_owner_for_thread_name(self) -> None:
        parser = build_runtime_admin_parser()
        args = parser.parse_args(["thread", "archive", "--thread-name", "demo"])
        bootstrap = CliInstanceTarget(instance_name="default", data_dir=Path("/tmp/default-data"))
        owner_target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))
        with patch("bot.runtime_admin.cli._resolve_target_instance", side_effect=[bootstrap, owner_target]):
            with patch(
                "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.resolve_archive_name",
                return_value="thread-1",
            ):
                with patch("bot.runtime_admin.cli._lease_owner_instance", return_value="explorer"):
                    target, target_params = _resolve_thread_archive_target(args)

        self.assertEqual(target.instance_name, "explorer")
        self.assertEqual(target.data_dir, Path("/tmp/explorer-data"))
        self.assertEqual(target_params, {"thread_id": "thread-1"})

    def test_resolve_thread_archive_target_resolves_name_before_explicit_instance_mutation(self) -> None:
        parser = build_runtime_admin_parser()
        args = parser.parse_args(
            ["--instance", "explorer", "thread", "archive", "--thread-name", "demo"]
        )
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        with patch("bot.runtime_admin.cli._resolve_target_instance", return_value=target):
            with patch(
                "bot.runtime_admin.offline_lifecycle.RuntimeAdminOfflineLifecycle.resolve_archive_name",
                return_value="thread-1",
            ) as mock_resolve_name:
                resolved_target, target_params = _resolve_thread_archive_target(args)

        self.assertIs(resolved_target, target)
        self.assertEqual(target_params, {"thread_id": "thread-1"})
        mock_resolve_name.assert_called_once_with(target, "demo")

    def test_resolve_thread_archive_name_reports_read_only_failure_as_safe_to_retry(self) -> None:
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        class FailingAdapter:
            def list_threads(self, **_kwargs):
                raise TimeoutError("page timed out")

            def stop(self) -> None:
                self.stopped = True

        adapter = FailingAdapter()
        adapter.stopped = False
        with patch(
            "bot.runtime_admin.cli._attached_endpoint_adapter",
            return_value=(adapter, CodexConfig(), "ws://127.0.0.1:8765"),
        ):
            with self.assertRaisesRegex(ValueError, "mutation 尚未发送，可安全重试"):
                _resolve_thread_archive_name(target, "demo")

        self.assertTrue(adapter.stopped)

    def test_resolve_thread_archive_name_uses_read_only_paginated_resolver(self) -> None:
        target = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))

        class FakeAdapter:
            def stop(self) -> None:
                self.stopped = True

        adapter = FakeAdapter()
        adapter.stopped = False
        summary = ThreadSummary(
            thread_id="thread-1",
            cwd="/tmp/project",
            name="demo",
            preview="",
            created_at=0,
            updated_at=0,
            source="cli",
            status="idle",
        )
        with patch(
            "bot.runtime_admin.cli._attached_endpoint_adapter",
            return_value=(
                adapter,
                CodexConfig(thread_list_query_limit=37),
                "ws://127.0.0.1:8765",
            ),
        ):
            with patch(
                "bot.runtime_admin.offline_lifecycle.resolve_resume_target_by_name",
                return_value=summary,
            ) as mock_resolve:
                thread_id = _resolve_thread_archive_name(target, "demo")

        self.assertEqual(thread_id, "thread-1")
        self.assertTrue(adapter.stopped)
        mock_resolve.assert_called_once_with(adapter, name="demo", limit=37)

    def test_resolve_thread_archive_targets_prefers_live_runtime_owner_for_each_thread_id(self) -> None:
        parser = build_runtime_admin_parser()
        args = parser.parse_args(
            ["thread", "archive", "--thread-id", "thread-1", "--thread-id", "thread-2"]
        )
        target_a = CliInstanceTarget(instance_name="explorer", data_dir=Path("/tmp/explorer-data"))
        target_b = CliInstanceTarget(instance_name="aft", data_dir=Path("/tmp/aft-data"))

        with patch("bot.runtime_admin.cli._lease_owner_instance", side_effect=["explorer", "aft"]):
            with patch("bot.runtime_admin.cli._resolve_target_instance", side_effect=[target_a, target_b]):
                targets = _resolve_thread_archive_targets(args)

        self.assertEqual(
            targets,
            [
                (target_a, {"thread_id": "thread-1"}),
                (target_b, {"thread_id": "thread-2"}),
            ],
        )
