import pathlib
import tempfile
import unittest

from bot.stores.thread_resume_profile_store import ThreadResumeProfileStore


class ThreadResumeProfileStoreTests(unittest.TestCase):
    def test_save_load_and_clear(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadResumeProfileStore(pathlib.Path(tempdir.name))

        saved = store.save(
            "thread-1",
            profile="provider2",
            model="provider2-model",
            model_provider="provider2_api",
        )

        loaded = store.load("thread-1")
        assert loaded is not None
        self.assertEqual(loaded.thread_id, "thread-1")
        self.assertEqual(loaded.profile, "provider2")
        self.assertEqual(loaded.model, "provider2-model")
        self.assertEqual(loaded.model_provider, "provider2_api")
        self.assertGreater(loaded.updated_at, 0)
        self.assertEqual(saved.profile, loaded.profile)

        self.assertTrue(store.clear("thread-1"))
        self.assertIsNone(store.load("thread-1"))


if __name__ == "__main__":
    unittest.main()
