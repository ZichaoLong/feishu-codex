import pathlib
import tempfile
import unittest

from bot.stores.thread_admission_store import ThreadAdmissionStore


class ThreadAdmissionStoreTests(unittest.TestCase):
    def test_admit_and_revoke_thread(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        store = ThreadAdmissionStore(pathlib.Path(tempdir.name))

        self.assertTrue(store.admit("thread-1"))
        self.assertFalse(store.admit("thread-1"))
        self.assertTrue(store.contains("thread-1"))
        self.assertEqual(store.list_all(), ("thread-1",))

        self.assertTrue(store.revoke("thread-1"))
        self.assertFalse(store.revoke("thread-1"))
        self.assertFalse(store.contains("thread-1"))


if __name__ == "__main__":
    unittest.main()
