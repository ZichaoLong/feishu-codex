from __future__ import annotations

import os

from bot.binding_runtime_contract import (
    BindingOwnerLossCommand,
    BindingOwnerLossSettlementReceipt,
)
from bot.binding_runtime_manager import BindingRuntimeManager
from bot.stores.interaction_lease_store import (
    InteractionLeaseStore,
    make_feishu_interaction_holder,
)
from bot.thread_subscription_registry import ThreadSubscriptionRegistry


def make_binding_runtime(
    *,
    data_dir,
    lock,
    chat_binding_store,
    owner_loss_observer=None,
):
    """Build the exact owner-loss capable binding runtime used by admin tests."""

    interaction_lease_store = InteractionLeaseStore(data_dir)
    transaction_nonce = 0

    def settle(command: BindingOwnerLossCommand):
        nonlocal transaction_nonce
        if owner_loss_observer is not None:
            owner_loss_observer(command)
        lease = interaction_lease_store.load(command.thread_id)
        holder = make_feishu_interaction_holder(
            command.binding[0], command.binding[1], owner_pid=os.getpid()
        )
        if lease is not None and lease.holder.same_holder(holder):
            try:
                released = interaction_lease_store.release_if_matches(lease)
            except Exception as exc:
                raise RuntimeError(f"lease release failed: {exc}") from exc
            if released is not True:
                raise RuntimeError("lease release failed")
        transaction_nonce += 1
        return BindingOwnerLossSettlementReceipt(
            command=command,
            _settler_nonce=1,
            _transaction_nonce=transaction_nonce,
        )

    runtime = BindingRuntimeManager(
        lock=lock,
        default_working_dir="/tmp/default",
        default_approval_policy="on-request",
        default_permissions_profile_id=":workspace",
        default_model="gpt-5.4",
        default_reasoning_effort="medium",
        chat_binding_store=chat_binding_store,
        thread_subscription_registry=ThreadSubscriptionRegistry(),
        interaction_lease_store=interaction_lease_store,
        is_group_chat=lambda chat_id, message_id: False,
        owner_loss_settler=settle,
    )
    return interaction_lease_store, runtime
