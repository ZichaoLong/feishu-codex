"""Reusable app-server fake for Web runtime controller tests."""

from bot.adapters.base import (
    RuntimeModelSummary,
    RuntimeReasoningEffortOption,
    ThreadGoalSummary,
    ThreadItemsPage,
    ThreadResumePage,
    ThreadSearchOccurrencesPage,
    ThreadSnapshot,
    ThreadSummary,
    ThreadTurnsPage,
)
from bot.thread_runtime_coordination import ManagedLoadedThreadInventorySnapshot


class _FakeRuntime:
    def __init__(self) -> None:
        self.status = "idle"
        self.cwd = "/work/project"
        self.turns: list[dict] = []
        self.started: list[dict] = []
        self.start_error: Exception | None = None
        self.read_error: Exception | None = None
        self.resume_error: Exception | None = None
        self.interrupted: list[tuple[str, str]] = []
        self.responses: list[tuple[object, object, object]] = []
        self.respond_error: Exception | None = None
        self.unsubscribe_error: Exception | None = None
        self.release_error: Exception | None = None
        self.unsubscribed: list[str] = []
        self.released: list[str] = []
        self.reads: list[tuple[str, bool]] = []
        self.resumed: list[str] = []
        self.resume_calls: list[dict] = []
        self.subscribers: tuple[tuple[str, str], ...] = ()
        self.created: list[dict] = []
        self.turn_pages: list[dict] = []
        self.item_pages: list[dict] = []
        self.search_pages: list[dict] = []
        self.steered: list[dict] = []
        self.loaded_thread_ids: list[str] = []
        self.loaded_thread_list_calls = 0
        self.managed_loaded_inventory = ManagedLoadedThreadInventorySnapshot()
        self.managed_loaded_inventory_calls = 0
        self.runtime_leases: dict[str, object] = {}
        self.runtime_lease_loads: list[str] = []
        self.compacted: list[str] = []
        self.reviews: list[tuple[str, dict, str]] = []
        self.renamed: list[tuple[str, str]] = []
        self.rename_error: Exception | None = None
        self.goal: ThreadGoalSummary | None = None
        self.goal_sets: list[dict] = []
        self.goal_clears: list[str] = []
        self.goal_set_error: Exception | None = None
        self.goal_clear_error: Exception | None = None
        self.archived: list[str] = []
        self.unarchived: list[str] = []
        self.deleted: list[str] = []
        self.extra_summaries: list[ThreadSummary] = []
        self.thread_name = "Demo"
        self.thread_path: str | None = None
        self.history_mode: str | None = None
        self.effective_model: str | None = "gpt-test"

    def summary(self) -> ThreadSummary:
        return ThreadSummary(
            thread_id="thread-1",
            cwd=self.cwd,
            name=self.thread_name,
            preview="hello",
            created_at=1,
            updated_at=2,
            source="appServer",
            status=self.status,
            path=self.thread_path,
            history_mode=self.history_mode,
        )

    def list_threads(self, **kwargs):
        return [self.summary(), *self.extra_summaries]

    def list_loaded_thread_ids(self, **_kwargs):
        self.loaded_thread_list_calls += 1
        return list(self.loaded_thread_ids)

    def list_managed_loaded_thread_inventory(self):
        self.managed_loaded_inventory_calls += 1
        return self.managed_loaded_inventory

    def load_thread_runtime_lease(self, thread_id: str):
        self.runtime_lease_loads.append(thread_id)
        return self.runtime_leases.get(thread_id)

    def list_thread_runtime_leases(self):
        self.runtime_lease_loads.append("*")
        return list(self.runtime_leases.values())

    def read_thread(
        self,
        thread_id: str,
        include_turns: bool,
        *,
        timeout: float | None = None,
        require_existing_connection: bool = False,
        expected_connection_generation: int | None = None,
    ):
        del timeout
        del require_existing_connection
        del expected_connection_generation
        if self.read_error is not None:
            raise self.read_error
        summary = self.summary_for(thread_id)
        self.reads.append((thread_id, include_turns))
        turns = self.turns if thread_id == "thread-1" else []
        return ThreadSnapshot(
            summary=summary,
            turns=list(turns) if include_turns else [],
            effective_model=self.effective_model,
            effective_reasoning_effort="high",
            effective_approval_policy="never",
            effective_permissions_profile_id=":workspace",
        )

    def resume_thread_page(self, thread_id: str, *, limit: int, **kwargs):
        self.assert_thread(thread_id)
        self.resumed.append(thread_id)
        self.resume_calls.append({"thread_id": thread_id, "limit": limit, **kwargs})
        if self.resume_error is not None:
            raise self.resume_error
        return ThreadResumePage(
            snapshot=ThreadSnapshot(
                summary=self.summary(),
                turns=[],
                effective_model=self.effective_model,
                effective_reasoning_effort="high",
                effective_approval_policy="never",
                effective_permissions_profile_id=":workspace",
            ),
            initial_turns_page=ThreadTurnsPage(
                turns=list(self.turns[-limit:]),
                next_cursor="older" if len(self.turns) > limit else None,
            ),
        )

    def list_thread_turns(self, thread_id: str, **kwargs):
        self.summary_for(thread_id)
        self.turn_pages.append(dict(kwargs))
        turns = self.turns if thread_id == "thread-1" else []
        limit = int(kwargs.get("limit") or len(turns) or 1)
        return ThreadTurnsPage(
            turns=list(turns[-limit:]),
            backwards_cursor=(
                f"page:{kwargs.get('cursor') or 'head'}" if turns else None
            ),
        )

    def list_thread_items(self, thread_id: str, **kwargs):
        self.summary_for(thread_id)
        self.item_pages.append({"thread_id": thread_id, **kwargs})
        return ThreadItemsPage()

    def search_thread_occurrences(self, thread_id: str, **kwargs):
        self.summary_for(thread_id)
        self.search_pages.append({"thread_id": thread_id, **kwargs})
        return ThreadSearchOccurrencesPage()

    def create_thread(self, **kwargs):
        self.created.append(dict(kwargs))
        return ThreadSnapshot(
            summary=self.summary(),
            turns=[],
            effective_model=self.effective_model,
            effective_reasoning_effort="high",
            effective_approval_policy="never",
            effective_permissions_profile_id=":workspace",
        )

    def start_turn(self, **kwargs):
        if self.start_error is not None:
            raise self.start_error
        self.started.append(dict(kwargs))
        self.status = "active"
        self.turns = [{"id": "turn-1", "status": "inProgress", "items": []}]
        return {"turn": {"id": "turn-1"}}

    def steer_turn(self, **kwargs):
        self.steered.append(dict(kwargs))
        return {"turnId": kwargs["expected_turn_id"]}

    def compact_thread(self, thread_id: str):
        self.assert_thread(thread_id)
        self.compacted.append(thread_id)

    def start_review(self, thread_id: str, *, target: dict, delivery: str):
        self.assert_thread(thread_id)
        self.reviews.append((thread_id, dict(target), delivery))
        return {"turn": {"id": "review-turn"}}

    def rename_thread(self, thread_id: str, name: str):
        self.assert_thread(thread_id)
        if self.rename_error is not None:
            raise self.rename_error
        self.thread_name = name
        self.renamed.append((thread_id, name))

    def get_thread_goal(self, thread_id: str, **_kwargs):
        self.assert_thread(thread_id)
        return self.goal

    def set_thread_goal(self, thread_id: str, **kwargs):
        self.assert_thread(thread_id)
        if self.goal_set_error is not None:
            raise self.goal_set_error
        self.goal_sets.append(dict(kwargs))
        objective = kwargs.get("objective")
        status = kwargs.get("status")
        self.goal = ThreadGoalSummary(
            thread_id=thread_id,
            objective=str(objective or (self.goal.objective if self.goal else "")),
            status=str(status or (self.goal.status if self.goal else "active")),
            token_budget=1000,
            tokens_used=25,
            time_used_seconds=3,
        )
        return self.goal

    def clear_thread_goal(self, thread_id: str, **_kwargs):
        self.assert_thread(thread_id)
        if self.goal_clear_error is not None:
            raise self.goal_clear_error
        self.goal_clears.append(thread_id)
        self.goal = None
        return True

    def archive_thread(self, thread_id: str, **_kwargs):
        self.assert_thread(thread_id)
        self.archived.append(thread_id)
        return {
            "thread_id": thread_id,
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleanup_errors": [],
        }

    def unarchive_thread(self, thread_id: str, **_kwargs):
        self.assert_thread(thread_id)
        self.unarchived.append(thread_id)
        return {
            "thread_id": thread_id,
            "upstream_outcome": "success",
            "focus_cleanup": "skipped",
            "cleanup_errors": [],
        }

    def delete_thread(self, thread_id: str, **_kwargs):
        self.assert_thread(thread_id)
        self.deleted.append(thread_id)
        return {
            "thread_id": thread_id,
            "upstream_outcome": "success",
            "focus_cleanup": "complete",
            "cleanup_errors": [],
        }

    def interrupt_turn(self, *, thread_id: str, turn_id: str):
        self.interrupted.append((thread_id, turn_id))

    def respond(
        self,
        identity,
        *,
        result=None,
        error=None,
        timeout: float | None = None,
    ):
        del timeout
        if self.respond_error is not None:
            raise self.respond_error
        self.responses.append((identity.request_id, result, error))

    def unsubscribe_thread(
        self,
        thread_id: str,
        *,
        expected_connection_generation: int | None = None,
    ):
        del expected_connection_generation
        self.unsubscribed.append(thread_id)
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error

    def release_runtime(self, thread_id: str):
        self.released.append(thread_id)
        if self.release_error is not None:
            raise self.release_error
        return True

    def thread_subscribers(self, _thread_id: str):
        return self.subscribers

    @staticmethod
    def list_models():
        return [
            RuntimeModelSummary(
                model="gpt-test",
                display_name="GPT Test",
                is_default=True,
                input_modalities=["text", "image"],
                supported_reasoning_efforts=[
                    RuntimeReasoningEffortOption("medium"),
                    RuntimeReasoningEffortOption("high"),
                ],
            ),
            RuntimeModelSummary(
                model="gpt-small",
                display_name="GPT Small",
                input_modalities=["text"],
                supported_reasoning_efforts=[RuntimeReasoningEffortOption("low")],
            ),
        ]

    def summary_for(self, thread_id: str) -> ThreadSummary:
        if thread_id == "thread-1":
            return self.summary()
        for summary in self.extra_summaries:
            if summary.thread_id == thread_id:
                return summary
        raise ValueError("unknown thread")

    def assert_thread(self, thread_id: str):
        self.summary_for(thread_id)
