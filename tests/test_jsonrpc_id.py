import unittest

from bot.jsonrpc_id import jsonrpc_id_key, optional_jsonrpc_id_key


class JsonRpcIdKeyTests(unittest.TestCase):
    def test_preserves_json_rpc_id_type_and_produces_transport_safe_string_token(self) -> None:
        numeric = jsonrpc_id_key(1)
        textual = jsonrpc_id_key("1")

        self.assertNotEqual(numeric, textual)
        self.assertEqual(numeric, "integer:1")
        self.assertEqual(textual, "string:MQ")
        self.assertEqual(jsonrpc_id_key(" value / with spaces "), "string:IHZhbHVlIC8gd2l0aCBzcGFjZXMg")
        self.assertTrue(textual.isascii())
        self.assertEqual(textual.strip(), textual)

    def test_missing_or_invalid_notification_id_never_matches_a_pending_key(self) -> None:
        self.assertEqual(optional_jsonrpc_id_key(None), "")
        self.assertEqual(optional_jsonrpc_id_key(""), "")
        self.assertEqual(optional_jsonrpc_id_key(True), "")
        self.assertEqual(optional_jsonrpc_id_key({"id": 1}), "")
