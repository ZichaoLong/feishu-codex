import unittest

from bot.web_runtime.auth import WebAuthManager


class WebAuthManagerTests(unittest.TestCase):
    def test_bootstrap_is_single_use_and_rotates(self):
        rotated: list[str] = []
        auth = WebAuthManager(session_ttl_seconds=3600, on_bootstrap_rotated=rotated.append)
        original = auth.bootstrap_token

        session = auth.exchange_bootstrap(original)

        self.assertIsNotNone(session)
        self.assertNotEqual(auth.bootstrap_token, original)
        self.assertEqual(rotated, [auth.bootstrap_token])
        self.assertIsNone(auth.exchange_bootstrap(original))
        self.assertEqual(auth.authenticate(session.session_token), session)

    def test_revoke_invalidates_session(self):
        auth = WebAuthManager(session_ttl_seconds=3600)
        session = auth.exchange_bootstrap(auth.bootstrap_token)
        self.assertIsNotNone(session)

        self.assertTrue(auth.revoke(session.session_token))
        self.assertIsNone(auth.authenticate(session.session_token))

    def test_local_and_external_sessions_retain_distinct_audiences(self):
        auth = WebAuthManager(session_ttl_seconds=3600)
        local = auth.exchange_bootstrap(auth.bootstrap_token)
        external = auth.issue_external_session(
            external_origin="https://focus.example.test",
            proxy_identity="proxy:user@example.test",
        )

        self.assertIsNotNone(local)
        self.assertIsNotNone(external)
        assert local is not None
        assert external is not None
        self.assertEqual(local.audience.kind, "local")
        self.assertEqual(external.audience.kind, "external")
        self.assertEqual(
            external.audience.external_origin,
            "https://focus.example.test",
        )
        self.assertEqual(
            external.audience.proxy_identity,
            "proxy:user@example.test",
        )
        self.assertEqual(auth.authenticate(external.session_token), external)

    def test_external_session_issuance_has_a_fixed_process_bound(self):
        auth = WebAuthManager(session_ttl_seconds=3600)
        sessions = [
            auth.issue_external_session(
                external_origin="https://focus.example.test",
                proxy_identity=f"proxy:user-{index}",
            )
            for index in range(128)
        ]

        self.assertTrue(all(session is not None for session in sessions))
        self.assertIsNone(
            auth.issue_external_session(
                external_origin="https://focus.example.test",
                proxy_identity="proxy:overflow",
            )
        )
        first = sessions[0]
        assert first is not None
        self.assertTrue(auth.revoke(first.session_token))
        self.assertIsNotNone(
            auth.issue_external_session(
                external_origin="https://focus.example.test",
                proxy_identity="proxy:replacement",
            )
        )

    def test_failed_rotation_persistence_keeps_old_bootstrap_valid(self):
        attempts: list[str] = []

        def persist(token: str) -> None:
            attempts.append(token)
            if len(attempts) == 1:
                raise OSError("disk full")

        auth = WebAuthManager(session_ttl_seconds=3600, on_bootstrap_rotated=persist)
        original = auth.bootstrap_token

        with self.assertRaisesRegex(OSError, "disk full"):
            auth.exchange_bootstrap(original)

        self.assertEqual(auth.bootstrap_token, original)
        session = auth.exchange_bootstrap(original)
        self.assertIsNotNone(session)
        self.assertNotEqual(auth.bootstrap_token, original)
        self.assertEqual(attempts[-1], auth.bootstrap_token)

    def test_rotation_callback_runs_outside_state_lock(self):
        observed: list[str] = []
        auth: WebAuthManager

        def persist(_token: str) -> None:
            observed.append(auth.bootstrap_token)

        auth = WebAuthManager(session_ttl_seconds=3600, on_bootstrap_rotated=persist)
        original = auth.bootstrap_token

        self.assertIsNotNone(auth.exchange_bootstrap(original))
        self.assertEqual(observed, [original])


if __name__ == "__main__":
    unittest.main()
