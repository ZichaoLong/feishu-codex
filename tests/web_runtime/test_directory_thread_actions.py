from types import SimpleNamespace

from bot.adapters.base import (
    ThreadSummary,
)
from bot.stores.interaction_lease_store import (
    make_fcodex_interaction_holder,
)
from bot.thread_runtime_coordination import (
    ManagedInstanceLoadedThreadInventory,
    ManagedLoadedThreadInventorySnapshot,
)
from bot.web_runtime.controller import WebRuntimeError
from tests.web_runtime.harness import (
    WebRuntimeControllerHarness,
)


class WebRuntimeDirectoryThreadActionTests(WebRuntimeControllerHarness):
    def test_authoritative_archive_notification_drops_web_observation(self):
        self.controller.read_thread("tab-1", "thread-1")
        self.profile_store.update(
            "disconnected-tab",
            selected_thread_id="thread-1",
            working_dir="/work/other",
        )

        self.controller.handle_notification("thread/archived", {"threadId": "thread-1"})

        self.assertFalse(self.controller.retains_runtime("thread-1"))
        self.assertEqual(self.fake.released, ["thread-1"])
        self.assertEqual(
            self.controller.meta("tab-1")["writer_profile"]["selected_thread_id"],
            "",
        )
        disconnected = self.profile_store.load("disconnected-tab")
        self.assertEqual(disconnected.selected_thread_id, "")
        self.assertEqual(disconnected.working_dir, "/work/other")

        self.controller.handle_notification("thread/archived", {"threadId": "thread-1"})
        self.assertEqual(self.fake.released, ["thread-1"])

    def test_current_thread_directory_filters_to_this_instance(self):
        self.fake.extra_summaries = [
            ThreadSummary(
                thread_id="thread-other",
                cwd="/work/other",
                name="Other",
                preview="other",
                created_at=1,
                updated_at=2,
                source="appServer",
                status="active",
            )
        ]
        self.fake.runtime_leases["thread-1"] = SimpleNamespace(
            thread_id="thread-1",
            owner_instance="default",
        )
        self.fake.runtime_leases["thread-other"] = SimpleNamespace(
            thread_id="thread-other",
            owner_instance="explorer"
        )

        current = self.controller.list_threads(client_id="tab-1", scope="current")
        global_threads = self.controller.list_threads(client_id="tab-1", scope="global")

        self.assertEqual([item["id"] for item in current["threads"]], ["thread-1"])
        self.assertEqual(
            [item["id"] for item in global_threads["threads"]],
            ["thread-1", "thread-other"],
        )
        self.assertEqual(global_threads["threads"][1]["loaded_instance"], "explorer")

    def test_global_directory_uses_remote_loaded_inventory_without_a_lease(self):
        self.fake.extra_summaries = [
            ThreadSummary(
                thread_id="thread-idle",
                cwd="/work/other",
                name="Remote idle",
                preview="remote",
                created_at=1,
                updated_at=2,
                source="appServer",
                status="idle",
            )
        ]
        self.fake.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot(
            instances=(
                ManagedInstanceLoadedThreadInventory(
                    instance_name="explorer",
                    loaded_thread_ids=("thread-idle",),
                ),
            )
        )

        result = self.controller.list_threads(client_id="tab-1", scope="global")

        remote = next(item for item in result["threads"] if item["id"] == "thread-idle")
        self.assertEqual(remote["loaded_instance"], "explorer")
        self.assertTrue(remote["loaded_state_verified"])
        self.assertFalse(remote["selectable"])
        self.assertEqual(self.fake.managed_loaded_inventory_calls, 1)

    def test_global_directory_marks_unverified_empty_owner_unknown(self):
        self.fake.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot(
            instances=(
                ManagedInstanceLoadedThreadInventory(
                    instance_name="explorer",
                    error="unavailable",
                ),
            )
        )

        result = self.controller.list_threads(client_id="tab-1", scope="global")

        thread = result["threads"][0]
        self.assertEqual(thread["loaded_instance"], "")
        self.assertFalse(thread["loaded_state_verified"])
        self.assertFalse(thread["selectable"])

    def test_verified_remote_owner_survives_an_unrelated_instance_error(self):
        self.fake.extra_summaries = [
            ThreadSummary(
                thread_id="thread-idle",
                cwd="/work/other",
                name="Remote idle",
                preview="remote",
                created_at=1,
                updated_at=2,
                source="appServer",
                status="idle",
            )
        ]
        self.fake.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot(
            instances=(
                ManagedInstanceLoadedThreadInventory(
                    instance_name="explorer",
                    loaded_thread_ids=("thread-idle",),
                ),
                ManagedInstanceLoadedThreadInventory(
                    instance_name="research",
                    error="unavailable",
                ),
            )
        )

        result = self.controller.list_threads(client_id="tab-1", scope="global")

        remote = next(item for item in result["threads"] if item["id"] == "thread-idle")
        self.assertEqual(remote["loaded_instance"], "explorer")
        self.assertTrue(remote["loaded_state_verified"])
        self.assertFalse(remote["selectable"])

    def test_multiple_loaded_owners_are_unknown_and_not_selectable(self):
        self.fake.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot(
            instances=(
                ManagedInstanceLoadedThreadInventory(
                    instance_name="explorer",
                    loaded_thread_ids=("thread-1",),
                ),
                ManagedInstanceLoadedThreadInventory(
                    instance_name="research",
                    loaded_thread_ids=("thread-1",),
                ),
            )
        )

        result = self.controller.list_threads(client_id="tab-1", scope="global")

        thread = result["threads"][0]
        self.assertEqual(thread["loaded_instance"], "")
        self.assertFalse(thread["loaded_state_verified"])
        self.assertFalse(thread["selectable"])
        self.assertIn("explorer, research", thread["unavailable_reason"])

    def test_exact_local_owner_remains_selectable_when_remote_query_fails(self):
        self.fake.loaded_thread_ids = ["thread-1"]
        self.fake.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot(
            registry_error="registry unavailable",
        )

        result = self.controller.list_threads(client_id="tab-1", scope="global")

        thread = result["threads"][0]
        self.assertEqual(thread["loaded_instance"], "default")
        self.assertTrue(thread["loaded_state_verified"])
        self.assertTrue(thread["selectable"])

    def test_current_and_archived_directories_do_not_query_managed_inventory(self):
        self.controller.list_threads(client_id="tab-1", scope="current")
        self.controller.list_threads(client_id="tab-1", scope="global", archived=True)

        self.assertEqual(self.fake.managed_loaded_inventory_calls, 0)

    def test_archived_directory_does_not_query_loaded_threads(self):
        self.controller.list_threads(client_id="tab-1", scope="global", archived=True)

        self.assertEqual(self.fake.loaded_thread_list_calls, 0)
        self.assertEqual(self.fake.runtime_lease_loads, [])

    def test_compact_and_review_hold_web_owner_until_turn_completion(self):
        compact = self.controller.compact_thread("tab-1", "thread-1")

        self.assertEqual(compact["action"], "compact")
        self.assertEqual(self.fake.compacted, ["thread-1"])
        compact_submission = self.store.load("thread-1")
        self.assertEqual(compact_submission.holder.kind, "web")
        self.assertEqual(compact_submission.turn_id, "")
        self.assertIsNotNone(
            self.store.activate_turn(compact_submission, "compact-turn")
        )

        self.controller.handle_notification(
            "turn/completed",
            {
                "threadId": "thread-1",
                "turn": {"id": "compact-turn", "status": "completed"},
            },
        )
        self.assertIsNotNone(self.store.load("thread-1"))
        self.assertTrue(self.store.release_turn("thread-1", "compact-turn"))
        self.assertIsNone(self.store.load("thread-1"))

        review = self.controller.start_review(
            "tab-1",
            "thread-1",
            target={"type": "baseBranch", "branch": "main"},
        )
        self.assertEqual(review["turn_id"], "review-turn")
        self.assertEqual(
            self.fake.reviews,
            [("thread-1", {"type": "baseBranch", "branch": "main"}, "inline")],
        )
        self.assertEqual(self.store.load("thread-1").holder.kind, "web")

    def test_compact_post_resume_failure_keeps_committed_runtime_interest(self):
        def fail_compact(_thread_id: str) -> None:
            raise RuntimeError("compact rejected")

        self.fake.compact_thread = fail_compact

        with self.assertRaisesRegex(RuntimeError, "compact rejected"):
            self.controller.compact_thread("tab-1", "thread-1")

        interest = self.controller._runtime_interest.snapshot("thread-1")
        self.assertIsNotNone(interest)
        self.assertEqual(interest.outcome, "confirmed")
        self.assertEqual(interest.desired_client_ids, ("tab-1",))
        self.assertIsNone(self.store.load("thread-1"))
        self.assertEqual(self.fake.unsubscribed, [])
        self.assertEqual(self.fake.released, [])

    def test_rename_and_goal_mutations_return_normalized_projection(self):
        renamed = self.controller.rename_thread("tab-1", "thread-1", name="Renamed")
        goal = self.controller.set_goal(
            "tab-1",
            "thread-1",
            objective="Ship Web UI",
            status="active",
        )

        self.assertEqual(renamed["name"], "Renamed")
        self.assertEqual(self.fake.renamed, [("thread-1", "Renamed")])
        self.assertEqual(goal["goal"]["objective"], "Ship Web UI")
        self.assertEqual(goal["goal"]["budget"]["token_budget"], 1000)
        self.assertEqual(
            self.controller.goal("tab-1", "thread-1")["goal"]["tokens_used"], 25
        )
        self.assertTrue(
            self.controller.list_threads(client_id="tab-1", scope="global")["threads"][
                0
            ]["action_capabilities"]["goal"]
        )

        cleared = self.controller.clear_goal("tab-1", "thread-1")
        self.assertTrue(cleared["cleared"])
        self.assertIsNone(self.controller.goal("tab-1", "thread-1")["goal"])
        lease = self.store.load("thread-1")
        self.assertIsNotNone(lease)
        self.assertEqual(lease and lease.holder.holder_id, "web:tab-1")
        self.assertEqual(lease and lease.turn_id, "")

    def test_rename_and_unarchive_reject_another_interaction_owner(self):
        self.store.acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:12", owner_pid=0),
        )

        with self.assertRaises(WebRuntimeError) as rename_error:
            self.controller.rename_thread("tab-1", "thread-1", name="Renamed")
        with self.assertRaises(WebRuntimeError) as unarchive_error:
            self.controller.unarchive_thread("tab-1", "thread-1")

        self.assertEqual(rename_error.exception.code, "interaction_owned")
        self.assertEqual(unarchive_error.exception.code, "interaction_owned")
        self.assertEqual(self.fake.renamed, [])
        self.assertEqual(self.fake.unarchived, [])

    def test_thread_action_capabilities_fail_closed_for_observer_and_active_states(
        self,
    ):
        def capabilities() -> dict:
            listing = self.controller.list_threads(client_id="tab-1", scope="global")
            return listing["threads"][0]["action_capabilities"]

        self.assertEqual(
            capabilities(),
            {
                "rename": True,
                "archive": True,
                "unarchive": False,
                "delete": False,
                "compact": True,
                "fork": False,
                "export": False,
                "review": True,
                "goal": True,
            },
        )

        self.store.acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:12", owner_pid=0),
        )
        self.assertEqual(
            capabilities(),
            {
                "rename": False,
                "archive": False,
                "unarchive": False,
                "delete": False,
                "compact": False,
                "fork": False,
                "export": False,
                "review": False,
                "goal": False,
            },
        )

        self.store.clear_thread("thread-1")
        self.fake.status = "active"
        self.assertEqual(
            capabilities(),
            {
                "rename": True,
                "archive": False,
                "unarchive": False,
                "delete": False,
                "compact": False,
                "fork": False,
                "export": False,
                "review": False,
                "goal": False,
            },
        )

        # `systemError` is not proof that the thread is inactive.  The
        # endpoints already reject lifecycle/compact/review in that state;
        # the list projection must not invite the user to attempt them.
        self.fake.status = "systemError"
        self.assertEqual(
            capabilities(),
            {
                "rename": True,
                "archive": False,
                "unarchive": False,
                "delete": False,
                "compact": False,
                "fork": False,
                "export": False,
                "review": False,
                "goal": False,
            },
        )

    def test_archived_thread_action_capabilities_hide_lifecycle_controls_from_other_writers(
        self,
    ):
        archived = self.controller.list_threads(
            client_id="tab-1",
            scope="global",
            archived=True,
        )["threads"][0]["action_capabilities"]
        self.assertEqual(
            archived,
            {
                "rename": True,
                "archive": False,
                "unarchive": True,
                "delete": True,
                "compact": False,
                "fork": False,
                "export": False,
                "review": False,
                "goal": True,
            },
        )

        self.store.acquire(
            "thread-1",
            make_fcodex_interaction_holder("fcodex:12", owner_pid=0),
        )
        blocked = self.controller.list_threads(
            client_id="tab-1",
            scope="global",
            archived=True,
        )["threads"][0]["action_capabilities"]
        self.assertFalse(blocked["unarchive"])
        self.assertFalse(blocked["delete"])
