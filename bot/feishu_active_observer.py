"""Prime and present a Feishu mirror for one already-running root turn."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from bot.adapters.base import ThreadSnapshot
from bot.binding_execution_runtime import (
    ActiveObserverSnapshotItem,
    BindingExecutionRuntimeTransitions,
    PrimeActiveObserverExecutionCommand,
    RollbackDetachedActiveObserverExecutionCommand,
)
from bot.binding_runtime_contract import BindingSessionSnapshot
from bot.execution_output_controller import ExecutionOutputController
from bot.execution_page_output_contract import InitialExecutionPageOpenStatus
from bot.runtime_state import BACKEND_THREAD_STATUS_ACTIVE


logger = logging.getLogger(__name__)

ActiveObserverPresentationStatus = Literal[
    "opened",
    "send_unknown",
    "send_rejected",
    "stale",
]


@dataclass(frozen=True, slots=True)
class ActiveObserverResumeSnapshot:
    turn_id: str
    reply_items: tuple[ActiveObserverSnapshotItem, ...]

    def __post_init__(self) -> None:
        if type(self.turn_id) is not str or not self.turn_id.strip():
            raise ValueError("active observer snapshot requires an exact turn id")
        if type(self.reply_items) is not tuple or any(
            type(item) is not ActiveObserverSnapshotItem
            for item in self.reply_items
        ):
            raise TypeError(
                "active observer snapshot requires exact typed reply items"
            )


@dataclass(frozen=True, slots=True)
class ActiveObserverExecution:
    session: BindingSessionSnapshot
    turn_id: str

    def __post_init__(self) -> None:
        if type(self.session) is not BindingSessionSnapshot:
            raise TypeError("active observer execution requires an exact session")
        if type(self.turn_id) is not str or not self.turn_id.strip():
            raise ValueError("active observer execution requires an exact turn id")


@dataclass(frozen=True, slots=True)
class ActiveObserverPresentationResult:
    status: ActiveObserverPresentationStatus
    turn_id: str = ""

    @property
    def presented(self) -> bool:
        return self.status in {"opened", "send_unknown"}


class ActiveObserverResumeSnapshotRejected(RuntimeError):
    """The acknowledged resume cannot identify one exact active turn."""


class FeishuActiveObserverController:
    """Join resume facts to the existing execution state and output owners."""

    def __init__(
        self,
        *,
        execution_runtime: BindingExecutionRuntimeTransitions,
        execution_output: ExecutionOutputController,
    ) -> None:
        if not isinstance(
            execution_runtime,
            BindingExecutionRuntimeTransitions,
        ):
            raise TypeError(
                "active observer requires the binding execution runtime"
            )
        if not isinstance(execution_output, ExecutionOutputController):
            raise TypeError("active observer requires the execution output owner")
        self._execution_runtime = execution_runtime
        self._execution_output = execution_output

    def prepare_resume_snapshot(
        self,
        snapshot: ThreadSnapshot,
    ) -> ActiveObserverResumeSnapshot | None:
        if type(snapshot) is not ThreadSnapshot:
            raise TypeError("active observer requires an exact resume snapshot")
        if type(snapshot.turns) is not list or any(
            type(turn) is not dict for turn in snapshot.turns
        ):
            raise ActiveObserverResumeSnapshotRejected(
                "active observer resume returned invalid turn history"
            )

        active_turns = [
            turn
            for turn in snapshot.turns
            if turn.get("status") == "inProgress"
        ]
        if not active_turns:
            if snapshot.summary.status == BACKEND_THREAD_STATUS_ACTIVE:
                raise ActiveObserverResumeSnapshotRejected(
                    "active observer resume reported active without an active turn"
                )
            return None
        if snapshot.summary.status != BACKEND_THREAD_STATUS_ACTIVE:
            raise ActiveObserverResumeSnapshotRejected(
                "active observer resume returned inconsistent active state"
            )
        if len(active_turns) != 1:
            logger.warning(
                "active observer resume returned multiple active turns: "
                "thread=%s count=%s",
                snapshot.summary.thread_id[:12],
                len(active_turns),
            )
            raise ActiveObserverResumeSnapshotRejected(
                "active observer resume did not return one exact active turn"
            )
        active_turn = active_turns[0]
        turn_id_value = active_turn.get("id")
        turn_id = (
            turn_id_value.strip()
            if type(turn_id_value) is str
            else ""
        )
        if not turn_id:
            logger.warning(
                "active observer resume returned an active turn without id: "
                "thread=%s",
                snapshot.summary.thread_id[:12],
            )
            raise ActiveObserverResumeSnapshotRejected(
                "active observer resume returned an active turn without id"
            )

        raw_items = active_turn.get("items")
        if type(raw_items) is not list or any(
            type(item) is not dict for item in raw_items
        ):
            raise ActiveObserverResumeSnapshotRejected(
                "active observer turn returned invalid items"
            )
        items = raw_items
        return ActiveObserverResumeSnapshot(
            turn_id=turn_id,
            reply_items=tuple(
                ActiveObserverSnapshotItem(
                    item_type=str(item.get("type", "") or "").strip(),
                    text=(
                        item.get("text", "")
                        if type(item.get("text")) is str
                        else ""
                    ),
                    text_available=(
                        item.get("type") != "agentMessage"
                        or type(item.get("text")) is str
                    ),
                )
                for item in items
                if type(item) is dict
            ),
        )

    def prime_execution(
        self,
        session: BindingSessionSnapshot,
        snapshot: ActiveObserverResumeSnapshot,
    ) -> ActiveObserverExecution:
        if type(session) is not BindingSessionSnapshot:
            raise TypeError("active observer requires an exact binding session")
        if type(snapshot) is not ActiveObserverResumeSnapshot:
            raise TypeError("active observer requires an exact prepared snapshot")
        primed = self._execution_runtime.prime_active_observer_execution(
            PrimeActiveObserverExecutionCommand(
                session=session,
                turn_id=snapshot.turn_id,
                reply_items=snapshot.reply_items,
                started_at=time.monotonic(),
            )
        )
        return ActiveObserverExecution(
            session=primed,
            turn_id=snapshot.turn_id,
        )

    def rollback_execution(
        self,
        session: BindingSessionSnapshot,
        snapshot: ActiveObserverResumeSnapshot,
    ) -> None:
        if type(session) is not BindingSessionSnapshot:
            raise TypeError("active observer rollback requires an exact session")
        if type(snapshot) is not ActiveObserverResumeSnapshot:
            raise TypeError("active observer rollback requires an exact snapshot")
        self._execution_runtime.rollback_detached_active_observer_execution(
            RollbackDetachedActiveObserverExecutionCommand(
                session=session,
                turn_id=snapshot.turn_id,
            )
        )

    def present_execution(
        self,
        execution: ActiveObserverExecution,
    ) -> ActiveObserverPresentationResult:
        if type(execution) is not ActiveObserverExecution:
            raise TypeError("active observer requires an exact primed execution")
        opened = self._execution_output.open_initial_execution_page(
            execution.session,
            "",
        )
        if (
            opened.status is InitialExecutionPageOpenStatus.ACTIVE
            and opened.session is not None
        ):
            self._execution_output.flush_execution_card_for_session(
                opened.session,
                immediate=True,
            )
            return ActiveObserverPresentationResult(
                status="opened",
                turn_id=execution.turn_id,
            )
        status = {
            InitialExecutionPageOpenStatus.SEND_UNKNOWN: "send_unknown",
            InitialExecutionPageOpenStatus.REJECTED: "send_rejected",
            InitialExecutionPageOpenStatus.STALE: "stale",
        }.get(opened.status, "stale")
        return ActiveObserverPresentationResult(
            status=status,
            turn_id=execution.turn_id,
        )
