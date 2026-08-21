import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from bot.adapters.base import AgentAdapter, ThreadGoalSummary
from bot.adapters.codex_app_server import (
    CodexAppServerAdapter,
    CodexAppServerConfig,
)
from bot.codex_protocol.connection import (
    AppServerEndpointMode,
    CodexRpcError,
    CodexRpcPreSendError,
    CodexRpcProtocolError,
    CodexRpcTransportError,
)


from tests.codex_app_server_test_support import (
    _FakeRpc,
    _PermissionsUnsupportedRpc,
)


class CodexAppServerAdapterTests(unittest.TestCase):
    def test_agent_adapter_requires_explicit_steer_turn_implementation(self) -> None:
        self.assertIn("steer_turn", AgentAdapter.__abstractmethods__)

    def test_shared_fake_rejects_unknown_rpc_methods(self) -> None:
        with self.assertRaisesRegex(AssertionError, "turn/private"):
            _FakeRpc().request("turn/private", {})

    def test_service_epoch_gate_rejects_ordinary_request_before_rpc_use(self) -> None:
        admission_error = RuntimeError("backend reset remains fenced")
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=Mock(side_effect=admission_error),
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=Mock(),
        )
        fake_rpc = Mock()
        adapter._rpc = fake_rpc

        with self.assertRaises(CodexRpcPreSendError) as raised:
            adapter.list_loaded_thread_ids()

        self.assertIs(raised.exception.__cause__, admission_error)
        fake_rpc.start.assert_not_called()
        fake_rpc.request.assert_not_called()

    def test_existing_connection_authority_bypasses_ordinary_epoch_gate(self) -> None:
        issue = Mock(side_effect=AssertionError("ordinary gate must not run"))
        confirm = Mock(side_effect=AssertionError("ordinary gate must not run"))
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=issue,
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=confirm,
        )
        fake_rpc = Mock()
        fake_rpc.request.return_value = {"data": [], "nextCursor": None}
        adapter._rpc = fake_rpc

        self.assertEqual(
            adapter.list_loaded_thread_ids(require_existing_connection=True),
            [],
        )

        issue.assert_not_called()
        confirm.assert_not_called()
        fake_rpc.request.assert_called_once_with(
            "thread/loaded/list",
            {},
            timeout=30.0,
            require_existing_connection=True,
        )

    def test_control_inventory_uses_existing_connection_with_epoch_admission(self) -> None:
        permit = object()
        issue = Mock(return_value=permit)
        guard = Mock(side_effect=lambda _permit: nullcontext())
        confirm = Mock()
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=issue,
            guard_outbound_send=guard,
            confirm_outbound_request=confirm,
        )
        fake_rpc = Mock()
        fake_rpc.request.return_value = {
            "data": ["thread-2", "thread-1"],
            "nextCursor": None,
        }
        adapter._rpc = fake_rpc

        result = adapter.list_loaded_thread_ids_for_control(timeout=2.5)

        self.assertEqual(result, ["thread-2", "thread-1"])
        issue.assert_called_once_with("thread/loaded/list")
        confirm.assert_called_once_with(permit)
        fake_rpc.request.assert_called_once()
        args, kwargs = fake_rpc.request.call_args
        self.assertEqual(args, ("thread/loaded/list", {}))
        self.assertEqual(kwargs["timeout"], 2.5)
        self.assertTrue(kwargs["require_existing_connection"])
        outbound_guard = kwargs["outbound_transport_guard"]
        with outbound_guard():
            pass
        guard.assert_called_once_with(permit)

    def test_control_inventory_json_rpc_error_confirms_exact_epoch(self) -> None:
        permit = object()
        confirm = Mock()
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=Mock(return_value=permit),
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=confirm,
        )
        fake_rpc = Mock()
        rpc_error = CodexRpcError(
            "thread/loaded/list",
            {"code": -32601, "message": "unsupported"},
        )
        fake_rpc.request.side_effect = rpc_error
        adapter._rpc = fake_rpc

        with self.assertRaises(CodexRpcError) as raised:
            adapter.list_loaded_thread_ids_for_control(timeout=2.5)

        self.assertIs(raised.exception, rpc_error)
        confirm.assert_called_once_with(permit)

    def test_control_inventory_transport_failure_does_not_confirm_epoch(self) -> None:
        permit = object()
        confirm = Mock()
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=Mock(return_value=permit),
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=confirm,
        )
        fake_rpc = Mock()
        transport_error = CodexRpcTransportError(
            "thread/loaded/list",
            {"code": -32000, "message": "connection closed"},
        )
        fake_rpc.request.side_effect = transport_error
        adapter._rpc = fake_rpc

        with self.assertRaises(CodexRpcTransportError) as raised:
            adapter.list_loaded_thread_ids_for_control(timeout=2.5)

        self.assertIs(raised.exception, transport_error)
        confirm.assert_not_called()

    def test_connection_initialization_bypasses_ordinary_epoch_gate(self) -> None:
        issue = Mock(side_effect=AssertionError("ordinary gate must not run"))
        confirm = Mock(side_effect=AssertionError("ordinary gate must not run"))
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=issue,
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=confirm,
        )
        fake_rpc = Mock()
        fake_rpc.request.return_value = {"requirements": None}
        adapter._rpc = fake_rpc

        adapter._handle_rpc_initialized(7, {})

        issue.assert_not_called()
        confirm.assert_not_called()
        fake_rpc.request.assert_called_once_with(
            "configRequirements/read",
            None,
            timeout=30.0,
        )

    def test_ordinary_server_request_response_requires_open_epoch(self) -> None:
        admission_error = RuntimeError("backend reset remains fenced")
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=Mock(side_effect=admission_error),
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=Mock(),
        )
        fake_rpc = Mock()
        adapter._rpc = fake_rpc

        with self.assertRaises(CodexRpcPreSendError):
            adapter.respond(
                "request-1", connection_generation=1, result={"decision": "accept"}
            )

        fake_rpc.respond.assert_not_called()

    def test_stop_settlement_response_keeps_exact_existing_authority(self) -> None:
        issue = Mock(side_effect=AssertionError("ordinary gate must not run"))
        confirm = Mock(side_effect=AssertionError("ordinary gate must not run"))
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=issue,
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=confirm,
        )
        fake_rpc = Mock()
        adapter._rpc = fake_rpc

        adapter.respond_with_existing_backend_authority(
            "request-1",
            connection_generation=7,
            error={"code": -1, "message": "stopped"},
        )

        issue.assert_not_called()
        confirm.assert_not_called()
        fake_rpc.respond.assert_called_once_with(
            "request-1",
            result=None,
            error={"code": -1, "message": "stopped"},
            timeout=30.0,
            require_existing_connection=True,
            expected_connection_generation=7,
        )

    def test_response_after_epoch_loss_is_transport_unknown(self) -> None:
        permit = object()
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=Mock(return_value=permit),
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=Mock(side_effect=RuntimeError("epoch lost")),
        )
        fake_rpc = Mock()
        fake_rpc.request.return_value = {"data": [], "nextCursor": None}
        adapter._rpc = fake_rpc

        with self.assertRaises(CodexRpcTransportError):
            adapter.list_loaded_thread_ids()

    def test_json_rpc_error_after_epoch_loss_is_transport_unknown(self) -> None:
        permit = object()
        confirm = Mock(side_effect=RuntimeError("epoch lost"))
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(),
            issue_outbound_request=Mock(return_value=permit),
            guard_outbound_send=Mock(side_effect=lambda _permit: nullcontext()),
            confirm_outbound_request=confirm,
        )
        fake_rpc = Mock()
        fake_rpc.request.side_effect = CodexRpcError(
            "thread/start",
            {"code": -32602, "message": "unknown field permissions"},
        )
        adapter._rpc = fake_rpc

        with self.assertRaises(CodexRpcTransportError):
            adapter.create_thread(cwd="/tmp/project")

        confirm.assert_called_once_with(permit)
        fake_rpc.request.assert_called_once()

    def test_unsubscribe_propagates_failure_before_local_cleanup_can_claim_success(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        fake_rpc.request.side_effect = RuntimeError("unload failed")
        adapter._rpc = fake_rpc

        with self.assertRaisesRegex(RuntimeError, "unload failed"):
            adapter.unsubscribe_thread("thread-1")

        fake_rpc.request.assert_called_once_with(
            "thread/unsubscribe",
            {"threadId": "thread-1"},
            timeout=30.0,
        )

    def test_unsubscribe_rejects_non_object_success_response(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        fake_rpc.request.return_value = None
        adapter._rpc = fake_rpc

        with self.assertRaisesRegex(CodexRpcProtocolError, "non-object"):
            adapter.unsubscribe_thread("thread-1")

    def test_unsubscribe_accepts_only_reviewed_upstream_statuses(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        adapter._rpc = fake_rpc

        for status in ("unsubscribed", "notSubscribed", "notLoaded"):
            with self.subTest(status=status):
                fake_rpc.request.return_value = {"status": status}
                adapter.unsubscribe_thread("thread-1")

        for response in ({}, {"status": "futureStatus"}, {"status": None}):
            with self.subTest(response=response):
                fake_rpc.request.return_value = response
                with self.assertRaisesRegex(
                    CodexRpcProtocolError,
                    "invalid unsubscribe status",
                ):
                    adapter.unsubscribe_thread("thread-1")

    def test_initialized_accepts_partial_requirements_without_params(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        fake_rpc.request.return_value = {
            "requirements": {
                "allowedApprovalsReviewers": ["user"],
                "allowedApprovalPolicies": ["on-request"],
                "allowedSandboxModes": ["read-only"],
                "allowedPermissionProfiles": {":read-only": True},
            }
        }
        adapter._rpc = fake_rpc

        adapter._handle_rpc_initialized(
            7,
            {
                "userAgent": "codex_cli_rs/0.146.0",
                "codexHome": "/tmp/codex-home",
                "platformFamily": "unix",
                "platformOs": "linux",
            },
        )

        fake_rpc.request.assert_called_once_with(
            "configRequirements/read",
            None,
            timeout=30.0,
        )
        self.assertEqual(
            adapter._initialize_identity,
            {
                "connection_generation": 7,
                "user_agent": "codex_cli_rs/0.146.0",
                "codex_home": "/tmp/codex-home",
                "platform_family": "unix",
                "platform_os": "linux",
            },
        )

    def test_requirements_envelope_must_be_present_and_typed(self) -> None:
        for result in ({}, {"requirements": []}, {"requirements": "managed"}):
            with self.subTest(result=result):
                with self.assertRaises(CodexRpcProtocolError):
                    CodexAppServerAdapter._validate_focus_requirements(result)

    def test_requirements_allow_optional_user_reviewer_constraint(self) -> None:
        for requirements in (
            None,
            {},
            {"allowedApprovalsReviewers": None},
            {"allowedApprovalsReviewers": ["user"]},
            {"allowedApprovalsReviewers": ["auto_review", "user"]},
        ):
            with self.subTest(requirements=requirements):
                CodexAppServerAdapter._validate_focus_requirements(
                    {"requirements": requirements}
                )

    def test_requirements_reject_auto_review_only_backend(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "approvalsReviewer=user"):
            CodexAppServerAdapter._validate_focus_requirements(
                {
                    "requirements": {
                        "allowedApprovalsReviewers": ["auto_review"],
                    }
                }
            )

    def test_requirements_reject_malformed_reviewer_constraint(self) -> None:
        for raw_value in ("user", {"user": True}, ["user", 1]):
            with self.subTest(raw_value=raw_value):
                with self.assertRaisesRegex(
                    CodexRpcProtocolError,
                    "allowedApprovalsReviewers",
                ):
                    CodexAppServerAdapter._validate_focus_requirements(
                        {
                            "requirements": {
                                "allowedApprovalsReviewers": raw_value,
                            }
                        }
                    )

    def test_partial_non_reviewer_requirements_do_not_reject_connection(self) -> None:
        for requirements in (
            {"allowedApprovalPolicies": ["on-request"]},
            {"allowedApprovalPolicies": []},
            {"allowedSandboxModes": ["read-only"]},
            {"allowedSandboxModes": []},
            {"allowedPermissionProfiles": {":danger-full-access": False}},
            {"allowedPermissionProfiles": {}},
            {
                "allowedApprovalPolicies": "future-shape",
                "allowedSandboxModes": {"read-only": True},
                "allowedPermissionProfiles": [":workspace"],
            },
        ):
            with self.subTest(requirements=requirements):
                CodexAppServerAdapter._validate_focus_requirements(
                    {"requirements": requirements}
                )

    def test_requirements_are_revalidated_after_reconnect(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        fake_rpc.request.side_effect = [
            {"requirements": None},
            {
                "requirements": {
                    "allowedApprovalsReviewers": ["auto_review"],
                }
            },
        ]
        adapter._rpc = fake_rpc

        adapter._handle_rpc_initialized(1, {"userAgent": "codex/first"})
        with self.assertRaisesRegex(RuntimeError, "approvalsReviewer=user"):
            adapter._handle_rpc_initialized(2, {"userAgent": "codex/second"})

        self.assertEqual(
            fake_rpc.request.call_args_list,
            [
                unittest.mock.call("configRequirements/read", None, timeout=30.0),
                unittest.mock.call("configRequirements/read", None, timeout=30.0),
            ],
        )

    def test_from_dict_normalizes_deprecated_approval_policy(self) -> None:
        config = CodexAppServerConfig.from_dict({"approval_policy": "on-failure"})

        self.assertEqual(config.approval_policy, "on-request")

    def test_from_dict_always_builds_an_owned_process_config(self) -> None:
        owned = CodexAppServerConfig.from_dict({})

        self.assertIs(owned.endpoint_mode, AppServerEndpointMode.OWNED_PROCESS)

    def test_with_attached_endpoint_does_not_mutate_owned_process_config(self) -> None:
        owned = CodexAppServerConfig()

        attached = owned.with_attached_endpoint(
            app_server_url="ws://127.0.0.1:43210",
            app_server_data_dir="/tmp/focus-data",
        )

        self.assertIs(owned.endpoint_mode, AppServerEndpointMode.OWNED_PROCESS)
        self.assertIs(attached.endpoint_mode, AppServerEndpointMode.ATTACHED_ENDPOINT)
        self.assertEqual(attached.app_server_url, "ws://127.0.0.1:43210")
        self.assertEqual(attached.app_server_data_dir, "/tmp/focus-data")

    def test_from_dict_rejects_removed_new_thread_memory_mode_seed(self) -> None:
        with self.assertRaisesRegex(ValueError, "new_thread_memory_mode_seed"):
            CodexAppServerConfig.from_dict(
                {"new_thread_memory_mode_seed": "read_write"}
            )

    def test_from_dict_rejects_removed_default_thread_memory_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "default_thread_memory_mode"):
            CodexAppServerConfig.from_dict({"default_thread_memory_mode": "read"})

    def test_from_dict_rejects_removed_managed_startup_profile(self) -> None:
        with self.assertRaisesRegex(ValueError, "managed_startup_profile"):
            CodexAppServerConfig.from_dict({"managed_startup_profile": "work"})

    def test_from_dict_rejects_removed_collaboration_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "collaboration_mode"):
            CodexAppServerConfig.from_dict({"collaboration_mode": "plan"})

    def test_create_thread_merges_memory_config_overrides(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(
            cwd="/tmp/project",
            config_overrides={
                "memories": {
                    "use_memories": True,
                    "generate_memories": False,
                }
            },
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/start",
                {
                    "cwd": "/tmp/project",
                    "historyMode": "paginated",
                    "permissions": ":danger-full-access",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "personality": "pragmatic",
                    "serviceName": "focus",
                    "config": {
                        "memories": {
                            "use_memories": True,
                            "generate_memories": False,
                        },
                    },
                },
            ),
        )

    def test_create_thread_can_attach_model_and_provider_hints(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(
            cwd="/tmp/project",
            model="gpt-5.4",
            model_provider="provider2_api",
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/start",
                {
                    "cwd": "/tmp/project",
                    "historyMode": "paginated",
                    "permissions": ":danger-full-access",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "personality": "pragmatic",
                    "serviceName": "focus",
                    "model": "gpt-5.4",
                    "modelProvider": "provider2_api",
                },
            ),
        )

    def test_create_and_resume_thread_override_unreviewed_adapter_reviewer(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(approvals_reviewer="auto_review")
        )
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(cwd="/tmp/project")
        adapter.resume_thread("thread-1")

        self.assertEqual(fake_rpc.calls[0][1]["approvalsReviewer"], "user")
        self.assertEqual(fake_rpc.calls[1][1]["approvalsReviewer"], "user")

    def test_create_thread_does_not_fallback_to_configured_model(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig(model="gpt-5.4"))
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(cwd="/tmp/project")

        method, params = fake_rpc.calls[0]
        self.assertEqual(method, "thread/start")
        self.assertNotIn("model", params)
        self.assertNotIn("modelProvider", params)

    def test_start_and_resume_snapshots_preserve_effective_model(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        fake_rpc.thread_model = "gpt-effective"
        adapter._rpc = fake_rpc

        created = adapter.create_thread(cwd="/tmp/project")
        resumed = adapter.resume_thread("thread-1")
        resumed_page = adapter.resume_thread_page("thread-1", limit=2)

        self.assertEqual(created.effective_model, "gpt-effective")
        self.assertEqual(resumed.effective_model, "gpt-effective")
        self.assertEqual(resumed_page.snapshot.effective_model, "gpt-effective")

    def test_create_thread_rejects_success_response_without_thread(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value={}):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.create_thread(cwd="/tmp/project")

    def test_config_rejects_model_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "model_provider"):
            CodexAppServerConfig.from_dict({"model_provider": "provider2_api"})

    def test_create_thread_allows_permission_overrides(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(
            cwd="/tmp/project",
            approval_policy="never",
            sandbox="danger-full-access",
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/start",
                {
                    "cwd": "/tmp/project",
                    "historyMode": "paginated",
                    "permissions": ":danger-full-access",
                    "approvalPolicy": "never",
                    "approvalsReviewer": "user",
                    "personality": "pragmatic",
                    "serviceName": "focus",
                },
            ),
        )

    def test_create_thread_falls_back_to_legacy_sandbox_when_permissions_unsupported(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _PermissionsUnsupportedRpc()
        adapter._rpc = fake_rpc

        adapter.create_thread(cwd="/tmp/project")

        self.assertEqual(fake_rpc.calls[0][0], "thread/start")
        self.assertEqual(fake_rpc.calls[0][1]["historyMode"], "paginated")
        self.assertEqual(fake_rpc.calls[0][1]["permissions"], ":danger-full-access")
        self.assertEqual(fake_rpc.calls[1][0], "thread/start")
        self.assertEqual(fake_rpc.calls[1][1]["historyMode"], "paginated")
        self.assertNotIn("permissions", fake_rpc.calls[1][1])
        self.assertEqual(fake_rpc.calls[1][1]["sandbox"], "danger-full-access")

    def test_permissions_configuration_errors_never_trigger_legacy_fallback(
        self,
    ) -> None:
        params = {"permissions": ":workspace"}
        for message in (
            "Configured value for [permissions] is disallowed",
            "invalid permissions profile: :workspace",
            "permission profile :workspace was not found",
            "unknown field approvalPolicy",
        ):
            with self.subTest(message=message):
                self.assertFalse(
                    CodexAppServerAdapter._should_retry_without_permissions(
                        {"code": -32602, "message": message},
                        params,
                        legacy_value="workspace-write",
                    )
                )

        self.assertTrue(
            CodexAppServerAdapter._should_retry_without_permissions(
                {"code": -32602, "message": "unknown field `permissions`"},
                params,
                legacy_value="workspace-write",
            )
        )

    def test_resume_thread_can_attach_model_and_provider_hints(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.resume_thread(
            "thread-1",
            model="gpt-5.4",
            model_provider="provider2_api",
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/resume",
                {
                    "threadId": "thread-1",
                    "approvalsReviewer": "user",
                    "model": "gpt-5.4",
                    "modelProvider": "provider2_api",
                },
            ),
        )

    def test_resume_thread_rejects_mismatched_thread_id(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        malformed = {
            "thread": {
                "id": "thread-other",
                "cwd": "/tmp/project",
                "createdAt": 0,
                "updatedAt": 0,
                "source": "cli",
                "status": {"type": "idle", "activeFlags": []},
            }
        }

        with patch.object(fake_rpc, "request", return_value=malformed):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.resume_thread("thread-1")

    def test_resume_thread_rejects_persisted_auto_review_reviewer(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        response = fake_rpc.request("thread/resume", {"threadId": "thread-1"})
        response["approvalsReviewer"] = "auto_review"
        fake_rpc.calls.clear()

        with patch.object(fake_rpc, "request", return_value=response):
            with self.assertRaisesRegex(
                CodexRpcProtocolError,
                "approvalsReviewer=user",
            ):
                adapter.resume_thread("thread-1")

        self.assertEqual(
            fake_rpc.calls,
            [],
        )

    def test_start_thread_rejects_non_user_approvals_reviewer_response(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        response = fake_rpc.request(
            "thread/start",
            {"cwd": "/tmp/project", "historyMode": "paginated"},
        )
        response["approvalsReviewer"] = "auto_review"

        with patch.object(fake_rpc, "request", return_value=response):
            with self.assertRaisesRegex(
                CodexRpcProtocolError,
                "approvalsReviewer=user",
            ):
                adapter.create_thread(cwd="/tmp/project")

    def test_start_thread_records_upstream_effective_settings(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        response = fake_rpc.request(
            "thread/start",
            {"cwd": "/tmp/project", "historyMode": "paginated"},
        )
        response["approvalPolicy"] = "on-request"
        response["activePermissionProfile"] = {"id": ":read-only"}
        fake_rpc.calls.clear()

        with patch.object(fake_rpc, "request", return_value=response) as request:
            snapshot = adapter.create_thread(
                cwd="/tmp/project",
                approval_policy="never",
                permissions_profile_id=":danger-full-access",
            )

        self.assertEqual(snapshot.effective_approval_policy, "on-request")
        self.assertEqual(snapshot.effective_permissions_profile_id, ":read-only")
        request_params = request.call_args.args[1]
        self.assertEqual(request_params["approvalPolicy"], "never")
        self.assertEqual(request_params["permissions"], ":danger-full-access")

    def test_start_thread_preserves_known_null_effort_and_optional_experimental_facts(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        response = fake_rpc.request(
            "thread/start",
            {"cwd": "/tmp/project", "historyMode": "paginated"},
        )
        response.pop("approvalPolicy")
        response.pop("activePermissionProfile")

        with patch.object(fake_rpc, "request", return_value=response):
            snapshot = adapter.create_thread(cwd="/tmp/project")

        self.assertEqual(snapshot.effective_model, fake_rpc.default_model)
        self.assertIsNone(snapshot.effective_reasoning_effort)
        self.assertIsNone(snapshot.effective_approval_policy)
        self.assertIsNone(snapshot.effective_permissions_profile_id)

    def test_start_thread_strictly_rejects_malformed_effective_setting_fields(
        self,
    ) -> None:
        for field, value in (
            ("model", None),
            ("reasoningEffort", 3),
            ("approvalPolicy", {"type": "never"}),
            ("activePermissionProfile", "workspace"),
            ("activePermissionProfile", {"id": ""}),
        ):
            with self.subTest(field=field, value=value):
                adapter = CodexAppServerAdapter(CodexAppServerConfig())
                fake_rpc = _FakeRpc()
                adapter._rpc = fake_rpc
                response = fake_rpc.request(
                    "thread/start",
                    {"cwd": "/tmp/project", "historyMode": "paginated"},
                )
                response[field] = value

                with patch.object(fake_rpc, "request", return_value=response):
                    with self.assertRaises(CodexRpcProtocolError):
                        adapter.create_thread(cwd="/tmp/project")

    def test_start_thread_requires_reasoning_effort_field(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        response = fake_rpc.request(
            "thread/start",
            {"cwd": "/tmp/project", "historyMode": "paginated"},
        )
        response.pop("reasoningEffort")

        with patch.object(fake_rpc, "request", return_value=response):
            with self.assertRaisesRegex(
                CodexRpcProtocolError,
                "missing reasoningEffort",
            ):
                adapter.create_thread(cwd="/tmp/project")

    def test_read_thread_rejects_invalid_thread_payload(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(
            fake_rpc,
            "request",
            return_value={"thread": {"id": "thread-1", "status": "invalid"}},
        ):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.read_thread("thread-1")

    def test_read_thread_does_not_require_start_resume_setting_fields(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        response = fake_rpc.request(
            "thread/start",
            {"cwd": "/tmp/project", "historyMode": "paginated"},
        )
        thread_only_response = {"thread": response["thread"]}

        with patch.object(fake_rpc, "request", return_value=thread_only_response):
            snapshot = adapter.read_thread("thread-1")

        self.assertEqual(snapshot.summary.thread_id, "thread-1")
        self.assertIsNone(snapshot.effective_model)
        self.assertIsNone(snapshot.effective_reasoning_effort)
        self.assertIsNone(snapshot.effective_approval_policy)
        self.assertIsNone(snapshot.effective_permissions_profile_id)

    def test_resume_thread_page_requests_bounded_full_history(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        result = adapter.resume_thread_page("thread-1", limit=40)

        method, params = fake_rpc.calls[-1]
        self.assertEqual(method, "thread/resume")
        self.assertTrue(params["excludeTurns"])
        self.assertEqual(
            params["initialTurnsPage"],
            {"limit": 40, "sortDirection": "desc", "itemsView": "full"},
        )
        self.assertEqual(
            [turn["id"] for turn in result.initial_turns_page.turns],
            ["turn-old", "turn-new"],
        )
        self.assertEqual(result.initial_turns_page.next_cursor, "older-cursor")

    def test_list_thread_turns_and_steer_use_explicit_v2_contracts(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        page = adapter.list_thread_turns("thread-1", cursor="cursor-1", limit=20)
        steered = adapter.steer_turn(
            thread_id="thread-1",
            expected_turn_id="turn-1",
            input_items=[{"type": "text", "text": "more"}],
            client_user_message_id="focus-web:message-1",
        )

        self.assertEqual([turn["id"] for turn in page.turns], ["turn-old", "turn-new"])
        self.assertEqual(
            fake_rpc.calls[-2],
            (
                "thread/turns/list",
                {
                    "threadId": "thread-1",
                    "cursor": "cursor-1",
                    "limit": 20,
                    "sortDirection": "desc",
                    "itemsView": "full",
                },
            ),
        )
        self.assertEqual(steered["turnId"], "turn-1")
        self.assertEqual(
            fake_rpc.calls[-1][1]["clientUserMessageId"], "focus-web:message-1"
        )

    def test_resume_thread_merges_memory_config_overrides(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.resume_thread(
            "thread-1",
            config_overrides={
                "memories": {
                    "use_memories": True,
                    "generate_memories": True,
                }
            },
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/resume",
                {
                    "threadId": "thread-1",
                    "approvalsReviewer": "user",
                    "config": {
                        "memories": {
                            "use_memories": True,
                            "generate_memories": True,
                        },
                    },
                },
            ),
        )

    def test_resume_thread_can_attach_runtime_permission_overrides(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        snapshot = adapter.resume_thread(
            "thread-1",
            approval_policy="on-request",
            permissions_profile_id=":workspace",
        )

        self.assertEqual(snapshot.effective_approval_policy, "on-request")
        self.assertEqual(snapshot.effective_permissions_profile_id, ":workspace")

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/resume",
                {
                    "threadId": "thread-1",
                    "approvalsReviewer": "user",
                    "approvalPolicy": "on-request",
                    "permissions": ":workspace",
                },
            ),
        )

    def test_resume_thread_rejects_mismatched_effective_safety_profile(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc
        response = fake_rpc.request(
            "thread/resume",
            {
                "threadId": "thread-1",
                "approvalPolicy": "on-request",
                "permissions": ":workspace",
            },
        )

        for field, value, message in (
            ("approvalPolicy", "never", "approvalPolicy"),
            (
                "activePermissionProfile",
                {"id": ":read-only"},
                "activePermissionProfile",
            ),
            ("approvalPolicy", None, "approvalPolicy"),
            ("activePermissionProfile", None, "activePermissionProfile"),
        ):
            with self.subTest(field=field):
                mismatched = dict(response)
                if value is None:
                    mismatched.pop(field, None)
                else:
                    mismatched[field] = value
                with patch.object(fake_rpc, "request", return_value=mismatched):
                    with self.assertRaisesRegex(CodexRpcProtocolError, message):
                        adapter.resume_thread(
                            "thread-1",
                            approval_policy="on-request",
                            permissions_profile_id=":workspace",
                        )

    def test_resume_thread_falls_back_to_legacy_sandbox_when_permissions_unsupported(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _PermissionsUnsupportedRpc()
        adapter._rpc = fake_rpc

        adapter.resume_thread(
            "thread-1",
            approval_policy="on-request",
            permissions_profile_id=":workspace",
        )

        self.assertEqual(fake_rpc.calls[0][0], "thread/resume")
        self.assertEqual(fake_rpc.calls[0][1]["permissions"], ":workspace")
        self.assertEqual(fake_rpc.calls[1][0], "thread/resume")
        self.assertNotIn("permissions", fake_rpc.calls[1][1])
        self.assertEqual(fake_rpc.calls[1][1]["sandbox"], "workspace-write")

    def test_update_thread_settings_uses_canonical_runtime_fields(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.update_thread_settings(
            "thread-1",
            approval_policy="on-request",
            permissions_profile_id=":workspace",
            model="gpt-5.4",
            reasoning_effort="high",
        )

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/settings/update",
                {
                    "threadId": "thread-1",
                    "approvalPolicy": "on-request",
                    "permissions": ":workspace",
                    "model": "gpt-5.4",
                    "effort": "high",
                },
            ),
        )

    def test_update_thread_settings_falls_back_to_legacy_sandbox_policy_when_permissions_unsupported(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _PermissionsUnsupportedRpc()
        adapter._rpc = fake_rpc

        adapter.update_thread_settings("thread-1", permissions_profile_id=":workspace")

        self.assertEqual(fake_rpc.calls[0][0], "thread/settings/update")
        self.assertEqual(fake_rpc.calls[0][1]["permissions"], ":workspace")
        self.assertEqual(fake_rpc.calls[1][0], "thread/settings/update")
        self.assertNotIn("permissions", fake_rpc.calls[1][1])
        self.assertEqual(
            fake_rpc.calls[1][1]["sandboxPolicy"],
            {
                "type": "workspaceWrite",
                "writableRoots": [],
                "readOnlyAccess": {"type": "fullAccess"},
                "networkAccess": False,
                "excludeTmpdirEnvVar": False,
                "excludeSlashTmp": False,
            },
        )

    def test_compact_thread_calls_upstream_endpoint(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.compact_thread("thread-1")

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/compact/start",
                {
                    "threadId": "thread-1",
                },
            ),
        )

    def test_compact_thread_rejects_non_object_success_response(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value=None):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.compact_thread("thread-1")

    def test_start_review_uses_inline_public_api(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        result = adapter.start_review(
            "thread-1",
            target={"type": "baseBranch", "branch": "main"},
            delivery="inline",
        )

        self.assertEqual(result["turn"]["id"], "review-turn-1")
        self.assertEqual(
            fake_rpc.calls[0],
            (
                "review/start",
                {
                    "threadId": "thread-1",
                    "target": {"type": "baseBranch", "branch": "main"},
                    "delivery": "inline",
                },
            ),
        )

    def test_start_review_rejects_non_object_success_response(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value=None):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.start_review(
                    "thread-1",
                    target={"type": "uncommittedChanges"},
                )

    def test_start_review_rejects_success_response_without_turn(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value={}):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.start_review(
                    "thread-1",
                    target={"type": "uncommittedChanges"},
                )

    def test_get_thread_goal_reads_current_goal(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        goal = adapter.get_thread_goal("thread-1")

        self.assertEqual(
            fake_rpc.calls[0], ("thread/goal/get", {"threadId": "thread-1"})
        )
        self.assertEqual(
            goal,
            ThreadGoalSummary(
                thread_id="thread-1",
                objective="ship goal support",
                status="active",
                token_budget=123,
                tokens_used=45,
                time_used_seconds=67,
                created_at=1712476800,
                updated_at=1712476801,
            ),
        )

    def test_set_thread_goal_supports_status_only_update(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        goal = adapter.set_thread_goal("thread-1", status="paused")

        self.assertEqual(
            fake_rpc.calls[0],
            (
                "thread/goal/set",
                {
                    "threadId": "thread-1",
                    "status": "paused",
                },
            ),
        )
        self.assertEqual(goal.objective, "ship goal support")

    def test_clear_thread_goal_returns_backend_result(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        cleared = adapter.clear_thread_goal("thread-1")

        self.assertEqual(
            fake_rpc.calls[0], ("thread/goal/clear", {"threadId": "thread-1"})
        )
        self.assertTrue(cleared)

    def test_start_turn_default_mode_does_not_send_complete_collaboration_mode(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
        )

        self.assertEqual(
            fake_rpc.calls,
            [
                (
                    "turn/start",
                    {
                        "threadId": "thread-1",
                        "input": [{"type": "text", "text": "hello"}],
                        "cwd": "/tmp",
                        "approvalPolicy": "never",
                        "approvalsReviewer": "user",
                        "permissions": ":danger-full-access",
                        "personality": "pragmatic",
                    },
                )
            ],
        )

    def test_start_turn_rejects_success_response_without_turn(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value={}):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.start_turn(
                    thread_id="thread-1",
                    input_items=[{"type": "text", "text": "hello"}],
                    cwd="/tmp",
                )

    def test_interrupt_turn_rejects_non_object_success_response(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value=None):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.interrupt_turn(thread_id="thread-1", turn_id="turn-1")

    def test_interrupt_forwards_only_the_exact_active_turn(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = Mock()
        fake_rpc.request.return_value = {}
        adapter._rpc = fake_rpc

        adapter.interrupt_turn(
            thread_id="thread-1",
            turn_id="turn-1",
        )

        fake_rpc.request.assert_called_once_with(
            "turn/interrupt",
            {"threadId": "thread-1", "turnId": "turn-1"},
            timeout=30.0,
        )

    def test_start_turn_config_seed_does_not_materialize_model_or_effort(self) -> None:
        adapter = CodexAppServerAdapter(
            CodexAppServerConfig(model="gpt-5.4", reasoning_effort="high")
        )
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
        )

        self.assertEqual(len(fake_rpc.calls), 1)
        method, params = fake_rpc.calls[0]
        self.assertEqual(method, "turn/start")
        self.assertNotIn("collaborationMode", params)
        self.assertNotIn("model", params)
        self.assertNotIn("effort", params)

    def test_start_turn_uses_model_without_provider_override_surface(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
            model="provider2-model",
        )

        self.assertEqual(fake_rpc.calls[0][0], "turn/start")
        params = fake_rpc.calls[0][1]
        self.assertEqual(params["model"], "provider2-model")
        self.assertNotIn("modelProvider", params)
        self.assertNotIn("config", params)
        self.assertNotIn("collaborationMode", params)

    def test_start_turn_auto_effort_omits_complete_collaboration_mode_to_preserve_backend_state(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        fake_rpc.thread_model = "gpt-5.5"
        fake_rpc.thread_reasoning_effort = "xhigh"
        adapter._rpc = fake_rpc

        adapter.create_thread(cwd="/tmp/project")
        fake_rpc.calls.clear()

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
        )

        self.assertEqual(len(fake_rpc.calls), 1)
        method, params = fake_rpc.calls[0]
        self.assertEqual(method, "turn/start")
        self.assertNotIn("effort", params)
        self.assertNotIn("collaborationMode", params)

    def test_start_turn_explicit_reasoning_effort_overrides_cached_thread_effort(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        fake_rpc.thread_model = "gpt-5.5"
        fake_rpc.thread_reasoning_effort = "xhigh"
        adapter._rpc = fake_rpc

        adapter.create_thread(cwd="/tmp/project")
        fake_rpc.calls.clear()

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
            reasoning_effort="high",
        )

        self.assertEqual(len(fake_rpc.calls), 1)
        method, params = fake_rpc.calls[0]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["effort"], "high")
        self.assertNotIn("collaborationMode", params)

    def test_start_turn_sends_ultra_unchanged(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
            reasoning_effort="ultra",
        )

        method, params = fake_rpc.calls[0]
        self.assertEqual(method, "turn/start")
        self.assertEqual(params["effort"], "ultra")
        self.assertNotIn("collaborationMode", params)

    def test_start_turn_explicit_effort_is_not_reinjected_on_auto(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        fake_rpc.thread_model = "gpt-5.5"
        fake_rpc.thread_reasoning_effort = "xhigh"
        adapter._rpc = fake_rpc

        adapter.create_thread(cwd="/tmp/project")
        fake_rpc.calls.clear()

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "low"}],
            cwd="/tmp",
            reasoning_effort="low",
        )
        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "auto"}],
            cwd="/tmp",
        )
        self.assertEqual(fake_rpc.calls[0][1]["effort"], "low")
        self.assertNotIn("effort", fake_rpc.calls[1][1])
        self.assertNotIn("collaborationMode", fake_rpc.calls[1][1])

    def test_start_turn_can_override_sandbox_policy(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
            sandbox="danger-full-access",
        )

        self.assertEqual(fake_rpc.calls[0][0], "turn/start")
        self.assertEqual(fake_rpc.calls[0][1]["permissions"], ":danger-full-access")

    def test_start_turn_forwards_client_user_message_id(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
            client_user_message_id="focus-web:message-1",
        )

        self.assertEqual(
            fake_rpc.calls[0][1]["clientUserMessageId"], "focus-web:message-1"
        )

    def test_start_turn_falls_back_to_legacy_sandbox_policy_when_permissions_unsupported(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _PermissionsUnsupportedRpc()
        adapter._rpc = fake_rpc

        adapter.start_turn(
            thread_id="thread-1",
            input_items=[{"type": "text", "text": "hello"}],
            cwd="/tmp",
        )

        self.assertEqual(fake_rpc.calls[0][0], "turn/start")
        self.assertEqual(fake_rpc.calls[0][1]["permissions"], ":danger-full-access")
        self.assertEqual(fake_rpc.calls[1][0], "turn/start")
        self.assertNotIn("permissions", fake_rpc.calls[1][1])
        self.assertEqual(
            fake_rpc.calls[1][1]["sandboxPolicy"], {"type": "dangerFullAccess"}
        )

    def test_stop_stops_rpc_client(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.stop()

        self.assertTrue(fake_rpc.stopped)

    def test_archive_thread_calls_public_archive_api(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.archive_thread("thread-1")

        self.assertEqual(
            fake_rpc.calls[0], ("thread/archive", {"threadId": "thread-1"})
        )

    def test_archive_thread_rejects_non_object_success_response(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value=None):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.archive_thread("thread-1")

    def test_unarchive_thread_calls_public_unarchive_api(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        summary = adapter.unarchive_thread("thread-1")

        self.assertEqual(
            fake_rpc.calls[0], ("thread/unarchive", {"threadId": "thread-1"})
        )
        self.assertEqual(summary.thread_id, "thread-1")
        self.assertEqual(summary.name, "demo")

    def test_unarchive_thread_rejects_malformed_success_response_as_protocol_error(
        self,
    ) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value={"thread": "invalid"}):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.unarchive_thread("thread-1")

    def test_delete_thread_calls_public_delete_api(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        adapter.delete_thread("thread-1")

        self.assertEqual(fake_rpc.calls[0], ("thread/delete", {"threadId": "thread-1"}))

    def test_delete_thread_rejects_non_object_success_response(self) -> None:
        adapter = CodexAppServerAdapter(CodexAppServerConfig())
        fake_rpc = _FakeRpc()
        adapter._rpc = fake_rpc

        with patch.object(fake_rpc, "request", return_value=None):
            with self.assertRaises(CodexRpcProtocolError):
                adapter.delete_thread("thread-1")

    def test_config_rejects_removed_app_server_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "app_server_mode.*已移除"):
            CodexAppServerConfig.from_dict({"app_server_mode": "remote"})


if __name__ == "__main__":
    unittest.main()
