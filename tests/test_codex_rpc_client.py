import ast
import pathlib
import unittest
from unittest.mock import Mock, patch

from bot.codex_protocol.client import (
    AppServerEndpointMode,
    CodexRpcClient,
)


class CodexRpcClientFacadeTests(unittest.TestCase):
    def test_public_commands_delegate_to_one_connection_owner(self) -> None:
        connection = Mock()
        connection.rotate_server_request_authority_after_backend_stop.return_value = (
            "rotation"
        )
        connection.current_app_server_url.return_value = "ws://127.0.0.1:43210"
        connection.connection_generation.return_value = 7
        connection.request.return_value = {"ok": True}
        guard = Mock()

        with patch(
            "bot.codex_protocol.client.CodexRpcConnection",
            return_value=connection,
        ) as connection_type:
            client = CodexRpcClient(
                endpoint_mode=AppServerEndpointMode.ATTACHED_ENDPOINT,
                app_server_url="ws://127.0.0.1:43210",
            )

        connection_type.assert_called_once()
        client.require_owned_backend_lifecycle()
        client.start(
            outbound_transport_guard=guard,
            outbound_guard_method="thread/read",
        )
        client.stop(timeout=1.5)
        rotation = client.rotate_server_request_authority_after_backend_stop()
        url = client.current_app_server_url()
        generation = client.connection_generation(
            timeout=0.5,
            require_existing_connection=True,
        )
        fence = Mock()
        client.fence_backend_reset_generation(
            expected_connection_generation=7,
            fence_ingress=fence,
            timeout=0.25,
        )
        result = client.request(
            "thread/read",
            {"threadId": "thread-1"},
            timeout=0.5,
            require_existing_connection=True,
            expected_connection_generation=7,
            outbound_transport_guard=guard,
        )
        client.notify(
            "initialized",
            {"ready": True},
            timeout=0.5,
            outbound_transport_guard=guard,
        )
        client.respond(
            "request-1",
            result={"decision": "accept"},
            timeout=0.5,
            require_existing_connection=True,
            expected_connection_generation=7,
            outbound_transport_guard=guard,
        )

        connection.require_owned_backend_lifecycle.assert_called_once_with()
        connection.start.assert_called_once_with(
            outbound_transport_guard=guard,
            outbound_guard_method="thread/read",
        )
        connection.stop.assert_called_once_with(timeout=1.5)
        self.assertEqual(rotation, "rotation")
        self.assertEqual(url, "ws://127.0.0.1:43210")
        self.assertEqual(generation, 7)
        self.assertEqual(result, {"ok": True})
        connection.connection_generation.assert_called_once_with(
            timeout=0.5,
            require_existing_connection=True,
        )
        connection.fence_backend_reset_generation.assert_called_once_with(
            expected_connection_generation=7,
            fence_ingress=fence,
            timeout=0.25,
        )
        connection.request.assert_called_once_with(
            "thread/read",
            {"threadId": "thread-1"},
            timeout=0.5,
            require_existing_connection=True,
            expected_connection_generation=7,
            outbound_transport_guard=guard,
        )
        connection.notify.assert_called_once_with(
            "initialized",
            {"ready": True},
            timeout=0.5,
            outbound_transport_guard=guard,
        )
        connection.respond.assert_called_once_with(
            "request-1",
            result={"decision": "accept"},
            error=None,
            timeout=0.5,
            require_existing_connection=True,
            expected_connection_generation=7,
            outbound_transport_guard=guard,
        )

    def test_facade_owns_no_connection_or_stop_facts(self) -> None:
        root = pathlib.Path(__file__).parents[1] / "bot" / "codex_protocol"
        facade_module = ast.parse(
            (root / "client.py").read_text(encoding="utf-8")
        )
        connection_module = ast.parse(
            (root / "connection.py").read_text(encoding="utf-8")
        )
        facade = next(
            node
            for node in facade_module.body
            if isinstance(node, ast.ClassDef) and node.name == "CodexRpcClient"
        )
        connection = next(
            node
            for node in connection_module.body
            if isinstance(node, ast.ClassDef)
            and node.name == "CodexRpcConnection"
        )
        facade_methods = {
            node.name: node
            for node in facade.body
            if isinstance(node, ast.FunctionDef)
        }
        facade_attributes = {
            node.attr
            for node in ast.walk(facade)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }
        connection_attributes = {
            node.attr
            for node in ast.walk(connection)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        }

        self.assertEqual(facade.bases, [])
        self.assertEqual(facade_attributes, {"_connection"})
        self.assertEqual(
            set(facade_methods),
            {
                "__init__",
                "require_owned_backend_lifecycle",
                "start",
                "stop",
                "rotate_server_request_authority_after_backend_stop",
                "current_app_server_url",
                "connection_generation",
                "fence_backend_reset_generation",
                "request",
                "notify",
                "respond",
            },
        )
        for name, method in facade_methods.items():
            with self.subTest(name=name):
                self.assertEqual(len(method.body), 1)
                self.assertIn("_connection", ast.unparse(method))
        self.assertTrue(
            {
                "_ws",
                "_pending",
                "_connection_generation",
                "_connection_state",
                "_handshake_attempt",
                "_reader_threads",
                "_callback_threads",
                "_stop_barrier",
            }
            <= connection_attributes
        )
        imported_modules = {
            imported
            for node in connection_module.body
            for imported in self._imported_modules(node)
        }
        self.assertNotIn("bot.codex_protocol.client", imported_modules)

    @staticmethod
    def _imported_modules(node: ast.AST) -> tuple[str, ...]:
        if isinstance(node, ast.Import):
            return tuple(alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            return (node.module,)
        return ()


if __name__ == "__main__":
    unittest.main()
