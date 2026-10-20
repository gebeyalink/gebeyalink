"""The session engine: one keypress in, one screen out.

The engine owns the shape of a USSD conversation and nothing else. It decides whether this
post continues a session or starts one, moves the trader between the nodes the trees declare,
runs the operations a move asks for through the gateway, and renders the result. It holds no
repository and knows no listing: everything it cannot answer from the session it asks the
gateway, which the application implements.

Three rules are worth stating, because each is a place a USSD engine is easy to get wrong.

The session survives only in the store, so a lost or corrupt one is not an error to report but
a fresh start: the trader is shown the top of the menu again and the input that arrived with
the missing session is dropped. Rebuilding a menu from a broken state would guess at what the
trader meant.

A refusal is a screen, not a failure. A choice the menu does not accept, a quantity that is
not a number, a pickup code that does not match — each re-renders the menu it was entered on
with a reason line, three times over; the fourth time the session is closed to the help line,
because a trader who has failed four times needs a person rather than a fifth prompt.

The marker on that reason line is the renderer's, not the template's. The registry holds the
sentence; this module puts §14.1's ``❌`` in front of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from packages.shared.errors import ApiError, ErrorCode
from packages.shared.i18n.languages import Language
from packages.shared.messaging.dispatcher import USSD_SHORT_CODE
from packages.ussd.render import ERROR_MARKER, render, render_screen
from packages.ussd.session import SessionState, SessionStateError, SessionStore
from packages.ussd.trees.model import Action, Node

#: How many refused choices a trader gets before the session is handed to the help line.
MAX_INVALID_TRIES = 3


class UnknownNodeError(LookupError):
    """A tree's move named a node that does not exist. A bug, not bad input."""


class UssdRefusalError(Exception):
    """A screen could not be built because its data is not there to build it from.

    Unlike a refused choice, this is not the trader's doing, so it does not count against the
    retries: the engine renders ``template_id`` as a terminal screen and stops. A missing price
    board is the case that motivates it.
    """

    def __init__(self, template_id: str, context: dict[str, Any] | None = None) -> None:
        super().__init__(template_id)
        self.template_id = template_id
        self.context = context if context is not None else {}


class UssdGateway(Protocol):
    """Everything the engine cannot answer from the session alone.

    The application implements this over the same services the HTTP and SMS paths use, so a
    bid placed by dialling is the same bid, held to the same rules.
    """

    async def resolve_user(self, phone_e164: str) -> str | None:
        """The account on this number, or ``None`` for a handset we have not registered."""
        ...

    async def user_language(self, user_id: str) -> Language:
        """The language a registered account has chosen."""
        ...

    async def prefetch(self, name: str, state: SessionState) -> dict[str, Any]:
        """The context values one screen needs before it can be shown."""
        ...

    async def apply(
        self, action: Action, text: str, state: SessionState, session_id: str
    ) -> dict[str, Any]:
        """Perform a move's operation and return the context it changed."""
        ...


async def handle(
    *,
    store: SessionStore,
    gateway: UssdGateway,
    nodes: Mapping[str, Node],
    session_id: str,
    phone_e164: str,
    text: str,
) -> str:
    """Answer one gateway post with the ``CON`` or ``END`` plain-text reply."""
    current_input = text.split("*")[-1] if text else ""
    try:
        state = await store.load(session_id)
    except SessionStateError:
        state = None
    if state is not None and state.context.get("phone_e164") not in (None, phone_e164):
        state = None
    if state is None:
        return await _fresh(store, gateway, nodes, session_id, phone_e164)
    return await _continue(store, gateway, nodes, session_id, phone_e164, state, current_input)


async def _fresh(
    store: SessionStore,
    gateway: UssdGateway,
    nodes: Mapping[str, Node],
    session_id: str,
    phone_e164: str,
) -> str:
    user_id = await gateway.resolve_user(phone_e164)
    if user_id is not None:
        language: Language | None = await gateway.user_language(user_id)
    else:
        language = await store.load_language(phone_e164)

    context: dict[str, Any] = {"phone_e164": phone_e164, "invalid_tries": 0}
    draft = await store.load_draft(phone_e164)
    if language is None:
        node_id = "language"
    elif draft is not None and draft.get("_draft_step") in nodes:
        node_id = "resume"
        context.update(draft)
    else:
        node_id = "main_menu"

    state = SessionState(user_id=user_id, lang=language, menu_stack=[node_id], context=context)
    node = nodes[node_id]
    try:
        for name in node.prefetch:
            state.context.update(await gateway.prefetch(name, state))
    except UssdRefusalError as refusal:
        await store.save(session_id, state)
        return _end(render_screen((refusal.template_id,), state.lang, refusal.context))
    await store.save(session_id, state)
    return _reply(node, state)


async def _continue(
    store: SessionStore,
    gateway: UssdGateway,
    nodes: Mapping[str, Node],
    session_id: str,
    phone_e164: str,
    state: SessionState,
    text: str,
) -> str:
    stack = state.menu_stack
    node = nodes[stack[-1]]

    if text == "":
        await store.save(session_id, state)
        return _end(_screen(node, state))

    if node.terminal:
        # The session is over; whatever press arrived is a replay of the last screen. The
        # arrival action has already fired once, so the only honest answer is the same screen
        # again - never a second delivery confirmation, never a second offer.
        await store.save(session_id, state)
        return _reply(node, state)

    if text == "0":
        if node.allows_back and len(stack) > 1:
            stack.pop()
            state.context["invalid_tries"] = 0
            await store.save(session_id, state)
            return _reply(nodes[stack[-1]], state)
        return await _refuse(store, nodes, session_id, state, "error_invalid_choice", {})

    if text == "00":
        if not node.allows_back:
            return await _refuse(store, nodes, session_id, state, "error_invalid_choice", {})
        target: str | None = "main_menu"
    else:
        target = node.next(text, state) if node.next is not None else None
        if target is None:
            return await _refuse(
                store,
                nodes,
                session_id,
                state,
                node.error_reason_id or "error_invalid_choice",
                {},
            )

    if target is None:  # defensive; both branches above have produced a node id
        raise UnknownNodeError(target)
    if target not in nodes:
        raise UnknownNodeError(target)
    target_node = nodes[target]

    resumed = False
    if node.resumes_draft:
        resumed = await _apply_resume(store, state, phone_e164, text)

    if target_node.requires_account and state.user_id is None:
        await store.save(session_id, state)
        return _end(render("register_prompt_ussd", state.lang, {"ussd": USSD_SHORT_CODE}))

    if node.action is not None:
        try:
            updates = await gateway.apply(node.action, text, state, session_id)
        except ApiError as error:
            return await _refuse_api(store, nodes, session_id, state, error)
        if "lang" in updates:
            chosen = updates.pop("lang")
            state.lang = chosen if isinstance(chosen, Language) else Language(chosen)
            if state.user_id is None:
                await store.save_language(phone_e164, state.lang)
        state.context.update(updates)

    # An arrival action exists to commit numbers entered on the screen *before* the move, and
    # a resume landing has no screen before it: the press that walked out of the prompt is
    # "1", not a price and not a crop row, and firing the action on it would overwrite the
    # very values the checkpoint restored. The draft is the answer on this landing, so the
    # arrival action is skipped — prefetch is not, because a menu still has to be rebuilt.
    if target_node.arrival_action is not None and not resumed:
        try:
            arrival = await gateway.apply(target_node.arrival_action, text, state, session_id)
        except ApiError as error:
            return await _refuse_api(store, nodes, session_id, state, error)
        state.context.update(arrival)

    try:
        for name in target_node.prefetch:
            state.context.update(await gateway.prefetch(name, state))
    except UssdRefusalError as refusal:
        await store.save(session_id, state)
        return _end(render_screen((refusal.template_id,), state.lang, refusal.context))

    stack.append(target)
    state.context["invalid_tries"] = 0
    await _checkpoint(store, phone_e164, target_node, state)
    await store.save(session_id, state)
    return _reply(target_node, state)


async def _checkpoint(
    store: SessionStore, phone_e164: str, node: Node, state: SessionState
) -> None:
    """FR-2501's write: what the flow has gathered, under the number that dialled.

    One key per phone holds one draft, whichever flow wrote it — the step's node id says
    which flow it is, so a resume lands in the right one, and starting any flow overwrites
    whatever was there before. That is deliberate: a trader gets one unfinished thing at a
    time, and the flow they started last is the one they meant.

    A terminal node that names fields is the flow's last screen, so the trader has finished
    it and the checkpoint is deleted rather than refreshed: a completed listing or a placed
    offer owes nothing to resume, and a stale one replayed from a later dial would walk into
    a second publish under that dial's fresh idempotency key.
    """
    if not node.draft_fields:
        return
    if node.terminal:
        await store.delete_draft(phone_e164)
        return
    await store.save_draft(
        phone_e164,
        {
            "_draft_step": node.id,
            "_draft_body": {
                key: state.context[key] for key in node.draft_fields if key in state.context
            },
        },
    )


async def _apply_resume(
    store: SessionStore, state: SessionState, phone_e164: str, text: str
) -> bool:
    """Carry the checkpoint into the session, or throw it away — the trader's call.

    The answer says whether the move landing here is a resume, which is what tells the
    engine that the keypress was a choice about the draft rather than a value for the
    screen it opens.
    """
    if text == "1":
        body = state.context.get("_draft_body")
        if isinstance(body, dict):
            state.context.update(body)
        return True
    await store.delete_draft(phone_e164)
    for key in [key for key in state.context if key.startswith("_draft")]:
        state.context.pop(key, None)
    return False


async def _refuse_api(
    store: SessionStore,
    nodes: Mapping[str, Node],
    session_id: str,
    state: SessionState,
    error: ApiError,
) -> str:
    details = error.details if isinstance(error.details, dict) else {}
    fields = details.get("fields")
    if isinstance(fields, dict) and "pickup_code" in fields:
        return await _refuse(store, nodes, session_id, state, "error_wrong_code", {})
    if isinstance(fields, dict) and "profile" in fields:
        missing = str(fields.get("profile") or "profile")
        return await _refuse(
            store,
            nodes,
            session_id,
            state,
            "error_profile_incomplete",
            {"missing": missing},
            count=False,
        )
    if error.code == ErrorCode.VALIDATION_ERROR:
        return await _refuse(store, nodes, session_id, state, "error_invalid_choice", {})
    return await _refuse(store, nodes, session_id, state, "error_unavailable", {}, count=False)


async def _refuse(
    store: SessionStore,
    nodes: Mapping[str, Node],
    session_id: str,
    state: SessionState,
    reason_id: str,
    reason_context: dict[str, Any],
    *,
    count: bool = True,
) -> str:
    if count:
        tries = int(state.context.get("invalid_tries", 0)) + 1
        state.context["invalid_tries"] = tries
        if tries > MAX_INVALID_TRIES:
            state.menu_stack = ["help"]
            await store.save(session_id, state)
            return _reply(nodes["help"], state)
    reason = render(reason_id, state.lang, reason_context)
    # The reason line is the whole screen. A re-rendered prompt used to sit under it, but a menu
    # plus a reason is past the channel's UCS-2 budget - a harvest refusal ran to ~130 characters
    # - and the network truncated it on feature phones. D-22 chose the short line over the menu:
    # a trader who presses the wrong key sees the reason, and repeated refusals hand them to Help.
    await store.save(session_id, state)
    return f"CON {ERROR_MARKER}{reason}"


def _screen(node: Node, state: SessionState) -> str:
    return render_screen(node.prompt, state.lang, state.context)


def _reply(node: Node, state: SessionState) -> str:
    body = _screen(node, state)
    return f"END {body}" if node.terminal else f"CON {body}"


def _end(body: str) -> str:
    return f"END {body}"
