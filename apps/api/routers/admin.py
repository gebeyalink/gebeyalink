"""The staff surface: sign-in, the KYC queue, and the audited agent decisions.

Every route here is behind ``require_staff``, and the role it names is the narrowest one that
may perform it: a verifier works the queue, an admin administers staff and agents. The routes
are thin on purpose — a route decides who may call and what the request looks like, and the
service decides what happens. Reading a route should not require reading the rules.

Two route families are grouped by actor rather than by resource. ``/v1/admin/login``,
``/change-password`` and ``/logout`` are the sign-in handshake and are the only admin routes a
token with ``scope=change_password_only`` may reach. Everything else requires a full token.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from apps.api.dependencies import (
    AdminOnlyDep,
    AdminServiceDep,
    CurrentStaffDep,
    KycReviewerDep,
    StaffAuthServiceDep,
)
from apps.api.models.kyc_case import KycRejectionReason, KycStatus, KycTier
from apps.api.schemas.admin import (
    AgentApproveBody,
    AgentDecisionResponse,
    KycCaseAdminResponse,
    KycRejectionBody,
    StaffCreateBody,
    StaffCreatedResponse,
    StaffDisableBody,
    StaffLoginBody,
    StaffPasswordChangeBody,
    StaffResponse,
    StaffRoleChangeBody,
    StaffTokenResponse,
)
from apps.api.schemas.pagination import CursorPage

router = APIRouter(prefix="/admin", tags=["admin"])


def _ip(request: Request) -> str | None:
    """The caller's address, for the audit and session rows.

    The forwarding header is honoured first, for the same reason the trader routes honour it:
    the platform sits behind a load balancer that sets it. The value is then *parsed* rather
    than passed through, because the columns it feeds are ``inet`` — a value that is not an
    address (a test client's ``"testclient"``, a proxy's placeholder, an empty header) is not
    an address and is recorded as absent, not smuggled in. An absent address is a true fact;
    an invented one is not.
    """
    forwarded = request.headers.get("x-forwarded-for")
    candidate = forwarded.split(",")[0].strip() if forwarded else None
    if candidate is None and request.client is not None:
        candidate = request.client.host
    if candidate is None:
        return None
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


# -- the sign-in handshake ----------------------------------------------------


@router.post("/login", status_code=status.HTTP_200_OK)
async def login(
    body: StaffLoginBody,
    request: Request,
    service: StaffAuthServiceDep,
) -> StaffTokenResponse:
    """Prove a password and a one-time code, and open a session."""
    issued = await service.login(
        email=body.email,
        password=body.password,
        totp_code=body.totp_code,
        ip=_ip(request),
    )
    return StaffTokenResponse(
        access_token=issued.access_token,
        expires_at=issued.expires_at,
        scope=issued.scope,
    )


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: StaffPasswordChangeBody,
    staff: CurrentStaffDep,
    service: StaffAuthServiceDep,
) -> None:
    """Replace the caller's own password. Reachable with either scope."""
    await service.change_password(
        staff=staff.staff,
        session_id=staff.session_id,
        old_password=body.old_password,
        new_password=body.new_password,
        totp_code=body.totp_code,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(staff: CurrentStaffDep, service: StaffAuthServiceDep) -> None:
    """End the caller's own session."""
    await service.logout(session_id=staff.session_id)


# -- staff administration ------------------------------------------------------


@router.post("/staff", status_code=status.HTTP_201_CREATED)
async def create_staff(
    body: StaffCreateBody,
    request: Request,
    actor: AdminOnlyDep,
    service: AdminServiceDep,
) -> StaffCreatedResponse:
    """Create a staff account. The provisioning URI is returned exactly once, here."""
    row, uri, generated = await service.create_staff(
        actor=actor.staff,
        email=body.email,
        role=body.role,
        password=body.password,
        ip=_ip(request),
    )
    return StaffCreatedResponse(
        id=row.id,
        email=row.email,
        role=row.role,
        must_change_password=row.must_change_password,
        provisioning_uri=uri,
        generated_password=generated,
    )


@router.patch("/staff/{staff_id}", status_code=status.HTTP_200_OK)
async def set_staff_disabled(
    staff_id: UUID,
    body: StaffDisableBody,
    request: Request,
    actor: AdminOnlyDep,
    service: AdminServiceDep,
) -> StaffResponse:
    """Disable or enable a staff account."""
    row = await service.set_disabled(
        actor=actor.staff, staff_id=staff_id, disabled=body.disabled, ip=_ip(request)
    )
    return StaffResponse.model_validate(row)


@router.post("/staff/{staff_id}/role", status_code=status.HTTP_200_OK)
async def change_staff_role(
    staff_id: UUID,
    body: StaffRoleChangeBody,
    request: Request,
    actor: AdminOnlyDep,
    service: AdminServiceDep,
) -> StaffResponse:
    """Move a staff account to a new role, ending the sessions the old role opened."""
    row = await service.change_role(
        actor=actor.staff, staff_id=staff_id, role=body.role, ip=_ip(request)
    )
    return StaffResponse.model_validate(row)


# -- the KYC queue -------------------------------------------------------------


@router.get("/kyc-cases", status_code=status.HTTP_200_OK)
async def list_kyc_cases(
    staff: KycReviewerDep,
    service: AdminServiceDep,
    case_status: Annotated[KycStatus | None, Query(alias="status")] = None,
    tier_requested: Annotated[KycTier | None, Query()] = None,
    cursor: Annotated[str | None, Query(description="Opaque; echo the last one given.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CursorPage[KycCaseAdminResponse]:
    """One page of the reviewer's queue. Defaults to the waiting cases."""
    after = _decode_case_cursor(cursor) if cursor else None
    effective_status = case_status if case_status is not None else KycStatus.PENDING
    rows, has_more = await service.list_kyc_cases(
        status=effective_status, tier_requested=tier_requested, after=after, limit=limit
    )
    return CursorPage[KycCaseAdminResponse](
        data=[KycCaseAdminResponse.model_validate(row) for row in rows],
        next_cursor=_encode_case_cursor(rows[-1]) if has_more and rows else None,
        has_more=has_more,
    )


@router.get("/kyc-cases/{case_id}", status_code=status.HTTP_200_OK)
async def read_kyc_case(
    case_id: UUID,
    staff: KycReviewerDep,
    service: AdminServiceDep,
) -> KycCaseAdminResponse:
    """One case, documents included."""
    case = await service.read_case(case_id=case_id)
    return KycCaseAdminResponse.model_validate(case)


@router.post("/kyc-cases/{case_id}/approve", status_code=status.HTTP_200_OK)
async def approve_kyc_case(
    case_id: UUID,
    request: Request,
    actor: KycReviewerDep,
    service: AdminServiceDep,
) -> KycCaseAdminResponse:
    """Approve a waiting case and move the account's tier."""
    case = await service.approve_case(actor=actor.staff, case_id=case_id, ip=_ip(request))
    return KycCaseAdminResponse.model_validate(case)


@router.post("/kyc-cases/{case_id}/reject", status_code=status.HTTP_200_OK)
async def reject_kyc_case(
    case_id: UUID,
    body: KycRejectionBody,
    request: Request,
    actor: KycReviewerDep,
    service: AdminServiceDep,
) -> KycCaseAdminResponse:
    """Reject a waiting case with a reason; a fraud suspicion freezes the account."""
    case = await service.reject_case(
        actor=actor.staff,
        case_id=case_id,
        reason_code=KycRejectionReason(body.reason_code),
        ip=_ip(request),
    )
    return KycCaseAdminResponse.model_validate(case)


# -- agent administration ------------------------------------------------------


@router.post("/agents/approve", status_code=status.HTTP_200_OK)
async def approve_agent(
    body: AgentApproveBody,
    request: Request,
    actor: AdminOnlyDep,
    service: AdminServiceDep,
) -> AgentDecisionResponse:
    """Approve an account as an agent, audited. The CLI's workaround, now a route."""
    user, _granted = await service.approve_agent(
        actor=actor.staff, user_id=body.user_id, ip=_ip(request)
    )
    return AgentDecisionResponse(user_id=user.id, agent=True)


@router.post("/agents/{user_id}/revoke", status_code=status.HTTP_200_OK)
async def revoke_agent(
    user_id: UUID,
    request: Request,
    actor: AdminOnlyDep,
    service: AdminServiceDep,
) -> AgentDecisionResponse:
    """Revoke the agent role and end the relationships the agent holds, audited."""
    user, ended = await service.revoke_agent(actor=actor.staff, user_id=user_id, ip=_ip(request))
    return AgentDecisionResponse(user_id=user.id, agent=False, assignments_ended=ended)


# -- cursor helpers ------------------------------------------------------------


def _encode_case_cursor(case: object) -> str:
    """Where a page ended: the (created_at, id) pair the next page seeks past."""
    import base64

    raw = f"{case.created_at.isoformat()}|{case.id}".encode()  # type: ignore[attr-defined]
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_case_cursor(cursor: str) -> tuple[datetime, UUID]:
    """Read a boundary back, refusing one we did not write."""
    import base64
    from datetime import datetime as _dt

    from packages.shared.errors import ApiError, ErrorCode

    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        stamp, _, identity = raw.partition("|")
        return _dt.fromisoformat(stamp), UUID(identity)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            details={"fields": {"cursor": "not_a_cursor_this_server_wrote"}},
        ) from exc
