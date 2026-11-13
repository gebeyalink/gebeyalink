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
from datetime import date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request, status

from apps.api.dependencies import (
    AdminOnlyDep,
    AdminServiceDep,
    AuditReaderDep,
    CurrentStaffDep,
    KycReviewerDep,
    MakerCheckerServiceDep,
    ModeratorDep,
    StaffAuthServiceDep,
    UserSearchDep,
)
from apps.api.models.kyc_case import KycRejectionReason, KycStatus, KycTier
from apps.api.models.listing import Listing, ListingStatus
from apps.api.models.pending_change import PendingChange
from apps.api.models.user import AccountStatus
from apps.api.schemas.admin import (
    AdminDashboardResponse,
    AdminUserPatchBody,
    AdminUserResponse,
    AgentApproveBody,
    AgentDecisionResponse,
    AuditLogResponse,
    FeeChangeBody,
    FeeConfigResponse,
    FeeEditorResponse,
    KycCaseAdminResponse,
    KycRejectionBody,
    ListingAdminResponse,
    ListingModerationBody,
    ListingModerationResponse,
    OrderLedgerResponse,
    PendingChangeRejectionBody,
    PendingFeeChangeResponse,
    PriceOverrideBody,
    PriceOverrideResponse,
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
from apps.api.schemas.search import RegionCode
from apps.api.services.admin_service import AdminUserRow
from apps.api.services.maker_checker import FeeConfigView

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


def _admin_user_response(row: AdminUserRow) -> AdminUserResponse:
    """Turn a service row into the console's account view."""
    user = row.user
    return AdminUserResponse(
        id=user.id,
        phone_e164=user.phone_e164,
        role=user.role,
        language=user.language,
        kyc_tier=user.kyc_tier,
        status=user.status,
        additional_roles=row.additional_roles,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def _fee_config_response(config: FeeConfigView) -> FeeConfigResponse:
    """Turn the service's current-policy view into the editor's row."""
    return FeeConfigResponse(
        id=config.id,
        fee_payer=config.fee_payer,
        platform_fee_bps=config.platform_fee_bps,
        min_fee_santim=config.min_fee_santim,
        max_fee_santim=config.max_fee_santim,
        vat_bps=config.vat_bps,
        effective_from=config.effective_from,
    )


def _pending_change_response(change: PendingChange) -> PendingFeeChangeResponse:
    """Turn a queue row into the reviewer's view.

    ``created_at`` is exposed as ``submitted_at`` because that is what it means for a proposal.
    The column keeps the name every other table uses; the contract says what the instant is.
    """
    return PendingFeeChangeResponse(
        id=change.id,
        kind=change.kind,
        status=change.status,
        payload=change.payload,
        submitted_by=change.submitted_by,
        submitted_at=change.created_at,
        approved_by=change.approved_by,
        approved_at=change.approved_at,
        rejected_by=change.rejected_by,
        rejected_at=change.rejected_at,
        rejection_reason=change.rejection_reason,
    )


def _listing_admin_response(listing: Listing) -> ListingAdminResponse:
    """Turn a listing into the moderation screen's row.

    ``available_kg`` is the listing's own quantity less the weight already consumed against
    it. Live reservations are not counted on this screen; that count belongs to the
    buyer-facing listing read.
    """
    return ListingAdminResponse(
        id=listing.id,
        seller_id=listing.seller_id,
        crop_id=listing.crop_id,
        quantity_kg=listing.quantity_kg,
        available_kg=listing.quantity_kg - listing.sold_kg,
        unit_price_santim=listing.unit_price_santim,
        status=listing.status,
        region_code=listing.region_code,
        short_code=listing.short_code,
        created_at=listing.created_at,
    )


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


# -- trader account administration ---------------------------------------------


@router.get("/users", status_code=status.HTTP_200_OK)
async def list_users(
    staff: UserSearchDep,
    service: AdminServiceDep,
    phone_e164: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=16,
            description="An E.164 number, or the prefix of one; a full number is exact.",
        ),
    ] = None,
    account_status: Annotated[AccountStatus | None, Query(alias="status")] = None,
    cursor: Annotated[str | None, Query(description="Opaque; echo the last one given.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CursorPage[AdminUserResponse]:
    """One page of trader accounts, filtered by phone prefix and status."""
    after = _decode_cursor(cursor) if cursor else None
    rows, has_more = await service.list_users(
        phone_e164=phone_e164, status=account_status, after=after, limit=limit
    )
    return CursorPage[AdminUserResponse](
        data=[_admin_user_response(row) for row in rows],
        next_cursor=(
            _encode_cursor(rows[-1].user.created_at, rows[-1].user.id)
            if has_more and rows
            else None
        ),
        has_more=has_more,
    )


@router.patch("/users/{id}", status_code=status.HTTP_200_OK)
async def update_user(
    id: UUID,
    body: AdminUserPatchBody,
    request: Request,
    actor: AdminOnlyDep,
    service: AdminServiceDep,
) -> AdminUserResponse:
    """Change a trader's status, primary role, or additional roles; every change is audited."""
    row = await service.update_user(
        actor=actor.staff,
        user_id=id,
        status=body.status,
        role=body.role,
        additional_roles_add=body.additional_roles_add,
        additional_roles_remove=body.additional_roles_remove,
        reason=body.reason,
        ip=_ip(request),
    )
    return _admin_user_response(row)


# -- listing moderation --------------------------------------------------------


@router.get("/listings", status_code=status.HTTP_200_OK)
async def list_listings(
    staff: ModeratorDep,
    service: AdminServiceDep,
    seller_id: Annotated[UUID | None, Query()] = None,
    listing_status: Annotated[ListingStatus | None, Query(alias="status")] = None,
    crop_id: Annotated[int | None, Query(ge=1)] = None,
    region_code: Annotated[RegionCode | None, Query()] = None,
    q: Annotated[
        str | None,
        Query(max_length=200, description="Case-insensitive substring of the description."),
    ] = None,
    cursor: Annotated[str | None, Query(description="Opaque; echo the last one given.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CursorPage[ListingAdminResponse]:
    """One page of listings in every state, narrowed by seller, status, crop, region or text.

    This is the moderator's way to find the listing behind a report before suspending or
    restoring it. The buyer search is deliberately a different question: it shows only what
    can be bought, while moderation has to be able to reach a listing that is already
    suspended.
    """
    after = _decode_cursor(cursor) if cursor else None
    rows, has_more = await service.list_listings(
        seller_id=seller_id,
        status=listing_status,
        crop_id=crop_id,
        region_code=region_code,
        q=q,
        after=after,
        limit=limit,
    )
    return CursorPage[ListingAdminResponse](
        data=[_listing_admin_response(row) for row in rows],
        next_cursor=(
            _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        ),
        has_more=has_more,
    )


@router.post("/listings/{id}/suspend", status_code=status.HTTP_200_OK)
async def suspend_listing(
    id: UUID,
    body: ListingModerationBody,
    request: Request,
    actor: ModeratorDep,
    service: AdminServiceDep,
) -> ListingModerationResponse:
    """Take an ACTIVE listing off the board, with the moderator's reason in the audit row."""
    listing = await service.suspend_listing(
        actor=actor.staff, listing_id=id, reason=body.reason, ip=_ip(request)
    )
    return ListingModerationResponse(id=listing.id, status=listing.status)


@router.post("/listings/{id}/restore", status_code=status.HTTP_200_OK)
async def restore_listing(
    id: UUID,
    request: Request,
    actor: ModeratorDep,
    service: AdminServiceDep,
) -> ListingModerationResponse:
    """Put a SUSPENDED listing back in front of buyers, audited."""
    listing = await service.restore_listing(actor=actor.staff, listing_id=id, ip=_ip(request))
    return ListingModerationResponse(id=listing.id, status=listing.status)


# -- the audit trail -----------------------------------------------------------


@router.get("/audit-log", status_code=status.HTTP_200_OK)
async def read_audit_log(
    staff: AuditReaderDep,
    service: AdminServiceDep,
    actor_id: Annotated[UUID | None, Query()] = None,
    action: Annotated[str | None, Query(max_length=64)] = None,
    target_type: Annotated[str | None, Query(max_length=32)] = None,
    target_id: Annotated[UUID | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    cursor: Annotated[str | None, Query(description="Opaque; echo the last one given.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> CursorPage[AuditLogResponse]:
    """One page of the append-only trail, filtered by actor, action, target and time."""
    after = _decode_cursor(cursor) if cursor else None
    rows, has_more = await service.list_audit_log(
        actor_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        since=since,
        until=until,
        after=after,
        limit=limit,
    )
    return CursorPage[AuditLogResponse](
        data=[AuditLogResponse.model_validate(row) for row in rows],
        next_cursor=(_encode_cursor(rows[-1].at, rows[-1].id) if has_more and rows else None),
        has_more=has_more,
    )


# -- the dashboard and the order ledger ---------------------------------------


@router.get("/dashboard", status_code=status.HTTP_200_OK)
async def read_dashboard(
    staff: AuditReaderDep,
    service: AdminServiceDep,
) -> AdminDashboardResponse:
    """Read §3.3's metrics, including the nulls the COD pilot cannot compute.

    The role gate is the audit reader's because both surfaces answer the same question —
    ADMIN and SUPPORT are the two staff roles responsible for the platform's numbers and for
    explaining them to a trader.
    """
    metrics = await service.dashboard()
    return AdminDashboardResponse(
        gmv_santim=metrics.gmv_santim,
        cod_gmv_santim=metrics.cod_gmv_santim,
        completed_orders=metrics.completed_orders,
        active_farmers=metrics.active_farmers,
        offer_to_order_conversion=metrics.offer_to_order_conversion,
        offer_to_order_conversion_note=metrics.offer_to_order_conversion_note,
        cod_offer_to_order_conversion=metrics.cod_offer_to_order_conversion,
        dispute_rate=metrics.dispute_rate,
        dispute_rate_note=metrics.dispute_rate_note,
        nps=metrics.nps,
        nps_surveyed_at=metrics.nps_surveyed_at,
        nps_note=metrics.nps_note,
        ussd_share=metrics.ussd_share,
        as_of=metrics.as_of,
    )


@router.get("/orders/{id}/ledger", status_code=status.HTTP_200_OK)
async def read_order_ledger(
    id: UUID,
    staff: AuditReaderDep,
    service: AdminServiceDep,
) -> OrderLedgerResponse:
    """Read any order's balanced journals; a COD order honestly has none.

    Staff may read an order that is not theirs. The route is read-only and deliberately does
    not audit: looking at the ledger is not an action the trail needs to answer for.
    """
    ledger = await service.order_ledger(order_id=id)
    return OrderLedgerResponse(order_id=ledger.order_id, entries=[], note=ledger.note)


# -- the price board ----------------------------------------------------------


@router.put("/prices/{crop_id}/{region_code}/{day}", status_code=status.HTTP_200_OK)
async def override_price_board(
    crop_id: int,
    region_code: RegionCode,
    day: date,
    body: PriceOverrideBody,
    request: Request,
    actor: AdminOnlyDep,
    service: AdminServiceDep,
) -> PriceOverrideResponse:
    """Replace one day's computed board price, with the reason in the audit hash."""
    row = await service.override_price(
        actor=actor.staff,
        crop_id=crop_id,
        region_code=region_code,
        day=day,
        price_santim=body.price_santim,
        reason=body.reason,
        ip=_ip(request),
    )
    return PriceOverrideResponse(
        crop_id=row.crop_id,
        region_code=row.region_code,
        day=row.day,
        price_santim=row.price_santim,
    )


# -- the fee editor ------------------------------------------------------------


@router.get("/fees", status_code=status.HTTP_200_OK)
async def read_fees(
    actor: AdminOnlyDep,
    service: MakerCheckerServiceDep,
) -> FeeEditorResponse:
    """The policy in force and every proposal waiting for a second signature.

    Reading is not itself a change, so this route does not write an audit row. The editor needs
    both halves together: what an order is charged under now, and what somebody has proposed
    changing it to.
    """
    view = await service.fee_editor()
    return FeeEditorResponse(
        current=_fee_config_response(view.current),
        pending=[_pending_change_response(change) for change in view.pending],
    )


@router.post("/fees/changes", status_code=status.HTTP_201_CREATED)
async def submit_fee_change(
    body: FeeChangeBody,
    request: Request,
    actor: AdminOnlyDep,
    service: MakerCheckerServiceDep,
) -> PendingFeeChangeResponse:
    """Propose a new policy. Nothing starts being charged differently yet."""
    change = await service.submit_fee_change(
        actor=actor.staff,
        fee_payer=body.fee_payer,
        platform_fee_bps=body.platform_fee_bps,
        min_fee_santim=body.min_fee_santim,
        max_fee_santim=body.max_fee_santim,
        vat_bps=body.vat_bps,
        reason=body.reason,
        ip=_ip(request),
    )
    return _pending_change_response(change)


@router.post("/fees/changes/{change_id}/approve", status_code=status.HTTP_200_OK)
async def approve_fee_change(
    change_id: UUID,
    request: Request,
    actor: AdminOnlyDep,
    service: MakerCheckerServiceDep,
) -> PendingFeeChangeResponse:
    """Apply a proposal, provided the caller did not submit it."""
    change = await service.approve(
        change_id=change_id,
        actor=actor.staff,
        ip=_ip(request),
    )
    return _pending_change_response(change)


@router.post("/fees/changes/{change_id}/reject", status_code=status.HTTP_200_OK)
async def reject_fee_change(
    change_id: UUID,
    body: PendingChangeRejectionBody,
    request: Request,
    actor: AdminOnlyDep,
    service: MakerCheckerServiceDep,
) -> PendingFeeChangeResponse:
    """Refuse a proposal without touching the policy in force."""
    change = await service.reject(
        change_id=change_id,
        actor=actor.staff,
        reason=body.reason,
        ip=_ip(request),
    )
    return _pending_change_response(change)


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
    after = _decode_cursor(cursor) if cursor else None
    effective_status = case_status if case_status is not None else KycStatus.PENDING
    rows, has_more = await service.list_kyc_cases(
        status=effective_status, tier_requested=tier_requested, after=after, limit=limit
    )
    return CursorPage[KycCaseAdminResponse](
        data=[KycCaseAdminResponse.model_validate(row) for row in rows],
        next_cursor=(
            _encode_cursor(rows[-1].created_at, rows[-1].id) if has_more and rows else None
        ),
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


def _encode_cursor(moment: datetime, identity: UUID) -> str:
    """Where a page ended: the (timestamp, id) pair the next page seeks past."""
    import base64

    raw = f"{moment.isoformat()}|{identity}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
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
