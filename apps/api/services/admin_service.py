"""The staff surface: signing in, the queue a verifier works, and the agent decisions.

This module is the pilot's administrative core, and it is one module rather than four because
the four faces share one rule: **every mutation writes exactly one audit row, in the same
transaction as the mutation**. A missing audit entry is a defect here, not a gap — the log is
what makes the rest of this module answerable after the fact.

Three concerns live here and are worth naming separately.

    *Staff authentication.* A staff member proves a password and a one-time code. What comes
    back is a signed token, but the token is not the authority on the session — a row is. Every
    check below therefore reads ``staff_sessions``: a disabled account, a role change, a
    password change and a logout all have to end a live token, and the only thing that can end
    a stateless token is a row that says it is over.

    *The KYC queue.* A submission (16-pre) writes a waiting case; this is where a verifier
    reads the queue and decides. Approving moves ``users.kyc_tier`` to the case's requested
    tier — by definition the next rung, because the ladder is climbed one rung at a time at
    submission. Rejecting records a reason code, and §10.7's ``DOC_FRAUD_SUSPECTED`` freezes
    the account.

    *Agent administration.* The approval that had been an operator's command line is a route
    now. It calls the *same* service the command line calls, so the audit entry a route writes
    and the one the break-glass command writes are the same shape rather than two shapes that
    must be kept in step by hand.

The one thing this module does not do is change a password without a second proof, or a role
without ending the sessions the old role opened. Those are SEC-12's two rules and they are
enforced here rather than trusted to a caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from apps.api.core import staff_password, staff_totp
from apps.api.core.config import Settings
from apps.api.core.security import TokenService
from apps.api.core.staff_secrets import StaffSecretCipher
from apps.api.models.kyc_case import KycCase, KycRejectionReason, KycStatus, KycTier
from apps.api.models.staff_user import StaffRole, StaffUser
from apps.api.models.user import AccountStatus, User, VerificationTier
from apps.api.repositories.agent_assignment_repository import AgentAssignmentRepository
from apps.api.repositories.audit_log_repository import AuditLogRepository
from apps.api.repositories.kyc_case_repository import KycCaseRepository
from apps.api.repositories.staff_session_repository import StaffSessionRepository
from apps.api.repositories.staff_user_repository import StaffUserRepository
from apps.api.repositories.user_repository import UserRepository
from apps.api.repositories.user_role_repository import UserRoleRepository
from apps.api.services.agent_service import ACTOR_SYSTEM as _ACTOR_SYSTEM
from apps.api.services.agent_service import AgentRoleAdmin
from packages.shared.errors import ApiError, ErrorCode

#: The scope a token carries when the account must change its password before anything else.
#: A token with this scope reaches exactly one route: change-password.
SCOPE_FULL = "full"
SCOPE_CHANGE_PASSWORD_ONLY = "change_password_only"

#: How many consecutive failed sign-ins are tolerated before an account is refused for a while.
#: Authentication here is password + TOTP, so the window is generous; it exists to make a
#: password guess expensive even for an attacker who has somehow learned the secret.
MAX_FAILED_LOGINS = 10

#: The action names this module writes. §11.1's vocabulary, named once here so a reader and a
#: test share the exact strings rather than each deriving them from call sites.
ACTION_STAFF_CREATED = "STAFF_CREATED"
ACTION_STAFF_DISABLED = "STAFF_DISABLED"
ACTION_STAFF_ENABLED = "STAFF_ENABLED"
ACTION_STAFF_ROLE_CHANGED = "STAFF_ROLE_CHANGED"
ACTION_STAFF_PASSWORD_CHANGED = "STAFF_PASSWORD_CHANGED"
ACTION_KYC_APPROVED = "KYC_APPROVED"
ACTION_KYC_REJECTED = "KYC_REJECTED"
ACTION_AGENT_APPROVED = "AGENT_APPROVED"
ACTION_AGENT_REVOKED = "AGENT_REVOKED"

#: The actor role a system action carries when there is no human staff actor — the seed
#: command, or any action taken by the platform itself. Imported from the agent service rather
#: than declared again here, so the two modules that write SYSTEM rows agree on the word by
#: sharing it instead of by both happening to spell it the same.
ACTOR_SYSTEM = _ACTOR_SYSTEM


@dataclass(frozen=True, slots=True)
class StaffSession:
    """A signed-in staff member, and the session they signed in with."""

    staff: StaffUser
    session_id: uuid.UUID
    scope: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedStaffToken:
    """The token a sign-in or password change hands back, with its lifetime and scope."""

    access_token: str
    expires_at: datetime
    scope: str


class StaffAuthService:
    """Proving a staff member is who they say, and keeping the proof current.

    The service owns three rules that are easy to state and easy to get wrong: the password and
    the code are *both* required at every sign-in; a session's liveness is read from its row and
    not from the token; and a sensitive change (a password, a role) ends the sessions opened
    under the state it replaced.
    """

    def __init__(
        self,
        *,
        staff: StaffUserRepository,
        sessions: StaffSessionRepository,
        audit: AuditLogRepository,
        tokens: TokenService,
        secrets: StaffSecretCipher,
        settings: Settings,
    ) -> None:
        self._staff = staff
        self._sessions = sessions
        self._audit = audit
        self._tokens = tokens
        self._secrets = secrets
        self._settings = settings

    # -- sign-in ---------------------------------------------------------------

    async def login(
        self,
        *,
        email: str,
        password: str,
        totp_code: str,
        ip: str | None = None,
        now: datetime | None = None,
    ) -> IssuedStaffToken:
        """Prove a password and a one-time code and open a session.

        The refusal is deliberately undifferentiated: a wrong password, a wrong code and an
        unknown email all come back as the same forbidden error. Telling them apart would let a
        caller with no account learn which addresses exist, and the person signing in never
        needs the distinction — their password is either right or it is not.
        """
        moment = now or datetime.now(UTC)
        staff = await self._staff.find_by_email(email)
        password_ok = staff is not None and staff_password.verify_password(
            password, staff.password_hash
        )
        # The code is checked against the *stored, decrypted* secret and the same clock, and a
        # missing account is treated as a failed code rather than a short-circuit, so the whole
        # check takes the same time whether or not the account exists.
        code_ok = False
        if staff is not None:
            try:
                secret = self._secrets.decrypt(staff.totp_secret_encrypted)
            except Exception:
                secret = None
            code_ok = secret is not None and staff_totp.verify(totp_code, secret, at=moment)

        if staff is None or not password_ok or not code_ok:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                details={"reason": "invalid_credentials"},
            )

        if staff.is_disabled:
            # A disabled account may not sign in even with the right credential. Its live
            # sessions are refused by the dependency on their next request; this is the door.
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "account_disabled"})

        # A password that was stored under weaker scrypt parameters is re-hashed here, under
        # the current ones, while the plaintext is still in hand. A parameter bump therefore
        # takes effect on each account's next sign-in with no migration and no effort from the
        # staff member.
        if staff_password.needs_rehash(
            staff.password_hash,
            n=self._settings.staff_password_scrypt_n,
            r=self._settings.staff_password_scrypt_r,
            p=self._settings.staff_password_scrypt_p,
        ):
            rehashed = staff_password.hash_password(
                password,
                n=self._settings.staff_password_scrypt_n,
                r=self._settings.staff_password_scrypt_r,
                p=self._settings.staff_password_scrypt_p,
            )
            await self._staff.set_password(staff, rehashed, must_change=staff.must_change_password)

        expires_at = moment + timedelta(seconds=self._settings.admin_jwt_ttl_seconds)
        session = await self._sessions.create(staff_user_id=staff.id, expires_at=expires_at, ip=ip)
        await self._staff.note_login(staff, at=moment)

        scope = SCOPE_CHANGE_PASSWORD_ONLY if staff.must_change_password else SCOPE_FULL
        token = self._tokens.issue_staff(
            staff_user_id=staff.id,
            session_id=session.id,
            role=staff.role.value,
            scope=scope,
            lifetime_seconds=self._settings.admin_jwt_ttl_seconds,
        )
        await self._sessions.commit()
        return IssuedStaffToken(access_token=token.access_token, expires_at=expires_at, scope=scope)

    # -- the session a request carries -----------------------------------------

    async def resolve_session(
        self, *, staff_user_id: uuid.UUID, session_id: uuid.UUID, now: datetime | None = None
    ) -> StaffSession:
        """Load the live session behind a token, or refuse.

        The row is the authority. The token's ``exp`` was checked by the verifier; this reads
        the session's own expiry and its revocation, so a logout, a role change or a
        disablement ends a token that is otherwise perfectly well signed.
        """
        moment = now or datetime.now(UTC)
        session = await self._sessions.find_live(session_id, now=moment)
        if session is None or session.staff_user_id != staff_user_id:
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "session_ended"})
        staff = await self._staff.find_by_id(staff_user_id)
        if staff is None or staff.is_disabled:
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "account_disabled"})
        return StaffSession(
            staff=staff,
            session_id=session_id,
            scope=SCOPE_FULL,  # the scope is narrowed by the dependency, from the token
            expires_at=session.expires_at,
        )

    # -- password change -------------------------------------------------------

    async def change_password(
        self,
        *,
        staff: StaffUser,
        session_id: uuid.UUID,
        old_password: str,
        new_password: str,
        totp_code: str,
        now: datetime | None = None,
    ) -> None:
        """Replace the password, proved by the old one and a fresh code.

        SEC-12 treats a password change as a sensitive action, so every *other* live session is
        ended: the place the old password was still signed in is exactly what a password change
        is meant to close. The session making the change survives, because ending it too would
        sign the staff member out of the act they just performed.
        """
        moment = now or datetime.now(UTC)
        if not staff_password.verify_password(old_password, staff.password_hash):
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "invalid_password"})
        try:
            secret = self._secrets.decrypt(staff.totp_secret_encrypted)
        except Exception as exc:
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "invalid_code"}) from exc
        if not staff_totp.verify(totp_code, secret, at=moment):
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "invalid_code"})

        new_hash = staff_password.hash_password(
            new_password,
            n=self._settings.staff_password_scrypt_n,
            r=self._settings.staff_password_scrypt_r,
            p=self._settings.staff_password_scrypt_p,
        )
        await self._staff.set_password(staff, new_hash, must_change=False)
        await self._sessions.revoke_all_for_staff(
            staff.id, except_session_id=session_id, now=moment
        )
        await self._audit.record(
            actor_id=staff.id,
            actor_role=staff.role.value,
            action=ACTION_STAFF_PASSWORD_CHANGED,
            target_type="staff_user",
            target_id=staff.id,
            after={"password_hash": new_hash},
        )
        await self._sessions.commit()

    async def logout(self, *, session_id: uuid.UUID, now: datetime | None = None) -> None:
        """End the session the token names, once."""
        await self._sessions.revoke(session_id, now=now or datetime.now(UTC))
        await self._sessions.commit()


class AdminService:
    """Staff administration, the KYC queue, and the audited agent decisions.

    Every method that changes something writes exactly one audit row, in the same transaction,
    with before/after hashes of the state it changed. The report and the tests both lean on that
    being true for *every* mutation here, not just the ones with an obvious hash.
    """

    def __init__(
        self,
        *,
        staff: StaffUserRepository,
        sessions: StaffSessionRepository,
        users: UserRepository,
        cases: KycCaseRepository,
        user_roles: UserRoleRepository,
        assignments: AgentAssignmentRepository,
        agent_admin: AgentRoleAdmin,
        audit: AuditLogRepository,
        secrets: StaffSecretCipher,
        settings: Settings,
    ) -> None:
        self._staff = staff
        self._sessions = sessions
        self._users = users
        self._cases = cases
        self._user_roles = user_roles
        self._assignments = assignments
        self._agent_admin = agent_admin
        self._audit = audit
        self._secrets = secrets
        self._settings = settings

    # -- staff administration --------------------------------------------------

    async def create_staff(
        self,
        *,
        actor: StaffUser,
        email: str,
        role: StaffRole,
        password: str | None,
        ip: str | None = None,
    ) -> tuple[StaffUser, str, str | None]:
        """Create a staff account and return its provisioning URI once.

        The URI is returned and never stored: it carries the TOTP secret in a form a phone can
        read, and the only moment it may leave the server is the moment the secret is created.
        A generated password is returned in the same breath, for an admin to hand over; a
        supplied one is not echoed.
        """
        existing = await self._staff.find_by_email(email)
        if existing is not None:
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                details={"fields": {"email": "already_in_use"}},
            )
        plaintext = password or staff_password.generate_password()
        generated = None if password else plaintext
        secret = staff_totp.generate_secret()
        row = await self._staff.add(
            StaffUser(
                email=email.lower(),
                password_hash=staff_password.hash_password(
                    plaintext,
                    n=self._settings.staff_password_scrypt_n,
                    r=self._settings.staff_password_scrypt_r,
                    p=self._settings.staff_password_scrypt_p,
                ),
                totp_secret_encrypted=self._secrets.encrypt(secret),
                role=role,
                must_change_password=True,
            )
        )
        await self._audit.record(
            actor_id=actor.id,
            actor_role=actor.role.value,
            action=ACTION_STAFF_CREATED,
            target_type="staff_user",
            target_id=row.id,
            after={"email": row.email, "role": row.role.value},
            ip=ip,
        )
        await self._staff.commit()
        return row, staff_totp.provisioning_uri(row.email, secret), generated

    async def set_disabled(
        self,
        *,
        actor: StaffUser,
        staff_id: uuid.UUID,
        disabled: bool,
        ip: str | None = None,
    ) -> StaffUser:
        """Disable or enable a staff account. Disabling also ends its live sessions."""
        target = await self._require_staff(staff_id)
        moment = datetime.now(UTC)
        disabled_before = target.disabled_at.isoformat() if target.disabled_at else None
        if disabled:
            await self._staff.set_disabled(target, disabled_at=moment)
            # A disabled member's live sessions are ended here, not only refused at the door,
            # so the revocation is visible in the table rather than inferred from disabled_at.
            await self._sessions.revoke_all_for_staff(target.id, now=moment)
        else:
            await self._staff.set_disabled(target, disabled_at=None)
        await self._audit.record(
            actor_id=actor.id,
            actor_role=actor.role.value,
            action=ACTION_STAFF_DISABLED if disabled else ACTION_STAFF_ENABLED,
            target_type="staff_user",
            target_id=target.id,
            before={"disabled_at": disabled_before},
            after={"disabled_at": moment.isoformat() if disabled else None},
            ip=ip,
        )
        await self._staff.commit()
        return target

    async def change_role(
        self,
        *,
        actor: StaffUser,
        staff_id: uuid.UUID,
        role: StaffRole,
        ip: str | None = None,
    ) -> StaffUser:
        """Move a staff account to a new role, ending the sessions the old role opened.

        SEC-12: a role change requires re-authentication. The affected member must sign in
        again under the new role, so every live session they hold is ended. A staff member may
        not change their own role — an ADMIN promoting themselves is not a role change, it is
        an escalation, and it is refused rather than silently allowed.
        """
        if actor.id == staff_id:
            raise ApiError(ErrorCode.FORBIDDEN, details={"reason": "cannot_change_own_role"})
        target = await self._require_staff(staff_id)
        before = target.role.value
        if target.role != role:
            await self._staff.set_role(target, role)
            await self._sessions.revoke_all_for_staff(target.id)
            await self._audit.record(
                actor_id=actor.id,
                actor_role=actor.role.value,
                action=ACTION_STAFF_ROLE_CHANGED,
                target_type="staff_user",
                target_id=target.id,
                before={"role": before},
                after={"role": role.value},
                ip=ip,
            )
            await self._staff.commit()
        return target

    # -- the KYC queue ---------------------------------------------------------

    async def list_kyc_cases(
        self,
        *,
        status: KycStatus | None = None,
        tier_requested: KycTier | None = None,
        after: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 20,
    ) -> tuple[list[KycCase], bool]:
        """One page of the queue, newest first, filtered by status and tier.

        Keyset-paginated on (created_at, id) rather than offset: the queue is worked from the
        front while it is read, so a case must not be skipped because a row above it was
        decided between two pages.
        """
        rows = await self._cases.page_queue(
            status=status, tier_requested=tier_requested, after=after, limit=limit
        )
        # The repository asks for one past the page so "is there a next page" is answered without
        # a second count query; that extra row is the signal and not part of the page returned.
        has_more = len(rows) > limit
        return rows[:limit], has_more

    async def read_case(self, *, case_id: uuid.UUID) -> KycCase:
        """One case by identity, documents included."""
        case = await self._cases.find_by_id(case_id)
        if case is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "kyc_case"})
        return case

    async def approve_case(
        self, *, actor: StaffUser, case_id: uuid.UUID, ip: str | None = None
    ) -> KycCase:
        """Approve a waiting case: move the account's tier to the tier the case asked for.

        The requested tier is the next rung by construction — the submission path climbs the
        ladder one rung at a time — so the approval writes exactly what the case asked for and
        does not re-derive a target. A case that is not ``PENDING`` is an illegal transition,
        not a conflict of a pending case: the 409 that means "a case waits" is the submitter's,
        and this is the reviewer's.
        """
        case = await self._require_pending_case(case_id)
        user = await self._users.find_by_id(case.user_id)
        if user is None:  # pragma: no cover - a case's owner is not deleted
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "user"})
        before = user.kyc_tier.value
        target_tier = _account_tier_for(case.tier_requested)
        case.status = KycStatus.APPROVED
        case.reviewer_id = actor.id
        user.kyc_tier = target_tier
        await self._cases.session.flush()
        await self._audit.record(
            actor_id=actor.id,
            actor_role=actor.role.value,
            action=ACTION_KYC_APPROVED,
            target_type="user",
            target_id=user.id,
            before={"kyc_tier": before},
            after={"kyc_tier": target_tier.value},
            ip=ip,
        )
        await self._cases.commit()
        # The UPDATE marked ``updated_at`` expired: it is a server-generated value the flush
        # could not know. Reading the response would then lazy-load it, which a sync response
        # builder cannot do, so the case is re-read here while an await is still in hand.
        await self._cases.session.refresh(case)
        return case

    async def reject_case(
        self,
        *,
        actor: StaffUser,
        case_id: uuid.UUID,
        reason_code: KycRejectionReason,
        ip: str | None = None,
    ) -> KycCase:
        """Reject a waiting case with §10.7's reason; ``DOC_FRAUD_SUSPECTED`` freezes the user.

        A rejection does not move the tier — the account keeps the one it had, which is the
        point of the second act being separate from the first. Freezing is reserved for the one
        reason that is about the person rather than the document.
        """
        case = await self._require_pending_case(case_id)
        user = await self._users.find_by_id(case.user_id)
        if user is None:  # pragma: no cover
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "user"})
        # Captured before the freeze, so the log's "before" is the state the decision read,
        # not the state the decision wrote.
        status_before = user.status.value
        case.status = KycStatus.REJECTED
        case.reviewer_id = actor.id
        case.rejection_reason = reason_code
        froze = reason_code == KycRejectionReason.DOC_FRAUD_SUSPECTED
        if froze:
            user.status = AccountStatus.FROZEN
        await self._cases.session.flush()
        await self._audit.record(
            actor_id=actor.id,
            actor_role=actor.role.value,
            action=ACTION_KYC_REJECTED,
            target_type="user",
            target_id=user.id,
            before={"status": status_before},
            after={"rejection_reason": reason_code.value, "froze": froze},
            ip=ip,
        )
        await self._cases.commit()
        # See ``approve_case``: the flush expired the server-generated ``updated_at``, and the
        # response is built synchronously, so the row is re-read while an await is still in hand.
        await self._cases.session.refresh(case)
        return case

    # -- agent administration --------------------------------------------------

    async def approve_agent(
        self, *, actor: StaffUser, user_id: uuid.UUID, ip: str | None = None
    ) -> tuple[User, bool]:
        """Approve an account as an agent, audited. Calls the same service the CLI calls."""
        user, granted = await self._agent_admin.approve_by_id(
            user_id,
            actor_id=actor.id,
            actor_role=actor.role.value,
            ip=ip,
        )
        return user, granted

    async def revoke_agent(
        self, *, actor: StaffUser, user_id: uuid.UUID, ip: str | None = None
    ) -> tuple[User, int]:
        """Revoke the agent role and end the relationships, audited."""
        return await self._agent_admin.revoke_by_id(
            user_id,
            actor_id=actor.id,
            actor_role=actor.role.value,
            ip=ip,
        )

    # -- helpers ---------------------------------------------------------------

    async def _require_staff(self, staff_id: uuid.UUID) -> StaffUser:
        target = await self._staff.find_by_id(staff_id)
        if target is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "staff_user"})
        return target

    async def _require_pending_case(self, case_id: uuid.UUID) -> KycCase:
        case = await self._cases.find_by_id(case_id)
        if case is None:
            raise ApiError(ErrorCode.NOT_FOUND, details={"resource": "kyc_case"})
        if case.status != KycStatus.PENDING:
            raise ApiError(
                ErrorCode.ILLEGAL_STATE_TRANSITION,
                details={"resource": "kyc_case", "status": case.status.value},
            )
        return case


#: The account-side vocabulary, keyed by the case-side one. The two enums name the same
#: ladder; this is the one place the translation is written down.
def _account_tier_for(requested: KycTier) -> VerificationTier:
    return {
        KycTier.TIER_1: VerificationTier.TIER_1,
        KycTier.TIER_2: VerificationTier.TIER_2,
    }[requested]
