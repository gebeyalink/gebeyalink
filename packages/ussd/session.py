"""Keeping a USSD session between the gateway's calls.

A gateway holds no state: every keypress arrives as a fresh POST that names the session and
the whole input so far, and the menu the trader is looking at has to be remembered between
those calls. It is remembered in Redis under the session id the gateway sends.

Two facts shape everything here. The first is that a USSD session is short-lived: the network
drops it after twenty or thirty seconds of inattention, so the session key slides its expiry
forward on every write and a session nobody touched is gone rather than stale. The second is
that the store and the network disagree about loss: the network may time out precisely when
the trader is mid-listing, so the half-finished values are checkpointed under the phone
number as a draft with a longer life, and a re-dial offers them back. A draft is bound to the
handset that wrote it, which is why it is keyed by the number rather than by the session.

The choice of language is remembered the same way and outlives both: a trader who picks
Oromo at the language menu should not have to pick it again, so it is written under the
number for thirty days and adopted into the account when that number finally registers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from packages.shared.i18n.languages import Language

#: A session lives while the gateway keeps calling and the network keeps the call up.
SESSION_TTL_SECONDS = 120
#: A draft outlives the session so that a dropped call can be resumed.
DRAFT_TTL_SECONDS = 15 * 60
#: A language choice outlives both, until the account itself carries one.
LANGUAGE_TTL_SECONDS = 30 * 24 * 60 * 60


def session_key(session_id: str) -> str:
    return f"ussd:{session_id}"


def draft_key(phone_e164: str) -> str:
    return f"ussd:draft:{phone_e164}"


def language_key(phone_e164: str) -> str:
    return f"ussd:lang:{phone_e164}"


class SessionStateError(ValueError):
    """A stored session could not be read back as the shape a session has.

    Raised by :meth:`SessionState.from_json` and treated by the engine as "there is no
    session", which means the next input starts a fresh one. A corrupt session is not worth
    salvaging: the trader cannot have meant anything by it, and half a menu is worse than the
    top of one.
    """


@dataclass
class SessionState:
    """Everything the engine keeps between two posts of one USSD session.

    Exactly the four keys §14.1 pins: who is dialling (if anyone), the language chosen, the
    stack of menus visited, and a free-form context the trees and the gateway share. The
    phone number travels in the context because it belongs to this session, not to the trader
    — an unregistered caller has no account to hang it on.
    """

    user_id: str | None
    lang: Language | None
    menu_stack: list[str]
    context: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "user_id": self.user_id,
                "lang": self.lang.value if self.lang is not None else None,
                "menu_stack": list(self.menu_stack),
                "context": self.context,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> SessionState:
        """Read a stored session, refusing anything that is not one."""
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise SessionStateError(f"session is not JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise SessionStateError(f"session is {type(parsed).__name__}, not an object")

        user_id = parsed.get("user_id")
        if user_id is not None and not isinstance(user_id, str):
            raise SessionStateError("session user_id must be a string or null")

        lang_raw = parsed.get("lang")
        if lang_raw is None:
            lang: Language | None = None
        else:
            try:
                lang = Language(lang_raw)
            except (ValueError, TypeError) as error:
                raise SessionStateError(f"session lang {lang_raw!r} is not a language") from error

        stack_raw = parsed.get("menu_stack")
        if not isinstance(stack_raw, list) or not all(isinstance(step, str) for step in stack_raw):
            raise SessionStateError("session menu_stack must be a list of node ids")

        context = parsed.get("context")
        if not isinstance(context, dict):
            raise SessionStateError("session context must be an object")

        return cls(user_id=user_id, lang=lang, menu_stack=list(stack_raw), context=context)


@runtime_checkable
class RedisLike(Protocol):
    """The three Redis calls this channel needs, so the store can be faked in a test."""

    async def get(self, name: str) -> Any: ...

    async def set(self, name: str, value: str, *, ex: int | None = None) -> Any: ...

    async def delete(self, *names: str) -> Any: ...


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


class SessionStore:
    """Reading and writing the three kinds of state a USSD caller accumulates."""

    def __init__(self, redis: RedisLike) -> None:
        self._redis = redis

    async def load(self, session_id: str) -> SessionState | None:
        raw = await self._redis.get(session_key(session_id))
        if raw is None:
            return None
        return SessionState.from_json(_as_text(raw))

    async def save(self, session_id: str, state: SessionState) -> None:
        await self._redis.set(session_key(session_id), state.to_json(), ex=SESSION_TTL_SECONDS)

    async def load_draft(self, phone_e164: str) -> dict[str, Any] | None:
        raw = await self._redis.get(draft_key(phone_e164))
        if raw is None:
            return None
        try:
            parsed = json.loads(_as_text(raw))
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    async def save_draft(self, phone_e164: str, draft: dict[str, Any]) -> None:
        """Checkpoint the field values of a flow that has not finished (FR-2501).

        Public because Task 15d writes here as the create-listing flow advances; the engine
        only reads, resumes and discards.
        """
        await self._redis.set(
            draft_key(phone_e164), json.dumps(draft, ensure_ascii=False), ex=DRAFT_TTL_SECONDS
        )

    async def delete_draft(self, phone_e164: str) -> None:
        await self._redis.delete(draft_key(phone_e164))

    async def load_language(self, phone_e164: str) -> Language | None:
        raw = await self._redis.get(language_key(phone_e164))
        if raw is None:
            return None
        try:
            return Language(_as_text(raw))
        except ValueError:
            return None

    async def save_language(self, phone_e164: str, language: Language) -> None:
        await self._redis.set(language_key(phone_e164), language.value, ex=LANGUAGE_TTL_SECONDS)
