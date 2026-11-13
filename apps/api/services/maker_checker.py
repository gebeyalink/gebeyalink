"""Maker/checker for changes that must not be made by one person alone.

The rule is simple and old: the person who proposes a financial change is not the person who
approves it. What makes it worth a module rather than a check in one route is that the *kinds*
of change will grow. A fee change is the first applier; feature flags, refunds, manual journals
and payouts all want the same queue, the same reviewer comparison, and the same audit shape.

A proposal is data. It is written with a ``kind`` and a ``payload``, and it changes nothing
until a different staff account approves it. Approval is one transaction: lock the proposal,
prove it is still pending and that the approver is not its maker, apply the kind's change, mark
the proposal approved, and append the audit row. The lock is what makes two approvers produce
one approval rather than two applications of the same change.

The self-approval refusal is checked before the applier runs, and the database carries the same
rule as a constraint. The service check is what produces a `FORBIDDEN` a console can explain;
the constraint is what makes a bypass in a future applier fail instead of becoming a second
self-approved policy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from apps.api.models.fee_config import FeeConfig as FeeConfigRow
from apps.api.models.pending_change import PendingChange, PendingChangeStatus
from apps.api.models.staff_user import StaffUser
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.fee_config_repository import FeeConfigRepository
from apps.api.repositories.pending_change_repository import PendingChangeRepository
from packages.shared.errors import ApiError, ErrorCode
from packages.shared.money import FeeConfig, FeePayer

#: The one kind this slice's applier understands. The queue itself is generic.
KIND_FEE_CONFIG_CHANGE = "FEE_CONFIG_CHANGE"

ACTION_FEE_CONFIG_CHANGE_SUBMITTED = "FEE_CONFIG_CHANGE_SUBMITTED"
ACTION_FEE_CONFIG_CHANGE_APPROVED = "FEE_CONFIG_CHANGE_APPROVED"
ACTION_FEE_CONFIG_CHANGE_REJECTED = "FEE_CONFIG_CHANGE_REJECTED"

#: A console may ask for less, but a queue that returns everything is not a queue.
PENDING_PAGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class FeeConfigView:
    """The active fee policy as the editor reads it, including when it took effect."""

    id: uuid.UUID
    fee_payer: FeePayer
    platform_fee_bps: int
    min_fee_santim: int
    max_fee_santim: int
    vat_bps: int
    effective_from: datetime


@dataclass(frozen=True, slots=True)
class FeeEditorView:
    """The policy in force and the proposals waiting for a second signature."""

    current: FeeConfigView
    pending: list[PendingChange]


class MakerCheckerService:
    """Propose, approve and reject changes that require a second staff account."""

    def __init__(
        self,
        *,
        pending: PendingChangeRepository,
        fee_configs: FeeConfigRepository,
        audit: AuditLogRepository,
    ) -> None:
        self._pending = pending
        self._fee_configs = fee_configs
        self._audit = audit

    async def fee_editor(self, *, now: datetime | None = None) -> FeeEditorView:
        """The current policy and every proposal still waiting."""
        row = await self._fee_configs.current_row(now=now)
        if row is None:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                details={"reason": "fee_config_missing"},
            )
        return FeeEditorView(
            current=FeeConfigView(
                id=row.id,
                fee_payer=row.fee_payer,
                platform_fee_bps=row.platform_fee_bps,
                min_fee_santim=row.min_fee_santim,
                max_fee_santim=row.max_fee_santim,
                vat_bps=row.vat_bps,
                effective_from=row.effective_from,
            ),
            pending=await self._pending.pending(limit=PENDING_PAGE_LIMIT),
        )

    async def submit_fee_change(
        self,
        *,
        actor: StaffUser,
        fee_payer: FeePayer,
        platform_fee_bps: int,
        min_fee_santim: int,
        max_fee_santim: int,
        vat_bps: int,
        reason: str,
        ip: str | None = None,
    ) -> PendingChange:
        """Record a proposed policy. Nothing about money changes yet."""
        cleaned_reason = _validate_fee_change(
            platform_fee_bps=platform_fee_bps,
            min_fee_santim=min_fee_santim,
            max_fee_santim=max_fee_santim,
            vat_bps=vat_bps,
            reason=reason,
        )
        payload: dict[str, Any] = {
            "fee_payer": fee_payer.value,
            "platform_fee_bps": platform_fee_bps,
            "min_fee_santim": min_fee_santim,
            "max_fee_santim": max_fee_santim,
            "vat_bps": vat_bps,
            "reason": cleaned_reason,
        }
        change = await self._pending.add(
            kind=KIND_FEE_CONFIG_CHANGE,
            payload=payload,
            submitted_by=actor.id,
        )
        await self._audit.record(
            actor_id=actor.id,
            actor_role=actor.role.value,
            action=ACTION_FEE_CONFIG_CHANGE_SUBMITTED,
            target_type="pending_change",
            target_id=change.id,
            before=None,
            after=_change_state(change),
            ip=ip,
        )
        await self._pending.commit()
        return change

    async def approve(
        self,
        *,
        change_id: uuid.UUID,
        actor: StaffUser,
        ip: str | None = None,
        now: datetime | None = None,
    ) -> PendingChange:
        """Apply a proposal, provided a different staff account is approving it."""
        moment = now or datetime.now(UTC)
        change = await self._pending.find_for_update(change_id)
        if change is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "pending_change"})
        self._require_pending(change)
        _refuse_self_review(change, actor=actor)

        before = _change_state(change)
        if change.kind == KIND_FEE_CONFIG_CHANGE:
            config, reason = _fee_change_from_payload(change.payload)
            applied = await self._apply_fee_change(
                config=config,
                reason=reason,
                actor=actor,
                at=moment,
            )
        else:  # pragma: no cover - only the fee kind can be submitted in this slice
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                details={
                    "resource": "pending_change",
                    "reason": "unsupported_change_kind",
                    "kind": change.kind,
                },
            )

        change.status = PendingChangeStatus.APPROVED
        change.approved_by = actor.id
        change.approved_at = moment
        after = {
            **_change_state(change),
            "approved_at": moment.isoformat(),
            "fee_config_id": str(applied.id),
        }
        await self._audit.record(
            actor_id=actor.id,
            actor_role=actor.role.value,
            action=ACTION_FEE_CONFIG_CHANGE_APPROVED,
            target_type="pending_change",
            target_id=change.id,
            before=before,
            after=after,
            ip=ip,
        )
        await self._pending.commit()
        return change

    async def reject(
        self,
        *,
        change_id: uuid.UUID,
        actor: StaffUser,
        reason: str,
        ip: str | None = None,
        now: datetime | None = None,
    ) -> PendingChange:
        """Close a proposal without changing the policy it described."""
        cleaned_reason = reason.strip()
        if not cleaned_reason:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"reason": "must_not_be_blank"}},
            )
        moment = now or datetime.now(UTC)
        change = await self._pending.find_for_update(change_id)
        if change is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "pending_change"})
        self._require_pending(change)
        _refuse_self_review(change, actor=actor)

        before = _change_state(change)
        change.status = PendingChangeStatus.REJECTED
        change.rejected_by = actor.id
        change.rejected_at = moment
        change.rejection_reason = cleaned_reason
        after = {
            **_change_state(change),
            "rejected_at": moment.isoformat(),
            "rejection_reason": cleaned_reason,
        }
        await self._audit.record(
            actor_id=actor.id,
            actor_role=actor.role.value,
            action=ACTION_FEE_CONFIG_CHANGE_REJECTED,
            target_type="pending_change",
            target_id=change.id,
            before=before,
            after=after,
            ip=ip,
        )
        await self._pending.commit()
        return change

    async def _apply_fee_change(
        self,
        *,
        config: FeeConfig,
        reason: str,
        actor: StaffUser,
        at: datetime,
    ) -> FeeConfigRow:
        """Close the policy in force and open the approved one, at one instant."""
        current = await self._fee_configs.current_row(now=at, for_update=True)
        if current is None:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR,
                details={"reason": "fee_config_missing"},
            )
        await self._fee_configs.close(current, at=at)
        return await self._fee_configs.add(
            config=config,
            effective_from=at,
            created_by=actor.id,
            reason=reason,
        )

    @staticmethod
    def _require_pending(change: PendingChange) -> None:
        if change.status is not PendingChangeStatus.PENDING:
            raise ApiError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                details={
                    "resource": "pending_change",
                    "reason": "already_reviewed",
                    "status": change.status.value,
                },
            )


def _refuse_self_review(change: PendingChange, *, actor: StaffUser) -> None:
    """A proposal's maker is not its checker, no matter what the endpoint is called."""
    if change.submitted_by == actor.id:
        raise ApiError(
            ErrorCode.FORBIDDEN,
            details={
                "resource": "pending_change",
                "reason": "self_review_forbidden",
            },
        )


def _validate_fee_change(
    *,
    platform_fee_bps: int,
    min_fee_santim: int,
    max_fee_santim: int,
    vat_bps: int,
    reason: str,
) -> str:
    fields: dict[str, str] = {}
    if platform_fee_bps <= 0 or platform_fee_bps > 10_000:
        fields["platform_fee_bps"] = "must_be_between_1_and_10000"
    if vat_bps < 0 or vat_bps > 10_000:
        fields["vat_bps"] = "must_be_between_0_and_10000"
    if min_fee_santim < 0:
        fields["min_fee_santim"] = "must_not_be_negative"
    if max_fee_santim < min_fee_santim:
        fields["max_fee_santim"] = "must_not_be_below_min_fee_santim"
    cleaned_reason = reason.strip()
    if not cleaned_reason:
        fields["reason"] = "must_not_be_blank"
    if fields:
        raise ApiError(ErrorCode.VALIDATION_ERROR, details={"fields": fields})
    return cleaned_reason


def _fee_change_from_payload(payload: dict[str, Any]) -> tuple[FeeConfig, str]:
    """Read a stored payload defensively; a malformed row is an internal error, not a 422."""
    try:
        fee_payer = FeePayer(cast(str, payload["fee_payer"]))
        platform_fee_bps = int(cast(int, payload["platform_fee_bps"]))
        min_fee_santim = int(cast(int, payload["min_fee_santim"]))
        max_fee_santim = int(cast(int, payload["max_fee_santim"]))
        vat_bps = int(cast(int, payload["vat_bps"]))
        reason = str(payload["reason"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            details={"reason": "invalid_fee_change_payload"},
        ) from exc
    if not reason:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            details={"reason": "invalid_fee_change_payload"},
        )
    try:
        reason = _validate_fee_change(
            platform_fee_bps=platform_fee_bps,
            min_fee_santim=min_fee_santim,
            max_fee_santim=max_fee_santim,
            vat_bps=vat_bps,
            reason=reason,
        )
    except ApiError as exc:
        raise ApiError(
            ErrorCode.INTERNAL_ERROR,
            details={"reason": "invalid_fee_change_payload"},
        ) from exc
    return (
        FeeConfig(
            fee_payer=fee_payer,
            platform_fee_bps=platform_fee_bps,
            min_fee_santim=min_fee_santim,
            max_fee_santim=max_fee_santim,
            vat_bps=vat_bps,
        ),
        reason,
    )


def _change_state(change: PendingChange) -> dict[str, Any]:
    """The serializable shape the audit hash is taken over."""
    return {
        "kind": change.kind,
        "status": change.status.value,
        "payload": change.payload,
        "submitted_by": str(change.submitted_by),
        "approved_by": None if change.approved_by is None else str(change.approved_by),
        "rejected_by": None if change.rejected_by is None else str(change.rejected_by),
    }
