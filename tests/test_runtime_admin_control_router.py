from __future__ import annotations

import ast
import os
import pathlib
import unittest

from bot.adapters.base import ThreadSummary
from bot.backend_reset.contract import BackendResetPreview
from bot.runtime_admin.control_router import (
    RuntimeAdminBindingControlPorts,
    RuntimeAdminControlRouter,
    RuntimeAdminControlRouterPorts,
    RuntimeAdminServiceControlPorts,
    RuntimeAdminThreadControlPorts,
)


def _thread(thread_id: str = "thread-1") -> ThreadSummary:
    return ThreadSummary(
        thread_id=thread_id,
        cwd="/workspace",
        name="Thread",
        preview="",
        created_at=1,
        updated_at=2,
        source="appServer",
        status="idle",
    )


class _Harness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.inventory = [
            {
                "binding_id": "p2p:ou-a:chat-a",
                "binding_state": "bound",
                "feishu_runtime_state": "attached",
                "thread_id": "thread-1",
                "running_turn": True,
            },
            {
                "binding_id": "p2p:ou-b:chat-b",
                "binding_state": "bound",
                "feishu_runtime_state": "detached",
                "thread_id": "thread-1",
                "running_turn": False,
            },
        ]
        self.loaded_thread_ids = ["thread-1", "thread-2"]
        self.resolved_thread = _thread()
        self.thread_status = {
            "thread_id": "thread-1",
            "thread_title": "Thread",
            "working_dir": "/workspace",
            "bound_binding_ids": ["p2p:ou-a:chat-a", "p2p:ou-b:chat-b"],
            "attached_binding_ids": ["p2p:ou-a:chat-a"],
        }
        self.router = RuntimeAdminControlRouter(
            RuntimeAdminControlRouterPorts(
                service=RuntimeAdminServiceControlPorts(
                    binding_inventory_snapshot=lambda: list(self.inventory),
                    backend_reset_preview=lambda: BackendResetPreview(
                        status="available",
                        reason_code="",
                        reason_text="ready",
                    ),
                    list_loaded_thread_ids=lambda: list(self.loaded_thread_ids),
                    instance_name=lambda: "default",
                    service_control_endpoint=lambda: "/tmp/focus.sock",
                    current_app_server_url=lambda: "ws://127.0.0.1:8765",
                    web_gateway_enabled=lambda: True,
                    current_web_gateway_url=lambda: "http://127.0.0.1:8766",
                    operational_status=lambda: {"phase": "running"},
                    reset_backend=self._call("reset_backend"),
                    attach_service=self._call("attach_service"),
                ),
                binding=RuntimeAdminBindingControlPorts(
                    list_response=self._call("binding_list"),
                    status_snapshot=self._call("binding_status"),
                    attach=self._call("binding_attach"),
                    submit_prompt=self._call("binding_prompt"),
                    detach=self._call("binding_detach"),
                    clear=self._call("binding_clear"),
                    clear_all=self._call("binding_clear_all"),
                    clear_stale=self._call("binding_clear_stale"),
                ),
                thread=RuntimeAdminThreadControlPorts(
                    resolve_target=self._resolve_target,
                    status_snapshot=self._status_snapshot,
                    goal_snapshot=self._call("thread_goal"),
                    set_goal=self._call("thread_goal_set"),
                    clear_goal=self._call("thread_goal_clear"),
                    clear_archived_bindings=self._call("clear_archived"),
                    local_bindings=self._call("local_bindings"),
                    loaded_status=self._call("loaded_status"),
                    archive=self._call("archive"),
                    unarchive=self._call("unarchive"),
                    delete=self._call("delete"),
                    send_image=self._call("send_image"),
                    attach=self._call("thread_attach"),
                    detach=self._call("thread_detach"),
                ),
            )
        )

    def _call(self, name: str):
        def invoke(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"owner": name, "args": args, "kwargs": kwargs}

        return invoke

    def _resolve_target(self, params):
        self.calls.append(("resolve_target", (params,), {}))
        return self.resolved_thread

    def _status_snapshot(self, thread_id, *, summary):
        self.calls.append(
            ("thread_status", (thread_id,), {"summary": summary})
        )
        return dict(self.thread_status)


class RuntimeAdminControlRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = _Harness()

    def test_service_status_is_projected_from_named_ports(self) -> None:
        result = self.harness.router.handle("service/status", {})

        self.assertEqual(result["instance_name"], "default")
        self.assertEqual(result["pid"], os.getpid())
        self.assertEqual(result["binding_count"], 2)
        self.assertEqual(result["bound_binding_count"], 2)
        self.assertEqual(result["attached_binding_count"], 1)
        self.assertEqual(result["thread_count"], 1)
        self.assertEqual(result["attached_thread_count"], 1)
        self.assertEqual(result["loaded_thread_ids"], ["thread-1", "thread-2"])
        self.assertEqual(result["running_binding_ids"], ["p2p:ou-a:chat-a"])
        self.assertTrue(result["web_gateway_enabled"])
        self.assertEqual(result["web_gateway_url"], "http://127.0.0.1:8766")
        self.assertEqual(result["backend_reset_status"], "available")
        self.assertEqual(result["operator_status"], {"phase": "running"})

    def test_loaded_thread_inventory_is_strict_and_sorted(self) -> None:
        self.harness.loaded_thread_ids = ["thread-2", "thread-1"]

        result = self.harness.router.handle("thread/loaded/list", {})

        self.assertEqual(
            result,
            {
                "instance_name": "default",
                "loaded_thread_ids": ["thread-1", "thread-2"],
            },
        )

    def test_loaded_thread_inventory_rejects_params_and_invalid_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "不接受参数"):
            self.harness.router.handle("thread/loaded/list", {"limit": 1})

        for loaded_ids in (["thread-1", "thread-1"], [""], [1]):
            with self.subTest(loaded_ids=loaded_ids):
                self.harness.loaded_thread_ids = loaded_ids
                with self.assertRaisesRegex(ValueError, "无效 thread id"):
                    self.harness.router.handle("thread/loaded/list", {})

    def test_loaded_thread_inventory_does_not_turn_failure_into_empty(self) -> None:
        class UnavailableInventory:
            def __iter__(self):
                raise RuntimeError("inventory unavailable")

        self.harness.loaded_thread_ids = UnavailableInventory()

        with self.assertRaisesRegex(RuntimeError, "inventory unavailable"):
            self.harness.router.handle("thread/loaded/list", {})

    def test_binding_prompt_normalizes_exact_wire_payload(self) -> None:
        result = self.harness.router.handle(
            "binding/submit-prompt",
            {
                "binding_id": "p2p:ou-a:chat-a",
                "text": "hello",
                "actor_open_id": "ou-actor",
                "input_items": [{"type": "text", "text": "hello"}],
                "synthetic_source": "timer",
                "display_mode": "visible",
            },
        )

        self.assertEqual(result["owner"], "binding_prompt")
        name, args, kwargs = self.harness.calls[-1]
        self.assertEqual(name, "binding_prompt")
        self.assertEqual(args, (("ou-a", "chat-a"),))
        self.assertEqual(kwargs["text"], "hello")
        self.assertEqual(kwargs["input_items"][0]["type"], "text")
        self.assertEqual(kwargs["display_mode"], "visible")

    def test_binding_prompt_rejects_malformed_items_before_domain_call(self) -> None:
        for value in ({"type": "text"}, ["not-an-object"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "input_items"):
                    self.harness.router.handle(
                        "binding/submit-prompt",
                        {
                            "binding_id": "p2p:ou-a:chat-a",
                            "input_items": value,
                        },
                    )
        self.assertEqual(self.harness.calls, [])

    def test_simple_methods_dispatch_only_normalized_arguments(self) -> None:
        cases = (
            ("service/reset-backend", {"force": True}, "reset_backend", (True,), {}),
            ("service/attach", {}, "attach_service", (), {}),
            (
                "binding/list",
                {"refresh_names": 1},
                "binding_list",
                (),
                {"refresh_names": True},
            ),
            (
                "binding/clear-stale",
                {"dry_run": 1},
                "binding_clear_stale",
                (),
                {"dry_run": True},
            ),
            ("thread/archive", {"thread_id": " t-1 "}, "archive", ("t-1",), {}),
            (
                "thread/clear-archived-bindings",
                {"thread_id": "t-1", "dry_run": True},
                "clear_archived",
                ("t-1",),
                {"dry_run": True},
            ),
        )
        for method, params, expected_name, expected_args, expected_kwargs in cases:
            with self.subTest(method=method):
                self.harness.calls.clear()
                self.harness.router.handle(method, params)
                self.assertEqual(
                    self.harness.calls,
                    [(expected_name, expected_args, expected_kwargs)],
                )

    def test_backend_reset_force_accepts_only_exact_json_booleans(self) -> None:
        for params, expected in (({}, False), ({"force": False}, False), ({"force": True}, True)):
            with self.subTest(params=params):
                self.harness.calls.clear()
                self.harness.router.handle("service/reset-backend", params)
                self.assertEqual(
                    self.harness.calls,
                    [("reset_backend", (expected,), {})],
                )

        for force in (None, 0, 1, "", "false", [], {}):
            with self.subTest(force=force):
                self.harness.calls.clear()
                with self.assertRaisesRegex(ValueError, "JSON boolean"):
                    self.harness.router.handle(
                        "service/reset-backend",
                        {"force": force},
                    )
                self.assertEqual(self.harness.calls, [])

    def test_direct_thread_status_does_not_require_name_resolution(self) -> None:
        result = self.harness.router.handle(
            "thread/status",
            {"thread_id": "thread-direct"},
        )

        self.assertEqual(result["thread_id"], "thread-1")
        self.assertEqual(self.harness.calls[0][0], "thread_status")
        summary = self.harness.calls[0][2]["summary"]
        self.assertEqual(summary.thread_id, "thread-direct")
        self.assertNotIn("resolve_target", [name for name, _args, _kwargs in self.harness.calls])

    def test_thread_bindings_projection_uses_status_snapshot(self) -> None:
        result = self.harness.router.handle(
            "thread/bindings",
            {"thread_id": "thread-1"},
        )

        self.assertEqual(
            result["bindings"],
            [
                {
                    "binding_id": "p2p:ou-a:chat-a",
                    "feishu_runtime_state": "attached",
                },
                {
                    "binding_id": "p2p:ou-b:chat-b",
                    "feishu_runtime_state": "detached",
                },
            ],
        )

    def test_goal_and_image_methods_use_resolved_thread(self) -> None:
        goal = self.harness.router.handle(
            "thread/goal/set",
            {"thread_name": "Thread", "objective": "ship", "status": "paused"},
        )
        image = self.harness.router.handle(
            "thread/send-image",
            {"thread_name": "Thread", "local_path": "/tmp/image.png"},
        )

        self.assertEqual(goal["owner"], "thread_goal_set")
        self.assertEqual(image["owner"], "send_image")
        self.assertEqual(
            [name for name, _args, _kwargs in self.harness.calls],
            ["resolve_target", "thread_goal_set", "resolve_target", "send_image"],
        )

    def test_missing_or_unknown_method_is_rejected_before_domain_call(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少 thread_id"):
            self.harness.router.handle("thread/archive", {})
        with self.assertRaisesRegex(ValueError, "未知控制面方法"):
            self.harness.router.handle("unknown/method", {})
        self.assertEqual(self.harness.calls, [])

    def test_controller_facade_contains_only_router_delegation(self) -> None:
        path = (
            pathlib.Path(__file__).parents[1]
            / "bot"
            / "runtime_admin"
            / "controller.py"
        )
        module = ast.parse(path.read_text(encoding="utf-8"))
        controller = next(
            node
            for node in module.body
            if isinstance(node, ast.ClassDef)
            and node.name == "RuntimeAdminController"
        )
        handler = next(
            node
            for node in controller.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "handle_service_control_request"
        )

        self.assertEqual(len(handler.body), 1)
        self.assertIn("_control_router.handle", ast.unparse(handler))


if __name__ == "__main__":
    unittest.main()
